"""Frames of the KingSmith FTMS supplement channel (24e2521c-...).

Layout: ``[cmd] [flags] [len] [payload] [checksum]``; the checksum is the
sum of all preceding bytes. See docs/ftms-protocol-reference.md §5.
"""

from __future__ import annotations

CMD_UNLOCK = 0x71
CMD_PROPERTIES = 0x72

FLAG_GET = 0x00
FLAG_SET = 0x01
FLAG_GET_REPLY = 0x80
FLAG_SET_REPLY = 0x81

PROPERTY_OTHER_SWITCH = 0x08
# Bit of `otherSwitch` that enables the buzzer.
OTHER_SWITCH_SOUND = 0x02

# KS Fit sends one extra 00 that the length byte doesn't count.
GET_PROPERTIES = bytes((CMD_PROPERTIES, FLAG_GET, 0x00, 0x00, 0x72))


def frame(cmd: int, flags: int, payload: bytes) -> bytes:
    """Build a frame including its checksum."""
    body = bytes((cmd, flags, len(payload))) + payload
    return body + bytes((sum(body) & 0xFF,))


def unlock_frame(ble_name: str, nonce: int) -> bytes:
    """Build the unlock frame for the device advertising ``ble_name``.

    The token is the nonce byte followed by the last four characters of the
    BLE name, read as a little-endian integer, plus the nonce.
    """
    key = int.from_bytes(ble_name[-4:].encode("ascii"), "little")
    token = ((key + nonce) & 0xFFFFFFFF).to_bytes(4, "little")
    return frame(CMD_UNLOCK, FLAG_GET, bytes((nonce,)) + token)


def set_property_frame(prop: int, value: int) -> bytes:
    """Build the frame that sets one property."""
    return frame(
        CMD_PROPERTIES, FLAG_SET, bytes((prop,)) + value.to_bytes(2, "little")
    )


def parse_properties(data: bytes) -> dict[int, int]:
    """Return the properties of a get-properties reply.

    Raises ValueError for a frame with a bad length or checksum.
    """
    if len(data) < 4 or len(data) != data[2] + 4 or data[2] % 4:
        raise ValueError(f"bad property reply: {data.hex()}")
    if sum(data[:-1]) & 0xFF != data[-1]:
        raise ValueError(f"bad property checksum: {data.hex()}")
    payload = data[3:-1]
    return {
        int.from_bytes(payload[i : i + 2], "little"): int.from_bytes(
            payload[i + 2 : i + 4], "little"
        )
        for i in range(0, len(payload), 4)
    }
