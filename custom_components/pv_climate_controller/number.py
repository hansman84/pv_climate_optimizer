"""Interactive temperature thresholds for Shadow Mode evaluation."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BEDROOM_TARGET_TEMPERATURE, CONF_COMFORT_TEMPERATURE, CONF_HARD_MAX_TEMPERATURE, CONF_HOUSE_ZONES, CONF_MIN_PV_SURPLUS_W, CONF_NO_PV_HOLD_MAX_POWER_W, DOMAIN
from .entity import ControllerEntity
from .controller import serialize_zone_config


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Expose editable temperature thresholds for the configured zone."""
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ComfortTemperatureNumber(controller, entry.entry_id, "comfort_temperature"),
        HardMaxTemperatureNumber(controller, entry.entry_id, "hard_max_temperature"),
        MinPVSurplusNumber(controller, entry.entry_id, "min_pv_surplus"),
        NoPVHoldMaxPowerNumber(controller, entry.entry_id, "no_pv_hold_max_power"),
        BedroomTargetTemperatureNumber(controller, entry.entry_id, "bedroom_target_temperature"),
    ])
    zone_numbers = []
    for index, zone in enumerate(controller.config.house_zones, start=1):
        zone_numbers.extend((
            ZoneComfortTemperatureNumber(controller, entry.entry_id, f"zone_comfort_temperature_{index}", zone.zone_id),
            ZoneHardMaxTemperatureNumber(controller, entry.entry_id, f"zone_hard_max_temperature_{index}", zone.zone_id),
            ZonePilotMinTargetTemperatureNumber(controller, entry.entry_id, f"zone_pilot_min_target_temperature_{index}", zone.zone_id),
            ZonePilotMaxTargetTemperatureNumber(controller, entry.entry_id, f"zone_pilot_max_target_temperature_{index}", zone.zone_id),
            ZoneHardLimitFailsafeOffsetNumber(controller, entry.entry_id, f"zone_hard_limit_failsafe_offset_{index}", zone.zone_id),
            ZonePriorityNumber(controller, entry.entry_id, f"zone_priority_{index}", zone.zone_id),
        ))
    async_add_entities(zone_numbers)


