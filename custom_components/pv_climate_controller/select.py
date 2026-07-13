"""Policy diagnostic select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENERGY_POLICY, DOMAIN, EnergyPolicy
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EnergyPolicySelect(hass.data[DOMAIN][entry.entry_id], entry.entry_id, "energy_policy")])


class EnergyPolicySelect(ControllerEntity, SelectEntity):
    _attr_name = "Energiepolitik"
    _attr_options = [item.value for item in EnergyPolicy]

    @property
    def current_option(self) -> str:
        return self.controller.config.energy_policy.value

    async def async_select_option(self, option: str) -> None:
        """Persist a policy selection and refresh the device card."""
        self.controller.set_energy_policy(EnergyPolicy(option))
        await self.async_persist_option(CONF_ENERGY_POLICY, option)
        self.controller.notify_state_listeners()
