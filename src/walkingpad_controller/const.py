"""Constants, enums, and BLE UUIDs for WalkingPad treadmill control."""

from enum import Enum, IntEnum, unique


# --- BLE UUIDs ---

# Standard FTMS Service
FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"

# FTMS Characteristics
FTMS_FEATURE_UUID = "00002acc-0000-1000-8000-00805f9b34fb"
SOFTWARE_REVISION_UUID = "00002a28-0000-1000-8000-00805f9b34fb"
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

# KingSmith MC-21 vendor pre-amble. KS Fit writes a fixed 8-byte payload to
# this characteristic (located inside the FTMS service) before every Control
# Point command. Without it, MC-21 firmware rejects SET_TARGET_SPEED with
# CONTROL_NOT_PERMITTED. See issue #1.
KINGSMITH_VENDOR_PREAMBLE_UUID = "d18d2c10-c44c-11e8-a355-529269fb1459"
KINGSMITH_VENDOR_PREAMBLE_PAYLOAD = bytes.fromhex("01000d00060b0f0d")

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


@unique
class FitnessMachineStatusOpcode(IntEnum):
    """Fitness Machine Status (0x2ADA) event opcodes.

    These notifications are how the device reports state changes — both in
    response to client commands and to physical interactions like the remote
    control or safety key.
    """

    RESET = 0x01
    STOPPED_OR_PAUSED = 0x02              # param: 0x01=stop, 0x02=pause
    STOPPED_BY_SAFETY_KEY = 0x03
    STARTED_OR_RESUMED = 0x04
    TARGET_SPEED_CHANGED = 0x05           # param: UINT16 LE in 0.01 km/h
    TARGET_INCLINATION_CHANGED = 0x06     # param: INT16 LE in 0.1%
    TARGET_RESISTANCE_CHANGED = 0x07
    TARGET_POWER_CHANGED = 0x08
    TARGET_HEART_RATE_CHANGED = 0x09
    TARGET_EXPENDED_ENERGY_CHANGED = 0x0A
    TARGET_TIME_CHANGED = 0x0B
    SPIN_DOWN_STATUS = 0x14
    TARGET_CADENCE_CHANGED = 0x15
    CONTROL_PERMISSION_LOST = 0xFF


@unique
class KingSmithMode(IntEnum):
    """KingSmith operating mode enum (from KS Fit's `Mode` enum).

    Value semantics decoded from the v5.9.10 KS Fit AOT snapshot. Not all
    devices support every mode — e.g. `programCourse` / `programCustom`
    require the supplement service (KS-HD-*).
    """

    AUTO = 0x0
    MANUAL = 0x1
    STANDBY = 0x2
    NEWER = 0x3
    CORRECT = 0x4
    CHILD_LOCK = 0x5
    NORMAL = 0x6
    WALK = 0x7
    HIIT = 0x8
    BURN = 0x9
    OTHER = 0xA
    MASSAGE = 0xB
    PROGRAM_COURSE = 0xC
    PROGRAM_CUSTOM = 0xD


@unique
class KingSmithStatus(IntEnum):
    """KingSmith device status enum (from KS Fit's `Status` enum)."""

    READY = 0x0
    RUN = 0x1
    PAUSE = 0x2
    STOP = 0x3
    FAULT = 0x4
    SLEEP = 0x5
    COUNTDOWN_0 = 0x6
    COUNTDOWN_1 = 0x7
    COUNTDOWN_2 = 0x8
    COUNTDOWN_3 = 0x9
    OTHER = 0xA
    LOCK_OFF = 0xB
    PRE_START = 0xC
    PRE_END = 0xD


@unique
class KingSmithTreadmillStatus(IntEnum):
    """Treadmill-specific status (from KS Fit's `TreadmillStatus` enum)."""

    UNKNOWN = 0x0
    IDLE = 0x1
    RUNNING = 0x2
    PRE_START = 0x3
    PRE_END = 0x4
    SAFETY_OFF = 0x5


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
#
# - KS-HD-*       : modern KingSmith treadmills (e.g. KS-HD-Z1D), expose the
#                   FTMS supplement service (24e2521c-...) for vendor commands.
# - KS-MC21-*     : MC-21 series (e.g. KS-MC21-D06BFD), expose the ODM vendor
#                   characteristic (d18d2c10-...) for the per-command pre-amble.
# - KS-SMC21C-*   : "C" variant of the MC-21 family.
# - ZP-ZEALR1-*   : Zeal-branded OEM variant of the MC-21 (KS Fit's isMC21
#                   getter matches all three).
FTMS_NAME_PREFIXES = ("KS-HD-", "KS-MC21-", "KS-SMC21C-", "ZP-ZEALR1-")

# Default connection parameters.
# KingSmith FTMS firmware can be left in a bad state for several seconds
# after a previous abrupt disconnect — Bleak/BlueZ then accepts the next
# connect() call but the device closes the link before service discovery
# completes. A handful of retries with a few seconds between them rides
# this out reliably.
MAX_CONNECT_RETRIES = 5
RETRY_DELAY_SECONDS = 3.0
