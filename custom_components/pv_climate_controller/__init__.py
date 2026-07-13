"""PV Climate Controller integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .controller import PVClimateController
from .models import ZoneInput
from .storage import pack, unpack

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
    store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.learning")
    controller.restore_learning_state(unpack(await store.async_load()))
    hass.data[DOMAIN].setdefault("_learning_stores", {})[entry.entry_id] = store
    source_entities = _configured_entities(controller)
    if source_entities:
        entry.async_on_unload(async_track_state_change_event(hass, source_entities, _handle_state_change(hass, controller, store)))
    entry.async_on_unload(async_track_time_interval(hass, lambda _: hass.async_create_task(_async_refresh_controller(hass, controller, store)), timedelta(minutes=5)))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await _async_refresh_controller(hass, controller, store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and runtime data."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].get("_learning_stores", {}).pop(entry.entry_id, None)
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
            config.outdoor_temperature_entity_id,
            config.solar_irradiance_entity_id,
            config.sun_entity_id,
            *(entity for house_zone in config.house_zones for entity in (
                house_zone.climate_entity_id,
                house_zone.temperature_entity_id,
                house_zone.cooling_power_entity_id,
                *house_zone.shade_entity_ids,
                *(shade for group in house_zone.facade_shade_entity_ids for shade in group),
            )),
        )
        if entity_id is not None
    ]


def _handle_state_change(hass: HomeAssistant, controller: PVClimateController, store: Store):
    """Build a read-only state listener for selected inputs."""

    @callback
    def _listener(_: Event) -> None:
        hass.async_create_task(_async_refresh_controller(hass, controller, store))

    return _listener


async def _async_refresh_controller(hass: HomeAssistant, controller: PVClimateController, store: Store | None = None) -> None:
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
    outside_state = None if config.outdoor_temperature_entity_id is None else hass.states.get(config.outdoor_temperature_entity_id)
    irradiance_state = None if config.solar_irradiance_entity_id is None else hass.states.get(config.solar_irradiance_entity_id)
    sun_state = None if config.sun_entity_id is None else hass.states.get(config.sun_entity_id)
    outside_temperature = _temperature_value(None if outside_state is None else outside_state.state)
    irradiance = _temperature_value(None if irradiance_state is None else irradiance_state.state)
    sun_azimuth = _temperature_value(None if sun_state is None else sun_state.attributes.get("azimuth"))
    sun_elevation = _temperature_value(None if sun_state is None else sun_state.attributes.get("elevation"))
    house_states = {}
    contexts = {}
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
        direct_sun, shade_open = _sun_and_relevant_shade(
            hass, house_zone.facade_azimuths, house_zone.facade_shade_entity_ids,
            house_zone.shade_entity_ids, house_zone.overhang_cutoff_elevation,
            sun_azimuth, sun_elevation,
        )
        contexts[house_zone.zone_id] = {"outdoor_temperature_c": outside_temperature, "irradiance_w_m2": irradiance, "shade_open_percent": shade_open, "direct_sun": direct_sun}
    controller.evaluate_house(house_states, contexts)
    if store is not None:
        store.async_delay_save(lambda: pack(controller.export_learning_state()), 60)
    controller.notify_state_listeners()


def _temperature_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _direct_sun(facades: tuple[float, ...], cutoff: float | None, azimuth: float | None, elevation: float | None) -> bool:
    """Use configured facade geometry only; an overhang blocks high sun."""
    if azimuth is None or elevation is None or elevation <= 0:
        return False
    if cutoff is not None and elevation >= cutoff:
        return False
    return any(abs(((azimuth - facade + 540) % 360) - 180) <= 90 for facade in facades)


def _sun_and_relevant_shade(
    hass: HomeAssistant,
    facades: tuple[float, ...],
    facade_shades: tuple[tuple[str, ...], ...],
    fallback_shades: tuple[str, ...],
    cutoff: float | None,
    azimuth: float | None,
    elevation: float | None,
) -> tuple[bool, float | None]:
    """Return geometric exposure and the cover state belonging to lit façades.

    Existing zone profiles without façade groups deliberately fall back to their
    previous all-room cover selection, so this change never drops observations.
    """
    if not _direct_sun(facades, cutoff, azimuth, elevation):
        return False, None
    assert azimuth is not None
    active = [index for index, facade in enumerate(facades) if abs(((azimuth - facade + 540) % 360) - 180) <= 90]
    entities = tuple(
        entity
        for index in active
        for entity in (facade_shades[index] if index < len(facade_shades) and facade_shades[index] else fallback_shades)
    )
    positions = [
        _temperature_value(hass.states.get(entity).attributes.get("current_position"))
        for entity in dict.fromkeys(entities)
        if hass.states.get(entity) is not None
    ]
    known = [position for position in positions if position is not None]
    return True, None if not known else sum(known) / len(known)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only this integration after options are saved."""
    await hass.config_entries.async_reload(entry.entry_id)
