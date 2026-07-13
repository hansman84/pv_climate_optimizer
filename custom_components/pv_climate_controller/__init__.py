"""PV Climate Controller integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .controller import PVClimateController
from .models import ZoneInput

PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a controller entry in Shadow Mode by default."""
    controller = PVClimateController.from_config(entry.data, entry.options)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    source_entities = _configured_entities(controller)
    if source_entities:
        entry.async_on_unload(async_track_state_change_event(hass, source_entities, _handle_state_change(hass, controller)))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await _async_refresh_controller(hass, controller)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and runtime data."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


def _configured_entities(controller: PVClimateController) -> list[str]:
    """Return only entity IDs the user explicitly selected in the entry."""
    config = controller.config
    zone = config.zone
    return [
        entity_id
        for entity_id in (
            None if zone is None else zone.temperature_entity_id,
            None if zone is None else zone.climate_entity_id,
            config.ems_granted_stages_entity_id,
            config.pv_power_entity_id,
            config.export_power_entity_id,
            config.pv_forecast_power_entity_id,
            *(entity for house_zone in config.house_zones for entity in (house_zone.climate_entity_id, house_zone.temperature_entity_id, house_zone.cooling_power_entity_id)),
        )
        if entity_id is not None
    ]


def _handle_state_change(hass: HomeAssistant, controller: PVClimateController):
    """Build a read-only state listener for selected inputs."""

    @callback
    def _listener(_: Event) -> None:
        hass.async_create_task(_async_refresh_controller(hass, controller))

    return _listener


async def _async_refresh_controller(hass: HomeAssistant, controller: PVClimateController) -> None:
    """Refresh diagnostics from HA state; no service calls are made."""
    config = controller.config
    zone = config.zone
    temperature = None if zone is None else hass.states.get(zone.temperature_entity_id)
    climate = None if zone is None else hass.states.get(zone.climate_entity_id)
    grant = None if config.ems_granted_stages_entity_id is None else hass.states.get(config.ems_granted_stages_entity_id)
    pv_power = None if config.pv_power_entity_id is None else hass.states.get(config.pv_power_entity_id)
    export_power = None if config.export_power_entity_id is None else hass.states.get(config.export_power_entity_id)
    forecast = None if config.pv_forecast_power_entity_id is None else hass.states.get(config.pv_forecast_power_entity_id)
    controller.evaluate_from_states(
        temperature_state=None if temperature is None else temperature.state,
        climate_state=None if climate is None else climate.state,
        ems_grant_state=None if grant is None else grant.state,
        pv_power_state=None if pv_power is None else pv_power.state,
        pv_power_unit=None if pv_power is None else pv_power.attributes.get("unit_of_measurement"),
        export_power_state=None if export_power is None else export_power.state,
        export_power_unit=None if export_power is None else export_power.attributes.get("unit_of_measurement"),
        pv_forecast_power_state=None if forecast is None else forecast.state,
        pv_forecast_power_unit=None if forecast is None else forecast.attributes.get("unit_of_measurement"),
    )
    house_states = {}
    for house_zone in config.house_zones:
        temperature_state = hass.states.get(house_zone.temperature_entity_id)
        climate_state = hass.states.get(house_zone.climate_entity_id)
        cooling_state = None if house_zone.cooling_power_entity_id is None else hass.states.get(house_zone.cooling_power_entity_id)
        temperature_value = _temperature_value(None if temperature_state is None else temperature_state.state)
        temperature_source = "external_sensor"
        if (
            house_zone.use_climate_temperature_fallback
            and (temperature_value is None or not house_zone.minimum_plausible_temperature_c <= temperature_value <= house_zone.maximum_plausible_temperature_c)
            and climate_state is not None
        ):
            temperature_value = _temperature_value(climate_state.attributes.get("current_temperature"))
            temperature_source = "climate_current_temperature"
        house_states[house_zone.zone_id] = (
            ZoneInput(
                temperature_c=temperature_value,
                climate_available=climate_state is not None and climate_state.state not in {"unknown", "unavailable"},
                temperature_source=temperature_source,
            ),
            "off" if climate_state is None else climate_state.state,
            None if cooling_state is None else cooling_state.state,
        )
    controller.evaluate_house(house_states)
    controller.notify_state_listeners()


def _temperature_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only this integration after options are saved."""
    await hass.config_entries.async_reload(entry.entry_id)
