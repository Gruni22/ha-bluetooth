"""USB-Serial bridge: ESP32-S3 (via USB-CDC-ACM) ↔ HA internal Python APIs.

Architecture (ESP32 mode):
  Android ←(BLE)→ ESP32-S3 ←(USB-Serial 4-byte framing)→ UsbSerialServer ←→ HA APIs

Packet flow:
  Android sends a passcode-secured packet (see protocol.py).
  ESP32 reassembles BLE chunks and forwards the raw packet bytes over USB with
  a 4-byte big-endian length prefix.
  UsbSerialServer (via PacketDispatcher) decodes the packet, validates the
  passcode, dispatches the command to HA's internal APIs, builds a response
  packet, and writes it back over USB.

OTA control messages from ESP32 (special raw JSON, not passcode packets):
  {"type": "ota_ready",  "ssid": "…", "ip": "…"}   → HA Persistent Notification
  {"type": "ota_error",  "message": "…"}             → HA Persistent Notification
  {"type": "client_connected"}                        → log + state-change subscription
  {"type": "client_disconnected"}                     → log
"""

from __future__ import annotations

import asyncio
import json
import logging

import serial_asyncio_fast as serial_asyncio

from homeassistant.core import HomeAssistant

from .dispatcher import PacketDispatcher
from .protocol import frame_for_usb, rfcomm_read_frame

_LOGGER = logging.getLogger(__name__)


# Workaround: serial_asyncio_fast _write_ready AssertionError on close
def _patch_serial_asyncio_fast() -> None:
    transport_cls = getattr(serial_asyncio, "SerialTransport", None)
    if transport_cls is None:
        return
    original = transport_cls._write_ready

    def _safe_write_ready(self) -> None:  # type: ignore[override]
        buf = getattr(self, "_write_buffer", None)
        if not buf:
            self._write_scheduled = False  # type: ignore[attr-defined]
            try:
                self._loop.remove_writer(self._serial.fileno())  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        original(self)

    transport_cls._write_ready = _safe_write_ready  # type: ignore[method-assign]


_patch_serial_asyncio_fast()

_RECONNECT_DELAY = 5


class UsbSerialServer(PacketDispatcher):
    """Bridge between ESP32-S3 USB-Serial and HA using the passcode packet protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        port: str = "/dev/ttyACM0",
        baud: int = 115200,
        passcode: int = 0,
    ) -> None:
        super().__init__(hass, passcode)
        self._port = port
        self._baud = baud
        self._running = False
        self._task: asyncio.Task | None = None
        self._write_queue: asyncio.Queue[bytes | None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        _LOGGER.info("USB Serial server starting on %s @ %d baud", self._port, self._baud)

    async def stop(self) -> None:
        self._running = False
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enable_ota(self) -> None:
        """Send OTA-enable command to ESP32."""
        await self._send_raw(b'{"type":"ota_enable"}')

    # ── Transport hook (PacketDispatcher) ────────────────────────────────────

    async def _send_raw(self, data: bytes) -> None:
        """Enqueue raw bytes (with USB length prefix) for the write worker."""
        queue = self._write_queue
        if queue is None:
            return
        try:
            queue.put_nowait(frame_for_usb(data))
        except asyncio.QueueFull:
            _LOGGER.warning("USB write queue full — dropping %d bytes", len(data))

    # ── Serial port main loop ────────────────────────────────────────────────

    async def _run(self) -> None:
        while self._running:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(
                    url=self._port, baudrate=self._baud, dsrdtr=False, rtscts=False,
                )
                _LOGGER.info("USB Serial: ESP32-S3 connected on %s", self._port)
                try:
                    await asyncio.wait_for(self._drain(reader), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                await self._bridge(reader, writer)
            except FileNotFoundError:
                _LOGGER.warning("USB Serial port %s not found — retrying in %ds", self._port, _RECONNECT_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("USB Serial error: %s — retrying in %ds", exc, _RECONNECT_DELAY)
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                break

    async def _bridge(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        write_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self._write_queue = write_queue
        write_task = asyncio.ensure_future(self._write_worker(writer, write_queue))
        # Tell ESP32 to restart BLE advertising in case it stopped after rapid
        # BLE disconnect/reconnect cycles or a previous USB session ended unexpectedly.
        await self._send_raw(b'{"type":"restart_adv"}')
        try:
            await self._reader_loop(reader)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Bridge ended: %s", exc)
        finally:
            self._write_queue = None
            self._on_ble_client_disconnected()
            write_task.cancel()
            try:
                await write_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _write_worker(
        self, writer: asyncio.StreamWriter, queue: asyncio.Queue[bytes | None]
    ) -> None:
        # No await writer.drain() here — drain blocks the dispatcher when the
        # ESP32's USB-CDC RX is momentarily stalled (e.g. while it's busy chunking
        # a large ANS_DEVICES out over BLE). The write_queue's bounded size
        # (maxsize=64) provides backpressure: producers see asyncio.QueueFull.
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    writer.write(data)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.error("USB write error: %s", exc)
                    break  # writer is broken — exit and let _bridge restart
        except asyncio.CancelledError:
            pass

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            raw = await rfcomm_read_frame(reader)

            # Control messages from ESP32 are raw JSON (not passcode packets)
            if raw[:1] == b"{":
                await self._handle_control(raw)
                continue

            await self._handle_incoming_packet(raw)

    async def _handle_control(self, raw: bytes) -> None:
        """Handle raw JSON control messages from the ESP32 firmware."""
        try:
            msg = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        msg_type = msg.get("type")

        if msg_type == "log":
            _LOGGER.info("ESP32: %s", msg.get("msg", ""))
        elif msg_type == "client_connected":
            self._on_ble_client_connected()
        elif msg_type == "client_disconnected":
            self._on_ble_client_disconnected()
        elif msg_type == "ota_ready":
            await self._notify_ota_ready(msg.get("ssid", "ESP32-HA-OTA"), msg.get("ip", "192.168.200.1"))
        elif msg_type == "ota_error":
            _LOGGER.error("ESP32 OTA error: %s", msg.get("message", "unknown"))
            await self._hass.services.async_call(
                "persistent_notification", "create",
                {
                    "notification_id": "bluetooth_api_ota_error",
                    "title": "ESP32 OTA Fehler",
                    "message": f"OTA konnte nicht gestartet werden: **{msg.get('message')}**",
                },
            )

    # ── OTA notification ─────────────────────────────────────────────────────

    async def _notify_ota_ready(self, ssid: str, ip: str) -> None:
        _LOGGER.info("ESP32 OTA mode: AP=%s IP=%s", ssid, ip)
        await self._hass.services.async_call(
            "persistent_notification", "create",
            {
                "notification_id": "bluetooth_api_ota",
                "title": "ESP32 OTA bereit",
                "message": (
                    f"ESP32 hat Access Point **{ssid}** erstellt.\n\n"
                    f"1. Verbinde deinen PC mit dem WLAN **{ssid}**\n"
                    f"2. Führe aus: `pio run -e esp32-s3-ota -t upload`\n\n"
                    f"IP-Adresse: `{ip}`"
                ),
            },
        )
