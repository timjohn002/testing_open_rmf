"""
Traffic Light driver for robots (or vendor bridges) speaking a REST contract.

Contract (JSON, relative to ``fleet_manager.prefix``)::

    GET  /robots/{name}/state
         -> {"map": "L1",
             "position": {"x": 1.0, "y": 2.0, "yaw": 0.0},
             "battery": 0.9,
             "current_path": [{"map_name": "L1", "x": 1.0, "y": 2.0,
                               "yaw": 0.0}, ...],     # [] when idle
             "last_completed_checkpoint": 0,          # -1 if none yet
             "is_moving": true,
             "error": null}                           # string on a fault
    POST /robots/{name}/pause               {}                 -> {"success": true}
    POST /robots/{name}/resume              {}                 -> {"success": true}
    POST /robots/{name}/pause_at_checkpoint {"checkpoint": 3}  -> {"success": true}

This is the same ``/state`` endpoint as the Full Control contract, plus the
route fields. ``tools/mock_robot_server.py --tl-robot`` implements it.

``fleet_manager`` keys: ``prefix`` (required), ``token`` or
``user``/``password`` (optional), ``timeout``.
"""

from __future__ import annotations

from typing import Optional

import requests

from ..robot_api import TrafficLightRobotAPI, TrafficLightUpdateData


class GenericRestTrafficLightAPI(TrafficLightRobotAPI):

    def __init__(self, config: dict):
        super().__init__(config)
        self.prefix = config['prefix'].rstrip('/')
        self.session = requests.Session()
        if config.get('token'):
            self.session.headers['Authorization'] = f"Bearer {config['token']}"
        elif config.get('user'):
            self.session.auth = (config['user'], config.get('password', ''))

    def _url(self, robot_name, endpoint):
        return f'{self.prefix}/robots/{robot_name}/{endpoint}'

    def _post(self, robot_name, endpoint, body) -> bool:
        try:
            r = self.session.post(
                self._url(robot_name, endpoint), json=body,
                timeout=self.timeout)
            r.raise_for_status()
            return bool(r.json().get('success'))
        except (requests.RequestException, ValueError):
            return False

    def check_connection(self) -> bool:
        try:
            return self.session.get(
                f'{self.prefix}/health', timeout=self.timeout).ok
        except requests.RequestException:
            return False

    def get_data(self, robot_name) -> Optional[TrafficLightUpdateData]:
        try:
            r = self.session.get(
                self._url(robot_name, 'state'), timeout=self.timeout)
            r.raise_for_status()
            s = r.json()
            pos = s['position']
            return TrafficLightUpdateData.from_dict({
                'map_name': s['map'],
                'position': [pos['x'], pos['y'], pos.get('yaw', 0.0)],
                'current_path': s.get('current_path') or [],
                'last_completed_checkpoint':
                    s.get('last_completed_checkpoint', -1),
                'is_moving': s.get('is_moving', False),
                'battery_soc': s.get('battery', 1.0),
                'error': s.get('error'),
            }, robot_name)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None

    def pause(self, robot_name) -> bool:
        return self._post(robot_name, 'pause', {})

    def resume(self, robot_name) -> bool:
        return self._post(robot_name, 'resume', {})

    def pause_at_checkpoint(self, robot_name, checkpoint) -> bool:
        return self._post(
            robot_name, 'pause_at_checkpoint', {'checkpoint': int(checkpoint)})
