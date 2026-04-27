"""The Bluetooth API integration for Home Assistant.

Exposes the HA WebSocket API via an ESP32-S3 BLE gateway connected via USB-Serial.

Architecture (ESP32-S3 mode)
─────────────────────────────
Android App ←(BLE LE Secure Connections)→ ESP32-S3 ←(USB-CDC /dev/ttyACM0)→
  bluetooth_api (this integration) ←(local WebSocket)→ HA Core

The ESP32-S3 acts as the BLE radio. This integration bridges the USB-Serial
framing protocol (4-byte length prefix + JSON) to HA's WebSocket API.
Pairing notifications (code display) are shown as HA Persistent Notifications.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import BluetoothApiConfigView
from .const import CONF_USB_PORT, DOMAIN
from .usb_serial_server import UsbSerialServer

PLATFORMS = ["button"]

_LOGGER = logging.getLogger(__name__)

type BluetoothApiConfigEntry = ConfigEntry  # noqa: PYI042


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluetooth API from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    usb_port: str = entry.data.get(CONF_USB_PORT, "/dev/ttyACM0")

    server = UsbSerialServer(hass, port=usb_port)
    try:
        await server.start()
        _LOGGER.info("Bluetooth API USB Serial server started on %s", usb_port)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Failed to start USB Serial server on %s: %s", usb_port, exc)

    hass.data[DOMAIN][entry.entry_id] = [server]
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register REST endpoint once (guard survives reloads).
    if not hass.data[DOMAIN].get("view_registered"):
        hass.http.register_view(BluetoothApiConfigView())
        hass.data[DOMAIN]["view_registered"] = True

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the USB serial server and unload the config entry."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    servers = hass.data.get(DOMAIN, {}).pop(entry.entry_id, [])
    for server in servers:
        try:
            await server.stop()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Error stopping server during unload: %s", exc)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the config entry is updated."""
    await hass.config_entries.async_reload(entry.entry_id)
