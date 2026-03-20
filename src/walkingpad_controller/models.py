"""Data models for WalkingPad treadmill status and capabilities."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TreadmillStatus:
    """Unified treadmill status from either FTMS or WiLink protocol.

    All fields use consistent units regardless of protocol source.
    """

    belt_state: int = 0
    """Belt state: 0=stopped, 1=active, 5=standby, 9=starting."""

    speed: float = 0.0
    """Instantaneous speed in km/h."""

    mode: int = 1
    """Operating mode: 0=auto, 1=manual, 2=standby."""

    distance: int = 0
    """Total distance in meters."""

    duration: int = 0
    """Elapsed time in seconds."""

    steps: int = 0
    """Step count."""

    calories: int = 0
    """Total energy in kcal (FTMS only, 0 for WiLink)."""

    calories_per_hour: int = 0
    """Calories per hour (FTMS only, 0 for WiLink)."""

    heart_rate: int = 0
    """Heart rate in bpm (if available, 0 otherwise)."""

    timestamp: float = field(default_factory=time.time)
    """Wall-clock time when this status was received."""


@dataclass
class SpeedRange:
    """Speed capabilities read from the device."""

    min_speed: float = 0.5
    """Minimum speed in km/h."""

    max_speed: float = 6.0
    """Maximum speed in km/h."""

    increment: float = 0.1
    """Speed increment in km/h."""


@dataclass
class DeviceCapabilities:
    """Device capabilities discovered during connection."""

    speed_range: SpeedRange = field(default_factory=SpeedRange)
    """Speed range and increment."""

    machine_features: int = 0
    """FTMS machine feature flags (raw uint32)."""

    target_features: int = 0
    """FTMS target setting feature flags (raw uint32)."""

    has_supplement: bool = False
    """Whether the KingSmith supplement service is available."""
