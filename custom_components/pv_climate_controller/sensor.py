"""Diagnostic sensors for Shadow Mode."""

from __future__ import annotations

from time import monotonic

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, EnergyPolicy
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ControllerStateSensor(controller, entry.entry_id, "controller_state"),
        DecisionReasonSensor(controller, entry.entry_id, "decision_reason"),
        PilotActionSensor(controller, entry.entry_id, "pilot_action"),
        LivingRoomOutdoorComfortSensor(controller, entry.entry_id, "living_room_outdoor_comfort"),
        BedroomOutdoorComfortSensor(controller, entry.entry_id, "bedroom_outdoor_comfort"),
        OfficePilotActionSensor(controller, entry.entry_id, "office_pilot_action"),
        SpeisPilotActionSensor(controller, entry.entry_id, "speis_pilot_action"),
        RequestedStagesSensor(controller, entry.entry_id, "requested_stages"),
        GrantedStagesSensor(controller, entry.entry_id, "granted_stages"),
        PVPowerSensor(controller, entry.entry_id, "pv_power"),
        ExportPowerSensor(controller, entry.entry_id, "export_power"),
        PVForecastPowerSensor(controller, entry.entry_id, "pv_forecast_power"),
        OutdoorUnitPowerSensor(controller, entry.entry_id, "outdoor_unit_power"),
        OutdoorPowerLearningSensor(controller, entry.entry_id, "outdoor_power_learning"),
        HouseLearningModelSensor(controller, entry.entry_id, "house_learning_model"),
        EnergyRecommendationSensor(controller, entry.entry_id, "energy_recommendation"),
        HouseCoolingSensor(controller, entry.entry_id, "house_cooling"),
        HousePlanSensor(controller, entry.entry_id, "house_plan"),
        V2ShadowDecisionSensor(controller, entry.entry_id, "v2_shadow_house_decision"),
        V2AuthoritySensor(controller, entry.entry_id, "v2_room_authority"),
    ]
    entities.extend(
        BedroomPilotActionSensor(controller, entry.entry_id, f"bedroom_pilot_action_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
        if zone.name.strip().casefold() in {"schlafzimmer", "kinderzimmer"}
    )
    entities.extend(
        ZonePlanSensor(controller, entry.entry_id, f"zone_plan_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneForecastSensor(controller, entry.entry_id, f"zone_forecast_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneTemperatureGradientSensor(controller, entry.entry_id, f"zone_gradient_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneThresholdTimeSensor(controller, entry.entry_id, f"zone_time_to_comfort_{index}", zone.zone_id, "comfort")
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneThresholdTimeSensor(controller, entry.entry_id, f"zone_time_to_hard_limit_{index}", zone.zone_id, "hard_limit")
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneCoolingEffectSensor(controller, entry.entry_id, f"zone_cooling_effect_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZoneThermalProfileSensor(controller, entry.entry_id, f"zone_thermal_profile_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        ZonePowerEstimateSensor(controller, entry.entry_id, f"zone_power_estimate_{index}", zone.zone_id)
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


class V2ShadowDecisionSensor(ControllerEntity, SensorEntity):
    """Expose the active V2 house decision in plain language."""

    _attr_name = "V2-Hausentscheidung"

    def _room_name(self, room_id: str) -> str:
        """Return the configured, human-readable name for a V2 room."""
        return next(
            (zone.name for zone in self.controller.config.house_zones if zone.zone_id == room_id),
            room_id,
        )

    @property
    def native_value(self) -> str:
        if not self.controller.config.v2_shadow_enabled:
            return "V2-Automatik pausiert"
        decision = self.controller.last_v2_house_decision
        if decision is None:
            return "V2 prüft die erste Hauslage"
        if decision.approved_room_ids:
            approved = [
                room
                for room in decision.room_decisions
                if room.room_id in decision.approved_room_ids
            ]
            names = ", ".join(self._room_name(room.room_id) for room in approved)
            plan = self.controller.v2_command_plan_for(approved[0].room_id) if approved else None
            action = plan.action.value if plan is not None else (
                approved[0].selected_action.value if approved and approved[0].selected_action else "adjust"
            )
            action_text = {
                "start": "V2 startet sanft",
                "adjust": "V2 passt sanft an",
                "stop": "V2 beendet die Kühlung",
            }.get(action, "V2 steuert")
            return f"{action_text}: {names}"

        decisions = {item.room_id: item for item in decision.room_decisions}
        candidate_by_room = {
            candidate.policy.room_id: candidate
            for candidate in self.controller.last_v2_candidates
        }
        if any(candidate.reason_code == "vacation_active" for candidate in candidate_by_room.values()):
            return "V2-Automatik pausiert – Urlaubssperre aktiv"
        if any(candidate.reason_code == "cooling_season_inactive" for candidate in candidate_by_room.values()):
            return "V2-Automatik pausiert – Kühlsaison aus"
        blocked = next(
            (
                item for item in decision.room_decisions
                if item.reason_code in {
                    "priority_room_budget_unavailable",
                    "budget_reserved_for_priority_room",
                    "higher_priority_step_active",
                }
            ),
            None,
        )
        if blocked is not None:
            return f"V2 wartet auf Klimareserve: {self._room_name(blocked.room_id)}"
        for reason_code, text in (
            ("critical_input_not_fresh", "V2 wartet auf frische Messwerte"),
            ("budget_estimate_missing", "V2 lernt noch den Leistungsbedarf"),
            ("forecast_insufficient", "V2 sammelt noch genug Prognosedaten"),
        ):
            affected = [
                self._room_name(room_id)
                for room_id, candidate in candidate_by_room.items()
                if candidate.reason_code == reason_code
            ]
            if affected:
                return f"{text}: {', '.join(affected)}"
        if decisions and all(item.reason_code in {"comfort_holding", "pilot_target_floor_reached"} for item in decisions.values()):
            return "Alle Räume liegen im Komfortbereich"
        return "V2 beobachtet die Hauslage"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        decision = self.controller.last_v2_house_decision
        candidates = self.controller.last_v2_candidates
        room_inputs = {
            item.policy.room_id: item
            for item in self.controller.last_v2_room_inputs
        }
        if decision is None:
            return {"enabled": self.controller.config.v2_shadow_enabled, "reason_code": "not_evaluated"}

        def critical_input_issues(room_id: str) -> list[dict[str, object]]:
            room = room_inputs.get(room_id)
            if room is None:
                return []
            return [
                {
                    "entity_id": value.source_entity_id,
                    "quality": value.quality.value,
                    "reason_code": value.reason_code,
                    "age_s": value.age_s,
                }
                for value in room.snapshot.critical_input_issues
            ]

        return {
            "enabled": True,
            "approved_room_ids": list(decision.approved_room_ids),
            "reserved_budget_w": decision.reserved_budget_w,
            "available_budget_w": decision.available_budget_w,
            "candidates": [
                {
                    "room_id": candidate.policy.room_id,
                    "action": candidate.action.value,
                    "reason_code": candidate.reason_code,
                    "confidence": candidate.confidence,
                    "required_budget_w": candidate.required_budget_w,
                    "critical_input_issues": critical_input_issues(candidate.policy.room_id),
                }
                for candidate in candidates
            ],
            "decisions": [
                {
                    "room_id": room.room_id,
                    "state": room.state.value,
                    "reason_code": room.reason_code,
                    "next_review_at": room.next_review_at,
                }
                for room in decision.room_decisions
            ],
        }


class V2AuthoritySensor(ControllerEntity, SensorEntity):
    """Show the ownership state before any future handoff button is enabled."""

    _attr_name = "V2 Raumautorität"

    @property
    def native_value(self) -> str:
        active = [
            zone.name for zone in self.controller.config.house_zones
            if self.controller.v2_authority_for(zone.zone_id).authority.value == "v2_active"
        ]
        return "V2 führt: " + ", ".join(active) if active else "V1 führt alle Räume"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            zone.zone_id: {
                "room_name": zone.name,
                "authority": self.controller.v2_authority_for(zone.zone_id).authority.value,
                "reason_code": self.controller.v2_authority_for(zone.zone_id).reason_code,
                "reason": self.controller.v2_authority_for(zone.zone_id).reason_text,
                "v1_may_write": self.controller.v2_authority_for(zone.zone_id).v1_may_write,
                "v2_may_write": self.controller.v2_authority_for(zone.zone_id).v2_may_write,
                "handoff_ready": self.controller.v2_handoff_readiness(zone.zone_id).ready,
                "handoff_blockers": list(self.controller.v2_handoff_readiness(zone.zone_id).blocker_codes),
                "v2_next_action": None if self.controller.v2_command_plan_for(zone.zone_id) is None else self.controller.v2_command_plan_for(zone.zone_id).action.value,
                "v2_next_target_temperature_c": None if self.controller.v2_command_plan_for(zone.zone_id) is None else self.controller.v2_command_plan_for(zone.zone_id).target_temperature_c,
            }
            for zone in self.controller.config.house_zones
        }


class PilotActionSensor(ControllerEntity, SensorEntity):
    """Expose the productive pilot's own decision separately from Shadow plans."""

    _attr_name = "Wohnzimmer-Pilotentscheidung"

    @property
    def native_value(self) -> str:
        action = self.controller.last_pilot_action
        return "Pilot noch nicht ausgewertet." if action is None else action.reason_text

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        action = self.controller.last_pilot_action
        if action is None:
            return {"action": "none", "reason_code": "not_evaluated", "target_temperature_c": None}
        zone = self.controller.config.zone
        export_power_w = self.controller.last_energy.export_power_w
        minimum_surplus_w = self.controller.config.min_pv_surplus_w
        return {
            "action": action.action,
            "reason_code": action.reason_code,
            "target_temperature_c": action.target_temperature_c,
            "planned_target_temperature_c": action.planned_target_temperature_c,
            "export_power_w": export_power_w,
            "minimum_surplus_w": minimum_surplus_w,
            "outdoor_unit_power_w": self.controller.last_energy.outdoor_unit_power_w,
            "heat_pump_priority_active": self.controller.heat_pump_priority_active,
            "heat_pump_power_w": self.controller.last_energy.heat_pump_power_w,
            "no_pv_hold_max_power_w": self.controller.config.no_pv_hold_max_power_w,
            "pv_surplus_available": export_power_w is not None and export_power_w >= minimum_surplus_w,
            "comfort_temperature_c": None if zone is None else zone.comfort_temperature,
            "hard_temperature_limit_c": None if zone is None else zone.hard_max_temperature,
            "effective_comfort_temperature_c": self.controller.effective_living_room_comfort_temperature,
        }


class LivingRoomOutdoorComfortSensor(ControllerEntity, SensorEntity):
    """Explain the shared outdoor-based comfort relaxation for day rooms."""

    _attr_name = "Wohn- & Arbeitszimmer-Außenkomfort"

    @property
    def native_value(self) -> str:
        return str(self.controller.living_room_outdoor_comfort_status()["state"])

    @property
    def extra_state_attributes(self) -> dict[str, float | int | None]:
        status = self.controller.living_room_outdoor_comfort_status()
        return {key: value for key, value in status.items() if key != "state"}


class BedroomOutdoorComfortSensor(ControllerEntity, SensorEntity):
    """Explain the relaxed sleeping-room evening target."""

    _attr_name = "Schlafräume-Außenkomfort"

    @property
    def native_value(self) -> str:
        return str(self.controller.bedroom_outdoor_comfort_status()["state"])

    @property
    def extra_state_attributes(self) -> dict[str, float | int | None]:
        status = self.controller.bedroom_outdoor_comfort_status()
        return {key: value for key, value in status.items() if key != "state"}


class OfficePilotActionSensor(PilotActionSensor):
    """Expose the productive Arbeitszimmer pilot independently."""

    _attr_name = "Arbeitszimmer-Pilotentscheidung"

    @property
    def native_value(self) -> str:
        action = self.controller.last_office_pilot_action
        return "Pilot noch nicht ausgewertet." if action is None else action.reason_text

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        action = self.controller.last_office_pilot_action
        if action is None:
            return {"action": "none", "reason_code": "not_evaluated", "target_temperature_c": None}
        return {"action": action.action, "reason_code": action.reason_code, "target_temperature_c": action.target_temperature_c}


class SpeisPilotActionSensor(PilotActionSensor):
    """Expose the small-room Speis pilot independently."""

    _attr_name = "Speis-Pilotentscheidung"

    @property
    def native_value(self) -> str:
        action = self.controller.last_speis_pilot_action
        return "Pilot noch nicht ausgewertet." if action is None else action.reason_text

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        action = self.controller.last_speis_pilot_action
        if action is None:
            return {"action": "none", "reason_code": "not_evaluated", "target_temperature_c": None}
        return {"action": action.action, "reason_code": action.reason_code, "target_temperature_c": action.target_temperature_c}


class BedroomPilotActionSensor(PilotActionSensor):
    """Explain the scheduled decision for one explicitly configured sleeping room."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone_name(self) -> str:
        zone = next((item for item in self.controller.config.house_zones if item.zone_id == self._zone_id), None)
        return self._zone_id if zone is None else zone.name

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Schlafraum-Entscheidung"

    @property
    def native_value(self) -> str:
        action = self.controller.last_bedroom_pilot_actions.get(self._zone_id)
        return "Schlafraum-Modus noch nicht ausgewertet." if action is None else action.reason_text

    @property
    def extra_state_attributes(self) -> dict[str, str | float | None]:
        action = self.controller.last_bedroom_pilot_actions.get(self._zone_id)
        if action is None:
            return {"action": "none", "reason_code": "not_evaluated", "target_temperature_c": None}
        return {"action": action.action, "reason_code": action.reason_code, "target_temperature_c": action.target_temperature_c}


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


class OutdoorUnitPowerSensor(_PowerSensor):
    _attr_name = "Außeneinheit – gemessene Leistung"

    @property
    def native_value(self) -> float | None:
        return self.controller.last_energy.outdoor_unit_power_w

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"source_entity_id": self.controller.config.outdoor_unit_power_entity_id}


class OutdoorPowerLearningSensor(ControllerEntity, SensorEntity):
    """Show whether the current operating combination is producing usable data."""

    _attr_name = "Außeneinheit – Lernstatus"

    @property
    def native_value(self) -> str:
        if self.controller.config.outdoor_unit_power_entity_id is None:
            return "Messquelle fehlt"
        status = self.controller.power_learner.status(monotonic())
        count = int(status["active_set_sample_count"])
        needed = int(status["minimum_estimate_samples"])
        if count >= needed:
            return "Stabile Leistungsreihe vorhanden"
        if float(status["stable_for_s"]) < 300:
            return "Warte auf stabile 5 Minuten"
        return f"Sammle Vergleichsproben ({count}/{needed})"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        status = self.controller.power_learner.status(monotonic())
        names = {
            zone.zone_id: zone.name
            for zone in self.controller.config.house_zones
        }
        return {
            **status,
            "active_zone_names": [names.get(zone_id, zone_id) for zone_id in status["active_zone_ids"]],
            "source_entity_id": self.controller.config.outdoor_unit_power_entity_id,
            "meaning": "Eine Probe entsteht nur nach fünf Minuten unveränderter Raumkombination und maximal einmal je fünf Minuten.",
        }


class HouseLearningModelSensor(ControllerEntity, SensorEntity):
    """Expose the persisted model input and resulting power envelopes."""

    _attr_name = "Hausmodell – Lernstand"

    @property
    def native_value(self) -> str:
        count = len(self.controller.house_learning.observations)
        return "Noch keine stabile Beobachtung" if count == 0 else f"{count} stabile Hausbeobachtungen"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "meaning": "Jede Beobachtung enthält aktive Räume, Außenleistung, PV, Einspeisung, Außentemperatur, Solarstrahlung und lokale Stunde.",
            "observation_count": len(self.controller.house_learning.observations),
            "power_envelopes": self.controller.house_learning.summaries(),
            "thermal_profiles": {
                zone_id: {
                    "data_quality": profile.data_quality,
                    "passive_sun_trend_c_per_h": profile.passive_sun_trend_c_per_h,
                    "passive_shaded_trend_c_per_h": profile.passive_shaded_trend_c_per_h,
                    "cooling_trend_c_per_h": profile.cooling_trend_c_per_h,
                }
                for zone_id, profile in self.controller.last_thermal_profiles.items()
            },
        }


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


class _ZoneMetricSensor(ControllerEntity, SensorEntity):
    """Common lookup for a human-readable per-zone planning metric."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        plan = self.controller.last_house_plan
        return None if plan is None else next((zone for zone in plan.zones if zone.zone_id == self._zone_id), None)

    @property
    def _zone_name(self) -> str:
        zone = self._zone
        return zone.name if zone is not None else self._zone_id


class ZoneTemperatureGradientSensor(_ZoneMetricSensor):
    """Observed room temperature change per hour, never a synthetic gradient."""

    _attr_native_unit_of_measurement = "°C/h"

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Temperaturgradient"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        if zone is None or zone.forecast is None:
            return None
        return zone.forecast.trend_c_per_h

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        zone = self._zone
        return {
            "meaning": "Positive Werte bedeuten Erwärmung, negative Werte Abkühlung.",
            "data_quality": None if zone is None or zone.forecast is None else zone.forecast.data_quality,
            "sample_count": None if zone is None or zone.forecast is None else zone.forecast.sample_count,
        }


class ZoneThresholdTimeSensor(_ZoneMetricSensor):
    """Minutes until a zone reaches its configured comfort or hard threshold."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, controller, entry_id: str, key: str, zone_id: str, threshold: str) -> None:
        super().__init__(controller, entry_id, key, zone_id)
        self._threshold = threshold

    @property
    def name(self) -> str:
        label = "Zeit bis Komfortgrenze" if self._threshold == "comfort" else "Zeit bis harter Grenze"
        return f"{self._zone_name} – {label}"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        if zone is None or zone.thermal_budget is None:
            return None
        key = "minutes_to_comfort" if self._threshold == "comfort" else "minutes_to_hard_limit"
        return zone.thermal_budget.get(key)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        zone = self._zone
        if zone is None or zone.thermal_budget is None:
            return {"meaning": "Leer bedeutet: keine belastbare Erwärmung bis zu diesem Grenzwert ermittelt."}
        threshold_key = "comfort_reserve_c" if self._threshold == "comfort" else "hard_limit_reserve_c"
        return {
            "meaning": "0 Minuten bedeutet, dass der Grenzwert bereits erreicht oder überschritten ist.",
            "remaining_degrees_c": zone.thermal_budget.get(threshold_key),
            "priority_bonus": zone.thermal_budget.get("priority_bonus"),
        }


class ZoneCoolingEffectSensor(_ZoneMetricSensor):
    """Observed cooling effect compared with passive warming, in degrees per hour."""

    _attr_native_unit_of_measurement = "°C/h"

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Beobachteter Kühleffekt"

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        if zone is None or zone.thermal_response is None:
            return None
        return zone.thermal_response.observed_cooling_effect_c_per_h

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        zone = self._zone
        response = None if zone is None else zone.thermal_response
        return {
            "meaning": "Gemessener Unterschied zwischen passiver Erwärmung und Kühlbetrieb; erst nach beobachteten Proben verfügbar.",
            "passive_trend_c_per_h": None if response is None else response.passive_trend_c_per_h,
            "cooling_trend_c_per_h": None if response is None else response.cooling_trend_c_per_h,
            "passive_sample_count": None if response is None else response.passive_sample_count,
            "cooling_sample_count": None if response is None else response.cooling_sample_count,
        }


class ZoneThermalProfileSensor(_ZoneMetricSensor):
    """Expose the contextual learning state without inventing a recommendation."""

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Thermisches Lernprofil"

    @property
    def native_value(self) -> str:
        profile = self.controller.last_thermal_profiles.get(self._zone_id)
        return "unconfigured" if profile is None else profile.data_quality

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        profile = self.controller.last_thermal_profiles.get(self._zone_id)
        samples = self.controller._thermal_context_samples.get(self._zone_id, [])
        configured_zone = next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)
        last_sample_age_minutes = None if not samples else round((monotonic() - samples[-1][0]) / 60, 1)
        source_status = {
            "outdoor_temperature": self.controller.config.outdoor_temperature_entity_id is not None,
            "solar_irradiance": self.controller.config.solar_irradiance_entity_id is not None,
            "sun_position": self.controller.config.sun_entity_id is not None,
            "shade_entities": 0 if configured_zone is None else len(configured_zone.shade_entity_ids),
            "facade_azimuths": 0 if configured_zone is None else len(configured_zone.facade_azimuths),
            "facade_shade_groups": 0 if configured_zone is None else sum(bool(group) for group in configured_zone.facade_shade_entity_ids),
        }
        if profile is None:
            return {
                "meaning": "Für dieses Raumprofil fehlen noch kontextuelle Datenquellen oder Beobachtungen.",
                "context_samples_total": len(samples),
                "last_context_sample_age_minutes": last_sample_age_minutes,
                "source_status": source_status,
            }
        return {
            "meaning": "Nur beobachtete Temperaturänderungen in vergleichbaren Zeitintervallen.",
            "passive_sun_trend_c_per_h": profile.passive_sun_trend_c_per_h,
            "passive_shaded_trend_c_per_h": profile.passive_shaded_trend_c_per_h,
            "cooling_trend_c_per_h": profile.cooling_trend_c_per_h,
            "passive_sun_samples": profile.passive_sun_samples,
            "passive_shaded_samples": profile.passive_shaded_samples,
            "cooling_samples": profile.cooling_samples,
            "context_samples_total": len(samples),
            "last_context_sample_age_minutes": last_sample_age_minutes,
            "source_status": source_status,
        }


class ZonePowerEstimateSensor(_PowerSensor):
    """Conservative PV requirement for adding one room to the active set."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def name(self) -> str:
        zone = next((item for item in self.controller.config.house_zones if item.zone_id == self._zone_id), None)
        return f"{self._zone_id if zone is None else zone.name} – gelernter PV-Bedarf"

    @property
    def native_value(self) -> float | None:
        estimate = self.controller.last_power_estimates.get(self._zone_id)
        if estimate is None or estimate.incremental_w is None:
            return None
        return round(self.controller.config.min_pv_surplus_w + estimate.incremental_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        estimate = self.controller.last_power_estimates.get(self._zone_id)
        return {
            "meaning": "Benötigter PV-Überschuss für das zusätzliche Zuschalten dieses Raums; inklusive konfigurierter Reserve.",
            "incremental_outdoor_unit_power_w": None if estimate is None else estimate.incremental_w,
            "pv_reserve_w": self.controller.config.min_pv_surplus_w,
            "sample_count": 0 if estimate is None else estimate.sample_count,
            "data_quality": "outdoor_power_source_missing" if self.controller.config.outdoor_unit_power_entity_id is None else ("insufficient_history" if estimate is None else estimate.data_quality),
            "source_entity_id": self.controller.config.outdoor_unit_power_entity_id,
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
        "strategy": zone.decision.strategy,
        "recommended_target_temperature_c": zone.decision.recommended_target_temperature_c,
        "forecast_60m_c": None if zone.forecast is None else zone.forecast.predicted_temperature_60m_c,
        "temperature_trend_c_per_h": None if zone.forecast is None else zone.forecast.trend_c_per_h,
        "forecast_sample_count": None if zone.forecast is None else zone.forecast.sample_count,
        "data_quality": None if zone.forecast is None else zone.forecast.data_quality,
        "thermal_budget": zone.thermal_budget,
        "thermal_response": None if zone.thermal_response is None else {
            "passive_trend_c_per_h": zone.thermal_response.passive_trend_c_per_h,
            "cooling_trend_c_per_h": zone.thermal_response.cooling_trend_c_per_h,
            "observed_cooling_effect_c_per_h": zone.thermal_response.observed_cooling_effect_c_per_h,
            "passive_sample_count": zone.thermal_response.passive_sample_count,
            "cooling_sample_count": zone.thermal_response.cooling_sample_count,
        },
    }
