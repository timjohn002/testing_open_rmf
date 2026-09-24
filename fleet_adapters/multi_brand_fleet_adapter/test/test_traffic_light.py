"""Traffic Light tests. No ROS needed: RMF's Python bindings are faked."""

import enum
import importlib.util
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import types

import pytest

from multi_brand_fleet_adapter.traffic_light.api_server_reporter import (
    ApiServerReporter, robot_state_json)
from multi_brand_fleet_adapter.traffic_light.drivers import load_driver
from multi_brand_fleet_adapter.traffic_light.drivers.generic_rest import (
    GenericRestTrafficLightAPI)
from multi_brand_fleet_adapter.traffic_light.robot_api import (
    TrafficLightRobotAPI, TrafficLightUpdateData)

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- Fake rmf_adapter -----------------------------------------------------------
class MovingInstruction(enum.Enum):
    MovingError = 0
    ContinueAtNextCheckpoint = 1
    WaitAtNextCheckpoint = 2
    PauseImmediately = 3


class WaitingInstruction(enum.Enum):
    WaitingError = 0
    Resume = 1
    Wait = 2


class Waypoint:
    def __init__(self, map_name, position, mandatory_delay, yield_):
        self.map_name = map_name
        self.position = position


@pytest.fixture
def command_handle_module(monkeypatch):
    rmf_adapter = types.ModuleType('rmf_adapter')
    rmf_adapter.Waypoint = Waypoint
    etl = types.ModuleType('rmf_adapter.easy_traffic_light')
    etl.MovingInstruction = MovingInstruction
    etl.WaitingInstruction = WaitingInstruction
    rmf_adapter.easy_traffic_light = etl
    monkeypatch.setitem(sys.modules, 'rmf_adapter', rmf_adapter)
    monkeypatch.setitem(sys.modules, 'rmf_adapter.easy_traffic_light', etl)
    name = 'multi_brand_fleet_adapter.traffic_light.command_handle'
    monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module(name)


class FakeEasyTrafficLight:
    """Records calls; answers with scripted instructions."""

    def __init__(self):
        self.calls = []
        self.waiting = WaitingInstruction.Resume
        self.moving = MovingInstruction.ContinueAtNextCheckpoint

    def follow_new_path(self, wps):
        self.calls.append(('follow_new_path', len(wps)))

    def waiting_at(self, cp):
        self.calls.append(('waiting_at', cp))
        return self.waiting

    def waiting_after(self, cp, pose):
        self.calls.append(('waiting_after', cp))
        return self.waiting

    def moving_from(self, cp, pose):
        self.calls.append(('moving_from', cp))
        return self.moving

    def update_idle_location(self, map_name, pose):
        self.calls.append(('idle', map_name))


class RecordingAPI(TrafficLightRobotAPI):
    def __init__(self):
        super().__init__({})
        self.calls = []

    def get_data(self, robot_name):
        return None

    def pause(self, robot_name):
        self.calls.append('pause')
        return True

    def resume(self, robot_name):
        self.calls.append('resume')
        return True

    def pause_at_checkpoint(self, robot_name, checkpoint):
        self.calls.append(('pause_at_checkpoint', checkpoint))
        return True


class FakeLogger:
    def info(self, *_):
        pass

    warn = error = info


class FakeNode:
    def get_logger(self):
        return FakeLogger()


PATH = [{'map_name': 'L1', 'x': float(x), 'y': 0.0, 'yaw': 0.0}
        for x in range(4)]


def snapshot(last_cp, moving, path=PATH, error=None):
    return TrafficLightUpdateData(
        robot_name='r', map_name='L1', position=[0.0, 0.0, 0.0],
        current_path=[dict(w) for w in path],
        last_completed_checkpoint=last_cp, is_moving=moving, error=error)


