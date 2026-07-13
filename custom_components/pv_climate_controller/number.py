"""Interactive temperature thresholds for Shadow Mode evaluation."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_COMFORT_TEMPERATURE, CONF_HARD_MAX_TEMPERATURE, CONF_MIN_PV_SURPLUS_W, DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Expose editable temperature thresholds for the configured zone."""
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ComfortTemperatureNumber(controller, entry.entry_id, "comfort_temperature"),
        HardMaxTemperatureNumber(controller, entry.entry_id, "hard_max_temperature"),
        MinPVSurplusNumber(controller, entry.entry_id, "min_pv_surplus"),
    ])


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
