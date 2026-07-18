"""Unit tests for the Sperax / WLT6200 protocol codec and status parser.

These are pure-logic tests — no BLE hardware required. The frames below are
real captures from a Sperax P3 Max HCI snoop log (see
docs/sperax-p3max-protocol.md).
"""

from __future__ import annotations

from walkingpad_controller.sperax import crc16, decode, encode

# --- Real captured command frames (app -> device) ---------------------------
# (hex on wire, logical inner body [0x00, CMD, ARGS...])
CAPTURED_COMMANDS = [
    ("f507000126d8fa", "0001"),               # hello
    ("f50a0015010200be98fa", "0015010200"),   # run speed 0.2, incline 0
    ("f50a0015011e006ad9fa", "0015011e00"),   # run speed 3.0, incline 0
    ("f50a0015011e019eb7fa", "0015011e01"),   # run speed 3.0, incline 1
    ("f50a0015011e028204fa", "0015011e02"),   # run speed 3.0, incline 2
    ("f50a0015020000c319fa", "0015020000"),   # stop
    ("f5090016010155eafa", "00160101"),       # vibration level 1
    ("f509001601043e79fa", "00160104"),       # vibration level 4
    ("f50900160000346dfa", "00160000"),       # vibration off
    ("f5080019f00a59fa", "0019"),             # status poll (note CRC byte 0xFA is stuffed)
    ("f50b00150106003bf004fa", "0015010600"), # run speed 0.6 (CRC contains 0xF4 -> stuffed)
]

# A representative status notification (device -> app), running at 3.0 km/h,
# incline 0, no vibration.
STATUS_RUN_3KMH = "f51800190000100000001000010001000f1e0000009dd4fa"


def test_decode_captured_commands():
    for onwire, expected_inner in CAPTURED_COMMANDS:
        inner = decode(bytes.fromhex(onwire))
        assert inner is not None, f"decode failed for {onwire}"
        assert inner.hex() == expected_inner, (
            f"{onwire}: got {inner.hex()}, want {expected_inner}"
        )


def test_encode_roundtrips_captured_commands():
    # encode(inner) must reproduce the exact on-wire bytes (incl. stuffing).
    for onwire, inner_hex in CAPTURED_COMMANDS:
        got = encode(bytes.fromhex(inner_hex))
        assert got.hex() == onwire, f"encode({inner_hex}) = {got.hex()}, want {onwire}"


def test_decode_rejects_bad_crc():
    b = bytearray.fromhex("f50a0015011e006ad9fa")
    b[5] ^= 0xFF  # corrupt the speed byte, CRC no longer matches
    assert decode(bytes(b)) is None


def test_decode_rejects_malformed():
    assert decode(b"") is None
    assert decode(bytes.fromhex("f50a00")) is None            # too short
    assert decode(bytes.fromhex("aa0a0015011e006ad9fa")) is None  # bad SOF
    assert decode(bytes.fromhex("f50a0015011e006ad9bb")) is None  # bad EOF


def test_crc_known_value():
    # CRC over [F5, LEN, 00, 15, 01, 1E, 00] for the run-3.0 frame = 0xD96A LE.
    assert crc16(bytes.fromhex("f50a0015011e00")) == 0xD96A


def test_encode_speed_formula():
    # speed byte = round(km/h * 10)
    def run_speed_byte(kmh):
        s = int(round(kmh * 10))
        return decode(encode(bytes([0x00, 0x15, 0x01, s, 0x00])))[3]

    assert run_speed_byte(3.0) == 0x1E
    assert run_speed_byte(1.0) == 0x0A
    assert run_speed_byte(0.5) == 0x05


def test_status_parse_running():
    from walkingpad_controller.const import BeltState
    from walkingpad_controller.sperax import SperaxController

    ctrl = SperaxController()
    inner = decode(bytes.fromhex(STATUS_RUN_3KMH))
    assert inner is not None
    ctrl._parse_status(inner)
    assert ctrl.status.speed == 3.0
    assert ctrl.status.belt_state == BeltState.ACTIVE
    assert ctrl.vibration_level == 0