def test_handle_full_path_lifecycle(command_handle_module):
    api, rmf, reporter = RecordingAPI(), FakeEasyTrafficLight(), \
        ApiServerReporter('fleet', None)
    h = command_handle_module.TrafficLightCommandHandle(
        'fleet', 'r', api, FakeNode(), reporter=reporter)
    h.traffic_light_cb(rmf)

    # New path: registered with RMF and the robot is held at the start.
    h.update_state(snapshot(0, True))
    assert ('follow_new_path', 4) in rmf.calls
    assert api.calls == ['pause']
    assert reporter.message()['data']['robots']['r']['status'] == 'working'

    # RMF clears the start -> resume.
    h.update_state(snapshot(0, False))
    assert ('waiting_at', 0) in rmf.calls
    assert api.calls[-1] == 'resume'

    # Moving; RMF says stop at the next checkpoint -> gate at cp 2.
    rmf.moving = MovingInstruction.WaitAtNextCheckpoint
    h.update_state(snapshot(1, True))
    assert api.calls[-1] == ('pause_at_checkpoint', 2)

    # Stopped at the gate, waiting for traffic.
    rmf.waiting = WaitingInstruction.Wait
    h.update_state(snapshot(2, False))
    issues = reporter.message()['data']['robots']['r']['issues']
    assert [i['category'] for i in issues] == ['waiting_for_traffic']

    # Clear -> resume.
    rmf.waiting = WaitingInstruction.Resume
    h.update_state(snapshot(2, False))
    assert api.calls[-1] == 'resume'

    # Imminent conflict -> immediate pause.
    rmf.moving = MovingInstruction.PauseImmediately
    h.update_state(snapshot(2, True))
    assert api.calls[-1] == 'pause'

    # Path done: RMF told the robot has arrived, robot goes idle.
    h.update_state(snapshot(3, False))
    assert ('waiting_at', 3) in rmf.calls
    assert rmf.calls[-1] == ('idle', 'L1')
    assert reporter.message()['data']['robots']['r']['status'] == 'idle'


def test_handle_reports_robot_error(command_handle_module):
    api, rmf, reporter = RecordingAPI(), FakeEasyTrafficLight(), \
        ApiServerReporter('fleet', None)
    h = command_handle_module.TrafficLightCommandHandle(
        'fleet', 'r', api, FakeNode(), reporter=reporter)
    h.traffic_light_cb(rmf)
    h.update_state(snapshot(0, False, error='bumper pressed'))
    robot = reporter.message()['data']['robots']['r']
    assert robot['status'] == 'error'
    assert robot['issues'][0]['detail'] == 'bumper pressed'


def test_robot_state_json_shape():
    r = robot_state_json('r', 'L1', [1.0, 2.0, 0.5], 1.4, True, None, False)
    assert r['battery'] == 1.0  # clamped to the schema range
    assert r['location'] == {'map': 'L1', 'x': 1.0, 'y': 2.0, 'yaw': 0.5}
    assert r['status'] == 'working' and r['task_id'] == ''


# --- REST driver against the mock server ------------------------------------------
@pytest.fixture(scope='module')
def mock_server():
    spec = importlib.util.spec_from_file_location(
        'mock_robot_server_tl', REPO_ROOT / 'tools' / 'mock_robot_server.py')
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    mock.LINEAR_SPEED = 5.0
    mock.TrafficLightMockRobot.IDLE_S = 0.0
    mock.ROBOTS['c1'] = mock.parse_tl_robot('c1@L1:0,0;2,0;4,0;6,0')
    threading.Thread(target=mock.physics_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), mock.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


def wait_for(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return None


def test_load_traffic_light_driver():
    assert isinstance(load_driver({'prefix': 'http://x'}),
                      GenericRestTrafficLightAPI)
    with pytest.raises(ValueError):
        load_driver({'driver': 'nope'})


def test_rest_driver_pause_gate_resume(mock_server):
    api = GenericRestTrafficLightAPI({'prefix': mock_server})
    assert api.check_connection()

    data = wait_for(lambda: (d := api.get_data('c1')) and d.current_path and d)
    assert data.map_name == 'L1'
    assert len(data.current_path) == 4
    assert data.current_path[0]['map_name'] == 'L1'

    assert api.pause('c1')
    time.sleep(0.3)
    a = api.get_data('c1')
    time.sleep(0.3)
    b = api.get_data('c1')
    assert not b.is_moving and a.position == b.position

    assert api.pause_at_checkpoint('c1', 2)
    assert api.resume('c1')  # resume clears the gate on the mock...
    assert api.pause_at_checkpoint('c1', 2)  # ...so gate again
    gated = wait_for(lambda: (d := api.get_data('c1')) and
                     d.last_completed_checkpoint == 2 and not d.is_moving
                     and d)
    assert gated is not None
    time.sleep(0.3)
    assert api.get_data('c1').last_completed_checkpoint == 2

    assert api.resume('c1')
    assert wait_for(lambda: api.get_data('c1').last_completed_checkpoint == 3)

    assert api.get_data('missing') is None
    assert not api.pause('missing')
