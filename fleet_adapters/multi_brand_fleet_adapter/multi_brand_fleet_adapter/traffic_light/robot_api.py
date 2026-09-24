"""
Brand-neutral interface for *Traffic Light* robots.

A Traffic Light robot plans and drives its own routes (the vendor's fleet
manager decides where it goes). RMF cannot send it destinations; it can only
see where it is heading and tell it to pause or resume so it does not collide
with other fleets. See the "Fleet Adapters" section of the RMF book.

Drivers report positions in the robot's own frame; the adapter converts them
using ``conversions.reference_coordinates`` in the fleet config.

No ROS imports here, so drivers can be unit tested anywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrafficLightUpdateData:
    """A snapshot of one self-navigating robot.

    ``current_path`` is the route the robot's own fleet manager is executing,
    as a list of waypoint dicts in the robot frame::

        {"map_name": "L1", "x": 1.0, "y": 2.0, "yaw": 0.0,   # yaw optional
         "yield": True,           # optional: RMF may make it wait here
         "mandatory_delay": 0.0}  # optional: expected dwell (s)

    It is ``[]`` when the robot is idle. ``last_completed_checkpoint`` is the
    index of the last waypoint in ``current_path`` the robot has reached
    (-1 if none yet).
    """

    robot_name: str
    map_name: str
    position: list[float]  # [x, y, yaw], robot frame
    current_path: list[dict] = field(default_factory=list)
    last_completed_checkpoint: int = -1
    is_moving: bool = False
    battery_soc: float = 1.0
    error: Optional[str] = None  # set when the robot is not following its path

    @classmethod
    def from_dict(cls, data: dict, robot_name: str) -> 'TrafficLightUpdateData':
        return cls(
            robot_name=data.get('robot_name') or robot_name,
            map_name=data['map_name'],
            position=[float(v) for v in data['position']],
            current_path=list(data.get('current_path') or []),
            last_completed_checkpoint=int(
                data.get('last_completed_checkpoint', -1)),
            is_moving=bool(data.get('is_moving', False)),
            battery_soc=float(data.get('battery_soc', 1.0)),
            error=data.get('error') or None,
        )


class TrafficLightRobotAPI(ABC):
    """Base class for Traffic Light vendor drivers."""

    def __init__(self, config: dict):
        self.config = config
        self.timeout = float(config.get('timeout', 1.0))

    def check_connection(self) -> bool:
        return True

    @abstractmethod
    def get_data(self, robot_name: str) -> Optional[TrafficLightUpdateData]:
        """Return the robot's state, or None on a transient failure."""

    @abstractmethod
    def pause(self, robot_name: str) -> bool:
        """Stop the robot now (it keeps its route)."""

    @abstractmethod
    def resume(self, robot_name: str) -> bool:
        """Continue the route after a pause."""

    @abstractmethod
    def pause_at_checkpoint(self, robot_name: str, checkpoint: int) -> bool:
        """Keep driving, but stop at waypoint ``checkpoint`` of the route."""
