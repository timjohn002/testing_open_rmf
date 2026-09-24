#!/usr/bin/env python3
"""
Stand-in for the rmf-web api-server, for developing the portal without ROS.

It serves the subset of endpoints the portal uses, with the same request and
response shapes, and moves the robots of ``tools/mock_robot_server.py`` in
straight lines between nav-graph places. There is NO traffic scheduling,
task bidding or collision avoidance -- that is what real Open-RMF provides.
Use it to work on the UI; use the real stack to control robots.

    python3 tools/mock_robot_server.py &
    python3 tools/mock_rmf_api.py --robots-url http://127.0.0.1:7001 \
        --fleet brand_a=brand_a_1,brand_a_2 --fleet brand_b=brand_b_1
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import itertools
import time
from pathlib import Path

import httpx
import jwt
import uvicorn
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException

ROOT = Path(__file__).resolve().parents[1]
SECRET, ISS, AUD = 'rmfisawesome', 'stub', 'rmf_api_server'

@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(executor())
    yield
    task.cancel()


app = FastAPI(title='Mock rmf-web api-server', lifespan=lifespan)
CFG = {'robots_url': 'http://127.0.0.1:7001', 'fleets': {}, 'places': {}}
TASKS: dict[str, dict] = {}
QUEUES: dict[str, list[str]] = {}  # robot -> task ids
COMMISSION: dict[str, bool] = {}
_ids = itertools.count(1)


def auth(authorization: str = Header('')):
    try:
        scheme, token = authorization.split(' ', 1)
        assert scheme.lower() == 'bearer'
        jwt.decode(token, SECRET, algorithms=['HS256'], audience=AUD, issuer=ISS)
    except Exception:
        raise HTTPException(401, 'invalid token')


def now_ms():
    return int(time.time() * 1000)


async def robot_states():
    async with httpx.AsyncClient(timeout=2.0) as c:
        return (await c.get(f"{CFG['robots_url']}/robots")).json()


async def robot_cmd(robot, verb, body):
    async with httpx.AsyncClient(timeout=2.0) as c:
        return (await c.post(f"{CFG['robots_url']}/robots/{robot}/{verb}", json=body)).json()


def fleet_of(robot):
    return next((f for f, rs in CFG['fleets'].items() if robot in rs), None)


@app.get('/fleets', dependencies=[Depends(auth)])
async def fleets():
    try:
        states = await robot_states()
    except httpx.HTTPError:
        states = {}
    out = []
    for fleet, robots in CFG['fleets'].items():
        robot_map = {}
        for name in robots:
            s = states.get(name)
            active = next((t for t in QUEUES.get(name, [])
                           if TASKS[t]['status'] == 'underway'), '')
            robot_map[name] = {
                'name': name,
                'status': 'offline' if s is None else ('working' if active else 'idle'),
                'task_id': active,
                'unix_millis_time': now_ms(),
                'location': None if s is None else {
                    'map': s['map'], 'x': s['position']['x'],
                    'y': s['position']['y'], 'yaw': s['position']['yaw']},
                'battery': None if s is None else s['battery'],
                'issues': [],
                'commission': {'dispatch_tasks': COMMISSION.get(name, True),
                               'direct_tasks': COMMISSION.get(name, True),
                               'idle_behavior': True},
            }
        out.append({'name': fleet, 'robots': robot_map})
    return out


@app.get('/tasks', dependencies=[Depends(auth)])
async def tasks(limit: int = 100, order_by: str = ''):
    items = sorted(TASKS.values(), key=lambda t: t['booking']['unix_millis_request_time'],
                   reverse=True)
    return [{k: v for k, v in t.items() if k != '_places'} for t in items[:limit]]


def places_of(request):
    desc = request.get('description') or {}
    if request.get('category') == 'patrol':
        return list(desc.get('places') or []) * int(desc.get('rounds', 1))
    try:
        return [desc['phases'][0]['activity']['description']['one_of'][0]['waypoint']]
    except (KeyError, IndexError, TypeError):
        return []


def create_task(fleet, robot, request):
    for p in places_of(request):
        if p not in CFG['places']:
            return {'success': False, 'errors': [{'code': 4, 'category': 'Unknown place', 'detail': p}]}
    if not places_of(request):
        return {'success': False, 'errors': [{'category': 'Unsupported task'}]}
    task_id = f"{request.get('category', 'task')}.mock{next(_ids)}"
    TASKS[task_id] = {
        'booking': {'id': task_id, 'unix_millis_request_time': now_ms(),
                    'requester': request.get('requester'), 'labels': request.get('labels')},
        'category': request.get('category'),
        'assigned_to': {'group': fleet, 'name': robot},
        'status': 'queued',
        '_places': places_of(request),
    }
    QUEUES.setdefault(robot, []).append(task_id)
    return {'success': True, 'state': {k: v for k, v in TASKS[task_id].items() if k != '_places'}}


@app.post('/tasks/robot_task', dependencies=[Depends(auth)])
async def robot_task(body: dict):
    robot, fleet = body['robot'], body['fleet']
    if robot not in CFG['fleets'].get(fleet, []):
        return {'success': False, 'errors': [{'category': 'Unknown robot'}]}
    if not COMMISSION.get(robot, True):
        return {'success': False, 'errors': [{'category': 'Robot is decommissioned'}]}
    return create_task(fleet, robot, body['request'])


@app.post('/tasks/dispatch_task', dependencies=[Depends(auth)])
async def dispatch_task(body: dict):
    request = body['request']
    fleets = [request['fleet_name']] if request.get('fleet_name') else list(CFG['fleets'])
    candidates = [(f, r) for f in fleets for r in CFG['fleets'].get(f, [])
                  if COMMISSION.get(r, True)]
    if not candidates:
        return {'success': False, 'errors': [{'category': 'No robot available'}]}
    # "Bidding": shortest queue wins.
    fleet, robot = min(candidates, key=lambda fr: len(
        [t for t in QUEUES.get(fr[1], []) if TASKS[t]['status'] in ('queued', 'underway')]))
    return create_task(fleet, robot, request)


@app.post('/tasks/cancel_task', dependencies=[Depends(auth)])
async def cancel_task(body: dict):
    task = TASKS.get(body.get('task_id'))
    if task is None or task['status'] not in ('queued', 'underway'):
        return {'success': False, 'errors': [{'category': 'Task not cancellable'}]}
    if task['status'] == 'underway':
        await robot_cmd(task['assigned_to']['name'], 'stop', {})
    task['status'] = 'canceled'
    return {'success': True}


@app.post('/fleets/{fleet}/decommission', dependencies=[Depends(auth)])
async def decommission(fleet: str, robot_name: str, reassign_tasks: bool = False,
                       allow_idle_behavior: bool = False):
    COMMISSION[robot_name] = False
    return {'commission': {'success': True}}


@app.post('/fleets/{fleet}/recommission', dependencies=[Depends(auth)])
async def recommission(fleet: str, robot_name: str):
    COMMISSION[robot_name] = True
    return {'commission': {'success': True}}


async def executor():
    """Run each robot's queue: drive to each place in turn."""
    progress: dict[str, int] = {}
    while True:
        await asyncio.sleep(0.5)
        try:
            states = await robot_states()
        except httpx.HTTPError:
            continue
        for robot, queue in QUEUES.items():
            live = [t for t in queue if TASKS[t]['status'] in ('queued', 'underway')]
            if not live or robot not in states:
                continue
            task = TASKS[live[0]]
            if task['status'] == 'queued':
                task['status'] = 'underway'
                progress[live[0]] = -1
            step = progress[live[0]]
            if step >= 0 and not states[robot]['command']['completed']:
                continue
            step += 1
            if step >= len(task['_places']):
                task['status'] = 'completed'
                continue
            progress[live[0]] = step
            p = CFG['places'][task['_places'][step]]
            await robot_cmd(robot, 'navigate', {'map': p['map'], 'x': p['x'], 'y': p['y'], 'yaw': 0.0})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--robots-url', default='http://127.0.0.1:7001')
    parser.add_argument('--nav-graph', default=str(ROOT / 'maps' / 'site' / 'nav_graph.yaml'))
    parser.add_argument('--fleet', action='append', default=[],
                        help='fleet=robot1,robot2 (repeatable)')
    args = parser.parse_args()
    CFG['robots_url'] = args.robots_url
    for spec in args.fleet or ['brand_a=brand_a_1,brand_a_2', 'brand_b=brand_b_1']:
        fleet, robots = spec.split('=', 1)
        CFG['fleets'][fleet] = robots.split(',')
    graph = yaml.safe_load(Path(args.nav_graph).read_text())
    for level, content in graph['levels'].items():
        for v in content['vertices']:
            if v[2].get('name'):
                CFG['places'][v[2]['name']] = {'map': level, 'x': v[0], 'y': v[1]}
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
