# Copyright 2026 Open Source Robotics Foundation, Inc.
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
EasyTrafficLight fleet adapter for self-navigating robots.

Adapted from open-rmf/fleet_adapter_template (traffic_light_adapter_template).
Run one instance per Traffic Light fleet. The vendor is chosen by
``fleet_manager.driver`` (see ``traffic_light/drivers``).

Unlike the Full Control adapter, no nav graph is needed: the robot reports
its own route and RMF only decides when it must pause.
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
import rmf_adapter.geometry as geometry
import rmf_adapter.vehicletraits as traits
import yaml

from .api_server_reporter import ApiServerReporter
from .command_handle import TrafficLightCommandHandle
from .drivers import load_driver


def compute_transforms(level, coords, node=None):
    """Compute the robot->RMF nudged transform for one map level."""
    import nudged  # only needed when reference_coordinates are configured
    rmf_coords = coords['rmf']
    robot_coords = coords['robot']
    tf = nudged.estimate(robot_coords, rmf_coords)
    if node:
        mse = nudged.estimate_error(tf, robot_coords, rmf_coords)
        node.get_logger().info(
            f'Transformation error estimate for {level}: {mse}')
    return tf


def main(argv=sys.argv):
    rclpy.init(args=argv)
    rmf_adapter.init_rclcpp()
    args_without_ros = rclpy.utilities.remove_ros_args(argv)

    parser = argparse.ArgumentParser(
        prog='traffic_light_adapter',
        description='Configure and spin up a Traffic Light fleet adapter')
    parser.add_argument('-c', '--config_file', type=str, required=True,
                        help='Path to the fleet config.yaml file')
    parser.add_argument('-s', '--server_uri', type=str, default='',
                        help='rmf-web api-server websocket, e.g. '
                             'ws://localhost:8000/_internal')
    parser.add_argument('-sim', '--use_sim_time', action='store_true',
                        help='Use sim time, default: false')
    args = parser.parse_args(args_without_ros[1:])

    with open(args.config_file, 'r') as f:
        config_yaml = yaml.safe_load(f)

    fleet_config = config_yaml['rmf_fleet']
    fleet_name = fleet_config['name']
    robots_config = fleet_config['robots']
    fleet_manager_config = config_yaml['fleet_manager']

    node = rclpy.node.Node(f'{fleet_name}_command_handle')
    adapter = Adapter.make(f'{fleet_name}_fleet_adapter')
    assert adapter, (
        'Unable to initialize fleet adapter. '
        'Please ensure the RMF schedule node is running')

    if args.use_sim_time:
        node.set_parameters(
            [Parameter('use_sim_time', Parameter.Type.BOOL, True)])
        adapter.node.use_sim_time()

    linear = fleet_config['limits']['linear']
    angular = fleet_config['limits']['angular']
    profile = fleet_config['profile']
    vehicle_traits = traits.VehicleTraits(
        linear=traits.Limits(linear[0], linear[1]),
        angular=traits.Limits(angular[0], angular[1]),
        profile=traits.Profile(
            footprint=geometry.Circle(
                profile['footprint']).finalize_convex(),
            vicinity=geometry.Circle(profile['vicinity']).finalize_convex(),
        ),
    )

    conversions = config_yaml.get('conversions') or {}
    transforms = {
        level: compute_transforms(level, coords, node)
        for level, coords in
        (conversions.get('reference_coordinates') or {}).items()
    }

    robot_api = load_driver(fleet_manager_config)
    node.get_logger().info(
        f'[{fleet_name}] using traffic light driver '
        f"[{fleet_manager_config.get('driver', 'generic_rest')}]")
    if not robot_api.check_connection():
        node.get_logger().warn(
            f'[{fleet_name}] vendor API not reachable yet; will keep polling')

    reporter = ApiServerReporter(
        fleet_name, args.server_uri or None,
        period=1.0 / float(fleet_config.get('publish_fleet_state', 1.0)),
        logger=node.get_logger())

    handles = []
    for robot_name in robots_config:
        handle = TrafficLightCommandHandle(
            fleet_name=fleet_name,
            robot_name=robot_name,
            robot_api=robot_api,
            node=node,
            transforms=transforms,
            reporter=reporter,
        )
        adapter.add_easy_traffic_light(
            handle.traffic_light_cb,
            fleet_name,
            robot_name,
            vehicle_traits,
            handle.pause_cb,
            handle.resume_cb,
        )
        handles.append(handle)
        node.get_logger().info(
            f'Registered traffic light handle for [{robot_name}]')

    adapter.start()
    time.sleep(1.0)
    reporter.start()

    update_period = 1.0 / max(
        fleet_config.get('robot_state_update_frequency', 2.0), 0.5)

    def update_loop():
        with ThreadPoolExecutor(max_workers=max(1, len(handles))) as pool:
            while rclpy.ok():
                now = node.get_clock().now()
                list(pool.map(update_robot, handles))
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


def update_robot(handle: TrafficLightCommandHandle):
    try:
        data = handle.fetch_state()
        if data is None:
            return
        handle.update_state(data)
    except Exception as err:  # never let one robot kill the update loop
        handle.logger.error(
            f'[{handle.robot_name}] update loop error: {err}')


if __name__ == '__main__':
    main(sys.argv)
