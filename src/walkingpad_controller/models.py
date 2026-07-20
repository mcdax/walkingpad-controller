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

    training_status: int = 0
    """FTMS Training Status (0x2AD3) status code. Standard FTMS enum:
    0=other, 1=idle, 2=warming up, 3=low-intensity interval,
    4=high-intensity interval, 5=recovery, 6=isometric, 7=heart-rate control,
    8=fitness test, 9=speed out of control, 10=cool down, 11=watt control,
    12=manual mode, 13=pre-workout, 14=post-workout. Stays 0 if the device
    doesn't expose 0x2AD3."""

    last_fm_event: int = 0
    """Opcode of the most recent Fitness Machine Status (0x2ADA) event.
    See `FitnessMachineStatusOpcode` for known values; 0 means no event
    has been received yet this session."""

    vibration_level: int = 0
    """Vibration intensity level (0 = off, 1-4). Sperax / WLT6200 devices
    only; stays 0 for FTMS and WiLink."""

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

    has_vendor_preamble: bool = False
    """Whether the KingSmith MC-21 vendor pre-amble characteristic is present."""

    firmware_version: str = ""
    """Firmware version reported by the Software Revision String (0x2A28)."""
