"""Config and options flow for PV Climate Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_HARD_MAX_TEMPERATURE, CONF_SHADOW_MODE, CONF_TEMPERATURE_ENTITY_ID, CONF_ZONE_NAME, DEFAULT_NAME, DOMAIN, EnergyPolicy


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build a selector-only schema; users choose existing entities."""
    values = defaults or {}
    schema: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=values.get(CONF_NAME, DEFAULT_NAME)): str,
        vol.Required(CONF_SHADOW_MODE, default=values.get(CONF_SHADOW_MODE, True)): bool,
        vol.Required(CONF_ENERGY_POLICY, default=values.get(CONF_ENERGY_POLICY, EnergyPolicy.PV_PREFERRED)): vol.In([item.value for item in EnergyPolicy]),
        vol.Optional(CONF_ZONE_NAME, default=values.get(CONF_ZONE_NAME, "Wohnzone")): str,
        vol.Optional(CONF_CLIMATE_ENTITY_ID, default=values.get(CONF_CLIMATE_ENTITY_ID)): EntitySelector(EntitySelectorConfig(domain="climate", multiple=False)),
        vol.Optional(CONF_TEMPERATURE_ENTITY_ID, default=values.get(CONF_TEMPERATURE_ENTITY_ID)): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
        vol.Required(CONF_COMFORT_TEMPERATURE, default=values.get(CONF_COMFORT_TEMPERATURE, 24.0)): vol.All(vol.Coerce(float), vol.Range(min=16, max=30)),
        vol.Required(CONF_HARD_MAX_TEMPERATURE, default=values.get(CONF_HARD_MAX_TEMPERATURE, 25.0)): vol.All(vol.Coerce(float), vol.Range(min=16, max=32)),
        vol.Required(CONF_EMS_STALE_AFTER_S, default=values.get(CONF_EMS_STALE_AFTER_S, 300.0)): vol.All(vol.Coerce(float), vol.Range(min=1)),
    }
    ems_key = vol.Optional(CONF_EMS_GRANTED_STAGES_ENTITY_ID)
    if values.get(CONF_EMS_GRANTED_STAGES_ENTITY_ID):
        ems_key = vol.Optional(
            CONF_EMS_GRANTED_STAGES_ENTITY_ID,
            default=values[CONF_EMS_GRANTED_STAGES_ENTITY_ID],
        )
    schema[ems_key] = EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False))
    return vol.Schema(schema)


class PVClimateControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle UI setup."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            await self.async_set_unique_id("pv_climate_controller")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return PVClimateControllerOptionsFlow()


class PVClimateControllerOptionsFlow(config_entries.OptionsFlow):
    """Edit policy and Shadow Mode without mutating entry data."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults))
