"""Button entities for Bluetooth API — Generate Pairing PIN."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .rfcomm_server import RfcommServer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    servers = hass.data.get(DOMAIN, {}).get(entry.entry_id, [])
    rfcomm = next((s for s in servers if isinstance(s, RfcommServer)), None)
    async_add_entities([GeneratePairingPinButton(rfcomm)])


class GeneratePairingPinButton(ButtonEntity):
    _attr_name = "Generate Pairing PIN"
    _attr_unique_id = "bluetooth_api_generate_pairing_pin"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_should_poll = False

    def __init__(self, server: RfcommServer | None) -> None:
        self._server = server

    async def async_press(self) -> None:
        if self._server:
            self._server.generate_pairing_pin()
        else:
            _LOGGER.warning("Generate Pairing PIN: RFCOMM server not available")
