"""Safety diagnostics."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ShadowModeBinarySensor(hass.data[DOMAIN][entry.entry_id], entry.entry_id)])


class ShadowModeBinarySensor(ControllerEntity, BinarySensorEntity):
    _attr_name = "Shadow Mode aktiv"

    @property
    def is_on(self) -> bool:
        return self.controller.command_adapter.shadow_mode
