"""ESPHome native-API bridge: HA ↔ ESPHome ble_server device over WiFi.

Architecture (ESPHome mode):
  Android ←(BLE)→ ESPHome device (ble_server component) ←(native API/TCP)→ EsphomeApiServer ←→ HA APIs

Why a self-contained TCP client (instead of aioesphomeapi)?
  aioesphomeapi ships Cython-compiled connection internals (.pyd) on most
  platforms. Its message dispatch table is baked into the compiled binary and
  cannot be patched from Python to recognise our three new ble_server messages
  (IDs 149/150/151). A small native-API client avoids that whole problem and
  keeps the integration self-sufficient.

Transports:
  - Plaintext: device YAML has `api:` without `encryption:`.
      [0x00 preamble] [varint msg_size] [varint msg_type] [msg_size bytes msg_data]
  - Noise NNpsk0: device YAML has `api: encryption: key: <base64-32-byte-PSK>`.
      [0x01 preamble] [u16 BE size] [payload]
      Payload during handshake: [0x00 ok | 0x01 err] [noise bytes / error text]
      Payload after handshake (AEAD-encrypted plaintext):
          [u16 BE msg_type] [u16 BE data_len] [protobuf bytes]
  Selected per config entry by the presence of `noise_psk`.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from aioesphomeapi import api_pb2  # protobuf classes for stock messages
from noise.connection import NoiseConnection

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
_HANDSHAKE_TIMEOUT = 10  # seconds (noise handshake)
_API_VERSION_MAJOR = 1
_API_VERSION_MINOR = 10

_NOISE_PROTOCOL = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
_NOISE_PROLOGUE_INIT = b"NoiseAPIInit"


# ── Varint helpers (plaintext transport only) ────────────────────────────────


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


# ── Transport implementations ────────────────────────────────────────────────


class _PlaintextFrame:
    """Plaintext native-API framing: 0x00 + varint(size) + varint(type) + payload."""

    is_noise = False

    async def perform_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # No transport-level handshake — the API HelloRequest exchange is the
        # first thing on the wire and is handled by EsphomeApiServer.
        return

    def encode(self, msg_type: int, payload: bytes) -> bytes:
        return b"\x00" + _encode_varint(len(payload)) + _encode_varint(msg_type) + payload

    async def read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        preamble = (await reader.readexactly(1))[0]
        if preamble != 0x00:
            # 0x01 = device is in noise mode — config mismatch, not a stream error.
            raise RuntimeError(
                f"Unexpected preamble 0x{preamble:02X}: device is using noise encryption "
                f"but no PSK is configured for this entry"
            )
        msg_size = await _read_varint(reader)
        msg_type = await _read_varint(reader)
        data = await reader.readexactly(msg_size) if msg_size else b""
        return msg_type, data


class _NoiseFrame:
    """ESPHome-flavoured Noise NNpsk0 framing.

    Wire frame (both handshake and data):
        [0x01] [u16 BE size] [size bytes payload]

    Handshake payload: [type_byte] [body]
        type_byte = 0x00 → ok (noise message body follows)
        type_byte = 0x01 → server reject (UTF-8 reason follows)

    Data payload (after split): AEAD ciphertext of
        [u16 BE msg_type] [u16 BE data_len] [protobuf bytes]
    """

    is_noise = True

    def __init__(self, psk_b64: str) -> None:
        self._psk = self._decode_psk(psk_b64)
        self._noise: NoiseConnection | None = None
        # ESPHome's prologue is "NoiseAPIInit" + u16 BE client_hello_size + client_hello.
        # We send an empty client hello, so prologue is "NoiseAPIInit\x00\x00".
        self._client_hello = b""

    @staticmethod
    def _decode_psk(psk_b64: str) -> bytes:
        try:
            psk = base64.b64decode(psk_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise RuntimeError(f"noise_psk is not valid base64: {exc}") from exc
        if len(psk) != 32:
            raise RuntimeError(
                f"noise_psk must decode to 32 bytes (got {len(psk)}); copy the value "
                f"from the device's `api: encryption: key:` setting verbatim"
            )
        return psk

    async def perform_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await asyncio.wait_for(
            self._do_handshake(reader, writer), timeout=_HANDSHAKE_TIMEOUT
        )

    async def _do_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # 1. Send our (empty) client hello so the server knows we want to talk noise.
        await self._write_raw(writer, self._client_hello)

        # 2. Read server hello: [0x01 chosen_proto][name\0][mac\0]
        server_hello = await self._read_raw(reader)
        if not server_hello or server_hello[0] != 0x01:
            got = server_hello[:1].hex() if server_hello else ""
            raise RuntimeError(f"Server hello: chosen_proto byte missing/unexpected (got 0x{got})")
        rest = server_hello[1:]
        name, _, after = rest.partition(b"\x00")
        mac, _, _ = after.partition(b"\x00")
        _LOGGER.debug(
            "ESPHome API noise: server hello name=%s mac=%s",
            name.decode("utf-8", "replace"),
            mac.decode("utf-8", "replace"),
        )

        # 3. Initialise the Noise NN initiator.
        prologue = (
            _NOISE_PROLOGUE_INIT
            + len(self._client_hello).to_bytes(2, "big")
            + self._client_hello
        )
        n = NoiseConnection.from_name(_NOISE_PROTOCOL)
        n.set_as_initiator()
        n.set_psks(psks=[self._psk])
        n.set_prologue(prologue)
        n.start_handshake()
        self._noise = n

        # 4. Pattern is `NN`: -> e ; <- e, ee. Two messages, client writes first.
        msg1 = n.write_message()
        await self._write_raw(writer, b"\x00" + bytes(msg1))

        msg2_frame = await self._read_raw(reader)
        if not msg2_frame:
            raise RuntimeError("Empty handshake response from server")
        if msg2_frame[0] == 0x01:
            raise RuntimeError(
                "Noise handshake rejected: "
                + msg2_frame[1:].decode("utf-8", "replace").strip()
            )
        if msg2_frame[0] != 0x00:
            raise RuntimeError(f"Bad handshake indicator: 0x{msg2_frame[0]:02X}")
        n.read_message(msg2_frame[1:])

        if not n.handshake_finished:
            raise RuntimeError("Noise handshake did not complete after expected messages")

    def encode(self, msg_type: int, payload: bytes) -> bytes:
        if self._noise is None:
            raise RuntimeError("Noise transport not yet handshaken")
        plain = (
            msg_type.to_bytes(2, "big")
            + len(payload).to_bytes(2, "big")
            + payload
        )
        ciphertext = self._noise.encrypt(plain)
        return b"\x01" + len(ciphertext).to_bytes(2, "big") + ciphertext

    async def read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        if self._noise is None:
            raise RuntimeError("Noise transport not yet handshaken")
        ciphertext = await self._read_raw(reader)
        plain = self._noise.decrypt(ciphertext)
        if len(plain) < 4:
            raise RuntimeError(f"Decrypted message too short: {len(plain)} bytes")
        msg_type = (plain[0] << 8) | plain[1]
        data_len = (plain[2] << 8) | plain[3]
        if data_len > len(plain) - 4:
            raise RuntimeError(
                f"Decrypted data_len {data_len} exceeds remainder {len(plain) - 4}"
            )
        return msg_type, plain[4 : 4 + data_len]

    @staticmethod
    async def _write_raw(writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(b"\x01" + len(payload).to_bytes(2, "big") + payload)
        await writer.drain()

    @staticmethod
    async def _read_raw(reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(3)
        if header[0] != 0x01:
            raise RuntimeError(
                f"Unexpected noise frame indicator 0x{header[0]:02X} "
                f"(plaintext device? remove the noise PSK from the entry)"
            )
        length = (header[1] << 8) | header[2]
        return await reader.readexactly(length) if length else b""


# ── EsphomeApiServer ─────────────────────────────────────────────────────────


class EsphomeApiServer(PacketDispatcher):
    """Bridge between an ESPHome ble_server device and HA's packet protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        passcode: int = 0,
        noise_psk: str = "",
    ) -> None:
        super().__init__(hass, passcode)
        self._host = host
        self._port = port
        self._noise_psk = (noise_psk or "").strip()
        self._running = False
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._frame: _PlaintextFrame | _NoiseFrame | None = None
        self._write_lock = asyncio.Lock()
        self._connected = False  # API connection (not BLE client) state

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        _LOGGER.info(
            "ESPHome API bridge starting → %s:%d (%s)",
            self._host,
            self._port,
            "noise" if self._noise_psk else "plaintext",
        )

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
            self._frame = None
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_pump(self) -> None:
        # Pick the transport based on whether a PSK is configured.
        self._frame = _NoiseFrame(self._noise_psk) if self._noise_psk else _PlaintextFrame()

        reader, writer = await asyncio.open_connection(self._host, self._port)
        self._writer = writer
        try:
            await self._frame.perform_handshake(reader, writer)
            await self._handshake(reader, writer)
            self._connected = True
            _LOGGER.info(
                "ESPHome API: connected to %s:%d (%s)",
                self._host,
                self._port,
                "noise" if self._frame.is_noise else "plaintext",
            )
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
        if w is None or self._frame is None:
            return
        frame = self._frame.encode(msg_type, payload)
        async with self._write_lock:
            try:
                w.write(frame)
                await w.drain()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("ESPHome API: write failed: %s", exc)
                raise

    async def _read_message(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        if self._frame is None:
            raise RuntimeError("read before transport selected")
        return await self._frame.read(reader)

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
