"""Config flow for Bluetooth API integration."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    ADAPTER_MODE_ESP32,
    ADAPTER_MODE_NATIVE,
    CONF_ADAPTER_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_NAME_DEFAULT,
    CONF_PASSCODE,
    CONF_USB_PORT,
    CONF_USB_PORT_DEFAULT,
    DOMAIN,
)

MANUAL_PORT_SENTINEL = "__manual__"

# USB Vendor IDs that almost certainly mean "this is an ESP32".
# 0x303A is Espressif (native USB on ESP32-S3/C3); the others are USB-UART
# bridges commonly soldered onto dev boards.
ESPRESSIF_NATIVE_VID = 0x303A
USB_BRIDGE_VIDS = {
    0x10C4,  # Silicon Labs CP210x
    0x1A86,  # WCH CH340/CH341
    0x0403,  # FTDI
    0x067B,  # Prolific PL2303
}


def _generate_passcode() -> int:
    return random.SystemRandom().getrandbits(32)


def _passcode_display(passcode: int) -> str:
    hex_str = f"{passcode:08X}"
    return f"{hex_str[:4]}-{hex_str[4:]}"


def _list_serial_ports() -> list[tuple[str, str, int]]:
    """Scan for serial ports. Returns [(path, label, score)], sorted best-first.

    Prefers stable /dev/serial/by-id/ symlinks over /dev/ttyACM* paths because
    the kernel can rename ttyACM* across reboots, while by-id is stable.
    Higher score = better default (ESP32 native USB beats UART bridge beats
    unknown). Runs blocking pyserial calls; call via async_add_executor_job.
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        return []

    by_id_dir = Path("/dev/serial/by-id")
    by_id_map: dict[str, str] = {}
    if by_id_dir.is_dir():
        for link in by_id_dir.iterdir():
            try:
                resolved = str(link.resolve())
                by_id_map[resolved] = str(link)
            except OSError:
                continue

    results: list[tuple[str, str, int]] = []
    for p in serial.tools.list_ports.comports():
        stable_path = by_id_map.get(p.device, p.device)
        vid = p.vid or 0
        pid = p.pid or 0
        desc = (p.description or "").strip()
        manufacturer = (p.manufacturer or "").strip()

        if vid == ESPRESSIF_NATIVE_VID:
            score = 100
        elif vid in USB_BRIDGE_VIDS:
            score = 50
        else:
            score = 0

        parts = []
        if manufacturer and manufacturer.lower() not in desc.lower():
            parts.append(manufacturer)
        if desc and desc.lower() not in {"n/a", "unknown"}:
            parts.append(desc)
        if vid and pid:
            parts.append(f"{vid:04X}:{pid:04X}")
        suffix = " — ".join(parts)
        label = f"{stable_path}  ({suffix})" if suffix else stable_path

        results.append((stable_path, label, score))

    results.sort(key=lambda r: (-r[2], r[0]))
    return results


class BluetoothApiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Bluetooth API.

    Steps:
      1. user      — pick adapter mode (ESP32 or Pi-native) + device name
      2. esp32     — (only when adapter=esp32) ask USB port
      3. confirm   — show generated passcode for the QR code, user confirms
    """

    VERSION = 2

    def __init__(self) -> None:
        self._adapter_mode: str = ADAPTER_MODE_ESP32
        self._usb_port: str = CONF_USB_PORT_DEFAULT
        self._device_name: str = CONF_DEVICE_NAME_DEFAULT
        self._passcode: int = _generate_passcode()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: choose adapter mode + device name."""
        if user_input is not None:
            self._adapter_mode = user_input[CONF_ADAPTER_MODE]
            self._device_name = user_input.get(CONF_DEVICE_NAME, CONF_DEVICE_NAME_DEFAULT)
            return (
                await self.async_step_esp32()
                if self._adapter_mode == ADAPTER_MODE_ESP32
                else await self.async_step_confirm()
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADAPTER_MODE, default=ADAPTER_MODE_ESP32): vol.In(
                    {
                        ADAPTER_MODE_ESP32: "ESP32-S3 (USB-Serial Gateway)",
                        ADAPTER_MODE_NATIVE: "Raspberry Pi Bluetooth (geplant)",
                    }
                ),
                vol.Optional(CONF_DEVICE_NAME, default=CONF_DEVICE_NAME_DEFAULT): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 (ESP32 only): USB port — autodiscovered dropdown."""
        if user_input is not None:
            choice = user_input[CONF_USB_PORT]
            if choice == MANUAL_PORT_SENTINEL:
                return await self.async_step_esp32_manual()
            self._usb_port = choice
            return await self.async_step_confirm()

        ports = await self.hass.async_add_executor_job(_list_serial_ports)
        options: dict[str, str] = {path: label for path, label, _ in ports}
        options[MANUAL_PORT_SENTINEL] = "Manuell eingeben…"
        default = ports[0][0] if ports else MANUAL_PORT_SENTINEL

        schema = vol.Schema(
            {vol.Required(CONF_USB_PORT, default=default): vol.In(options)}
        )
        return self.async_show_form(step_id="esp32", data_schema=schema)

    async def async_step_esp32_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b: free-text USB port fallback when autodiscovery is missed."""
        if user_input is not None:
            self._usb_port = user_input.get(CONF_USB_PORT, CONF_USB_PORT_DEFAULT)
            return await self.async_step_confirm()

        schema = vol.Schema(
            {vol.Optional(CONF_USB_PORT, default=CONF_USB_PORT_DEFAULT): str}
        )
        return self.async_show_form(step_id="esp32_manual", data_schema=schema)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: show generated passcode (used in the QR code), let user confirm."""
        if user_input is not None:
            title = (
                f"Bluetooth API (ESP32, {self._usb_port})"
                if self._adapter_mode == ADAPTER_MODE_ESP32
                else f"Bluetooth API (Native Pi BT, {self._device_name})"
            )
            return self.async_create_entry(
                title=title,
                data={
                    CONF_ADAPTER_MODE: self._adapter_mode,
                    CONF_USB_PORT: self._usb_port,  # only used when adapter=esp32
                    CONF_DEVICE_NAME: self._device_name,
                    CONF_PASSCODE: self._passcode,
                },
            )

        passcode_str = _passcode_display(self._passcode)
        description = (
            f"Passcode: **{passcode_str}**\n\n"
            f"Nach der Einrichtung findest du den QR-Code unter:\n"
            f"`/api/bluetooth_api/setup_qr`\n\n"
            f"Scanne ihn in der btdashboard-App beim Einrichten."
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"passcode": passcode_str, "info": description},
        )
