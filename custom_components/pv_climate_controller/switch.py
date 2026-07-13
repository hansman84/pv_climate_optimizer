"""Read-only Gate C safety switch representation."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_SHADOW_MODE, DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ShadowModeSwitch(hass.data[DOMAIN][entry.entry_id], entry.entry_id, "shadow_mode")])


class ShadowModeSwitch(ControllerEntity, SwitchEntity):
    _attr_name = "Shadow Mode"

    @property
    def is_on(self) -> bool:
        return self.controller.config.shadow_mode

    async def async_turn_on(self, **kwargs) -> None:
        """Re-enable Shadow Mode; direct climate commands remain hard locked."""
        self.controller.set_shadow_mode(True)
        await self.async_persist_option(CONF_SHADOW_MODE, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable evaluations; this does not enable productive control."""
        self.controller.set_shadow_mode(False)
        await self.async_persist_option(CONF_SHADOW_MODE, False)
        self.controller.notify_state_listeners()
