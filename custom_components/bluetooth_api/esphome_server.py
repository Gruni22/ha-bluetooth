"""ESPHome native-API bridge: HA ↔ ESPHome ble_server device over WiFi.

Architecture (ESPHome mode):
  Android ←(BLE)→ ESPHome device (ble_server component) ←(native API/TCP)→ EsphomeApiServer ←→ HA APIs

Why a self-contained TCP client (instead of aioesphomeapi)?
  aioesphomeapi ships Cython-compiled connection internals (.pyd) on most
  platforms. Its message dispatch table is baked into the compiled binary and
  cannot be patched from Python to recognise our three new ble_server messages
  (IDs 149/150/151). A small plaintext client avoids that whole problem and
  keeps the integration self-sufficient.

  Constraint: this transport only supports an ESPHome device whose `api:`
  block has *no* `encryption_key` (plaintext mode). For noise encryption,
  rework this module to use aioesphomeapi.APIClient and resolve the dispatch
  patching problem via a fork or a different injection point.

Plaintext frame format (per ESPHome native API spec):
    [0x00 preamble] [varint msg_size] [varint msg_type] [msg_size bytes msg_data]
"""

from __future__ import annotations

import asyncio
import logging

from aioesphomeapi import api_pb2  # protobuf classes for stock messages

from homeassistant.core import HomeAssistant

from . import ble_server_pb2
from .const import (
    BLE_SERVER_FRAME_RESPONSE_ID,
    BLE_SERVER_SEND_FRAME_REQUEST_ID,
    BLE_SERVER_SUBSCRIBE_REQUEST_ID,
)
from .dispatcher import PacketDispatcher

_LOGGER = logging.getLogger(__name__)

# Stock ESPHome native API message IDs we care about.
_MSG_HELLO_REQUEST = 1
_MSG_HELLO_RESPONSE = 2
_MSG_DISCONNECT_REQUEST = 5
_MSG_DISCONNECT_RESPONSE = 6
_MSG_PING_REQUEST = 7
_MSG_PING_RESPONSE = 8

_RECONNECT_DELAY = 5
_PING_INTERVAL = 20  # seconds — matches aioesphomeapi default
_HELLO_TIMEOUT = 10  # seconds
_API_VERSION_MAJOR = 1
_API_VERSION_MINOR = 10


