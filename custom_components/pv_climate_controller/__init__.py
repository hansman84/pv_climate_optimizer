"""PV Climate Controller integration."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .controller import PVClimateController
from .models import ZoneInput
from .storage import pack, unpack
from .v2_models import EligibilityDecision, InputQuality, InputSnapshot, InputValue, RoomEstimate, RoomPolicy, V2RoomInput

PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
)
V2_INITIAL_SOURCE_MAX_AGE_S = 600.0


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
    async def _interval_refresh(_: datetime) -> None:
        """Keep control decisions current even when a source only reports."""
        await _async_refresh_controller(hass, controller, store)

    entry.async_on_unload(async_track_time_interval(
        hass,
        _interval_refresh,
        timedelta(minutes=1),
    ))
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
            config.outdoor_unit_power_entity_id,
            config.heat_pump_priority_entity_id,
            config.heat_pump_power_entity_id,
            config.outdoor_temperature_entity_id or "sensor.aussentemperatur",
            config.solar_irradiance_entity_id,
            config.sun_entity_id,
            config.v2_vacation_entity_id,
            config.v2_cooling_season_entity_id,
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
    def _listener(event: Event) -> None:
        # ConnectLife reports attribute refreshes and delayed cloud confirmations
        # as regular state events.  They must never be mistaken for a person
        # taking over a pilot-owned room.  An HA user context is the only
        # trustworthy signal that a control change came from the dashboard.
        hass.async_create_task(
            _async_refresh_controller(
                hass,
                controller,
                store,
                changed_entity_id=event.data.get("entity_id"),
                user_initiated_change=bool(getattr(event.context, "user_id", None)),
            )
        )

    return _listener


async def _async_refresh_controller(
    hass: HomeAssistant,
    controller: PVClimateController,
    store: Store | None = None,
    changed_entity_id: str | None = None,
    user_initiated_change: bool = False,
) -> None:
    """Refresh diagnostics from HA state; no service calls are made."""
    config = controller.config
    zone = config.zone
    temperature = None if zone is None else hass.states.get(zone.temperature_entity_id)
    climate = None if zone is None else hass.states.get(zone.climate_entity_id)
    grant = None if config.ems_granted_stages_entity_id is None else hass.states.get(config.ems_granted_stages_entity_id)
    pv_power = None if config.pv_power_entity_id is None else hass.states.get(config.pv_power_entity_id)
    export_power = None if config.export_power_entity_id is None else hass.states.get(config.export_power_entity_id)
    forecast = None if config.pv_forecast_power_entity_id is None else hass.states.get(config.pv_forecast_power_entity_id)
    outdoor_power = None if config.outdoor_unit_power_entity_id is None else hass.states.get(config.outdoor_unit_power_entity_id)
    heat_pump_power = None if config.heat_pump_power_entity_id is None else hass.states.get(config.heat_pump_power_entity_id)
    heat_pump_priority = None if config.heat_pump_priority_entity_id is None else hass.states.get(config.heat_pump_priority_entity_id)
    controller.evaluate_from_states(
        temperature_state=None if temperature is None else temperature.state,
        climate_state=None if climate is None else climate.state,
        ems_grant_state=None if grant is None else grant.state,
        ems_grant_age_s=_state_age_s(grant),
        pv_power_state=None if pv_power is None else pv_power.state,
        pv_power_unit=None if pv_power is None else pv_power.attributes.get("unit_of_measurement"),
        export_power_state=None if export_power is None else export_power.state,
        export_power_unit=None if export_power is None else export_power.attributes.get("unit_of_measurement"),
        pv_forecast_power_state=None if forecast is None else forecast.state,
        pv_forecast_power_unit=None if forecast is None else forecast.attributes.get("unit_of_measurement"),
        outdoor_unit_power_state=None if outdoor_power is None else outdoor_power.state,
        outdoor_unit_power_unit=None if outdoor_power is None else outdoor_power.attributes.get("unit_of_measurement"),
        heat_pump_power_state=None if heat_pump_power is None else heat_pump_power.state,
        heat_pump_power_unit=None if heat_pump_power is None else heat_pump_power.attributes.get("unit_of_measurement"),
        heat_pump_priority_state=None if heat_pump_priority is None else heat_pump_priority.state,
    )
    # This installation's agreed outdoor reference.  Keep the options-flow
    # selection authoritative when present, but do not silently disable the
    # comfort profile merely because a legacy entry predates that field.
    outside_temperature_entity_id = config.outdoor_temperature_entity_id or "sensor.aussentemperatur"
    outside_state = hass.states.get(outside_temperature_entity_id)
    irradiance_state = None if config.solar_irradiance_entity_id is None else hass.states.get(config.solar_irradiance_entity_id)
    sun_state = None if config.sun_entity_id is None else hass.states.get(config.sun_entity_id)
    outside_temperature = _temperature_value(None if outside_state is None else outside_state.state)
    irradiance = _temperature_value(None if irradiance_state is None else irradiance_state.state)
    sun_azimuth = _temperature_value(None if sun_state is None else sun_state.attributes.get("azimuth"))
    sun_elevation = _temperature_value(None if sun_state is None else sun_state.attributes.get("elevation"))
    pv_deadline_active = _pv_deadline_active(sun_state)
    house_states = {}
    contexts = {}
    for house_zone in config.house_zones:
        temperature_state = hass.states.get(house_zone.temperature_entity_id)
        climate_state = hass.states.get(house_zone.climate_entity_id)
        cooling_state = None if house_zone.cooling_power_entity_id is None else hass.states.get(house_zone.cooling_power_entity_id)
        temperature_value = _temperature_value(None if temperature_state is None else temperature_state.state)
        temperature_source = "external_sensor"
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
    if config.v2_shadow_enabled:
        controller.evaluate_v2_shadow(
            _v2_room_inputs(hass, controller, house_states),
            # V2 cannot treat total PV as capacity.  Until a V2 house-budget
            # source is configured, only observed positive export is exposed
            # as an upper bound and unknown room power keeps every candidate
            # safely blocked in the runner.
            available_budget_w=max(0.0, controller.last_energy.export_power_w or 0.0),
        )
        # V2 can only reach this shared command boundary after a room-specific
        # handoff.  A failed transport immediately returns that room to V1;
        # no retrying V2 loop or second climate executor is introduced here.
        for house_zone in config.house_zones:
            if not controller.v2_authority_for(house_zone.zone_id).v2_may_write:
                continue
            plan = controller.v2_command_plan_for(house_zone.zone_id)
            if plan is None:
                continue
            result = await controller.async_apply_v2_command(plan, _pilot_service_executor(hass))
            if result.status == "failed":
                controller.failback_v2_to_v1(house_zone.zone_id)
                if store is not None:
                    await store.async_save(pack(controller.export_learning_state()))
    controller.observe_outdoor_power(tuple(
        house_zone.zone_id for house_zone in config.house_zones
        if house_states[house_zone.zone_id][1] in {"cool", "dry"}
    ), {"outdoor_temperature_c": outside_temperature, "irradiance_w_m2": irradiance})
    action = controller.decide_living_room_pilot(
        temperature_c=_temperature_value(None if temperature is None else temperature.state),
        climate_mode=None if climate is None else climate.state,
        climate_target_temperature_c=_temperature_value(None if climate is None else climate.attributes.get("temperature")),
        climate_fan_mode=None if climate is None else climate.attributes.get("fan_mode"),
        climate_swing_mode=None if climate is None else climate.attributes.get("swing_mode"),
        pv_deadline_active=pv_deadline_active,
        manual_change_candidate=(
            controller.config.manual_override_enabled
            and user_initiated_change
            and controller.config.zone is not None
            and changed_entity_id == controller.config.zone.climate_entity_id
        ),
        direct_sun=bool(contexts.get(controller.config.zone.zone_id, {}).get("direct_sun", False)) if controller.config.zone is not None else False,
        irradiance_w_m2=irradiance,
        shade_open_percent=(
            contexts.get(controller.config.zone.zone_id, {}).get("shade_open_percent")
            if controller.config.zone is not None
            else None
        ),
        outdoor_temperature_c=outside_temperature,
    )
    await controller.async_apply_pilot_action(action, _pilot_service_executor(hass))
    office_zone = next((item for item in config.house_zones if item.name.strip().casefold() == "spielzimmer"), None)
    if office_zone is not None:
        office_climate = hass.states.get(office_zone.climate_entity_id)
        office_sample = house_states.get(office_zone.zone_id, (ZoneInput(None, False), "unavailable", None))[0]
        office_action = controller.decide_office_pilot(
            temperature_c=office_sample.temperature_c,
            climate_mode=None if office_climate is None else office_climate.state,
            climate_target_temperature_c=_temperature_value(None if office_climate is None else office_climate.attributes.get("temperature")),
            climate_fan_mode=None if office_climate is None else office_climate.attributes.get("fan_mode"),
            climate_swing_mode=None if office_climate is None else office_climate.attributes.get("swing_mode"),
            pv_deadline_active=pv_deadline_active,
            manual_change_candidate=controller.config.manual_override_enabled and user_initiated_change and changed_entity_id == office_zone.climate_entity_id,
            direct_sun=bool(contexts.get(office_zone.zone_id, {}).get("direct_sun", False)),
            irradiance_w_m2=irradiance,
            shade_open_percent=contexts.get(office_zone.zone_id, {}).get("shade_open_percent"),
            outdoor_temperature_c=outside_temperature,
        )
        await controller.async_apply_pilot_action(office_action, _pilot_service_executor(hass), zone=office_zone, room_pilot=controller.office_pilot)
    speis_zone = next((item for item in config.house_zones if item.name.strip().casefold() == "speis"), None)
    if speis_zone is not None:
        speis_climate = hass.states.get(speis_zone.climate_entity_id)
        speis_sample = house_states.get(speis_zone.zone_id, (ZoneInput(None, False), "unavailable", None))[0]
        speis_action = controller.decide_speis_pilot(
            temperature_c=speis_sample.temperature_c,
            climate_mode=None if speis_climate is None else speis_climate.state,
            climate_target_temperature_c=_temperature_value(None if speis_climate is None else speis_climate.attributes.get("temperature")),
            climate_fan_mode=None if speis_climate is None else speis_climate.attributes.get("fan_mode"),
            climate_swing_mode=None if speis_climate is None else speis_climate.attributes.get("swing_mode"),
            pv_deadline_active=pv_deadline_active,
            manual_change_candidate=controller.config.manual_override_enabled and user_initiated_change and changed_entity_id == speis_zone.climate_entity_id,
            direct_sun=bool(contexts.get(speis_zone.zone_id, {}).get("direct_sun", False)),
            irradiance_w_m2=irradiance,
            shade_open_percent=contexts.get(speis_zone.zone_id, {}).get("shade_open_percent"),
        )
        await controller.async_apply_pilot_action(speis_action, _pilot_service_executor(hass), zone=speis_zone, room_pilot=controller.speis_pilot)
    for bedroom_zone in (item for item in config.house_zones if item.name.strip().casefold() in {"schlafzimmer", "kinderzimmer"}):
        bedroom_climate = hass.states.get(bedroom_zone.climate_entity_id)
        bedroom_sample = house_states.get(bedroom_zone.zone_id, (ZoneInput(None, False), "unavailable", None))[0]
        bedroom_action = controller.decide_bedroom_pilot(
            bedroom_zone,
            temperature_c=bedroom_sample.temperature_c,
            climate_mode=None if bedroom_climate is None else bedroom_climate.state,
            climate_target_temperature_c=_temperature_value(None if bedroom_climate is None else bedroom_climate.attributes.get("temperature")),
            climate_fan_mode=None if bedroom_climate is None else bedroom_climate.attributes.get("fan_mode"),
            climate_swing_mode=None if bedroom_climate is None else bedroom_climate.attributes.get("swing_mode"),
            manual_change_candidate=controller.config.manual_override_enabled and user_initiated_change and changed_entity_id == bedroom_zone.climate_entity_id,
            direct_sun=bool(contexts.get(bedroom_zone.zone_id, {}).get("direct_sun", False)),
            irradiance_w_m2=irradiance,
            shade_open_percent=contexts.get(bedroom_zone.zone_id, {}).get("shade_open_percent"),
            outdoor_temperature_c=outside_temperature,
        )
        await controller.async_apply_pilot_action(
            bedroom_action,
            _pilot_service_executor(hass),
            zone=bedroom_zone,
            room_pilot=controller.bedroom_pilots[bedroom_zone.zone_id],
        )
    if store is not None:
        store.async_delay_save(lambda: pack(controller.export_learning_state()), 60)
    controller.notify_state_listeners()


def _pv_deadline_active(sun_state) -> bool:
    """Start evening PV ownership only during the final 45 minutes of sun."""
    if sun_state is None:
        return False
    if sun_state.state == "below_horizon":
        return False
    next_setting = sun_state.attributes.get("next_setting")
    if not isinstance(next_setting, str):
        return False
    try:
        sunset = datetime.fromisoformat(next_setting.replace("Z", "+00:00"))
    except ValueError:
        return False
    return 0.0 <= (sunset - datetime.now(sunset.tzinfo)).total_seconds() <= 45 * 60


def _pilot_service_executor(hass: HomeAssistant):
    """Build the sole HA service route for the explicitly enabled PoC.

    Hisense requires power before mode and temperature commands, so this order
    is intentional.  Home Assistant validates service availability; a raised
    service error becomes an adapter retry and then a fail-safe backoff.
    """

    async def _execute(command) -> bool:
        try:
            if command.action == "pilot_start":
                if command.value is None:
                    return False
                await hass.services.async_call("climate", "turn_on", {"entity_id": command.entity_id}, blocking=True)
                await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": command.entity_id, "hvac_mode": "cool"}, blocking=True)
                await hass.services.async_call("climate", "set_temperature", {"entity_id": command.entity_id, "temperature": command.value}, blocking=True)
                return True
            if command.action == "pilot_stop":
                await hass.services.async_call("climate", "turn_off", {"entity_id": command.entity_id}, blocking=True)
                return True
            if command.action == "pilot_adjust":
                if command.value is None:
                    return False
                await hass.services.async_call("climate", "set_temperature", {"entity_id": command.entity_id, "temperature": command.value}, blocking=True)
                return True
        except Exception:  # HA service errors must never escape into a command loop.
            return False
        return False

    return _execute


def _temperature_value(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _v2_room_inputs(hass: HomeAssistant, controller: PVClimateController, house_states: dict) -> tuple[V2RoomInput, ...]:
    """Build V2's read-only inputs from the same explicit zone sources as V1.

    Vacation and cooling-season sources deliberately remain missing until their
    selectors are introduced.  The pure V2 gate then reports that gap rather
    than guessing a policy or emitting a normal modulation request.
    """
    now = datetime.now().astimezone().isoformat()
    result: list[V2RoomInput] = []
    for zone in controller.config.house_zones:
        temperature_state = hass.states.get(zone.temperature_entity_id)
        climate_state = hass.states.get(zone.climate_entity_id)
        forecast = controller.last_zone_forecasts.get(zone.zone_id)
        estimate = controller.last_power_estimates.get(zone.zone_id)
        vacation_active = _v2_boolean_input(
            hass.states.get(controller.config.v2_vacation_entity_id) if controller.config.v2_vacation_entity_id else None,
            controller.config.v2_vacation_entity_id,
            true_means="active",
        )
        cooling_season_allowed = _v2_boolean_input(
            hass.states.get(controller.config.v2_cooling_season_entity_id) if controller.config.v2_cooling_season_entity_id else None,
            controller.config.v2_cooling_season_entity_id,
            true_means="allowed",
        )
        if vacation_active.is_valid and vacation_active.value is True:
            eligibility = EligibilityDecision(False, "vacation_active", "V2 Shadow: Automatik ist wegen Abwesenheit gesperrt.")
        elif cooling_season_allowed.is_valid and cooling_season_allowed.value is False:
            eligibility = EligibilityDecision(False, "cooling_season_inactive", "V2 Shadow: automatische Kühlung ist außerhalb der Saison gesperrt.")
        else:
            eligibility = EligibilityDecision(True, "shadow_eligibility_pending", "V2 Shadow bewertet die freigegebenen Quellen.")
        result.append(V2RoomInput(
            policy=RoomPolicy(zone.zone_id, zone.name, zone.modulation_priority),
            snapshot=InputSnapshot(
                observed_at=now,
                room_temperature=_v2_numeric_input(temperature_state, zone.temperature_entity_id, "°C"),
                climate_available=_v2_availability_input(climate_state, zone.climate_entity_id),
                pv_export_w=_v2_numeric_input(
                    hass.states.get(controller.config.export_power_entity_id) if controller.config.export_power_entity_id else None,
                    controller.config.export_power_entity_id,
                    "W",
                ),
                outdoor_unit_power_w=_v2_numeric_input(
                    hass.states.get(controller.config.outdoor_unit_power_entity_id) if controller.config.outdoor_unit_power_entity_id else None,
                    controller.config.outdoor_unit_power_entity_id,
                    "W",
                ),
                outdoor_temperature=_v2_numeric_input(
                    hass.states.get(controller.config.outdoor_temperature_entity_id or "sensor.aussentemperatur"),
                    controller.config.outdoor_temperature_entity_id or "sensor.aussentemperatur",
                    "°C",
                ),
                heat_pump_priority=_v2_availability_input(
                    hass.states.get(controller.config.heat_pump_priority_entity_id) if controller.config.heat_pump_priority_entity_id else None,
                    controller.config.heat_pump_priority_entity_id,
                ),
                automation_enabled=InputValue(None, True, None, 0.0, InputQuality.VALID, "v2_shadow_enabled"),
                vacation_active=vacation_active,
                cooling_season_allowed=cooling_season_allowed,
            ),
            estimate=RoomEstimate(
                room_id=zone.zone_id,
                temperature_c=house_states[zone.zone_id][0].temperature_c,
                trend_c_per_h=None if forecast is None else forecast.trend_c_per_h,
                predicted_temperature_60m_c=None if forecast is None else forecast.predicted_temperature_60m_c,
                confidence=0.0 if forecast is None or forecast.data_quality not in {"valid", "ok"} else 0.5,
                comfort_reserve_c=None if forecast is None or forecast.predicted_temperature_60m_c is None else zone.comfort_temperature - forecast.predicted_temperature_60m_c,
                thermal_factors=(),
                reason_code="v1_forecast_snapshot",
            ),
            eligibility=eligibility,
            comfort_temperature_c=zone.comfort_temperature,
            required_budget_w=None if estimate is None else estimate.incremental_w,
            observed_hvac_mode=None if climate_state is None else climate_state.state,
            observed_target_temperature_c=None if climate_state is None else _temperature_value(climate_state.attributes.get("temperature")),
            pilot_min_target_temperature_c=zone.pilot_min_target_temperature,
            pilot_max_target_temperature_c=zone.pilot_max_target_temperature,
            target_temperature_step_c=(
                None if climate_state is None else _temperature_value(climate_state.attributes.get("target_temp_step"))
            ),
        ))
    return tuple(result)


def _v2_numeric_input(state, entity_id: str | None, unit: str) -> InputValue:
    if state is None:
        return InputValue(entity_id, None, unit, None, InputQuality.MISSING, "source_not_configured_or_missing")
    value = _temperature_value(state.state)
    if value is None:
        return InputValue(entity_id, None, unit, _state_age_s(state), InputQuality.INVALID, "source_not_numeric")
    age_s = _state_age_s(state)
    if age_s is not None and age_s > V2_INITIAL_SOURCE_MAX_AGE_S:
        return InputValue(entity_id, value, unit, age_s, InputQuality.STALE, "source_stale")
    return InputValue(entity_id, value, unit, age_s, InputQuality.VALID, "source_fresh")


def _v2_availability_input(state, entity_id: str | None) -> InputValue:
    if state is None:
        return InputValue(entity_id, None, None, None, InputQuality.MISSING, "source_not_configured_or_missing")
    age_s = _state_age_s(state)
    available = state.state not in {"unknown", "unavailable"}
    if age_s is not None and age_s > V2_INITIAL_SOURCE_MAX_AGE_S:
        return InputValue(entity_id, available, None, age_s, InputQuality.STALE, "source_stale")
    return InputValue(entity_id, available, None, age_s, InputQuality.VALID if available else InputQuality.INVALID, "available" if available else "source_unavailable")


def _v2_boolean_input(state, entity_id: str | None, *, true_means: str) -> InputValue:
    """Normalize only standard HA boolean states; unknown vocabulary fails closed."""
    if state is None:
        return InputValue(entity_id, None, None, None, InputQuality.MISSING, f"{true_means}_source_not_configured")
    age_s = _state_age_s(state)
    if age_s is not None and age_s > V2_INITIAL_SOURCE_MAX_AGE_S:
        return InputValue(entity_id, None, None, age_s, InputQuality.STALE, "source_stale")
    raw = str(state.state).casefold()
    if raw in {"on", "true", "1"}:
        return InputValue(entity_id, True, None, age_s, InputQuality.VALID, true_means)
    if raw in {"off", "false", "0"}:
        return InputValue(entity_id, False, None, age_s, InputQuality.VALID, f"not_{true_means}")
    return InputValue(entity_id, None, None, age_s, InputQuality.INVALID, "source_not_boolean")


def _state_age_s(state) -> float | None:
    """Return the HA update age for a fail-closed EMS grant."""
    if state is None:
        return None
    updated = getattr(state, "last_updated", None)
    if updated is None:
        return None
    return max(0.0, (datetime.now(updated.tzinfo) - updated).total_seconds())


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
