"""Evaluation button placeholder; it performs no device action."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EvaluateNowButton(hass.data[DOMAIN][entry.entry_id], entry.entry_id, "evaluate_now")])


class EvaluateNowButton(ControllerEntity, ButtonEntity):
    _attr_name = "Jetzt auswerten"

    async def async_press(self) -> None:
        """Evaluate configured HA states; this button does not send commands."""
        zone = self.controller.config.zone
        if zone is None:
            return
        temperature = self.hass.states.get(zone.temperature_entity_id)
        climate = self.hass.states.get(zone.climate_entity_id)
        grant = None
        if self.controller.config.ems_granted_stages_entity_id:
            grant = self.hass.states.get(self.controller.config.ems_granted_stages_entity_id)
        self.controller.evaluate_from_states(
            temperature_state=None if temperature is None else temperature.state,
            climate_state=None if climate is None else climate.state,
            ems_grant_state=None if grant is None else grant.state,
            ems_grant_age_s=None,
        )
        self.async_write_ha_state()
