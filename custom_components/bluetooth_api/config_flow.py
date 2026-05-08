"""Config flow for Bluetooth API integration."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.data_entry_flow import FlowResult

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


def _conflicting_bluetooth_entries(hass: Any) -> list[Any]:
    """Return enabled `bluetooth`-domain config entries.

    `bless` needs exclusive control over the Pi's BT adapter via BlueZ. Any
    enabled HA Bluetooth integration entry holding hci0 (or any local adapter)
    will fight us — peripheral mode + active scan from the same process is
    not something the BlueZ stack handles cleanly. We list these so the user
    can decide whether to disable them.
    """
    try:
        return [
            e for e in hass.config_entries.async_entries("bluetooth")
            if e.disabled_by is None
        ]
    except Exception:  # noqa: BLE001
        return []


def _list_esphome_devices(hass: Any) -> list[tuple[str, int, str, str]]:
    """Return [(host, port, label, noise_psk)] for ESPHome integrations already configured in HA.

    Lets the user pick an existing ESPHome device from a dropdown instead of
    typing the host. Also harvests the device's `noise_psk` so the user
    doesn't have to paste their encryption key again. Falls back to manual
    entry when no ESPHome integrations are present.
    """
    out: list[tuple[str, int, str, str]] = []
    try:
        for entry in hass.config_entries.async_entries("esphome"):
            host = entry.data.get("host")
            port = entry.data.get("port", CONF_ESPHOME_PORT_DEFAULT)
            noise_psk = entry.data.get("noise_psk", "") or ""
            if not host:
                continue
            label = f"{entry.title} ({host}:{port})" if entry.title else f"{host}:{port}"
            out.append((host, int(port), label, noise_psk))
    except Exception:  # noqa: BLE001
        pass
    return out


class BluetoothApiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Bluetooth API.

    Steps:
      1. user      — pick adapter mode (ESP32 / ESPHome / Pi-native) + device name
      2a. esp32    — (esp32 only) pick USB port
      2b. esphome  — (esphome only) pick host + port (or manual)
      3. confirm   — show generated passcode for the QR code, user confirms
    """

    VERSION = 2

    def __init__(self) -> None:
        self._adapter_mode: str = ADAPTER_MODE_ESP32
        self._usb_port: str = CONF_USB_PORT_DEFAULT
        self._esphome_host: str = ""
        self._esphome_port: int = CONF_ESPHOME_PORT_DEFAULT
        self._esphome_noise_psk: str = ""
        self._device_name: str = CONF_DEVICE_NAME_DEFAULT
        self._passcode: int = _generate_passcode()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: choose adapter mode + device name."""
        if user_input is not None:
            self._adapter_mode = user_input[CONF_ADAPTER_MODE]
            self._device_name = user_input.get(CONF_DEVICE_NAME, CONF_DEVICE_NAME_DEFAULT)
            if self._adapter_mode == ADAPTER_MODE_ESP32:
                return await self.async_step_esp32()
            if self._adapter_mode == ADAPTER_MODE_ESPHOME:
                return await self.async_step_esphome()
            return await self.async_step_native_warn()

        schema = vol.Schema(
            {
                vol.Required(CONF_ADAPTER_MODE, default=ADAPTER_MODE_ESP32): vol.In(
                    {
                        ADAPTER_MODE_ESP32: "ESP32-S3 (USB-Serial Gateway)",
                        ADAPTER_MODE_ESPHOME: "ESPHome (ble_server über WLAN/native API)",
                        ADAPTER_MODE_NATIVE: "Raspberry Pi Bluetooth (native, bless)",
                    }
                ),
                vol.Optional(CONF_DEVICE_NAME, default=CONF_DEVICE_NAME_DEFAULT): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2a (ESP32 only): USB port — autodiscovered dropdown."""
        if user_input is not None:
            choice = user_input[CONF_USB_PORT]
            if choice == MANUAL_PORT_SENTINEL:
                return await self.async_step_esp32_manual()
            self._usb_port = choice
            return self._finish()

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
        """Step 2a-fallback: free-text USB port."""
        if user_input is not None:
            self._usb_port = user_input.get(CONF_USB_PORT, CONF_USB_PORT_DEFAULT)
            return self._finish()

        schema = vol.Schema(
            {vol.Optional(CONF_USB_PORT, default=CONF_USB_PORT_DEFAULT): str}
        )
        return self.async_show_form(step_id="esp32_manual", data_schema=schema)

    async def async_step_esphome(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b (ESPHome only): pick host:port from existing ESPHome integration entries.

        Picking from this list also auto-harvests the device's `noise_psk` from
        the existing ESPHome integration entry, so encrypted devices work
        without re-typing the key.
        """
        devices = _list_esphome_devices(self.hass)
        psk_by_host: dict[str, str] = {f"{h}|{p}": psk for h, p, _, psk in devices}

        if user_input is not None:
            choice = user_input[CONF_ESPHOME_HOST]
            if choice == MANUAL_PORT_SENTINEL:
                return await self.async_step_esphome_manual()
            host_str, _, port_str = choice.partition("|")
            self._esphome_host = host_str
            self._esphome_port = int(port_str) if port_str else CONF_ESPHOME_PORT_DEFAULT
            self._esphome_noise_psk = psk_by_host.get(choice, "")
            return self._finish()

        options: dict[str, str] = {f"{h}|{p}": label for h, p, label, _ in devices}
        options[MANUAL_PORT_SENTINEL] = "Manuell eingeben…"
        default = next(iter(options.keys()), MANUAL_PORT_SENTINEL)

        schema = vol.Schema(
            {vol.Required(CONF_ESPHOME_HOST, default=default): vol.In(options)}
        )
        return self.async_show_form(step_id="esphome", data_schema=schema)

    async def async_step_esphome_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b-fallback: free-text host + port + optional noise PSK for the ESPHome native API."""
        if user_input is not None:
            self._esphome_host = user_input[CONF_ESPHOME_HOST].strip()
            self._esphome_port = int(user_input.get(CONF_ESPHOME_PORT, CONF_ESPHOME_PORT_DEFAULT))
            self._esphome_noise_psk = user_input.get(CONF_ESPHOME_NOISE_PSK, "").strip()
            return self._finish()

        schema = vol.Schema(
            {
                vol.Required(CONF_ESPHOME_HOST): str,
                vol.Optional(CONF_ESPHOME_PORT, default=CONF_ESPHOME_PORT_DEFAULT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Optional(CONF_ESPHOME_NOISE_PSK, default=""): str,
            }
        )
        return self.async_show_form(step_id="esphome_manual", data_schema=schema)

    async def async_step_native_warn(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Native-mode pre-flight: warn about conflicts with HA's Bluetooth integration.

        bless drives the Pi BT adapter as a peripheral via BlueZ/D-Bus. Any
        already-running HA Bluetooth integration entry that's actively scanning
        or holding the same adapter will stop our GATT server from advertising
        / accepting connections cleanly. We list those entries and let the
        user disable them in one click. Skipped entirely if no conflicts.
        """
        conflicts = _conflicting_bluetooth_entries(self.hass)

        if user_input is not None:
            if user_input.get("disable_conflicts") and conflicts:
                for entry in conflicts:
                    try:
                        await self.hass.config_entries.async_set_disabled_by(
                            entry.entry_id, ConfigEntryDisabler.USER
                        )
                    except Exception:  # noqa: BLE001
                        # Non-fatal: setup proceeds, user can disable manually.
                        pass
            return self._finish()

        if not conflicts:
            # Nothing to warn about — straight to entry creation.
            return self._finish()

        conflict_list = "\n".join(
            f"- **{e.title or e.domain}** ({e.entry_id[:8]}…)" for e in conflicts
        )
        schema = vol.Schema({vol.Optional("disable_conflicts", default=True): bool})
        return self.async_show_form(
            step_id="native_warn",
            data_schema=schema,
            description_placeholders={"conflicts": conflict_list},
        )

    def _finish(self) -> FlowResult:
        """Create the config entry — no extra confirmation step.

        The passcode is auto-generated and shown to the user via a persistent
        notification *after* setup (so the QR endpoint can serve it). Forcing
        an extra empty form click before that happens was just noise.
        """
        if self._adapter_mode == ADAPTER_MODE_ESP32:
            title = f"Bluetooth API (ESP32, {self._usb_port})"
        elif self._adapter_mode == ADAPTER_MODE_ESPHOME:
            title = f"Bluetooth API (ESPHome, {self._esphome_host}:{self._esphome_port})"
        else:
            title = f"Bluetooth API (Native Pi BT, {self._device_name})"
        return self.async_create_entry(
            title=title,
            data={
                CONF_ADAPTER_MODE: self._adapter_mode,
                CONF_USB_PORT: self._usb_port,
                CONF_ESPHOME_HOST: self._esphome_host,
                CONF_ESPHOME_PORT: self._esphome_port,
                CONF_ESPHOME_NOISE_PSK: self._esphome_noise_psk,
                CONF_DEVICE_NAME: self._device_name,
                CONF_PASSCODE: self._passcode,
            },
        )
