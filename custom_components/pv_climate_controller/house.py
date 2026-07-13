"""Pure house-level cooling planning for a shared multi-split outdoor unit."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ZoneDecision
from .outdoor_unit import OutdoorUnitSpec


@dataclass(frozen=True, slots=True)
class ZoneTelemetry:
    """Read-only status of one confirmed indoor unit."""

    zone_id: str
    decision: ZoneDecision
    hvac_mode: str
    delivered_cooling_btu_h: float | None
    priority: int = 50
    name: str = ""
    temperature_c: float | None = None
    climate_available: bool = True


@dataclass(frozen=True, slots=True)
class ZonePlan:
    """A complete read-only outcome for one explicitly configured room."""

    zone_id: str
    name: str
    temperature_c: float | None
    hvac_mode: str
    climate_available: bool
    priority: int
    observed_cooling_btu_h: float | None
    decision: ZoneDecision


@dataclass(frozen=True, slots=True)
class HousePlan:
    """Shadow-only summary of shared outdoor-unit demand."""

    active_zone_count: int
    thermal_demand_count: int
    observed_cooling_btu_h: float
    nominal_budget_btu_h: float
    reason: str
    recommended_zone_ids: tuple[str, ...] = ()
    zones: tuple[ZonePlan, ...] = ()


def build_house_plan(spec: OutdoorUnitSpec, zones: list[ZoneTelemetry]) -> HousePlan:
    """Summarize all confirmed zones without making a control decision."""
    # ``auto`` is deliberately not interpreted as cooling: it may select heat.
    # The integration observes it, but never derives thermal output from it.
    active = [zone for zone in zones if zone.hvac_mode in {"cool", "dry"}]
    demand = [zone for zone in zones if zone.decision.demand]
    delivered = sum(max(0.0, zone.delivered_cooling_btu_h or 0.0) for zone in active)
    ranked = tuple(zone.zone_id for zone in sorted(demand, key=lambda zone: (zone.decision.score, zone.priority), reverse=True))
    if len(zones) > spec.max_indoor_units:
        reason = "Mehr Zonen als Anschlüsse der Außenanlage konfiguriert."
    elif any(zone.hvac_mode == "heat" for zone in zones) and active:
        reason = "Heiz- und Kühlmodus gleichzeitig beobachtet; keine automatische Empfehlung."
    elif delivered > spec.nominal_cooling_btu_h:
        reason = "Beobachtete Kühlleistung über Nennbudget; nur beobachten."
    elif demand:
        reason = "Gemeinsames Nennbudget verfügbar; Priorisierung folgt der Temperaturdringlichkeit."
    else:
        reason = "Kein thermischer Kühlbedarf."
    zone_plans = tuple(
        ZonePlan(
            zone_id=zone.zone_id,
            name=zone.name or zone.zone_id,
            temperature_c=zone.temperature_c,
            hvac_mode=zone.hvac_mode,
            climate_available=zone.climate_available,
            priority=zone.priority,
            observed_cooling_btu_h=zone.delivered_cooling_btu_h,
            decision=zone.decision,
        )
        for zone in zones
    )
    return HousePlan(len(active), len(demand), delivered, spec.nominal_cooling_btu_h, reason, ranked, zone_plans)
