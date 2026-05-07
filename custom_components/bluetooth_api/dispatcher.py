"""Shared passcode-packet dispatcher.

Both the USB-Serial bridge (UsbSerialServer) and the ESPHome native-API bridge
(EsphomeApiServer) carry the same packet protocol — only the underlying
transport differs. This module factors out the protocol/dispatch logic so each
transport only has to implement how to *send* a finished packet and what to do
on connect/disconnect.

Subclasses implement:
    - async def _send_raw(self, data: bytes) -> None
        Transmit *data* (a complete passcode packet) over the transport.
    - lifecycle hooks (start/stop) live on the subclass.

Subclasses call:
    - await self._handle_incoming_packet(raw)
        Decode + dispatch a fully-reassembled packet received from the
        Android client side of the bridge.
    - self._on_ble_client_connected() / _on_ble_client_disconnected()
        Signal that the *Android* end of the bridge has connected / dropped.
        Drives the state-changed subscription so HA only pushes updates while a
        client is actually listening.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
    LABEL_BTDASH,
    LABEL_BTDASHAA,
)
from .protocol import decode_packet, encode_packet_json

_LOGGER = logging.getLogger(__name__)


class PacketDispatcher:
    """Transport-agnostic dispatcher for the bluetooth_api packet protocol."""

    def __init__(self, hass: HomeAssistant, passcode: int) -> None:
        self._hass = hass
        self._passcode = passcode
        self._ble_client_connected: bool = False
        self._state_unsub: Any = None

    # ── Transport hook (subclass must override) ──────────────────────────────

    async def _send_raw(self, data: bytes) -> None:
        raise NotImplementedError

    # ── Outbound packet helper ───────────────────────────────────────────────

    async def _send_packet(self, cmd: int, payload_obj: object = None) -> None:
        pkt = encode_packet_json(self._passcode, cmd, payload_obj)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            if payload_obj is None:
                payload_str = "<empty>"
            else:
                payload_json = json.dumps(payload_obj, default=lambda o: str(o))
                payload_str = (
                    payload_json
                    if len(payload_json) <= 800
                    else f"{payload_json[:800]}…(+{len(payload_json) - 800})"
                )
            _LOGGER.debug("HA→BLE: cmd=0x%02X (%d bytes total) payload=%s", cmd, len(pkt), payload_str)
        await self._send_raw(pkt)

    # ── Inbound packet entry point ───────────────────────────────────────────

    async def _handle_incoming_packet(self, raw: bytes) -> None:
        result = decode_packet(raw)
        if result is None:
            _LOGGER.warning(
                "BLE→HA: malformed packet (%d bytes) first 8: %s",
                len(raw), raw[:8].hex(' '),
            )
            return

        passcode, cmd, payload_bytes = result
        if passcode != self._passcode:
            _LOGGER.warning(
                "BLE→HA: wrong passcode 0x%08X (expected 0x%08X) — discarded",
                passcode, self._passcode,
            )
            await self._send_packet(CMD_NACK, {"error": "wrong passcode"})
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                payload_str = payload_bytes.decode() if payload_bytes else "<empty>"
                if len(payload_str) > 200:
                    payload_str = f"{payload_str[:200]}…(+{len(payload_str) - 200})"
            except UnicodeDecodeError:
                payload_str = payload_bytes[:32].hex(' ')
            _LOGGER.debug("BLE→HA: cmd=0x%02X (%d bytes total) payload=%s", cmd, len(raw), payload_str)

        await self._dispatch(cmd, payload_bytes)

    async def _dispatch(self, cmd: int, payload_bytes: bytes) -> None:
        try:
            payload: dict = json.loads(payload_bytes.decode()) if payload_bytes else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

        if cmd == CMD_ACK:
            return

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
            _LOGGER.debug("Unknown cmd 0x%02X", cmd)
            await self._send_packet(CMD_NACK, {"error": f"unknown command 0x{cmd:02X}"})

    # ── BLE client lifecycle hooks (subclass calls these) ────────────────────

    def _on_ble_client_connected(self) -> None:
        if self._ble_client_connected:
            return
        self._ble_client_connected = True
        _LOGGER.info("BLE client connected")
        self._subscribe_state_changes()

    def _on_ble_client_disconnected(self) -> None:
        if not self._ble_client_connected:
            return
        self._ble_client_connected = False
        _LOGGER.info("BLE client disconnected")
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None

    # ── Command handlers ─────────────────────────────────────────────────────

    async def _handle_req_areas(self) -> None:
        from homeassistant.helpers import area_registry as ar
        registry = ar.async_get(self._hass)
        areas = [
            {"id": area.id, "name": area.name, "icon": area.icon}
            for area in registry.async_list_areas()
        ]
        await self._send_packet(CMD_ANS_AREAS, areas)

    async def _handle_req_devices(self, area_id: str | None) -> None:
        from homeassistant.helpers import (
            device_registry as dr,
            entity_registry as er,
            label_registry as lr,
        )
        ent_registry = er.async_get(self._hass)
        dev_registry = dr.async_get(self._hass)
        label_registry = lr.async_get(self._hass)
        exposure_ids = _exposure_label_ids(label_registry)
        if not exposure_ids:
            # BTDASH / BTDASHAA missing — integration setup didn't run yet, or
            # the user deleted the labels. Either way, nothing to expose.
            await self._send_packet(CMD_ANS_DEVICES, [])
            return

        # label_id → name map, built once for this response.
        id_to_name = {l.label_id: l.name for l in label_registry.async_list_labels()}

        states = self._hass.states.async_all()
        entities = []
        slim_keys = _SLIM_ATTR_KEYS
        for state in states:
            entry = ent_registry.async_get(state.entity_id)
            if entry is None:
                continue
            label_ids = _entity_label_ids(entry, dev_registry)
            if not (label_ids & exposure_ids):
                continue
            entity_area = entry.area_id
            if area_id is not None and entity_area != area_id:
                continue
            attrs = state.attributes
            slim_attrs = {k: attrs[k] for k in slim_keys if k in attrs}
            entities.append({
                "id": state.entity_id,
                "entity_id": state.entity_id,
                "name": attrs.get("friendly_name", state.entity_id),
                "domain": state.domain,
                "area_id": entity_area,
                "state": state.state,
                "attrs": slim_attrs,
                # Full label-name set so the app can do dashboard-level
                # filtering (e.g. show DASH_Battery on the Battery view).
                "labels": sorted(id_to_name[lid] for lid in label_ids if lid in id_to_name),
            })
        await self._send_packet(CMD_ANS_DEVICES, entities)

    async def _handle_req_dashboards(self) -> None:
        dashboards: list[dict] = []
        try:
            lovelace_data = self._hass.data.get("lovelace")
            ll_dashboards = (
                getattr(lovelace_data, "dashboards", None)
                or (lovelace_data or {}).get("dashboards")
                or {}
            )
            for url_path, ll_config in ll_dashboards.items():
                try:
                    raw = await ll_config.async_get_info()
                except Exception:  # noqa: BLE001
                    raw = {}
                try:
                    cfg = await ll_config.async_load(force=False)
                except Exception:  # noqa: BLE001
                    cfg = {}
                if not isinstance(cfg, dict):
                    cfg = {}
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
            dashboards.append({"id": "lovelace", "url_path": "lovelace", "title": "Home", "views": []})

        await self._send_packet(CMD_ANS_DASHBOARDS, dashboards)

    async def _handle_req_state(self, entity_id: str) -> None:
        state = self._hass.states.get(entity_id)
        if state is None:
            await self._send_packet(CMD_NACK, {"error": f"entity '{entity_id}' not found"})
            return
        # Honour the same exposure filter as ANS_DEVICES — an entity that's not
        # in the dashboard list shouldn't leak its state via direct REQ_STATE.
        if not _is_exposed(self._hass, entity_id):
            await self._send_packet(CMD_NACK, {"error": f"entity '{entity_id}' not exposed"})
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

    # ── State-change subscription ────────────────────────────────────────────

    def _subscribe_state_changes(self) -> None:
        if self._state_unsub:
            return

        hass = self._hass

        @callback
        def _on_state_changed(event: Any) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            # Only push state changes for entities the user explicitly exposed
            # via BTDASH / BTDASHAA. Without this filter every Supervisor /
            # update / sensor change would be sent over BLE — wasteful and
            # noisy on the chunked notify path.
            if not _is_exposed(hass, new_state.entity_id):
                return
            asyncio.ensure_future(
                self._send_packet(CMD_STATE_CHANGE, _state_to_dict(new_state))
            )

        self._state_unsub = self._hass.bus.async_listen("state_changed", _on_state_changed)
        _LOGGER.debug("Subscribed to state_changed events for BLE push")


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _state_to_dict(state: Any) -> dict:
    return {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat() if state.last_changed else None,
    }


def _exposure_label_ids(label_registry: Any) -> set[str]:
    """Return the set of label_ids that mark an entity as exposed (BTDASH / BTDASHAA)."""
    ids: set[str] = set()
    for name in (LABEL_BTDASH, LABEL_BTDASHAA):
        label = label_registry.async_get_label_by_name(name)
        if label is not None:
            ids.add(label.label_id)
    return ids


def _entity_label_ids(entry: Any, dev_registry: Any) -> set[str]:
    """Return the union of an entity's own labels and its parent device's labels.

    HA exposes labels at both the entity and the device level. For the
    btdashboard filter we treat them as one set: labelling the device is the
    common case (one click in the UI labels every entity it owns).
    """
    label_ids: set[str] = set(entry.labels)
    if entry.device_id:
        device = dev_registry.async_get(entry.device_id)
        if device is not None:
            label_ids |= set(device.labels)
    return label_ids


def _is_exposed(hass: HomeAssistant, entity_id: str) -> bool:
    """Cheap per-event check used by state_changed and REQ_STATE.

    Resolves the entity's effective label set on the fly. In the hot state-
    change path this means three registry lookups per event — all O(1) dict
    reads, so we don't bother caching across events.
    """
    from homeassistant.helpers import (
        device_registry as dr,
        entity_registry as er,
        label_registry as lr,
    )
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry is None:
        return False
    label_ids = _entity_label_ids(entry, dr.async_get(hass))
    if not label_ids:
        return False
    return bool(label_ids & _exposure_label_ids(lr.async_get(hass)))


def _extract_entity_ids(node: object) -> list[str]:
    ids: list[str] = []

    def looks_like_entity_id(s: str) -> bool:
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
    return list(dict.fromkeys(ids))
