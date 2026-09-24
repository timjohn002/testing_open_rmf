"""
Report Traffic Light robots to the rmf-web api-server.

Full Control fleet adapters send their fleet state to the api-server's
``/_internal`` websocket themselves (the ``server_uri`` option). The
EasyTrafficLight API does not: it only publishes the legacy
``rmf_fleet_msgs/FleetState`` ROS topic, which the api-server does not read.
Without this reporter, Traffic Light robots would be invisible in the portal.

This sends the same ``fleet_state_update`` message a Full Control adapter
sends. No ROS imports, so it can be tested anywhere.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional


def robot_state_json(name: str, map_name: str, rmf_pose: list[float],
                     battery_soc: float, has_path: bool,
                     error: Optional[str],
                     waiting_for_traffic: bool) -> dict:
    """One robot entry of an rmf_api ``fleet_state`` message."""
    if error:
        status = 'error'
    elif has_path:
        status = 'working'
    else:
        status = 'idle'
    issues = []
    if error:
        issues.append({'category': 'robot_error', 'detail': error})
    if waiting_for_traffic:
        issues.append({'category': 'waiting_for_traffic',
                       'detail': 'Paused by RMF to avoid a traffic conflict'})
    return {
        'name': name,
        'status': status,
        'task_id': '',
        'unix_millis_time': int(time.time() * 1000),
        'location': {'map': map_name, 'x': rmf_pose[0], 'y': rmf_pose[1],
                     'yaw': rmf_pose[2]},
        'battery': max(0.0, min(1.0, float(battery_soc))),
        'issues': issues,
    }


class ApiServerReporter:
    """Collects robot states and pushes them to ``server_uri`` periodically."""

    def __init__(self, fleet_name: str, server_uri: Optional[str],
                 period: float = 1.0, logger=None):
        self.fleet_name = fleet_name
        self.server_uri = server_uri
        self.period = period
        self.logger = logger
        self._robots: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ws = None
        self._last_error = None

    def update(self, robot_json: dict):
        with self._lock:
            self._robots[robot_json['name']] = robot_json

    def message(self) -> dict:
        with self._lock:
            robots = dict(self._robots)
        return {'type': 'fleet_state_update',
                'data': {'name': self.fleet_name, 'robots': robots}}

    def start(self):
        if not self.server_uri:
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import websocket  # websocket-client; imported lazily for tests

        while True:
            time.sleep(self.period)
            msg = self.message()
            if not msg['data']['robots']:
                continue
            try:
                if self._ws is None:
                    self._ws = websocket.create_connection(
                        self.server_uri, timeout=5)
                self._ws.send(json.dumps(msg))
                self._last_error = None
            except Exception as e:  # reconnect on the next cycle
                if self.logger and str(e) != self._last_error:
                    self.logger.warn(
                        f'[{self.fleet_name}] cannot reach api-server at '
                        f'{self.server_uri}: {e}')
                self._last_error = str(e)
                try:
                    if self._ws is not None:
                        self._ws.close()
                finally:
                    self._ws = None
