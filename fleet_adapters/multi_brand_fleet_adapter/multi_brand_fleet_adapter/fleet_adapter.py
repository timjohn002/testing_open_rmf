# Copyright 2021 Open Source Robotics Foundation, Inc.
# Copyright 2026 timjohn002
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
EasyFullControl fleet adapter, adapted from open-rmf/fleet_adapter_template.

Run one instance per fleet (typically one per brand). The robot brand is
chosen by ``fleet_manager.driver`` in the config file; see ``drivers/``.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time

import rclpy
import rclpy.node
from rclpy.duration import Duration
from rclpy.parameter import Parameter
import rmf_adapter
from rmf_adapter import Adapter
from rmf_adapter import Transformation
import rmf_adapter.easy_full_control as rmf_easy
import yaml

from .drivers import load_driver
from .robot_api import RobotAPI


def compute_transforms(level, coords, node=None):
    """Get the transform from RMF to robot coordinates for one level."""
    import nudged  # only needed when reference_coordinates are configured
    rmf_coords = coords['rmf']
    robot_coords = coords['robot']
    tf = nudged.estimate(rmf_coords, robot_coords)
    if node:
        mse = nudged.estimate_error(tf, rmf_coords, robot_coords)
        node.get_logger().info(
            f'Transformation error estimate for {level}: {mse}')
    return Transformation(
        tf.get_rotation(), tf.get_scale(), tf.get_translation())


def main(argv=sys.argv):
    rclpy.init(args=argv)
    rmf_adapter.init_rclcpp()
    args_without_ros = rclpy.utilities.remove_ros_args(argv)

    parser = argparse.ArgumentParser(
        prog='fleet_adapter',
        description='Configure and spin up a multi-brand fleet adapter')
    parser.add_argument('-c', '--config_file', type=str, required=True,
                        help='Path to the fleet config.yaml file')
    parser.add_argument('-n', '--nav_graph', type=str, required=True,
                        help='Path to the nav_graph for this fleet')
    parser.add_argument('-s', '--server_uri', type=str, default='',
                        help='rmf-web api-server websocket, e.g. '
                             'ws://localhost:8000/_internal')
    parser.add_argument('-sim', '--use_sim_time', action='store_true',
                        help='Use sim time, default: false')
    args = parser.parse_args(args_without_ros[1:])

    fleet_config = rmf_easy.FleetConfiguration.from_config_files(
        args.config_file, args.nav_graph)
    assert fleet_config, f'Failed to parse config file [{args.config_file}]'

    with open(args.config_file, 'r') as f:
        config_yaml = yaml.safe_load(f)

    fleet_name = fleet_config.fleet_name
    node = rclpy.node.Node(f'{fleet_name}_command_handle')
    adapter = Adapter.make(f'{fleet_name}_fleet_adapter')
    assert adapter, (
        'Unable to initialize fleet adapter. '
        'Please ensure the RMF schedule node is running')

    if args.use_sim_time:
        node.set_parameters(
            [Parameter('use_sim_time', Parameter.Type.BOOL, True)])
        adapter.node.use_sim_time()

    adapter.start()
    time.sleep(1.0)

    fleet_config.server_uri = args.server_uri or None

    # Without reference_coordinates the robot frame is assumed to be the
    # same as the RMF (nav graph) frame.
    for level, coords in (config_yaml.get('reference_coordinates') or {}).items():
        fleet_config.add_robot_coordinates_transformation(
            level, compute_transforms(level, coords, node))

    fleet_handle = adapter.add_easy_fleet(fleet_config)

    api = load_driver(config_yaml['fleet_manager'])
    node.get_logger().info(
        f'[{fleet_name}] using driver '
        f"[{config_yaml['fleet_manager'].get('driver', 'generic_rest')}]")
    if not api.check_connection():
        node.get_logger().warn(
            f'[{fleet_name}] vendor API not reachable yet; will keep polling')

    robots = {}
    for robot_name in fleet_config.known_robots:
        robot_config = fleet_config.get_known_robot_configuration(robot_name)
        robots[robot_name] = RobotAdapter(
            robot_name, robot_config, node, api, fleet_handle)

    update_period = 1.0 / config_yaml['rmf_fleet'].get(
        'robot_state_update_frequency', 10.0)

    def update_loop():
        with ThreadPoolExecutor(max_workers=max(1, len(robots))) as pool:
            while rclpy.ok():
                now = node.get_clock().now()
                # Poll all robots in parallel so one slow robot does not
                # delay the others.
                list(pool.map(update_robot, robots.values()))
                next_wakeup = now + Duration(nanoseconds=update_period * 1e9)
                while rclpy.ok() and node.get_clock().now() < next_wakeup:
                    time.sleep(0.001)

    threading.Thread(target=update_loop, daemon=True).start()

    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        executor.shutdown()
        rclpy.try_shutdown()


