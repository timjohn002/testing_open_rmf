"""Registry of Traffic Light drivers, selected by ``fleet_manager.driver``."""

from importlib import import_module

from ..robot_api import TrafficLightRobotAPI

DRIVERS = {
    'generic_rest': 'generic_rest:GenericRestTrafficLightAPI',
}


def load_driver(fleet_manager_config: dict) -> TrafficLightRobotAPI:
    """Instantiate the driver named in ``fleet_manager_config['driver']``.

    A fully qualified ``some.module:ClassName`` is also accepted.
    """
    name = fleet_manager_config.get('driver', 'generic_rest')
    target = DRIVERS.get(name, name)
    if ':' not in target:
        raise ValueError(
            f'Unknown traffic light driver [{name}]. '
            f'Known drivers: {sorted(DRIVERS)}')
    module_name, class_name = target.split(':', 1)
    if name in DRIVERS:
        module = import_module(f'.{module_name}', __name__)
    else:
        module = import_module(module_name)
    cls = getattr(module, class_name)
    if not issubclass(cls, TrafficLightRobotAPI):
        raise TypeError(f'{target} is not a TrafficLightRobotAPI subclass')
    return cls(fleet_manager_config)
