"""REST API views for the Bluetooth API integration."""

from __future__ import annotations

import io
import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_PASSCODE, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _passcode_display(passcode: int) -> str:
    hex_str = f"{passcode:08X}"
    return f"{hex_str[:4]}-{hex_str[4:]}"


class BluetoothApiSetupQrView(HomeAssistantView):
    """GET /api/bluetooth_api/setup_qr/{entry_id} – returns a PNG QR-Code.

    Also accepts GET /api/bluetooth_api/setup_qr (no entry_id) for backward
    compatibility — serves the first configured entry in that case.

    The QR-Code encodes the passcode as the string "btdashboard:<HEX>" so
    btdashboard can detect and parse it unambiguously.
    """

    url = "/api/bluetooth_api/setup_qr/{entry_id}"
    extra_urls = ["/api/bluetooth_api/setup_qr"]
    name = "api:bluetooth_api:setup_qr"
    requires_auth = False  # User scans from HA UI which may not have a session

    async def get(self, request: web.Request, entry_id: str | None = None) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return web.Response(status=404, text="bluetooth_api not configured")

        if entry_id:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN:
                return web.Response(status=404, text=f"entry '{entry_id}' not found")
        else:
            entry = entries[0]

        passcode: int = entry.data.get(CONF_PASSCODE, 0)
        qr_data = f"btdashboard:{passcode:08X}"

        try:
            import qrcode  # type: ignore[import]
            from qrcode.image.pil import PilImage  # type: ignore[import]

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(qr_data)
            qr.make(fit=True)
            img: PilImage = qr.make_image(fill_color="black", back_color="white")

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()

            return web.Response(
                body=png_bytes,
                content_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        except ImportError:
            _LOGGER.error("qrcode/Pillow not installed — cannot generate QR code")
            return web.Response(status=503, text="qrcode library not available")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("QR generation failed: %s", exc)
            return web.Response(status=500, text=str(exc))


class BluetoothApiStatusView(HomeAssistantView):
    """GET /api/bluetooth_api/status – returns status of all configured instances."""

    url = "/api/bluetooth_api/status"
    name = "api:bluetooth_api:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("bluetooth_api is not configured", status_code=404)

        result = []
        for entry in entries:
            passcode: int = entry.data.get(CONF_PASSCODE, 0)
            servers = hass.data.get(DOMAIN, {}).get(entry.entry_id, [])
            connected = any(getattr(s, "_ble_client_connected", False) for s in servers)
            result.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "adapter_mode": entry.data.get("adapter_mode", "esp32"),
                    "device_name": entry.data.get("device_name", "Homeassistant_Home"),
                    "passcode_display": _passcode_display(passcode),
                    "qr_url": f"/api/bluetooth_api/setup_qr/{entry.entry_id}",
                    "client_connected": connected,
                }
            )

        return self.json(result)


# Kept for backward compatibility
class BluetoothApiConfigView(BluetoothApiStatusView):
    url = "/api/bluetooth_api/config"
    name = "api:bluetooth_api:config"
