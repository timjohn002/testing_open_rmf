"""Registry of vendor drivers, selected by ``fleet_manager.driver``."""

from importlib import import_module

from ..robot_api import RobotAPI

# driver name in config -> "module:ClassName" inside this package
DRIVERS = {
    'generic_rest': 'generic_rest:GenericRestRobotAPI',
    'mir': 'mir:MiRRobotAPI',
}


def load_driver(fleet_manager_config: dict) -> RobotAPI:
    """Instantiate the driver named in ``fleet_manager_config['driver']``.

    A fully qualified ``some.module:ClassName`` is also accepted, so drivers
    can live outside this package.
    """
    name = fleet_manager_config.get('driver', 'generic_rest')
    target = DRIVERS.get(name, name)
    if ':' not in target:
        raise ValueError(
            f'Unknown driver [{name}]. Known drivers: {sorted(DRIVERS)}'
        )
    module_name, class_name = target.split(':', 1)
    if module_name in {v.split(':')[0] for v in DRIVERS.values()}:
        module = import_module(f'.{module_name}', __name__)
    else:
        module = import_module(module_name)
    cls = getattr(module, class_name)
    if not issubclass(cls, RobotAPI):
        raise TypeError(f'{target} is not a RobotAPI subclass')
    return cls(fleet_manager_config)
