"""Data models for WalkingPad treadmill status and capabilities."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TreadmillStatus:
    """Unified treadmill status from the FTMS, WiLink or Sperax protocol.

    All fields use consistent units regardless of protocol source.
    """

    belt_state: int = 0
    """Belt state (`BeltState`): 0=stopped, 1=active, 2=paused, 5=standby,
    9=starting, 1000=unknown."""

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
    """Total energy in kcal (FTMS only, 0 for WiLink and Sperax)."""

    calories_per_hour: int = 0
    """Calories per hour (FTMS only, 0 for WiLink and Sperax)."""

    heart_rate: int = 0
    """Heart rate in bpm (if available, 0 otherwise)."""

    training_status: int = 0
    """FTMS Training Status (0x2AD3) status code. Standard FTMS enum:
    0=other, 1=idle, 2=warming up, 3=low-intensity interval,
    4=high-intensity interval, 5=recovery, 6=isometric, 7=heart-rate control,
    8=fitness test, 9=speed out of control, 10=cool down, 11=watt control,
    12=manual mode, 13=pre-workout, 14=post-workout. Stays 0 unless
    `FTMSController.subscribe_training_status()` was called and the device
    exposes 0x2AD3."""

    last_fm_event: int = 0
    """Opcode of the most recently processed Fitness Machine Status (0x2ADA)
    event (a dropped replay doesn't count). See `FitnessMachineStatusOpcode`
    for known values; 0 means none has been processed yet. Kept across
    reconnects of the same controller."""

    vibration_level: int = 0
    """Vibration intensity level (0 = off, 1-4). Sperax / WLT6200 devices
    only; stays 0 for FTMS and WiLink."""

    incline: int = 0
    """Incline level (0 = flat .. 10 = max). Sperax / WLT6200 devices only;
    stays 0 for FTMS and WiLink."""

    timestamp: float = field(default_factory=time.time)
    """Wall-clock time of the last update (creation time for a default)."""


@dataclass
class SpeedRange:
    """Speed capabilities (read from the device where supported)."""

    min_speed: float = 0.5
    """Minimum speed in km/h."""

    max_speed: float = 6.0
    """Maximum speed in km/h."""

    increment: float = 0.1
    """Speed increment in km/h."""


@dataclass
class DeviceCapabilities:
    """Device capabilities (discovered during connection where supported)."""

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

    pause_before_stop: bool = False
    """Whether STOP must be preceded by PAUSE and a confirmed standstill.

    Set from the BLE name (``FTMS_PAUSE_BEFORE_STOP_PREFIXES``) for firmware
    that rejects STOP while the belt is moving.
    """
