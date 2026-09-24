"""
Brand-neutral interface between the RMF fleet adapter and a robot vendor.

Every robot brand gets one ``RobotAPI`` subclass (a "driver") in the
``drivers`` package. The fleet adapter only ever talks to this interface, so
adding a new brand means writing one driver and one config file -- the RMF
side stays the same.

All poses passed to and returned from a driver are in the *robot's own*
coordinate frame, as ``[x, y, yaw]`` (metres, metres, radians). The fleet
adapter converts to/from the RMF frame using ``reference_coordinates`` in the
fleet config.

This module deliberately has no ROS imports so drivers can be unit tested on
any machine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class RobotUpdateData:
    """A snapshot of one robot's state, reported by a driver."""

    robot_name: str
    map_name: str
    position: list[float]  # [x, y, yaw] in the robot frame
    battery_soc: float  # 0.0 - 1.0
    requires_replan: Optional[bool] = None


class RobotAPI(ABC):
    """Base class for vendor drivers.

    ``config`` is the ``fleet_manager`` block of the fleet config YAML. Each
    driver documents which keys it needs.
    """

    def __init__(self, config: dict):
        self.config = config
        self.timeout = float(config.get('timeout', 5.0))

    # --- Connection -------------------------------------------------------
    def check_connection(self) -> bool:
        """Return True if the vendor API is reachable."""
        return True

    # --- State (polled by the adapter's update loop) ----------------------
    @abstractmethod
    def get_data(self, robot_name: str) -> Optional[RobotUpdateData]:
        """Return the latest state of ``robot_name``, or None if unknown."""

    @abstractmethod
    def is_command_completed(self, robot_name: str) -> bool:
        """Return True once the last navigate/action command has finished."""

    # --- Commands (called by RMF) -----------------------------------------
    @abstractmethod
    def navigate(
        self,
        robot_name: str,
        pose: list[float],
        map_name: str,
        speed_limit: float = 0.0,
    ) -> bool:
        """Send the robot to ``pose`` ([x, y, yaw], robot frame)."""

    @abstractmethod
    def stop(self, robot_name: str) -> bool:
        """Stop whatever the robot is doing."""

    def start_activity(
        self, robot_name: str, activity: str, label: str
    ) -> bool:
        """Start a vendor-specific action (dock, clean, lift a cart, ...)."""
        return False

    def localize(
        self, robot_name: str, pose: list[float], map_name: str
    ) -> bool:
        """Tell the robot it is on ``map_name`` at ``pose``."""
        return False
