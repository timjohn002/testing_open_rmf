"""
Driver for any robot (or vendor bridge) that speaks a small REST contract.

Use this when a vendor has no ready-made driver: write a thin bridge on the
robot side (or next to the vendor's fleet manager) that exposes these
endpoints, and RMF can control it. ``tools/mock_robot_server.py`` is a
working reference implementation.

Contract (all JSON, relative to ``fleet_manager.prefix``)::

    GET  /robots/{name}/state
         -> {"map": "L1",
             "position": {"x": 1.0, "y": 2.0, "yaw": 0.0},
             "battery": 0.87,                       # 0.0 - 1.0
             "command": {"id": 12, "completed": false}}
    POST /robots/{name}/navigate  {"map", "x", "y", "yaw", "speed_limit"}
         -> {"success": true, "command_id": 12}
    POST /robots/{name}/action    {"activity", "label"}
         -> {"success": true, "command_id": 13}
    POST /robots/{name}/stop      {}  -> {"success": true}
    POST /robots/{name}/localize  {"map", "x", "y", "yaw"} -> {"success": true}

``fleet_manager`` config keys: ``prefix`` (required), ``token`` (optional
bearer token), ``user``/``password`` (optional basic auth), ``timeout``.
"""

from __future__ import annotations

import threading
from typing import Optional

import requests

from ..robot_api import RobotAPI, RobotUpdateData


class GenericRestRobotAPI(RobotAPI):

    def __init__(self, config: dict):
        super().__init__(config)
        self.prefix = config['prefix'].rstrip('/')
        self.session = requests.Session()
        if config.get('token'):
            self.session.headers['Authorization'] = f"Bearer {config['token']}"
        elif config.get('user'):
            self.session.auth = (config['user'], config.get('password', ''))
        self._lock = threading.Lock()
        # Last command id we issued, and the last state we saw, per robot.
        self._command_ids: dict[str, int] = {}
        self._last_state: dict[str, dict] = {}

    # --- helpers ------------------------------------------------------------
    def _url(self, robot_name: str, endpoint: str) -> str:
        return f'{self.prefix}/robots/{robot_name}/{endpoint}'

    def _post(self, robot_name: str, endpoint: str, body: dict) -> dict:
        try:
            r = self.session.post(
                self._url(robot_name, endpoint), json=body,
                timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            return {'success': False}

    def _command(self, robot_name: str, endpoint: str, body: dict) -> bool:
        resp = self._post(robot_name, endpoint, body)
        if not resp.get('success'):
            return False
        if 'command_id' in resp:
            with self._lock:
                self._command_ids[robot_name] = resp['command_id']
        return True

    # --- RobotAPI -------------------------------------------------------------
    def check_connection(self) -> bool:
        try:
            return self.session.get(
                f'{self.prefix}/health', timeout=self.timeout).ok
        except requests.RequestException:
            return False

    def get_data(self, robot_name: str) -> Optional[RobotUpdateData]:
        try:
            r = self.session.get(
                self._url(robot_name, 'state'), timeout=self.timeout)
            r.raise_for_status()
            state = r.json()
        except (requests.RequestException, ValueError):
            return None
        with self._lock:
            self._last_state[robot_name] = state
        pos = state.get('position') or {}
        try:
            return RobotUpdateData(
                robot_name=robot_name,
                map_name=state['map'],
                position=[float(pos['x']), float(pos['y']),
                          float(pos.get('yaw', 0.0))],
                battery_soc=float(state['battery']),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_command_completed(self, robot_name: str) -> bool:
        with self._lock:
            state = self._last_state.get(robot_name) or {}
            expected = self._command_ids.get(robot_name)
        command = state.get('command') or {}
        if expected is not None and command.get('id') != expected:
            # The robot has not reported on our latest command yet.
            return False
        return bool(command.get('completed'))

    def navigate(self, robot_name, pose, map_name, speed_limit=0.0) -> bool:
        return self._command(robot_name, 'navigate', {
            'map': map_name, 'x': pose[0], 'y': pose[1], 'yaw': pose[2],
            'speed_limit': speed_limit,
        })

    def start_activity(self, robot_name, activity, label) -> bool:
        return self._command(
            robot_name, 'action', {'activity': activity, 'label': label})

    def stop(self, robot_name) -> bool:
        return bool(self._post(robot_name, 'stop', {}).get('success'))

    def localize(self, robot_name, pose, map_name) -> bool:
        return bool(self._post(robot_name, 'localize', {
            'map': map_name, 'x': pose[0], 'y': pose[1], 'yaw': pose[2],
        }).get('success'))
