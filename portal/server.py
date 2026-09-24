"""
Robot control portal: a small backend-for-frontend over the rmf-web
api-server.

    browser  --->  portal (this file)  --->  rmf-web api-server  --->  ROS 2 / Open-RMF
                   * serves the UI           * REST + socket.io         * dispatcher
                   * holds the RMF token     * stores tasks/fleets      * fleet adapters
                   * exposes only the                                    (one per brand)
                     commands the UI needs

The browser never sees an RMF credential, and the portal only exposes a
fixed set of operations (go to place, patrol, cancel, (de)commission).

Configuration is by environment variable (see .env.example).
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx
import jwt
import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent

RMF_API_URL = os.environ.get('RMF_API_URL', 'http://localhost:8000').rstrip('/')
# Either a fixed token, or the settings to mint one that the api-server's
# JwtAuthenticator accepts (defaults match rmf-web's default_config.py).
RMF_API_TOKEN = os.environ.get('RMF_API_TOKEN', '')
RMF_JWT_SECRET = os.environ.get('RMF_JWT_SECRET', 'rmfisawesome')
RMF_JWT_ISS = os.environ.get('RMF_JWT_ISS', 'stub')
RMF_JWT_AUD = os.environ.get('RMF_JWT_AUD', 'rmf_api_server')
RMF_USER = os.environ.get('RMF_USER', 'admin')
NAV_GRAPHS = [p for p in os.environ.get(
    'NAV_GRAPHS', str(HERE.parent / 'maps' / 'site' / 'nav_graph.yaml')
).split(os.pathsep) if p]
PORTAL_USER = os.environ.get('PORTAL_USER', '')
PORTAL_PASSWORD = os.environ.get('PORTAL_PASSWORD', '')

app = FastAPI(title='Robot Control Portal')
_basic = HTTPBasic(auto_error=False)


def require_operator(
    creds: Optional[HTTPBasicCredentials] = Depends(_basic),
) -> str:
    """HTTP basic auth, enabled when PORTAL_USER/PORTAL_PASSWORD are set."""
    if not PORTAL_USER:
        return RMF_USER
    ok = creds is not None and \
        secrets.compare_digest(creds.username, PORTAL_USER) and \
        secrets.compare_digest(creds.password, PORTAL_PASSWORD)
    if not ok:
        raise HTTPException(
            401, 'Unauthorized', headers={'WWW-Authenticate': 'Basic'})
    return creds.username


# --- RMF api-server client ------------------------------------------------------
_token_cache: dict[str, float | str] = {'token': '', 'exp': 0.0}


def _rmf_token() -> str:
    if RMF_API_TOKEN:
        return RMF_API_TOKEN
    now = time.time()
    if _token_cache['token'] and float(_token_cache['exp']) - 60 > now:
        return str(_token_cache['token'])
    exp = now + 3600
    token = jwt.encode(
        {'preferred_username': RMF_USER, 'iss': RMF_JWT_ISS,
         'aud': RMF_JWT_AUD, 'iat': int(now), 'exp': int(exp)},
        RMF_JWT_SECRET, algorithm='HS256')
    _token_cache.update(token=token, exp=exp)
    return token


async def rmf(method: str, path: str, **kwargs):
    """Call the rmf-web api-server and return decoded JSON."""
    headers = {'Authorization': f'Bearer {_rmf_token()}'}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.request(
                method, f'{RMF_API_URL}{path}', headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(502, f'RMF api-server unreachable: {e}') from e
    try:
        body = r.json()
    except ValueError:
        body = {'detail': r.text}
    if r.status_code >= 400:
        raise HTTPException(r.status_code, body)
    return body


def _now_ms() -> int:
    return int(time.time() * 1000)


def _check_rmf_result(resp: dict) -> dict:
    """RMF can reply 200 with success=false for a rejected request."""
    if isinstance(resp, dict) and resp.get('success') is False:
        raise HTTPException(400, resp)
    return resp


# --- Models ---------------------------------------------------------------------
class GoToRequest(BaseModel):
    place: str = Field(min_length=1)


class PatrolRequest(BaseModel):
    places: list[str] = Field(min_length=1)
    rounds: int = Field(default=1, ge=1, le=100)
    fleet: Optional[str] = None  # None = let RMF pick the best fleet
    robot: Optional[str] = None  # set together with fleet for a direct task


class DecommissionRequest(BaseModel):
    reassign_tasks: bool = True
    allow_idle_behavior: bool = False


# --- API ------------------------------------------------------------------------
@app.get('/api/health')
async def health():
    try:
        await rmf('GET', '/fleets')
        return {'portal': 'ok', 'rmf': 'ok', 'rmf_api_url': RMF_API_URL}
    except HTTPException as e:
        return JSONResponse(
            {'portal': 'ok', 'rmf': 'error', 'detail': e.detail,
             'rmf_api_url': RMF_API_URL}, status_code=200)


@app.get('/api/fleets')
async def fleets(_: str = Depends(require_operator)):
    """Every fleet (brand) and its robots, flattened for the UI."""
    data = await rmf('GET', '/fleets')
    out = []
    for fleet in data or []:
        robots = []
        for name, r in sorted((fleet.get('robots') or {}).items()):
            commission = r.get('commission') or {}
            robots.append({
                'name': r.get('name') or name,
                'status': r.get('status') or 'uninitialized',
                'task_id': r.get('task_id') or '',
                'battery': r.get('battery'),
                'location': r.get('location'),
                'issues': r.get('issues') or [],
                'unix_millis_time': r.get('unix_millis_time'),
                'commissioned': commission.get('dispatch_tasks', True),
            })
        out.append({'name': fleet.get('name'), 'robots': robots})
    return out


@app.get('/api/tasks')
async def tasks(limit: int = 30, _: str = Depends(require_operator)):
    return await rmf('GET', '/tasks', params={
        'limit': max(1, min(limit, 200)),
        'order_by': '-unix_millis_request_time'})


def _load_site() -> dict:
    """Named places and lanes from the same nav graphs the fleets use."""
    places, lanes = {}, []
    for path in NAV_GRAPHS:
        try:
            graph = yaml.safe_load(Path(path).read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        for level, content in (graph.get('levels') or {}).items():
            vertices = content.get('vertices') or []
            for v in vertices:
                opts = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
                if opts.get('name'):
                    places[opts['name']] = {
                        'name': opts['name'], 'map': level,
                        'x': v[0], 'y': v[1],
                        'is_charger': bool(opts.get('is_charger')),
                    }
            for lane in content.get('lanes') or []:
                try:
                    a, b = vertices[lane[0]], vertices[lane[1]]
                except (IndexError, TypeError):
                    continue
                lanes.append({'map': level, 'x1': a[0], 'y1': a[1],
                              'x2': b[0], 'y2': b[1]})
    return {'places': sorted(places.values(), key=lambda p: p['name']),
            'lanes': lanes}


@app.get('/api/site')
async def site(_: str = Depends(require_operator)):
    return _load_site()


@app.post('/api/robots/{fleet}/{robot}/go_to')
async def go_to(fleet: str, robot: str, body: GoToRequest,
                user: str = Depends(require_operator)):
    now = _now_ms()
    payload = {
        'type': 'robot_task_request',
        'fleet': fleet,
        'robot': robot,
        'request': {
            'category': 'compose',
            'description': {
                'category': 'go_to_place',
                'phases': [{'activity': {
                    'category': 'go_to_place',
                    'description': {'one_of': [{'waypoint': body.place}]},
                }}],
            },
            'unix_millis_request_time': now,
            'unix_millis_earliest_start_time': now,
            'requester': user,
            'labels': ['app=portal'],
        },
    }
    return await rmf('POST', '/tasks/robot_task', json=payload)


@app.post('/api/tasks/patrol')
async def patrol(body: PatrolRequest, user: str = Depends(require_operator)):
    now = _now_ms()
    request = {
        'category': 'patrol',
        'description': {'places': body.places, 'rounds': body.rounds},
        'unix_millis_request_time': now,
        'unix_millis_earliest_start_time': now,
        'requester': user,
        'labels': ['app=portal'],
    }
    if body.robot and body.fleet:
        payload = {'type': 'robot_task_request', 'fleet': body.fleet,
                   'robot': body.robot, 'request': request}
        return await rmf('POST', '/tasks/robot_task', json=payload)
    if body.fleet:
        request['fleet_name'] = body.fleet
    payload = {'type': 'dispatch_task_request', 'request': request}
    return await rmf('POST', '/tasks/dispatch_task', json=payload)


@app.post('/api/tasks/{task_id}/cancel')
async def cancel(task_id: str, user: str = Depends(require_operator)):
    return _check_rmf_result(await rmf(
        'POST', '/tasks/cancel_task',
        json={'type': 'cancel_task_request', 'task_id': task_id,
              'labels': [f'cancelled_by={user}']}))


@app.post('/api/robots/{fleet}/{robot}/decommission')
async def decommission(fleet: str, robot: str, body: DecommissionRequest,
                       _: str = Depends(require_operator)):
    return await rmf('POST', f'/fleets/{fleet}/decommission', params={
        'robot_name': robot,
        'reassign_tasks': str(body.reassign_tasks).lower(),
        'allow_idle_behavior': str(body.allow_idle_behavior).lower()})


@app.post('/api/robots/{fleet}/{robot}/recommission')
async def recommission(fleet: str, robot: str,
                       _: str = Depends(require_operator)):
    return await rmf('POST', f'/fleets/{fleet}/recommission',
                     params={'robot_name': robot})


# --- UI -------------------------------------------------------------------------
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')


@app.get('/')
async def index(_: str = Depends(require_operator)):
    return FileResponse(HERE / 'static' / 'index.html')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        app, host=os.environ.get('PORTAL_HOST', '127.0.0.1'),
        port=int(os.environ.get('PORTAL_PORT', '8080')))
