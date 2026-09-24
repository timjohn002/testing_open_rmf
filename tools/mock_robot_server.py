#!/usr/bin/env python3
"""
Mock robots that speak the generic REST contract (see
fleet_adapters/multi_brand_fleet_adapter/.../drivers/generic_rest.py).

This is NOT a simulator: robots are points that move in a straight line
towards whatever pose they are sent to. It lets you run the full RMF stack
and the web portal with no hardware and no Gazebo. It is also a reference for
writing a real vendor bridge -- replace the fake motion with calls to the
vendor's SDK/API.

    python3 tools/mock_robot_server.py --port 7001 \
        --robot brand_a_1@L1:0,0 --robot brand_a_2@L1:0,2 \
        --robot brand_b_1@L1:0,4

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


ROBOTS: dict[str, MockRobot] = {}
LOCK = threading.Lock()
ROUTE = re.compile(r'^/robots/([^/]+)/(state|navigate|action|stop|localize)$')


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
                if verb == 'navigate':
                    cid = robot.navigate(body)
                    return self._send(200, {'success': True, 'command_id': cid})
                if verb == 'action':
                    cid = robot.action(body)
                    return self._send(200, {'success': True, 'command_id': cid})
                if verb == 'stop':
                    robot.stop()
                else:
                    robot.localize(body)
            except (KeyError, TypeError, ValueError) as e:
                return self._send(400, {'success': False, 'error': str(e)})
        self._send(200, {'success': True})


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


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7001)
    parser.add_argument(
        '--robot', action='append', default=[],
        help='name@map:x,y[,yaw]  (repeatable)')
    args = parser.parse_args()

    specs = args.robot or [
        'brand_a_1@L1:0,0', 'brand_a_2@L1:0,2', 'brand_b_1@L1:0,4']
    for spec in specs:
        robot = parse_robot(spec)
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
