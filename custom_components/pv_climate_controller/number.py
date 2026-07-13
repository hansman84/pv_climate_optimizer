"""Interactive temperature thresholds for Shadow Mode evaluation."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_COMFORT_TEMPERATURE, CONF_HARD_MAX_TEMPERATURE, CONF_HOUSE_ZONES, CONF_MIN_PV_SURPLUS_W, DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Expose editable temperature thresholds for the configured zone."""
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ComfortTemperatureNumber(controller, entry.entry_id, "comfort_temperature"),
        HardMaxTemperatureNumber(controller, entry.entry_id, "hard_max_temperature"),
        MinPVSurplusNumber(controller, entry.entry_id, "min_pv_surplus"),
    ])
    zone_numbers = []
    for index, zone in enumerate(controller.config.house_zones, start=1):
        zone_numbers.extend((
            ZoneComfortTemperatureNumber(controller, entry.entry_id, f"zone_comfort_temperature_{index}", zone.zone_id),
            ZoneHardMaxTemperatureNumber(controller, entry.entry_id, f"zone_hard_max_temperature_{index}", zone.zone_id),
            ZonePriorityNumber(controller, entry.entry_id, f"zone_priority_{index}", zone.zone_id),
        ))
    async_add_entities(zone_numbers)


class _TemperatureNumber(ControllerEntity, NumberEntity):
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
    _attr_native_min_value = 0.0
    _attr_native_max_value = 20000.0
    _attr_native_step = 100.0
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = NumberDeviceClass.POWER

    @property
    def native_value(self) -> float:
        return self.controller.config.min_pv_surplus_w

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_min_pv_surplus_w(value)
        await self.async_persist_option(CONF_MIN_PV_SURPLUS_W, value)
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
        await self.async_persist_option(CONF_HOUSE_ZONES, [
            {
                "zone_id": zone.zone_id,
                "name": zone.name,
                "climate_entity_id": zone.climate_entity_id,
                "temperature_entity_id": zone.temperature_entity_id,
                "cooling_power_entity_id": zone.cooling_power_entity_id,
                "comfort_temperature": zone.comfort_temperature,
                "hard_max_temperature": zone.hard_max_temperature,
                "priority": zone.priority,
                "use_climate_temperature_fallback": zone.use_climate_temperature_fallback,
            }
            for zone in self.controller.config.house_zones
        ])


class ZoneComfortTemperatureNumber(_ZoneSettingNumber):
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