class _TemperatureNumber(ControllerEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 16.0
    _attr_native_max_value = 32.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE


class ComfortTemperatureNumber(_TemperatureNumber):
    _attr_name = "Komforttemperatur"

    @property
    def native_value(self) -> float | None:
        return None if self.controller.config.zone is None else self.controller.config.zone.comfort_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_comfort_temperature(value)
        await self.async_persist_option(CONF_COMFORT_TEMPERATURE, value)
        self.controller.notify_state_listeners()


class HardMaxTemperatureNumber(_TemperatureNumber):
    _attr_name = "Harte Temperaturgrenze"

    @property
    def native_value(self) -> float | None:
        return None if self.controller.config.zone is None else self.controller.config.zone.hard_max_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_hard_max_temperature(value)
        await self.async_persist_option(CONF_HARD_MAX_TEMPERATURE, value)
        self.controller.notify_state_listeners()


class MinPVSurplusNumber(ControllerEntity, NumberEntity):
    """Minimum normalized export power required for the PV diagnostic."""

    _attr_name = "PV-Mindestüberschuss"
    # Zero turns an idle meter into a permanent PV approval.  A positive
    # threshold is a safety invariant for productive climate control.
    _attr_native_min_value = 100.0
    _attr_native_max_value = 20000.0
    _attr_native_step = 100.0
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = NumberDeviceClass.POWER
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float:
        return self.controller.config.min_pv_surplus_w

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_min_pv_surplus_w(value)
        await self.async_persist_option(CONF_MIN_PV_SURPLUS_W, value)
        self.controller.notify_state_listeners()


class NoPVHoldMaxPowerNumber(ControllerEntity, NumberEntity):
    """Maximum shared outdoor-unit power for a deliberate no-PV hold."""

    _attr_name = "Auslauf-Leistungsgrenze ohne PV"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 3000.0
    _attr_native_step = 50.0
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = NumberDeviceClass.POWER
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float:
        return self.controller.config.no_pv_hold_max_power_w

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_no_pv_hold_max_power_w(value)
        await self.async_persist_option(CONF_NO_PV_HOLD_MAX_POWER_W, value)
        self.controller.notify_state_listeners()


class BedroomTargetTemperatureNumber(_TemperatureNumber):
    """Summer target applied only during the scheduled sleeping-room mode."""

    _attr_name = "Schlafraum-Abendzieltemperatur"

    @property
    def native_value(self) -> float:
        return self.controller.config.bedroom_target_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_bedroom_target_temperature(value)
        await self.async_persist_option(CONF_BEDROOM_TARGET_TEMPERATURE, value)
        self.controller.notify_state_listeners()


class _ZoneSettingNumber(ControllerEntity, NumberEntity):
    """Editable per-room planning setting with config-entry persistence."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        return next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)

    @property
    def _zone_name(self) -> str:
        zone = self._zone
        return zone.name if zone is not None else self._zone_id

    async def _async_persist_zones(self) -> None:
        await self.async_persist_option(
            CONF_HOUSE_ZONES,
            [serialize_zone_config(zone) for zone in self.controller.config.house_zones],
        )


class ZoneComfortTemperatureNumber(_ZoneSettingNumber):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 16.0
    _attr_native_max_value = 32.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Komforttemperatur"

    @property
    def native_value(self) -> float | None:
        return None if self._zone is None else self._zone.comfort_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, comfort_temperature=value)
        await self._async_persist_zones()
        self.controller.notify_state_listeners()


class ZoneHardMaxTemperatureNumber(ZoneComfortTemperatureNumber):
    @property
    def name(self) -> str:
        return f"{self._zone_name} – Harte Temperaturgrenze"

    @property
    def native_value(self) -> float | None:
        return None if self._zone is None else self._zone.hard_max_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, hard_max_temperature=value)
        await self._async_persist_zones()
        self.controller.notify_state_listeners()


class _ZonePilotTargetTemperatureNumber(_ZoneSettingNumber):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 16.0
    _attr_native_max_value = 32.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    def _default_minimum(self) -> float:
        return 22.0 if self._zone_name.casefold() in {"schlafzimmer", "kinderzimmer"} else 23.0


class ZonePilotMinTargetTemperatureNumber(_ZonePilotTargetTemperatureNumber):
    @property
    def name(self) -> str:
        return f"{self._zone_name} – Minimale Pilot-Zieltemperatur"

    @property
    def native_value(self) -> float:
        return self._default_minimum() if self._zone is None or self._zone.pilot_min_target_temperature is None else self._zone.pilot_min_target_temperature

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, pilot_min_target_temperature=value)
        await self._async_persist_zones()
        self.controller.notify_state_listeners()


class ZonePilotMaxTargetTemperatureNumber(_ZonePilotTargetTemperatureNumber):
    @property
    def name(self) -> str:
        return f"{self._zone_name} – Maximale Pilot-Zieltemperatur"

    @property
    def native_value(self) -> float:
        if self._zone is not None and self._zone.pilot_max_target_temperature is not None:
            return self._zone.pilot_max_target_temperature
        return 24.0 if self._zone_name.casefold() in {"schlafzimmer", "kinderzimmer"} else 25.0

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, pilot_max_target_temperature=value)
        await self._async_persist_zones()
        self.controller.notify_state_listeners()


class ZoneHardLimitFailsafeOffsetNumber(_ZoneSettingNumber):
    """Room-specific gentle cooling offset used only above the hard limit without PV."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.0
    _attr_native_max_value = 8.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Fail-safe-Aufschlag ohne PV"

    @property
    def native_value(self) -> float:
        return 1.0 if self._zone is None else self._zone.hard_limit_failsafe_offset_c

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, hard_limit_failsafe_offset_c=value)
        await self._async_persist_zones()
        self.controller.notify_state_listeners()


class ZonePriorityNumber(_ZoneSettingNumber):
    _attr_native_min_value = 1.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Komfortpriorität"

    @property
    def native_value(self) -> float | None:
        return None if self._zone is None else self._zone.priority

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_zone_thermal_settings(self._zone_id, priority=int(value))
        await self._async_persist_zones()
        self.controller.notify_state_listeners()