class RobotAdapter:
    """Glue between one robot's RMF update handle and its vendor driver."""

    def __init__(self, name, configuration, node, api: RobotAPI,
                 fleet_handle):
        self.name = name
        self.execution = None
        self.update_handle = None
        self.configuration = configuration
        self.node = node
        self.api = api
        self.fleet_handle = fleet_handle

    def update(self, state):
        activity_identifier = None
        execution = self.execution
        if execution:
            if self.api.is_command_completed(self.name):
                execution.finished()
                self.execution = None
            else:
                activity_identifier = execution.identifier
        self.update_handle.update(state, activity_identifier)

    def make_callbacks(self):
        callbacks = rmf_easy.RobotCallbacks(
            lambda destination, execution: self.navigate(
                destination, execution),
            lambda activity: self.stop(activity),
            lambda category, description, execution: self.execute_action(
                category, description, execution))
        callbacks.localize = lambda estimate, execution: self.localize(
            estimate, execution)
        return callbacks

    def _replan(self):
        if self.update_handle is not None and \
                self.update_handle.more() is not None:
            self.update_handle.more().replan()

    def localize(self, estimate, execution):
        if self.api.localize(self.name, estimate.position, estimate.map):
            execution.finished()
        else:
            self.node.get_logger().warn(
                f'Failed to localize [{self.name}] on {estimate.map}; '
                'requesting replan')
            self._replan()

    def navigate(self, destination, execution):
        self.execution = execution
        dock_name = destination.dock
        if dock_name:
            # The nav graph lane into this waypoint has a dock_name, so RMF
            # expects the robot's own docking manoeuvre, not a plain move.
            self.node.get_logger().info(
                f'Commanding [{self.name}] to dock at [{dock_name}]')
            ok = self.api.dock(self.name, dock_name)
        else:
            self.node.get_logger().info(
                f'Commanding [{self.name}] to navigate to '
                f'{destination.position} on map [{destination.map}]')
            ok = self.api.navigate(
                self.name, destination.position, destination.map,
                destination.speed_limit)
        if not ok:
            self.node.get_logger().error(
                f'Vendor API rejected command for [{self.name}]')
            self.execution = None
            self._replan()

    def stop(self, activity):
        execution = self.execution
        if execution is not None and execution.identifier.is_same(activity):
            self.execution = None
            self.api.stop(self.name)

    def execute_action(self, category: str, description: dict, execution):
        """Run a custom action listed under ``rmf_fleet.actions``."""
        self.execution = execution
        label = str((description or {}).get('label', ''))
        if not self.api.start_activity(self.name, category, label):
            self.node.get_logger().error(
                f'[{self.name}] could not start action [{category}]')
            # Mark it finished so the task does not hang forever; the
            # vendor driver should raise issues for operators if needed.
            execution.finished()
            self.execution = None


def update_robot(robot: RobotAdapter):
    try:
        data = robot.api.get_data(robot.name)
    except Exception as e:  # never let one driver kill the update loop
        robot.node.get_logger().error(f'[{robot.name}] get_data failed: {e}')
        return
    if data is None:
        return

    state = rmf_easy.RobotState(
        data.map_name, data.position, data.battery_soc)

    if robot.update_handle is None:
        robot.update_handle = robot.fleet_handle.add_robot(
            robot.name, state, robot.configuration, robot.make_callbacks())
        return

    robot.update(state)


if __name__ == '__main__':
    main(sys.argv)
