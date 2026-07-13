"""Diagnostic sensors for Shadow Mode."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, EnergyPolicy
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ControllerStateSensor(controller, entry.entry_id, "controller_state"),
        DecisionReasonSensor(controller, entry.entry_id, "decision_reason"),
        RequestedStagesSensor(controller, entry.entry_id, "requested_stages"),
        GrantedStagesSensor(controller, entry.entry_id, "granted_stages"),
        PVPowerSensor(controller, entry.entry_id, "pv_power"),
        ExportPowerSensor(controller, entry.entry_id, "export_power"),
        PVForecastPowerSensor(controller, entry.entry_id, "pv_forecast_power"),
        EnergyRecommendationSensor(controller, entry.entry_id, "energy_recommendation"),
        HouseCoolingSensor(controller, entry.entry_id, "house_cooling"),
        HousePlanSensor(controller, entry.entry_id, "house_plan"),
    ]
    entities.extend(
        ZonePlanSensor(controller, entry.entry_id, f"zone_plan_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneForecastSensor(controller, entry.entry_id, f"zone_forecast_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    async_add_entities(entities)


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


class EnergyRecommendationSensor(ControllerEntity, SensorEntity):
    """Explain how the selected energy policy would treat the current PV state."""

    _attr_name = "PV-Entscheidung"

    @property
    def native_value(self) -> str:
        decision = self.controller.last_decision
        if decision is None:
            return "Keine Zonenauswertung verfügbar."
        if not decision.demand:
            return "Kein Kühlbedarf."
        export_power = self.controller.last_energy.export_power_w
        surplus = export_power is not None and export_power >= self.controller.config.min_pv_surplus_w
        policy = self.controller.config.energy_policy
        if policy is EnergyPolicy.STRICT_PV:
            return "PV-Freigabe vorhanden; Kühlung wäre zulässig." if surplus else "PV-Freigabe fehlt; würde mit Kühlung warten."
        if policy is EnergyPolicy.COMFORT_FIRST:
            return "Komfort priorisiert; PV-Lage wird mit angezeigt."
        return "PV wird bevorzugt; PV-Freigabe vorhanden." if surplus else "PV wird bevorzugt; noch kein Mindestüberschuss."

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        return {
            "energy_policy": self.controller.config.energy_policy.value,
            "export_power_w": self.controller.last_energy.export_power_w,
            "minimum_surplus_w": self.controller.config.min_pv_surplus_w,
        }


class HouseCoolingSensor(_PowerSensor):
    _attr_name = "Beobachtete Haus-Kühlleistung"

    @property
    def native_value(self) -> float | None:
        plan = self.controller.last_house_plan
        return None if plan is None else round(plan.observed_cooling_btu_h / 3.412142, 3)


class HousePlanSensor(ControllerEntity, SensorEntity):
    _attr_name = "Haus-Kühlplan"

    @property
    def native_value(self) -> str:
        plan = self.controller.last_house_plan
        return "Noch keine Hausauswertung." if plan is None else plan.reason

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        plan = self.controller.last_house_plan
        if plan is None:
            return {}
        return {
            "active_zones": plan.active_zone_count,
            "thermal_demand_zones": plan.thermal_demand_count,
            "nominal_budget_btu_h": round(plan.nominal_budget_btu_h, 1),
            "observed_cooling_btu_h": round(plan.observed_cooling_btu_h, 1),
            "priority_order": list(plan.recommended_zone_ids),
            "energy_permits_cooling": plan.energy_permits_cooling,
            "energy_reason": plan.energy_reason,
            "zones": [_zone_attributes(zone) for zone in plan.zones],
        }


class ZonePlanSensor(ControllerEntity, SensorEntity):
    """One explainable Shadow-Mode result per configured zone."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        plan = self.controller.last_house_plan
        if plan is None:
            return None
        return next((zone for zone in plan.zones if zone.zone_id == self._zone_id), None)

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone else self._zone_id} – Shadow-Plan"

    @property
    def native_value(self) -> str:
        zone = self._zone
        return "Noch keine Zonenauswertung." if zone is None else zone.decision.reason_text

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        zone = self._zone
        return {} if zone is None else _zone_attributes(zone)


class ZoneForecastSensor(ControllerEntity, SensorEntity):
    """One-hour outlook based only on local, observed temperature history."""

    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        plan = self.controller.last_house_plan
        return None if plan is None else next((zone for zone in plan.zones if zone.zone_id == self._zone_id), None)

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone else self._zone_id} – Temperaturprognose"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        return None if zone is None or zone.forecast is None else zone.forecast.predicted_temperature_60m_c

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        zone = self._zone
        if zone is None or zone.forecast is None:
            return {"data_quality": "missing"}
        return {
            "data_quality": zone.forecast.data_quality,
            "trend_c_per_h": zone.forecast.trend_c_per_h,
            "sample_count": zone.forecast.sample_count,
            "horizon": "60m",
        }


def _zone_attributes(zone) -> dict[str, object]:
    """Convert pure plan data to recorder-safe HA attributes."""
    return {
        "zone_id": zone.zone_id,
        "name": zone.name,
        "temperature_c": zone.temperature_c,
        "temperature_source": zone.temperature_source,
        "hvac_mode": zone.hvac_mode,
        "climate_available": zone.climate_available,
        "priority": zone.priority,
        "observed_cooling_btu_h": zone.observed_cooling_btu_h,
        "demand": zone.decision.demand,
        "score": zone.decision.score,
        "state": zone.decision.state.value,
        "reason_code": zone.decision.reason_code,
        "reason": zone.decision.reason_text,
        "forecast_60m_c": None if zone.forecast is None else zone.forecast.predicted_temperature_60m_c,
        "temperature_trend_c_per_h": None if zone.forecast is None else zone.forecast.trend_c_per_h,
        "forecast_sample_count": None if zone.forecast is None else zone.forecast.sample_count,
        "data_quality": None if zone.forecast is None else zone.forecast.data_quality,
    }
