"""Policy diagnostic select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, EnergyPolicy
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EnergyPolicySelect(hass.data[DOMAIN][entry.entry_id], entry.entry_id)])


class EnergyPolicySelect(ControllerEntity, SelectEntity):
    _attr_name = "Energiepolitik"
    _attr_options = [item.value for item in EnergyPolicy]
    _attr_available = False

    @property
    def current_option(self) -> str:
        return self.controller.config.energy_policy.value
