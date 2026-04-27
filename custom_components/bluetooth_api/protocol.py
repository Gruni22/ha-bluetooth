"""Shared framing and auth logic for the Bluetooth API transport."""

from __future__ import annotations

import asyncio
import datetime
import json
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ── USB-Serial framing ────────────────────────────────────────────────────────
# 4-byte big-endian length prefix followed by UTF-8 payload.

_HEADER = struct.Struct("!I")  # big-endian unsigned int (4 bytes)

# ── BLE chunking layer ────────────────────────────────────────────────────────
# 1-byte flag (0x00=more, 0x01=last) + up to 244 bytes of payload per chunk.
# This layer sits *below* the passcode packet layer.

BLE_CHUNK_CONTINUES: int = 0x00
BLE_CHUNK_FINAL: int = 0x01
BLE_MAX_PAYLOAD = 508  # 512 ATT MTU − 3 ATT header − 1 flag byte

# ── Passcode packet format ────────────────────────────────────────────────────
# Logical packet (transported over USB-Serial or BLE chunking):
#   [0xAA][0xBB]               HEADER       (2 bytes, magic)
#   [passcode: uint32 BE]      PASSCODE     (4 bytes)
#   [cmd: uint8]               COMMAND      (1 byte)
#   [flags: uint8]             FLAGS        (1 byte, currently 0x00)
#   [payload_len: uint16 BE]   PAYLOAD LEN  (2 bytes)
#   [payload: bytes]           PAYLOAD      (variable, UTF-8 JSON)
#   [crc16: uint16 BE]         CRC16-CCITT  (2 bytes, covers bytes 2..-4)
#   [0xCC][0xDD]               END HEADER   (2 bytes, magic)
# Total overhead: 14 bytes.

_PKT_INNER = struct.Struct("!IBBH")  # passcode, cmd, flags, payload_len


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT (init=0xFFFF, poly=0x1021, no bit inversion)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def encode_packet(passcode: int, cmd: int, payload: bytes = b"", flags: int = 0x00) -> bytes:
    """Encode a passcode-secured packet."""
    if len(payload) > 0xFFFF:
        raise ValueError(f"Payload too large for uint16 length field: {len(payload)} bytes")
    inner = _PKT_INNER.pack(passcode, cmd, flags, len(payload)) + payload
    crc = crc16_ccitt(inner)
    return b"\xaa\xbb" + inner + struct.pack("!H", crc) + b"\xcc\xdd"


def decode_packet(data: bytes) -> tuple[int, int, bytes] | None:
    """Decode and validate a passcode-secured packet.

    Returns (passcode, cmd, payload) or None if the packet is malformed or the
    CRC does not match.
    """
    if len(data) < 14:
        return None
    if data[:2] != b"\xaa\xbb" or data[-2:] != b"\xcc\xdd":
        return None
    inner = data[2:-4]
    crc_received = struct.unpack("!H", data[-4:-2])[0]
    if crc16_ccitt(inner) != crc_received:
        return None
    if len(inner) < _PKT_INNER.size:
        return None
    passcode, cmd, _flags, payload_len = _PKT_INNER.unpack_from(inner)
    expected_total = _PKT_INNER.size + payload_len
    if len(inner) < expected_total:
        return None
    payload = inner[_PKT_INNER.size : expected_total]
    return passcode, cmd, payload


def _json_default(obj: object) -> object:
    """Fallback serializer: convert datetime/date to ISO-8601 string."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def encode_packet_json(passcode: int, cmd: int, obj: object = None) -> bytes:
    """Encode a packet whose payload is JSON-serialised *obj*."""
    payload = json.dumps(obj, default=_json_default).encode() if obj is not None else b""
    return encode_packet(passcode, cmd, payload)


_USB_MAX_FRAME = 16 * 1024 * 1024  # 16 MB hard cap


_USB_MAX_RESYNC_BYTES = 4096  # safety cap on sliding-window resync


async def rfcomm_read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed frame from an RFCOMM/USB stream.

    Uses a sliding 4-byte window: if the parsed length is implausible
    (0 or > 16 MB), drop the oldest byte and read the next one. This makes
    the parser resilient to garbage bytes (e.g. ESP32 boot logs) without
    closing the bridge.

    Safety bound: if no valid frame header is found within
    ``_USB_MAX_RESYNC_BYTES`` extra bytes, raises ValueError so the bridge
    can reset rather than hang on a permanently garbage stream.
    """
    header = bytearray(await reader.readexactly(_HEADER.size))
    skipped = 0
    while True:
        (length,) = _HEADER.unpack(bytes(header))
        if 0 < length <= _USB_MAX_FRAME:
            return await reader.readexactly(length)
        skipped += 1
        if skipped > _USB_MAX_RESYNC_BYTES:
            raise ValueError(
                f"USB stream resync failed after {_USB_MAX_RESYNC_BYTES} bytes — bridge will restart"
            )
        next_byte = await reader.readexactly(1)
        header = header[1:] + bytearray(next_byte)


def frame_for_usb(data: bytes) -> bytes:
    """Return *data* wrapped in a 4-byte big-endian length prefix (no I/O)."""
    return _HEADER.pack(len(data)) + data


async def rfcomm_write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write a length-prefixed frame to an RFCOMM stream (used in tests)."""
    writer.write(frame_for_usb(data))
    await writer.drain()


def encode_ble_chunks(data: bytes) -> list[bytes]:
    """Split *data* into BLE chunks with the continuation flag byte prepended."""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        end = min(offset + BLE_MAX_PAYLOAD, len(data))
        flag = BLE_CHUNK_FINAL if end == len(data) else BLE_CHUNK_CONTINUES
        chunks.append(bytes([flag]) + data[offset:end])
        offset = end
    return chunks


def decode_ble_chunks(chunks: list[bytes]) -> bytes:
    """Reassemble BLE chunks into a complete frame payload."""
    return b"".join(chunk[1:] for chunk in chunks)


async def send_json(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Serialise *msg* and write it as an RFCOMM frame."""
    await rfcomm_write_frame(writer, json.dumps(msg).encode())


async def read_json(reader: asyncio.StreamReader) -> dict:
    """Read one RFCOMM frame and deserialise it as JSON."""
    raw = await rfcomm_read_frame(reader)
    return json.loads(raw.decode())
