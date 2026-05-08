"""The Bluetooth API integration for Home Assistant.

Exposes HA state/service APIs to the btdashboard Android app via BLE using a
passcode-secured packet protocol.

Three transport (adapter) modes:
  esp32    — ESP32-S3 over USB-Serial (UsbSerialServer)
  esphome  — ESPHome ble_server device over WiFi/native API (EsphomeApiServer)
  native   — Pi's own Bluetooth (HaBleGattServer, planned)

Architecture:
  btdashboard ←(BLE)→ adapter device ←(USB / WiFi / direct)→ HA internal Python APIs
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import BluetoothApiConfigView, BluetoothApiSetupQrView, BluetoothApiStatusView
from .const import (
    ADAPTER_MODE_ESP32,
    ADAPTER_MODE_ESPHOME,
    ADAPTER_MODE_NATIVE,
    CONF_ADAPTER_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_NAME_DEFAULT,
    CONF_ESPHOME_HOST,
    CONF_ESPHOME_NOISE_PSK,
    CONF_ESPHOME_PORT,
    CONF_ESPHOME_PORT_DEFAULT,
    CONF_PASSCODE,
    CONF_USB_PORT,
    CONF_USB_PORT_DEFAULT,
    DOMAIN,
    LABEL_BTDASH,
    LABEL_BTDASHAA,
)

PLATFORMS: list[str] = ["button"]

_LOGGER = logging.getLogger(__name__)

type BluetoothApiConfigEntry = ConfigEntry  # noqa: PYI042


def _ensure_btdash_labels(hass: HomeAssistant) -> None:
    """Create BTDASH / BTDASHAA labels in HA's label registry if missing.

    Idempotent — does nothing on subsequent setups. The user assigns these
    labels to entities or devices via HA's UI to control what shows up in the
    btdashboard app and Android Auto.
    """
    from homeassistant.helpers import label_registry as lr

    reg = lr.async_get(hass)
    for name in (LABEL_BTDASH, LABEL_BTDASHAA):
        if reg.async_get_label_by_name(name) is None:
            reg.async_create(name)
            _LOGGER.info(
                "Created HA label '%s' (used by bluetooth_api as exposure filter)",
                name,
            )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """One-shot migration: legacy entries (created before adapter_mode existed)
    are factually all ESP32-via-USB, so set the field explicitly. No guessing —
    every legacy entry was created when ESP32 was the only supported mode.
    """
    if CONF_ADAPTER_MODE not in entry.data:
        new = {**entry.data, CONF_ADAPTER_MODE: ADAPTER_MODE_ESP32}
        hass.config_entries.async_update_entry(entry, data=new)
        _LOGGER.info("Migrated entry %s: set adapter_mode=esp32", entry.entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluetooth API from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _ensure_btdash_labels(hass)

    adapter_mode: str = entry.data[CONF_ADAPTER_MODE]
    passcode: int = entry.data.get(CONF_PASSCODE, 0)

    if adapter_mode == ADAPTER_MODE_NATIVE:
        from .ble_gatt_server import NativeBleServer

        device_name: str = entry.data.get(CONF_DEVICE_NAME, CONF_DEVICE_NAME_DEFAULT)
        server = NativeBleServer(hass, device_name=device_name, passcode=passcode)
        try:
            await server.start()
            _LOGGER.info("Bluetooth API native BT server started as '%s'", device_name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to start native BT server '%s': %s", device_name, exc)
        hass.data[DOMAIN][entry.entry_id] = [server]
    elif adapter_mode == ADAPTER_MODE_ESPHOME:
        from .esphome_server import EsphomeApiServer

        host: str = entry.data.get(CONF_ESPHOME_HOST, "")
        port: int = int(entry.data.get(CONF_ESPHOME_PORT, CONF_ESPHOME_PORT_DEFAULT))
        noise_psk: str = entry.data.get(CONF_ESPHOME_NOISE_PSK, "") or ""
        if not host:
            _LOGGER.error("ESPHome adapter mode selected but no host configured")
            return False
        server = EsphomeApiServer(
            hass, host=host, port=port, passcode=passcode, noise_psk=noise_psk
        )
        try:
            await server.start()
            _LOGGER.info("Bluetooth API ESPHome bridge starting → %s:%d", host, port)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to start ESPHome bridge to %s:%d: %s", host, port, exc)
        hass.data[DOMAIN][entry.entry_id] = [server]
    else:
        # ADAPTER_MODE_ESP32
        from .usb_serial_server import UsbSerialServer

        usb_port: str = entry.data.get(CONF_USB_PORT, CONF_USB_PORT_DEFAULT)
        usb_server = UsbSerialServer(hass, port=usb_port, passcode=passcode)
        try:
            await usb_server.start()
            _LOGGER.info("Bluetooth API USB Serial server started on %s", usb_port)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to start USB Serial server on %s: %s", usb_port, exc)
        hass.data[DOMAIN][entry.entry_id] = [usb_server]

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register HTTP endpoints (guard survives reloads)
    if not hass.data[DOMAIN].get("views_registered"):
        hass.http.register_view(BluetoothApiSetupQrView())
        hass.http.register_view(BluetoothApiStatusView())
        hass.http.register_view(BluetoothApiConfigView())
        hass.data[DOMAIN]["views_registered"] = True

    hass.async_create_task(_post_setup_notification(hass, entry))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the bridge server and unload the config entry."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    servers = hass.data.get(DOMAIN, {}).pop(entry.entry_id, [])
    for server in servers:
        try:
            await server.stop()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Error stopping server during unload: %s", exc)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _post_setup_notification(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create a persistent notification with the setup QR code."""
    passcode: int = entry.data.get(CONF_PASSCODE, 0)
    hex_str = f"{passcode:08X}"
    display = f"{hex_str[:4]}-{hex_str[4:]}"
    qr_url = f"/api/bluetooth_api/setup_qr/{entry.entry_id}"
    device_name: str = entry.data.get(CONF_DEVICE_NAME, CONF_DEVICE_NAME_DEFAULT)
    await hass.services.async_call(
        "persistent_notification", "create",
        {
            "notification_id": f"bluetooth_api_setup_{entry.entry_id}",
            "title": f"Bluetooth API: {device_name}",
            "message": (
                f"![QR Code]({qr_url})\n\n"
                f"Scanne den QR-Code in der **btdashboard**-App, "
                f"um die Verbindung einzurichten.\n\n"
                f"Passcode (manuell): **{display}**"
            ),
        },
    )
