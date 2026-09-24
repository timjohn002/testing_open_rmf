"""
Driver for Mobile Industrial Robots (MiR) using the robot's REST API v2.0.0.

Each MiR robot runs its own REST server, so every robot gets its own
``prefix``. Verify endpoint names and fields against the REST API
documentation for your MiR software version before using this on hardware.

One-time setup on each robot (MiR web interface):
  1. Create a mission named e.g. ``rmf_move`` containing a single
     "Move to coordinate" action. Make X, Y and Orientation *mission
     parameters* with the ids ``x``, ``y`` and ``orientation``.
  2. Copy that mission's GUID into ``move_mission_guid`` below.
  3. Optionally create missions for docking/charging etc. and map them to
     RMF action names in ``action_missions``.

``fleet_manager`` config::

    driver: mir
    user: distributor              # MiR REST user
    password: ...
    move_mission_guid: "..."       # mission from step 1
    action_missions:               # optional: RMF action -> mission GUID
      dock: "..."
    dock_missions:                 # optional: nav graph dock_name -> mission GUID
      charger_b1_dock: "..."
    robots:                        # per-robot REST endpoint
      mir_1: {prefix: "http://192.168.12.20/api/v2.0.0"}
"""

from __future__ import annotations

import base64
import hashlib
import math
import threading
from typing import Optional

import requests

from ..robot_api import RobotAPI, RobotUpdateData

# MiR /status state_id values
_STATE_READY = 3
_STATE_PAUSE = 4


class MiRRobotAPI(RobotAPI):

    def __init__(self, config: dict):
        super().__init__(config)
        password_hash = hashlib.sha256(
            config.get('password', '').encode()).hexdigest()
        token = base64.b64encode(
            f"{config.get('user', '')}:{password_hash}".encode()).decode()
        self.headers = {
            'Authorization': f'Basic {token}',
            'Accept-Language': 'en_US',
            'Content-Type': 'application/json',
        }
        self.move_mission_guid = config.get('move_mission_guid', '')
        self.action_missions = config.get('action_missions', {}) or {}
        self.dock_missions = config.get('dock_missions', {}) or {}
        self.prefixes = {
            name: robot['prefix'].rstrip('/')
            for name, robot in (config.get('robots') or {}).items()
        }
        self._lock = threading.Lock()
        self._queue_ids: dict[str, int] = {}
        self._map_names: dict[str, str] = {}  # map guid -> name

    # --- helpers ------------------------------------------------------------
    def _request(self, robot_name, method, path, body=None):
        prefix = self.prefixes.get(robot_name)
        if prefix is None:
            return None
        try:
            r = requests.request(
                method, f'{prefix}{path}', json=body,
                headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json() if r.content else {}
        except (requests.RequestException, ValueError):
            return None

    def _map_name(self, robot_name: str, map_id: str) -> Optional[str]:
        if map_id in self._map_names:
            return self._map_names[map_id]
        resp = self._request(robot_name, 'GET', f'/maps/{map_id}')
        if not resp or 'name' not in resp:
            return None
        self._map_names[map_id] = resp['name']
        return resp['name']

    def _queue_mission(self, robot_name, mission_guid, parameters=None):
        body = {'mission_id': mission_guid}
        if parameters:
            body['parameters'] = parameters
        # Make sure the robot is not paused, then queue the mission.
        self._request(robot_name, 'PUT', '/status', {'state_id': _STATE_READY})
        resp = self._request(robot_name, 'POST', '/mission_queue', body)
        if not resp or 'id' not in resp:
            return False
        with self._lock:
            self._queue_ids[robot_name] = resp['id']
        return True

    # --- RobotAPI -------------------------------------------------------------
    def check_connection(self) -> bool:
        return all(
            self._request(name, 'GET', '/status') is not None
            for name in self.prefixes)

    def get_data(self, robot_name: str) -> Optional[RobotUpdateData]:
        status = self._request(robot_name, 'GET', '/status')
        if not status:
            return None
        map_name = self._map_name(robot_name, status.get('map_id', ''))
        pos = status.get('position') or {}
        if map_name is None or 'x' not in pos:
            return None
        return RobotUpdateData(
            robot_name=robot_name,
            map_name=map_name,
            position=[pos['x'], pos['y'],
                      math.radians(pos.get('orientation', 0.0))],
            battery_soc=float(status.get('battery_percentage', 0.0)) / 100.0,
        )

    def is_command_completed(self, robot_name: str) -> bool:
        with self._lock:
            queue_id = self._queue_ids.get(robot_name)
        if queue_id is None:
            return False
        resp = self._request(robot_name, 'GET', f'/mission_queue/{queue_id}')
        return bool(resp) and resp.get('state') == 'Done'

    def navigate(self, robot_name, pose, map_name, speed_limit=0.0) -> bool:
        return self._queue_mission(robot_name, self.move_mission_guid, [
            {'id': 'x', 'value': pose[0]},
            {'id': 'y', 'value': pose[1]},
            {'id': 'orientation', 'value': math.degrees(pose[2])},
        ])

    def start_activity(self, robot_name, activity, label) -> bool:
        guid = self.action_missions.get(activity)
        if not guid:
            return False
        return self._queue_mission(robot_name, guid)

    def dock(self, robot_name, dock_name) -> bool:
        guid = self.dock_missions.get(dock_name)
        if not guid:
            return False
        return self._queue_mission(robot_name, guid)

    def stop(self, robot_name) -> bool:
        cleared = self._request(robot_name, 'DELETE', '/mission_queue')
        paused = self._request(
            robot_name, 'PUT', '/status', {'state_id': _STATE_PAUSE})
        with self._lock:
            self._queue_ids.pop(robot_name, None)
        return cleared is not None and paused is not None
