"""Config and options flow for PV Climate Controller."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_CLIMATE_ENTITY_ID, CONF_COMFORT_TEMPERATURE, CONF_EMS_GRANTED_STAGES_ENTITY_ID, CONF_EMS_STALE_AFTER_S, CONF_ENERGY_POLICY, CONF_EXPORT_POWER_ENTITY_ID, CONF_EXPORT_POWER_POSITIVE, CONF_HARD_MAX_TEMPERATURE, CONF_HOUSE_ZONES, CONF_MIN_PV_SURPLUS_W, CONF_PV_FORECAST_POWER_ENTITY_ID, CONF_PV_POWER_ENTITY_ID, CONF_SHADOW_MODE, CONF_TEMPERATURE_ENTITY_ID, CONF_ZONE_NAME, DEFAULT_NAME, DOMAIN, EnergyPolicy


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
        vol.Required(CONF_COMFORT_TEMPERATURE, default=values.get(CONF_COMFORT_TEMPERATURE, 23.5)): vol.All(vol.Coerce(float), vol.Range(min=16, max=30)),
        vol.Required(CONF_HARD_MAX_TEMPERATURE, default=values.get(CONF_HARD_MAX_TEMPERATURE, 25.5)): vol.All(vol.Coerce(float), vol.Range(min=16, max=32)),
        vol.Required(CONF_EMS_STALE_AFTER_S, default=values.get(CONF_EMS_STALE_AFTER_S, 300.0)): vol.All(vol.Coerce(float), vol.Range(min=1)),
    }
    ems_key = vol.Optional(CONF_EMS_GRANTED_STAGES_ENTITY_ID)
    if values.get(CONF_EMS_GRANTED_STAGES_ENTITY_ID):
        ems_key = vol.Optional(
            CONF_EMS_GRANTED_STAGES_ENTITY_ID,
            default=values[CONF_EMS_GRANTED_STAGES_ENTITY_ID],
        )
    schema[ems_key] = EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False))
    for key in (CONF_PV_POWER_ENTITY_ID, CONF_EXPORT_POWER_ENTITY_ID, CONF_PV_FORECAST_POWER_ENTITY_ID):
        selector_key = vol.Optional(key)
        if values.get(key):
            selector_key = vol.Optional(key, default=values[key])
        schema[selector_key] = EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False))
    schema[vol.Required(CONF_EXPORT_POWER_POSITIVE, default=values.get(CONF_EXPORT_POWER_POSITIVE, True))] = bool
    schema[vol.Required(CONF_MIN_PV_SURPLUS_W, default=values.get(CONF_MIN_PV_SURPLUS_W, 1000.0))] = vol.All(vol.Coerce(float), vol.Range(min=0, max=20000))
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

    _selected_zone_id: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="init", menu_options=["general", "add_zone", "manage_zone"])

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            options = {**self._options(), **user_input}
            return self.async_create_entry(data=options)
        return self.async_show_form(step_id="general", data_schema=_schema(self._options()))

    async def async_step_add_zone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        schema = _zone_schema()
        if user_input is None:
            return self.async_show_form(step_id="add_zone", data_schema=schema)
        options = self._options()
        zones = self._zones(options)
        if any(zone.get("climate_entity_id") == user_input["climate_entity_id"] for zone in zones if isinstance(zone, dict)):
            return self.async_show_form(step_id="add_zone", data_schema=schema, errors={"base": "already_configured"})
        if user_input["hard_max_temperature"] < user_input["comfort_temperature"]:
            return self.async_show_form(step_id="add_zone", data_schema=schema, errors={"base": "invalid_temperature_limits"})
        zones.append({"zone_id": user_input["climate_entity_id"], **user_input})
        options[CONF_HOUSE_ZONES] = zones
        return self.async_create_entry(data=options)

    async def async_step_manage_zone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select an existing explicit room mapping for maintenance."""
        zones = self._zones(self._options())
        if not zones:
            return self.async_abort(reason="no_zones")
        choices = {str(zone["zone_id"]): str(zone["name"]) for zone in zones}
        schema = vol.Schema({vol.Required("zone_id"): vol.In(choices)})
        if user_input is None:
            return self.async_show_form(step_id="manage_zone", data_schema=schema)
        self._selected_zone_id = user_input["zone_id"]
        return await self.async_step_zone_action()

    async def async_step_zone_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="zone_action", menu_options=["edit_zone", "remove_zone"])

    async def async_step_edit_zone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        zone = self._selected_zone()
        if zone is None:
            return self.async_abort(reason="zone_not_found")
        schema = _zone_schema(zone)
        if user_input is None:
            return self.async_show_form(step_id="edit_zone", data_schema=schema)
        if user_input["hard_max_temperature"] < user_input["comfort_temperature"]:
            return self.async_show_form(step_id="edit_zone", data_schema=schema, errors={"base": "invalid_temperature_limits"})
        options = self._options()
        zones = self._zones(options)
        if any(
            item.get("climate_entity_id") == user_input["climate_entity_id"] and item.get("zone_id") != self._selected_zone_id
            for item in zones
        ):
            return self.async_show_form(step_id="edit_zone", data_schema=schema, errors={"base": "already_configured"})
        options[CONF_HOUSE_ZONES] = [
            {"zone_id": item["zone_id"], **user_input} if item.get("zone_id") == self._selected_zone_id else item
            for item in zones
        ]
        return self.async_create_entry(data=options)

    async def async_step_remove_zone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove only the user-selected mapping; climate entities are untouched."""
        zone = self._selected_zone()
        if zone is None:
            return self.async_abort(reason="zone_not_found")
        if user_input is None:
            return self.async_show_form(step_id="remove_zone", data_schema=vol.Schema({vol.Required("confirm", default=False): bool}))
        if not user_input["confirm"]:
            return self.async_abort(reason="remove_cancelled")
        options = self._options()
        options[CONF_HOUSE_ZONES] = [zone for zone in self._zones(options) if zone.get("zone_id") != self._selected_zone_id]
        return self.async_create_entry(data=options)

    def _options(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _zones(self, options: dict[str, Any]) -> list[dict[str, Any]]:
        zones = [dict(zone) for zone in options.get(CONF_HOUSE_ZONES, []) if isinstance(zone, dict)]
        if not zones and isinstance(options.get(CONF_CLIMATE_ENTITY_ID), str) and isinstance(options.get(CONF_TEMPERATURE_ENTITY_ID), str):
            zones.append({
                "zone_id": "configured_zone", "name": options.get(CONF_ZONE_NAME, "Wohnzone"),
                "climate_entity_id": options[CONF_CLIMATE_ENTITY_ID],
                "temperature_entity_id": options[CONF_TEMPERATURE_ENTITY_ID],
                "comfort_temperature": options.get(CONF_COMFORT_TEMPERATURE, 23.5),
                "hard_max_temperature": options.get(CONF_HARD_MAX_TEMPERATURE, 25.5),
                "priority": 50,
                "use_climate_temperature_fallback": False,
            })
        return zones

    def _selected_zone(self) -> dict[str, Any] | None:
        return next((zone for zone in self._zones(self._options()) if zone.get("zone_id") == self._selected_zone_id), None)


def _zone_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Schema shared by add and edit; all IDs must be explicitly chosen."""
    values = defaults or {}
    return vol.Schema({
        vol.Required("name", default=values.get("name", "")): str,
        vol.Required("climate_entity_id", default=values.get("climate_entity_id")): EntitySelector(EntitySelectorConfig(domain="climate", multiple=False)),
        vol.Required("temperature_entity_id", default=values.get("temperature_entity_id")): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
        vol.Optional("cooling_power_entity_id", default=values.get("cooling_power_entity_id")): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False)),
        vol.Required("comfort_temperature", default=values.get("comfort_temperature", 23.5)): vol.All(vol.Coerce(float), vol.Range(min=16, max=30)),
        vol.Required("hard_max_temperature", default=values.get("hard_max_temperature", 25.5)): vol.All(vol.Coerce(float), vol.Range(min=16, max=32)),
        vol.Required("priority", default=values.get("priority", 50)): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Required("use_climate_temperature_fallback", default=values.get("use_climate_temperature_fallback", False)): bool,
    })
