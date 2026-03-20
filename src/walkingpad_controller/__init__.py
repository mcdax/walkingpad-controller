"""walkingpad-controller — Python library for controlling WalkingPad treadmills over BLE.

Supports both FTMS (Fitness Machine Service) and legacy WiLink protocols.
Protocol is auto-detected based on the BLE device name and services.

Quick start:

    from bleak import BleakScanner
    from walkingpad_controller import WalkingPadController

    device = await BleakScanner.find_device_by_name("KS-HD-Z1D")
    controller = WalkingPadController(ble_device=device)
    await controller.connect()
    await controller.start()
    print(controller.status)
    await controller.stop()
    await controller.disconnect()
"""

from .const import (
    FTMS_NAME_PREFIXES,
    FTMS_SERVICE_UUID,
    WILINK_SERVICE_UUID,
    BeltState,
    FTMSOpcode,
    FTMSResultCode,
    OperatingMode,
    ProtocolType,
)
from .controller import WalkingPadController
from .ftms import FTMSController
from .models import DeviceCapabilities, SpeedRange, TreadmillStatus
from .wilink import WiLinkController

__version__ = "0.4.0"

__all__ = [
    # Main controller
    "WalkingPadController",
    # Protocol-specific controllers
    "FTMSController",
    "WiLinkController",
    # Data models
    "TreadmillStatus",
    "SpeedRange",
    "DeviceCapabilities",
    # Enums
    "BeltState",
    "OperatingMode",
    "ProtocolType",
    "FTMSOpcode",
    "FTMSResultCode",
    # Constants
    "FTMS_SERVICE_UUID",
    "WILINK_SERVICE_UUID",
    "FTMS_NAME_PREFIXES",
]
