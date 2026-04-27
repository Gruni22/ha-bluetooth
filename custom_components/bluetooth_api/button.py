"""Button entities for the Bluetooth API integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ADAPTER_MODE_ESP32, CONF_ADAPTER_MODE, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # OTA is only meaningful for ESP32-via-USB-Serial
    if entry.data.get(CONF_ADAPTER_MODE) == ADAPTER_MODE_ESP32:
        server = hass.data[DOMAIN][entry.entry_id][0]
        async_add_entities([EnableOtaButton(server, entry.entry_id)])


class EnableOtaButton(ButtonEntity):
    _attr_name = "Enable OTA"
    _attr_icon = "mdi:update"
    _attr_has_entity_name = True

    def __init__(self, server, entry_id: str) -> None:
        self._server = server
        self._attr_unique_id = f"{entry_id}_enable_ota"

    async def async_press(self) -> None:
        await self._server.enable_ota()
