"""Diagnostic sensors for Shadow Mode."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ControllerStateSensor(controller, entry.entry_id, "controller_state"),
        DecisionReasonSensor(controller, entry.entry_id, "decision_reason"),
        RequestedStagesSensor(controller, entry.entry_id, "requested_stages"),
        GrantedStagesSensor(controller, entry.entry_id, "granted_stages"),
        PVPowerSensor(controller, entry.entry_id, "pv_power"),
        ExportPowerSensor(controller, entry.entry_id, "export_power"),
        PVForecastPowerSensor(controller, entry.entry_id, "pv_forecast_power"),
    ])


class ControllerStateSensor(ControllerEntity, SensorEntity):
    _attr_name = "Controller-Zustand"

    @property
    def native_value(self) -> str:
        return self.controller.state.value


class DecisionReasonSensor(ControllerEntity, SensorEntity):
    _attr_name = "Entscheidungsgrund"

    @property
    def native_value(self) -> str:
        if self.controller.last_decision is None:
            return "Shadow Mode aktiv; noch keine Zonenauswertung."
        return self.controller.last_decision.reason_text


class RequestedStagesSensor(ControllerEntity, SensorEntity):
    _attr_name = "Angeforderte Klimastufen"

    @property
    def native_value(self) -> int:
        return self.controller.last_requested_stages


class GrantedStagesSensor(ControllerEntity, SensorEntity):
    _attr_name = "Freigegebene Klimastufen"

    @property
    def native_value(self) -> int:
        return 0 if self.controller.last_ems_grant is None else self.controller.last_ems_grant.stages

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        grant = self.controller.last_ems_grant
        if grant is None:
            return {"reason_code": "ems_grant_missing", "reason": "EMS-Freigabe noch nicht ausgewertet."}
        return {"reason_code": grant.reason_code, "reason": grant.reason_text}


class _PowerSensor(ControllerEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT


class PVPowerSensor(_PowerSensor):
    _attr_name = "PV-Leistung"

    @property
    def native_value(self) -> float | None:
        return self.controller.last_energy.pv_power_w

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"source_entity_id": self.controller.config.pv_power_entity_id}


class ExportPowerSensor(_PowerSensor):
    _attr_name = "Netzeinspeisung"

    @property
    def native_value(self) -> float | None:
        return self.controller.last_energy.export_power_w

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        return {
            "source_entity_id": self.controller.config.export_power_entity_id,
            "positive_when_exporting": self.controller.config.export_power_positive,
        }


class PVForecastPowerSensor(_PowerSensor):
    _attr_name = "PV-Prognose"

    @property
    def native_value(self) -> float | None:
        return self.controller.last_energy.pv_forecast_power_w

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"source_entity_id": self.controller.config.pv_forecast_power_entity_id}
