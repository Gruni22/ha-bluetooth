"""USB-Serial bridge: ESP32-S3 (via USB-CDC-ACM) ↔ HA WebSocket API.

The ESP32-S3 is connected via USB and appears as /dev/ttyACM0 (or /dev/ttyUSB0).
It speaks the same 4-byte big-endian length-prefix framing as the RFCOMM transport.
This file reuses protocol.py's rfcomm_read_frame / rfcomm_write_frame unchanged.

Special messages from ESP32 (not forwarded to HA WebSocket):
  {"type": "pairing_request", "code": "123456"}
    → Creates a HA Persistent Notification with the pairing code.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
import serial_asyncio_fast as serial_asyncio

from homeassistant.core import HomeAssistant

from .protocol import rfcomm_read_frame, rfcomm_write_frame

_LOGGER = logging.getLogger(__name__)

_RECONNECT_DELAY = 5  # seconds before retrying serial port open


class UsbSerialServer:
    """Bridge between USB-CDC-ACM serial port (ESP32-S3) and HA WebSocket API."""

    def __init__(self, hass: HomeAssistant, port: str = "/dev/ttyACM0", baud: int = 115200) -> None:
        self._hass = hass
        self._port = port
        self._baud = baud
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.ensure_future(self._run())
        _LOGGER.info("USB Serial server starting on %s @ %d baud", self._port, self._baud)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("USB Serial server stopped")

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Open the serial port and bridge to HA WebSocket. Reconnects on error."""
        while self._running:
            try:
                reader, writer = await serial_asyncio.open_serial_connection(
                    url=self._port, baudrate=self._baud,
                    dsrdtr=False, rtscts=False,  # Prevent DTR/RTS from resetting ESP32
                )
                _LOGGER.info("USB Serial: ESP32-S3 connected on %s", self._port)
                # Drain any pending boot messages from ESP32 (up to 1 second)
                try:
                    await asyncio.wait_for(self._drain(reader), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                await self._bridge(reader, writer)
            except FileNotFoundError:
                _LOGGER.warning(
                    "USB Serial port %s not found — ESP32-S3 not plugged in? "
                    "Retrying in %ds", self._port, _RECONNECT_DELAY
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("USB Serial error: %s — retrying in %ds", exc, _RECONNECT_DELAY)

            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    # ─── Drain startup noise ──────────────────────────────────────────────────

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        """Read and discard bytes until the stream is quiet for 100 ms."""
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                break

    # ─── Bridge: USB-Serial ↔ HA WebSocket ───────────────────────────────────

    async def _bridge(
        self, usb_reader: asyncio.StreamReader, usb_writer: asyncio.StreamWriter
    ) -> None:
        """Bridge one USB-serial session to the local HA WebSocket API."""
        ws_url = f"ws://127.0.0.1:{self._hass.config.api.port}/api/websocket"  # type: ignore[union-attr]
        _LOGGER.info("USB Serial: connecting to HA WebSocket at %s", ws_url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    _LOGGER.debug("USB Serial: HA WebSocket established, bridging")
                    await asyncio.gather(
                        self._usb_to_ws(usb_reader, ws),
                        self._ws_to_usb(ws, usb_writer),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("USB Serial bridge ended: %s", exc)
        finally:
            try:
                usb_writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _usb_to_ws(
        self, reader: asyncio.StreamReader, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """USB-Serial frames → HA WebSocket text messages."""
        try:
            while True:
                frame = await rfcomm_read_frame(reader)

                # Intercept ESP32 control messages (e.g. pairing_request)
                try:
                    msg = json.loads(frame.decode())
                    if msg.get("type") == "pairing_request":
                        await self._handle_pairing_request(msg.get("code", "??????"))
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                # Forward JSON payload to HA WebSocket
                _LOGGER.debug("USB→WS (%d bytes): %.200s", len(frame), frame.decode(errors="replace"))
                await ws.send_str(frame.decode(errors="replace"))
        except asyncio.IncompleteReadError:
            _LOGGER.debug("USB Serial: ESP32 disconnected (IncompleteReadError)")

    async def _ws_to_usb(
        self, ws: aiohttp.ClientWebSocketResponse, writer: asyncio.StreamWriter
    ) -> None:
        """HA WebSocket text messages → USB-Serial frames."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                _LOGGER.debug("WS→USB (%d bytes): %.200s", len(msg.data), msg.data)
                await rfcomm_write_frame(writer, msg.data.encode())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                _LOGGER.debug("USB Serial: HA WebSocket closed: %s", msg.type)
                break

    # ─── Pairing notification ─────────────────────────────────────────────────

    async def _handle_pairing_request(self, code: str) -> None:
        """Show Bluetooth pairing code as a HA Persistent Notification."""
        _LOGGER.info("BLE Pairing request from ESP32, code: %s", code)
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": "bluetooth_api_pairing",
                "title": "Bluetooth Pairing",
                "message": (
                    f"Ein Android-Gerät möchte sich verbinden.\n\n"
                    f"**Pairing-Code: {code}**\n\n"
                    f"Bestätige denselben Code in der Android-App."
                ),
            },
        )