# ── Varint helpers ───────────────────────────────────────────────────────────


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as protobuf varint."""
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    """Read one protobuf varint from *reader*."""
    value = 0
    shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value
        shift += 7
        if shift > 63:
            raise ValueError("Varint too large")


# ── EsphomeApiServer ─────────────────────────────────────────────────────────


class EsphomeApiServer(PacketDispatcher):
    """Bridge between an ESPHome ble_server device and HA's packet protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        passcode: int = 0,
    ) -> None:
        super().__init__(hass, passcode)
        self._host = host
        self._port = port
        self._running = False
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._connected = False  # API connection (not BLE client) state

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        _LOGGER.info("ESPHome API bridge starting → %s:%d", self._host, self._port)

    async def stop(self) -> None:
        self._running = False
        self._on_ble_client_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None

    # ── PacketDispatcher transport hook ──────────────────────────────────────

    async def _send_raw(self, data: bytes) -> None:
        """Send a complete passcode packet to the ESPHome device.

        Wraps the raw packet bytes in a BleServerSendFrameRequest protobuf and
        pushes it over the API connection.
        """
        msg = ble_server_pb2.BleServerSendFrameRequest(data=data)
        await self._send_message(BLE_SERVER_SEND_FRAME_REQUEST_ID, msg.SerializeToString())

    # ── Connection lifecycle ─────────────────────────────────────────────────

    async def _run(self) -> None:
        while self._running:
            try:
                await self._connect_and_pump()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("ESPHome API error: %s — retrying in %ds", exc, _RECONNECT_DELAY)
            self._connected = False
            self._on_ble_client_disconnected()
            self._writer = None
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_pump(self) -> None:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        self._writer = writer
        try:
            await self._handshake(reader, writer)
            self._connected = True
            _LOGGER.info("ESPHome API: connected to %s:%d", self._host, self._port)
            await self._send_message(
                BLE_SERVER_SUBSCRIBE_REQUEST_ID,
                ble_server_pb2.SubscribeBleServerFramesRequest().SerializeToString(),
            )
            # Treat API connection as the implicit "BLE client may be present"
            # signal. With no per-client connect/disconnect events on the API,
            # this means HA pushes state_changed events to the ESP whenever the
            # API session is up. The ESP drops them when no GATT client is
            # actually subscribed, so the cost is just protobuf serialisation.
            self._on_ble_client_connected()

            ping_task = asyncio.ensure_future(self._ping_loop())
            try:
                await self._reader_loop(reader)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hello = api_pb2.HelloRequest(
            client_info="bluetooth_api (HA)",
            api_version_major=_API_VERSION_MAJOR,
            api_version_minor=_API_VERSION_MINOR,
        )
        await self._send_message(_MSG_HELLO_REQUEST, hello.SerializeToString(), writer=writer)
        msg_type, data = await asyncio.wait_for(self._read_message(reader), timeout=_HELLO_TIMEOUT)
        if msg_type != _MSG_HELLO_RESPONSE:
            raise RuntimeError(f"Expected HelloResponse, got msg_type={msg_type}")
        resp = api_pb2.HelloResponse()
        resp.ParseFromString(data)
        _LOGGER.debug(
            "ESPHome API: server '%s' version %d.%d (server_info=%s)",
            resp.name, resp.api_version_major, resp.api_version_minor, resp.server_info,
        )

    # ── Frame I/O ────────────────────────────────────────────────────────────

    async def _send_message(
        self,
        msg_type: int,
        payload: bytes,
        writer: asyncio.StreamWriter | None = None,
    ) -> None:
        w = writer if writer is not None else self._writer
        if w is None:
            return
        frame = b"\x00" + _encode_varint(len(payload)) + _encode_varint(msg_type) + payload
        async with self._write_lock:
            try:
                w.write(frame)
                await w.drain()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("ESPHome API: write failed: %s", exc)
                raise

    async def _read_message(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        preamble = (await reader.readexactly(1))[0]
        if preamble != 0x00:
            # Either a noise-encrypted server (preamble 0x01) or a malformed stream.
            raise RuntimeError(
                f"Unexpected preamble 0x{preamble:02X}: device may be using noise encryption "
                f"(set api: without encryption_key for this transport)"
            )
        msg_size = await _read_varint(reader)
        msg_type = await _read_varint(reader)
        data = await reader.readexactly(msg_size) if msg_size else b""
        return msg_type, data

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            msg_type, data = await self._read_message(reader)

            if msg_type == BLE_SERVER_FRAME_RESPONSE_ID:
                resp = ble_server_pb2.BleServerFrameResponse()
                resp.ParseFromString(data)
                # The wrapped bytes are an already-reassembled passcode packet
                # (the ESP did the BLE chunk stitching). Hand it to the dispatcher.
                await self._handle_incoming_packet(resp.data)
            elif msg_type == _MSG_PING_REQUEST:
                # Server-initiated ping — answer with PingResponse.
                await self._send_message(_MSG_PING_RESPONSE, api_pb2.PingResponse().SerializeToString())
            elif msg_type == _MSG_PING_RESPONSE:
                pass  # response to our keep-alive
            elif msg_type == _MSG_DISCONNECT_REQUEST:
                await self._send_message(
                    _MSG_DISCONNECT_RESPONSE, api_pb2.DisconnectResponse().SerializeToString()
                )
                _LOGGER.info("ESPHome API: server requested disconnect")
                return
            else:
                # Anything else (state events, list_entities, etc.) — ignore.
                # We never subscribed to states, so this should be rare.
                _LOGGER.debug("ESPHome API: ignoring msg_type=%d (%d bytes)", msg_type, len(data))

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                if self._writer is None:
                    return
                await self._send_message(_MSG_PING_REQUEST, api_pb2.PingRequest().SerializeToString())
        except asyncio.CancelledError:
            pass
