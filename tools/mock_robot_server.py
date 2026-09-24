#!/usr/bin/env python3
"""
Mock robots that speak the generic REST contract (see
fleet_adapters/multi_brand_fleet_adapter/.../drivers/generic_rest.py).

This is NOT a simulator: robots are points that move in straight lines. It
lets you run the full RMF stack and the web portal with no hardware and no
Gazebo. It is also a reference for writing a real vendor bridge -- replace
the fake motion with calls to the vendor's SDK/API.

Two kinds of robot:

* ``--robot``     Full Control: goes wherever it is sent (navigate/stop/...).
* ``--tl-robot``  Traffic Light: drives its own route back and forth and only
                  accepts pause / resume / pause_at_checkpoint.

    python3 tools/mock_robot_server.py --port 7001 \
        --robot brand_a_1@L1:0,0 --robot brand_a_2@L1:0,2 \
        --robot brand_b_1@L1:0,4 \
        --tl-robot "brand_c_1@L1:15,2;10,2;10,0;5,0"

Only the Python standard library is used.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import threading
import time

LINEAR_SPEED = 0.5    # m/s
ANGULAR_SPEED = 1.0   # rad/s
DRAIN_PER_S = 0.0005  # battery fraction per second while moving
TICK = 0.1            # s


class MockRobot:

    def __init__(self, name, map_name, x, y, yaw=0.0):
        self.name = name
        self.map = map_name
        self.x, self.y, self.yaw = x, y, yaw
        self.battery = 1.0
        self.command_id = 0
        self.completed = True
        self.target = None          # (x, y, yaw) or None
        self.action_until = None    # end time of a fake action

    def state(self):
        return {
            'map': self.map,
            'position': {'x': round(self.x, 3), 'y': round(self.y, 3),
                         'yaw': round(self.yaw, 3)},
            'battery': round(self.battery, 4),
            'command': {'id': self.command_id, 'completed': self.completed},
        }

    def _new_command(self):
        self.command_id += 1
        self.completed = False
        return self.command_id

    def navigate(self, body):
        self.map = body.get('map', self.map)
        self.target = (float(body['x']), float(body['y']),
                       float(body.get('yaw', 0.0)))
        self.action_until = None
        return self._new_command()

    def action(self, body):
        self.target = None
        self.action_until = time.time() + 5.0
        return self._new_command()

    def stop(self):
        self.target = None
        self.action_until = None
        self.completed = True

    def localize(self, body):
        self.map = body.get('map', self.map)
        self.x, self.y = float(body['x']), float(body['y'])
        self.yaw = float(body.get('yaw', 0.0))

    def handle(self, verb, body):
        """Dispatch a POST; None means the verb is not supported."""
        if verb == 'navigate':
            return {'success': True, 'command_id': self.navigate(body)}
        if verb in ('action', 'dock'):
            return {'success': True, 'command_id': self.action(body)}
        if verb == 'stop':
            self.stop()
        elif verb == 'localize':
            self.localize(body)
        else:
            return None
        return {'success': True}

    def step(self, dt):
        if self.action_until is not None:
            if time.time() >= self.action_until:
                self.action_until = None
                self.completed = True
            return
        if self.target is None:
            # Idle robots slowly "charge" so long demos don't run flat.
            self.battery = min(1.0, self.battery + DRAIN_PER_S / 2 * dt)
            return
        tx, ty, tyaw = self.target
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        if dist > 1e-3:
            step = min(dist, LINEAR_SPEED * dt)
            self.x += dx / dist * step
            self.y += dy / dist * step
            self.yaw = math.atan2(dy, dx)
        else:
            err = math.atan2(math.sin(tyaw - self.yaw),
                             math.cos(tyaw - self.yaw))
            if abs(err) > 1e-2:
                self.yaw += max(-ANGULAR_SPEED * dt,
                                min(ANGULAR_SPEED * dt, err))
            else:
                self.yaw = tyaw
                self.target = None
                self.completed = True
        self.battery = max(0.0, self.battery - DRAIN_PER_S * dt)


class TrafficLightMockRobot:
    """Self-navigating robot: drives its route, then the reverse, forever.

    Between routes it idles for IDLE_S seconds with no path, like a real
    robot waiting for its next mission from the vendor's fleet manager.
    """

    IDLE_S = 4.0
    FINISH_HOLD_S = 1.5  # keep reporting the finished path briefly

    def __init__(self, name, map_name, route):
        self.name = name
        self.map = map_name
        self.route = route
        self.x, self.y = route[0]
        self.yaw = 0.0
        self.battery = 1.0
        self.path = []
        self.last_cp = -1
        self.moving = False
        self.paused = False
        self.gate_cp = None
        self.idle_until = time.time() + self.IDLE_S
        self.finished_at = None
        self.error = None

    def _start_path(self):
        pts = self.route
        self.path = []
        for i, (x, y) in enumerate(pts):
            nx, ny = pts[min(i + 1, len(pts) - 1)]
            yaw = math.atan2(ny - y, nx - x) if i + 1 < len(pts) else \
                self.path[-1]['yaw'] if self.path else 0.0
            self.path.append(
                {'map_name': self.map, 'x': x, 'y': y, 'yaw': yaw})
        self.last_cp = 0
        self.paused = False
        self.gate_cp = None
        self.finished_at = None
        self.route = list(reversed(self.route))  # come back next time

    def state(self):
        return {
            'map': self.map,
            'position': {'x': round(self.x, 3), 'y': round(self.y, 3),
                         'yaw': round(self.yaw, 3)},
            'battery': round(self.battery, 4),
            'current_path': self.path,
            'last_completed_checkpoint': self.last_cp,
            'is_moving': self.moving,
            'error': self.error,
        }

    def handle(self, verb, body):
        if verb == 'pause':
            self.paused = True
        elif verb == 'resume':
            self.paused = False
            self.gate_cp = None
        elif verb == 'pause_at_checkpoint':
            self.gate_cp = int(body['checkpoint'])
        else:
            return None
        return {'success': True}

    def step(self, dt):
        now = time.time()
        if not self.path:
            self.moving = False
            if now >= self.idle_until:
                self._start_path()
            return
        if self.finished_at is not None:
            if now - self.finished_at >= self.FINISH_HOLD_S:
                self.path, self.last_cp = [], -1
                self.idle_until = now + self.IDLE_S
            return
        gated = self.gate_cp is not None and self.last_cp >= self.gate_cp
        if self.paused or gated:
            self.moving = False
            return
        target = self.path[self.last_cp + 1]
        dx, dy = target['x'] - self.x, target['y'] - self.y
        dist = math.hypot(dx, dy)
        step = LINEAR_SPEED * dt
        self.moving = True
        if dist <= step:
            self.x, self.y = target['x'], target['y']
            self.last_cp += 1
            if self.last_cp >= len(self.path) - 1:
                self.moving = False
                self.finished_at = now
        else:
            self.x += dx / dist * step
            self.y += dy / dist * step
            self.yaw = math.atan2(dy, dx)
        self.battery = max(0.0, self.battery - DRAIN_PER_S * dt)


ROBOTS: dict = {}
LOCK = threading.Lock()
ROUTE = re.compile(r'^/robots/([^/]+)/([a-z_]+)$')


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        match = ROUTE.match(self.path)
        if not match:
            return None, None
        robot = ROBOTS.get(match.group(1))
        return robot, match.group(2)

    def do_GET(self):
        if self.path == '/health':
            return self._send(200, {'ok': True})
        if self.path == '/robots':
            with LOCK:
                return self._send(
                    200, {n: r.state() for n, r in ROBOTS.items()})
        robot, verb = self._route()
        if robot is None or verb != 'state':
            return self._send(404, {'error': 'not found'})
        with LOCK:
            self._send(200, robot.state())

    def do_POST(self):
        robot, verb = self._route()
        if robot is None or verb == 'state':
            return self._send(404, {'success': False, 'error': 'not found'})
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self._send(400, {'success': False, 'error': 'bad json'})
        with LOCK:
            try:
                resp = robot.handle(verb, body)
            except (KeyError, TypeError, ValueError) as e:
                return self._send(400, {'success': False, 'error': str(e)})
        if resp is None:
            return self._send(404, {'success': False, 'error': 'not found'})
        self._send(200, resp)


def physics_loop():
    last = time.time()
    while True:
        time.sleep(TICK)
        now = time.time()
        with LOCK:
            for robot in ROBOTS.values():
                robot.step(now - last)
        last = now


def parse_robot(spec):
    """name@map:x,y[,yaw]"""
    name, rest = spec.split('@', 1)
    map_name, coords = rest.split(':', 1)
    values = [float(v) for v in coords.split(',')]
    return MockRobot(name, map_name, *values)


def parse_tl_robot(spec):
    """name@map:x1,y1;x2,y2;..."""
    name, rest = spec.split('@', 1)
    map_name, pts = rest.split(':', 1)
    route = [tuple(float(v) for v in p.split(',')) for p in pts.split(';')]
    if len(route) < 2:
        raise ValueError(f'{name}: a route needs at least 2 points')
    return TrafficLightMockRobot(name, map_name, route)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7001)
    parser.add_argument(
        '--robot', action='append', default=[],
        help='Full Control robot: name@map:x,y[,yaw]  (repeatable)')
    parser.add_argument(
        '--tl-robot', action='append', default=[],
        help='Traffic Light robot: "name@map:x1,y1;x2,y2;..."  (repeatable)')
    args = parser.parse_args()

    if not args.robot and not args.tl_robot:
        args.robot = ['brand_a_1@L1:0,0', 'brand_a_2@L1:0,2',
                      'brand_b_1@L1:0,4']
        args.tl_robot = ['brand_c_1@L1:15,2;10,2;10,0;5,0']
    for spec in args.robot:
        robot = parse_robot(spec)
        ROBOTS[robot.name] = robot
    for spec in args.tl_robot:
        robot = parse_tl_robot(spec)
        ROBOTS[robot.name] = robot

    threading.Thread(target=physics_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Mock robots {sorted(ROBOTS)} on http://{args.host}:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
