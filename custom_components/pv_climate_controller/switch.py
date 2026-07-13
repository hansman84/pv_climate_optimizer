"""Read-only Gate C safety switch representation."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ShadowModeSwitch(hass.data[DOMAIN][entry.entry_id], entry.entry_id)])


class ShadowModeSwitch(ControllerEntity, SwitchEntity):
    _attr_name = "Shadow Mode"
    _attr_available = False

    @property
    def is_on(self) -> bool:
        return self.controller.command_adapter.shadow_mode
