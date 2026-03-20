"""Constants, enums, and BLE UUIDs for WalkingPad treadmill control."""

from enum import Enum, IntEnum, unique


# --- BLE UUIDs ---

# Standard FTMS Service
FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"

# FTMS Characteristics
FTMS_FEATURE_UUID = "00002acc-0000-1000-8000-00805f9b34fb"
TREADMILL_DATA_UUID = "00002acd-0000-1000-8000-00805f9b34fb"
TRAINING_STATUS_UUID = "00002ad3-0000-1000-8000-00805f9b34fb"
SUPPORTED_SPEED_RANGE_UUID = "00002ad4-0000-1000-8000-00805f9b34fb"
SUPPORTED_INCLINATION_RANGE_UUID = "00002ad5-0000-1000-8000-00805f9b34fb"
FITNESS_MACHINE_STATUS_UUID = "00002ada-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"

# Custom KingSmith Supplement Service
SUPPLEMENT_SERVICE_UUID = "24e2521c-f63b-48ed-85be-c5330a00fdf7"
SUPPLEMENT_NOTIFY_UUID = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
SUPPLEMENT_WRITE_UUID = "24e2521c-f63b-48ed-85be-c5330d00fdf7"

# Legacy WiLink Service (for older devices)
WILINK_SERVICE_UUID = "0000fe00-0000-1000-8000-00805f9b34fb"


# --- Enums ---


@unique
class ProtocolType(Enum):
    """Supported BLE communication protocols."""

    WILINK = "wilink"  # Legacy protocol (service 0xFE00, ph4-walkingpad)
    FTMS = "ftms"  # Standard FTMS (service 0x1826)
    UNKNOWN = "unknown"


@unique
class BeltState(IntEnum):
    """Belt states."""

    STOPPED = 0
    ACTIVE = 1
    STANDBY = 5
    STARTING = 9
    UNKNOWN = 1000


@unique
class OperatingMode(Enum):
    """Treadmill operating modes."""

    AUTO = 0  # Belt starts/stops on foot detection
    MANUAL = 1  # Speed set via commands
    STANDBY = 2  # Belt off


# --- FTMS Protocol Constants ---


@unique
class FTMSOpcode(IntEnum):
    """FTMS Control Point opcodes (Bluetooth SIG standard)."""

    REQUEST_CONTROL = 0x00
    RESET = 0x01
    SET_TARGET_SPEED = 0x02  # param: UINT16 in 0.01 km/h
    SET_TARGET_INCLINATION = 0x03  # param: INT16 in 0.1%
    START_OR_RESUME = 0x07
    STOP_OR_PAUSE = 0x08  # param: UINT8 (0x01=Stop, 0x02=Pause)
    RESPONSE_CODE = 0x80


@unique
class FTMSResultCode(IntEnum):
    """FTMS Control Point result codes."""

    SUCCESS = 0x01
    OPCODE_NOT_SUPPORTED = 0x02
    INVALID_PARAMETER = 0x03
    OPERATION_FAILED = 0x04
    CONTROL_NOT_PERMITTED = 0x05


@unique
class FTMSStopPauseParam(IntEnum):
    """Parameter for the Stop or Pause opcode."""

    STOP = 0x01
    PAUSE = 0x02


class TreadmillDataFlags:
    """Bit flags for the FTMS Treadmill Data characteristic (0x2ACD).

    Per the Bluetooth FTMS specification, the flags field is a 16-bit value.
    If a bit is SET, the corresponding optional field is PRESENT in the data.
    Instantaneous Speed is always present (mandatory field).

    Bit 13 (0x2000) is a KingSmith-specific extension that carries 3 extra
    bytes: a uint16 LE step count + 1 zero byte. The step counter is
    pressure-sensor based -- it only increments when someone is walking.
    """

    MORE_DATA = 0x0001
    AVERAGE_SPEED = 0x0002
    TOTAL_DISTANCE = 0x0004
    INCLINATION = 0x0008
    ELEVATION_GAIN = 0x0010
    INSTANTANEOUS_PACE = 0x0020
    AVERAGE_PACE = 0x0040
    EXPENDED_ENERGY = 0x0080
    HEART_RATE = 0x0100
    METABOLIC_EQUIVALENT = 0x0200
    ELAPSED_TIME = 0x0400
    REMAINING_TIME = 0x0800
    FORCE_ON_BELT = 0x1000
    KINGSMITH_EXTENSION = 0x2000


# BLE name prefixes known to use FTMS protocol.
# These devices have service 0x1826 but NOT 0xFE00.
FTMS_NAME_PREFIXES = ("KS-HD-",)

# Default connection parameters
MAX_CONNECT_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
