"""Driver tests. No ROS needed: pytest fleet_adapters/multi_brand_fleet_adapter"""

import importlib.util
from http.server import ThreadingHTTPServer
import math
from pathlib import Path
import threading
import time

import pytest

from multi_brand_fleet_adapter.drivers import load_driver
from multi_brand_fleet_adapter.drivers.generic_rest import GenericRestRobotAPI
from multi_brand_fleet_adapter.drivers.mir import MiRRobotAPI

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope='module')
def mock_server():
    spec = importlib.util.spec_from_file_location(
        'mock_robot_server', REPO_ROOT / 'tools' / 'mock_robot_server.py')
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    mock.LINEAR_SPEED = 20.0  # make tests fast
    mock.ANGULAR_SPEED = 50.0
    mock.ROBOTS['r1'] = mock.MockRobot('r1', 'L1', 0.0, 0.0)
    threading.Thread(target=mock.physics_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), mock.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


def test_load_driver_by_name():
    api = load_driver({'driver': 'generic_rest', 'prefix': 'http://x'})
    assert isinstance(api, GenericRestRobotAPI)
    api = load_driver({'driver': 'mir', 'robots': {}})
    assert isinstance(api, MiRRobotAPI)
    with pytest.raises(ValueError):
        load_driver({'driver': 'nope'})


def test_generic_rest_navigate_until_complete(mock_server):
    api = GenericRestRobotAPI({'prefix': mock_server})
    assert api.check_connection()

    data = api.get_data('r1')
    assert data.map_name == 'L1'
    assert data.position == [0.0, 0.0, 0.0]
    assert 0.0 < data.battery_soc <= 1.0

    assert api.navigate('r1', [3.0, 4.0, 0.5], 'L1')
    api.get_data('r1')
    assert not api.is_command_completed('r1')

    deadline = time.time() + 5.0
    while time.time() < deadline:
        data = api.get_data('r1')
        if api.is_command_completed('r1'):
            break
        time.sleep(0.05)
    assert api.is_command_completed('r1')
    assert data.position[0] == pytest.approx(3.0, abs=1e-2)
    assert data.position[1] == pytest.approx(4.0, abs=1e-2)


def test_generic_rest_stop_and_unknown_robot(mock_server):
    api = GenericRestRobotAPI({'prefix': mock_server})
    assert api.navigate('r1', [50.0, 0.0, 0.0], 'L1')
    assert api.stop('r1')
    assert api.get_data('ghost') is None
    assert not api.navigate('ghost', [0.0, 0.0, 0.0], 'L1')


def test_mir_status_parsing(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, payload):
            self._payload = payload
            self.content = b'x'

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url, json, headers))
        if url.endswith('/status'):
            return Resp({'position': {'x': 1.0, 'y': 2.0, 'orientation': 90},
                         'battery_percentage': 55.0, 'map_id': 'abc'})
        if url.endswith('/maps/abc'):
            return Resp({'name': 'L1'})
        if url.endswith('/mission_queue') and method == 'POST':
            return Resp({'id': 7})
        if url.endswith('/mission_queue/7'):
            return Resp({'state': 'Done'})
        return Resp({})

    monkeypatch.setattr(
        'multi_brand_fleet_adapter.drivers.mir.requests.request', fake_request)
    api = MiRRobotAPI({
        'user': 'distributor', 'password': 'pw', 'move_mission_guid': 'm1',
        'robots': {'mir_1': {'prefix': 'http://mir/api/v2.0.0'}}})

    data = api.get_data('mir_1')
    assert data.map_name == 'L1'
    assert data.position[2] == pytest.approx(math.pi / 2)
    assert data.battery_soc == pytest.approx(0.55)
    assert calls[0][3]['Authorization'].startswith('Basic ')

    assert api.navigate('mir_1', [1.0, 2.0, math.pi], 'L1')
    post = [c for c in calls if c[0] == 'POST'][0]
    assert post[2]['mission_id'] == 'm1'
    assert {'id': 'orientation', 'value': pytest.approx(180.0)} in post[2]['parameters']
    assert api.is_command_completed('mir_1')


def test_generic_rest_dock(mock_server):
    api = GenericRestRobotAPI({'prefix': mock_server})
    assert api.dock('r1', 'charger_dock')
    api.get_data('r1')
    assert not api.is_command_completed('r1')
