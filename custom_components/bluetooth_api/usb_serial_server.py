"""USB-Serial bridge: ESP32-S3 (via USB-CDC-ACM) ↔ HA internal Python APIs.

Architecture (ESP32 mode):
  Android ←(BLE)→ ESP32-S3 ←(USB-Serial 4-byte framing)→ UsbSerialServer ←→ HA APIs

Packet flow:
  Android sends a passcode-secured packet (see protocol.py).
  ESP32 reassembles BLE chunks and forwards the raw packet bytes over USB with
  a 4-byte big-endian length prefix.
  UsbSerialServer decodes the packet, validates the passcode, dispatches the
  command to HA's internal APIs, builds a response packet, and writes it back
  over USB (ESP32 splits it into BLE chunks for Android).

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
from typing import Any

import serial_asyncio_fast as serial_asyncio

from homeassistant.core import HomeAssistant, callback

from .const import (
    CMD_ACK,
    CMD_ANS_AREAS,
    CMD_ANS_CALL_SERVICE,
    CMD_ANS_DASHBOARDS,
    CMD_ANS_DEVICES,
    CMD_ANS_STATE,
    CMD_CALL_SERVICE,
    CMD_NACK,
    CMD_REQ_AREAS,
    CMD_REQ_DASHBOARDS,
    CMD_REQ_DEVICES,
    CMD_REQ_STATE,
    CMD_STATE_CHANGE,
)
from .protocol import decode_packet, encode_packet, encode_packet_json, frame_for_usb, rfcomm_read_frame

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


class UsbSerialServer:
    """Bridge between ESP32-S3 USB-Serial and HA using the passcode packet protocol."""

    def __init__(
        self,
        hass: HomeAssistant,
        port: str = "/dev/ttyACM0",
        baud: int = 115200,
        passcode: int = 0,
    ) -> None:
        self._hass = hass
        self._port = port
        self._baud = baud
        self._passcode = passcode
        self._running = False
        self._task: asyncio.Task | None = None
        self._write_queue: asyncio.Queue[bytes | None] | None = None
        self._ble_client_connected: bool = False
        self._state_unsub: Any = None  # unsubscribe handle for state_changed events

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

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_raw(self, data: bytes) -> None:
        """Enqueue raw bytes (with USB length prefix) for the write worker."""
        queue = self._write_queue
        if queue is None:
            return
        try:
            queue.put_nowait(frame_for_usb(data))
        except asyncio.QueueFull:
            _LOGGER.warning("USB write queue full — dropping %d bytes", len(data))

    async def _send_packet(self, cmd: int, payload_obj: object = None) -> None:
        """Encode a passcode packet and enqueue it for USB transmission."""
        pkt = encode_packet_json(self._passcode, cmd, payload_obj)
        if payload_obj is None:
            payload_str = "<empty>"
        else:
            payload_json = json.dumps(payload_obj, default=lambda o: str(o))
            payload_str = payload_json if len(payload_json) <= 800 else f"{payload_json[:800]}…(+{len(payload_json)-800})"
        _LOGGER.debug("PI→ESP32: cmd=0x%02X (%d bytes total) payload=%s", cmd, len(pkt), payload_str)
        await self._send_raw(pkt)

    # ── Serial port main loop ─────────────────────────────────────────────────

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
            self._ble_client_connected = False
            if self._state_unsub:
                self._state_unsub()
                self._state_unsub = None
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

    # ── Packet reader & dispatcher ────────────────────────────────────────────

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            raw = await rfcomm_read_frame(reader)

            # Control messages from ESP32 are raw JSON (not passcode packets)
            if raw[:1] == b"{":
                await self._handle_control(raw)
                continue

            result = decode_packet(raw)
            if result is None:
                _LOGGER.warning(
                    "ESP32→PI: malformed packet (%d bytes) first 8: %s",
                    len(raw), raw[:8].hex(' '),
                )
                continue

            passcode, cmd, payload_bytes = result
            if passcode != self._passcode:
                _LOGGER.warning("ESP32→PI: wrong passcode 0x%08X (expected 0x%08X) — discarded",
                               passcode, self._passcode)
                await self._send_packet(CMD_NACK, {"error": "wrong passcode"})
                continue

            try:
                payload_str = payload_bytes.decode() if payload_bytes else "<empty>"
                if len(payload_str) > 200:
                    payload_str = f"{payload_str[:200]}…(+{len(payload_str)-200})"
            except UnicodeDecodeError:
                payload_str = payload_bytes[:32].hex(' ')
            _LOGGER.debug("ESP32→PI: cmd=0x%02X (%d bytes total) payload=%s",
                          cmd, len(raw), payload_str)

            await self._dispatch(cmd, payload_bytes)

    async def _handle_control(self, raw: bytes) -> None:
        """Handle raw JSON control messages from the ESP32 firmware."""
        try:
            msg = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        msg_type = msg.get("type")

        if msg_type == "log":
            _LOGGER.info("ESP32: %s", msg.get("msg", ""))
            return
        if msg_type == "client_connected":
            _LOGGER.info("BLE client connected")
            self._ble_client_connected = True
            self._subscribe_state_changes()
        elif msg_type == "client_disconnected":
            _LOGGER.info("BLE client disconnected")
            self._ble_client_connected = False
            if self._state_unsub:
                self._state_unsub()
                self._state_unsub = None
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

    async def _dispatch(self, cmd: int, payload_bytes: bytes) -> None:
        """Dispatch a validated packet by command code."""
        try:
            payload: dict = json.loads(payload_bytes.decode()) if payload_bytes else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

        if cmd == CMD_ACK:
            return  # nothing to do

        if cmd == CMD_REQ_AREAS:
            await self._send_packet(CMD_ACK)
            await self._handle_req_areas()
        elif cmd == CMD_REQ_DEVICES:
            await self._send_packet(CMD_ACK)
            await self._handle_req_devices(payload.get("area_id"))
        elif cmd == CMD_REQ_DASHBOARDS:
            await self._send_packet(CMD_ACK)
            await self._handle_req_dashboards()
        elif cmd == CMD_REQ_STATE:
            await self._send_packet(CMD_ACK)
            await self._handle_req_state(payload.get("entity_id", ""))
        elif cmd == CMD_CALL_SERVICE:
            await self._send_packet(CMD_ACK)
            await self._handle_call_service(payload)
        else:
            _LOGGER.debug("USB← unknown cmd 0x%02X", cmd)
            await self._send_packet(CMD_NACK, {"error": f"unknown command 0x{cmd:02X}"})

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _handle_req_areas(self) -> None:
        from homeassistant.helpers import area_registry as ar
        registry = ar.async_get(self._hass)
        areas = [
            {"id": area.id, "name": area.name, "icon": area.icon}
            for area in registry.async_list_areas()
        ]
        await self._send_packet(CMD_ANS_AREAS, areas)

    async def _handle_req_devices(self, area_id: str | None) -> None:
        from homeassistant.helpers import entity_registry as er
        ent_registry = er.async_get(self._hass)
        states = self._hass.states.async_all()
        entities = []
        _SLIM_ATTR_KEYS = frozenset((
            # core metadata
            "device_class", "unit_of_measurement", "state_class", "supported_features",
            # light
            "brightness", "supported_color_modes", "color_mode",
            "min_color_temp_kelvin", "max_color_temp_kelvin", "color_temp_kelvin",
            # fan
            "percentage", "percentage_step",
            # cover
            "current_position",
            # climate
            "hvac_modes", "min_temp", "max_temp", "current_temperature", "temperature",
            "hvac_action", "preset_mode", "preset_modes",
            # media_player
            "media_title", "volume_level",
        ))
        for state in states:
            entry = ent_registry.async_get(state.entity_id)
            entity_area = getattr(entry, "area_id", None) if entry else None
            if area_id is not None and entity_area != area_id:
                continue
            attrs = state.attributes
            slim_attrs = {k: attrs[k] for k in _SLIM_ATTR_KEYS if k in attrs}
            entities.append({
                "id": state.entity_id,
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", state.entity_id),
                "domain": state.domain,
                "area_id": entity_area,
                "state": state.state,
                "attrs": slim_attrs,
            })
        await self._send_packet(CMD_ANS_DEVICES, entities)

    async def _handle_req_dashboards(self) -> None:
        """Enumerate ALL Lovelace dashboards (default + user-created storage/yaml ones).

        HA's `lovelace` component stores dashboards in `hass.data['lovelace'].dashboards`,
        a dict keyed by url_path → LovelaceConfig (the default dashboard uses key None).
        We pull the raw config from each, then build a slim list of views per dashboard.
        """
        dashboards: list[dict] = []
        try:
            lovelace_data = self._hass.data.get("lovelace")
            # In modern HA this is a dataclass with a `dashboards` attribute.
            # In older HA it's a plain dict {"dashboards": {...}, ...}.
            ll_dashboards = (
                getattr(lovelace_data, "dashboards", None)
                or (lovelace_data or {}).get("dashboards")
                or {}
            )
            for url_path, ll_config in ll_dashboards.items():
                try:
                    raw = await ll_config.async_get_info()  # has dashboard metadata
                except Exception:  # noqa: BLE001
                    raw = {}
                # Get the actual yaml/storage config containing views
                try:
                    cfg = await ll_config.async_load(force=False)
                except Exception:  # noqa: BLE001
                    cfg = {}
                if not isinstance(cfg, dict):
                    cfg = {}
                # Default dashboard has url_path == None internally
                effective_url = url_path or "lovelace"
                title = (
                    raw.get("title")
                    or cfg.get("title")
                    or ("Übersicht" if url_path is None else effective_url)
                )
                views = cfg.get("views", []) or []
                dashboards.append({
                    "id": effective_url,
                    "url_path": effective_url,
                    "title": title,
                    "views": [
                        {
                            "id": f"{effective_url}_v{i}",
                            "path": v.get("path", str(i)),
                            "title": v.get("title", f"View {i}"),
                            "entity_ids": _extract_entity_ids(v),
                        }
                        for i, v in enumerate(views)
                    ],
                })
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Failed to enumerate dashboards: %s", exc)

        if not dashboards:
            # Fallback: at least return the default dashboard with no views
            dashboards.append({"id": "lovelace", "url_path": "lovelace", "title": "Home", "views": []})

        await self._send_packet(CMD_ANS_DASHBOARDS, dashboards)

    async def _handle_req_state(self, entity_id: str) -> None:
        state = self._hass.states.get(entity_id)
        if state is None:
            await self._send_packet(CMD_NACK, {"error": f"entity '{entity_id}' not found"})
            return
        await self._send_packet(CMD_ANS_STATE, _state_to_dict(state))

    async def _handle_call_service(self, payload: dict) -> None:
        domain = payload.get("domain", "")
        service = payload.get("service", "")
        entity_id = payload.get("entity_id")
        data = payload.get("data", {})
        if entity_id:
            data["entity_id"] = entity_id
        try:
            # 10s timeout — a hanging service (e.g. unresponsive integration) must not
            # block the entire packet dispatcher loop. The Android client waits 15s for ANS.
            await asyncio.wait_for(
                self._hass.services.async_call(domain, service, data, blocking=True),
                timeout=10.0,
            )
            await self._send_packet(CMD_ANS_CALL_SERVICE, {"success": True})
        except asyncio.TimeoutError:
            _LOGGER.warning("CALL_SERVICE %s.%s timed out after 10s", domain, service)
            await self._send_packet(CMD_ANS_CALL_SERVICE, {"success": False, "error": "service call timeout"})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("CALL_SERVICE %s.%s failed: %s", domain, service, exc)
            await self._send_packet(CMD_ANS_CALL_SERVICE, {"success": False, "error": str(exc)})

    # ── State-change subscription ─────────────────────────────────────────────

    def _subscribe_state_changes(self) -> None:
        if self._state_unsub:
            return
        @callback
        def _on_state_changed(event: Any) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            asyncio.ensure_future(
                self._send_packet(CMD_STATE_CHANGE, _state_to_dict(new_state))
            )

        self._state_unsub = self._hass.bus.async_listen("state_changed", _on_state_changed)
        _LOGGER.debug("Subscribed to state_changed events for BLE push")

    # ── OTA notification ──────────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _state_to_dict(state: Any) -> dict:
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat() if state.last_changed else None,
    }


def _extract_entity_ids(node: object) -> list[str]:
    """Recursively walk a Lovelace view/card config and pull every entity_id.

    Handles arbitrary nesting (grid, stack, conditional, custom cards) by
    descending into any list of dicts under common keys ('cards', 'entities',
    'sections', 'badges', 'elements'). Also picks up bare entity_id strings
    in entity lists.
    """
    ids: list[str] = []

    def looks_like_entity_id(s: str) -> bool:
        # domain.object_id, e.g. "light.bed_light"
        return "." in s and not s.startswith(".") and " " not in s

    def walk(n: object) -> None:
        if isinstance(n, dict):
            for key in ("entity", "entity_id"):
                v = n.get(key)
                if isinstance(v, str) and looks_like_entity_id(v):
                    ids.append(v)
            for key in ("cards", "entities", "sections", "badges", "elements", "views"):
                child = n.get(key)
                if isinstance(child, list):
                    for c in child:
                        walk(c)
        elif isinstance(n, str) and looks_like_entity_id(n):
            ids.append(n)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(node)
    return list(dict.fromkeys(ids))  # deduplicate preserving order
