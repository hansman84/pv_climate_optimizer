"""Read-only Gate C safety switch representation."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_EXPORT_POWER_POSITIVE, CONF_SHADOW_MODE, DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ShadowModeSwitch(controller, entry.entry_id, "shadow_mode"),
        ExportPowerPositiveSwitch(controller, entry.entry_id, "export_power_positive"),
    ])


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


class ExportPowerPositiveSwitch(ControllerEntity, SwitchEntity):
    """Expose the selected net-meter sign convention without changing its source."""

    _attr_name = "Netzeinspeisung positiv"

    @property
    def is_on(self) -> bool:
        return self.controller.config.export_power_positive

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_export_power_positive(True)
        await self.async_persist_option(CONF_EXPORT_POWER_POSITIVE, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_export_power_positive(False)
        await self.async_persist_option(CONF_EXPORT_POWER_POSITIVE, False)
        self.controller.notify_state_listeners()
