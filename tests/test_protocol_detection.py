"""Protocol detection from the BLE name.

Guards the name-prefix routing in particular: Sperax-branded pads that
actually speak FTMS (e.g. the RM-01) must NOT be routed to the Sperax
vendor protocol just because their name starts with "SPERAX_". Only the
P3 Max (SPERAX_P3MAX) uses the WLT6200 vendor protocol.
"""

from types import SimpleNamespace

import pytest

from walkingpad_controller import ProtocolType, WalkingPadController


def _controller(name: str) -> WalkingPadController:
    """Build a controller with a stub BLE device carrying the given name."""
    ble_device = SimpleNamespace(name=name, address="AA:BB:CC:DD:EE:FF")
    return WalkingPadController(ble_device=ble_device)


@pytest.mark.parametrize(
    "ble_name, expected",
    [
        # FTMS families, matched by name prefix.
        ("KS-HD-Z1D", ProtocolType.FTMS),
        ("KS-MC21-D06BFD", ProtocolType.FTMS),
        ("KS-SMC21C-XXXX", ProtocolType.FTMS),
        ("ZP-ZEALR1-XXXX", ProtocolType.FTMS),
        # The P3 Max is the only Sperax vendor-protocol device.
        ("SPERAX_P3MAX", ProtocolType.SPERAX),
        # Sperax-branded but FTMS: must fall through to service probing,
        # so the name alone leaves the protocol UNKNOWN (resolved on connect).
        ("SPERAX_RM-01_AB12CD", ProtocolType.UNKNOWN),
        # WiLink / other names give no definitive answer from the name.
        ("KS-ST-A1P", ProtocolType.UNKNOWN),
    ],
)
def test_protocol_detected_from_name(ble_name: str, expected: ProtocolType) -> None:
    assert _controller(ble_name).protocol == expected
