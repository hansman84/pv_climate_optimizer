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


@dataclass(frozen=True, slots=True)
class HousePlan:
    """Shadow-only summary of shared outdoor-unit demand."""

    active_zone_count: int
    thermal_demand_count: int
    observed_cooling_btu_h: float
    nominal_budget_btu_h: float
    reason: str
    recommended_zone_ids: tuple[str, ...] = ()


def build_house_plan(spec: OutdoorUnitSpec, zones: list[ZoneTelemetry]) -> HousePlan:
    """Summarize all confirmed zones without making a control decision."""
    active = [zone for zone in zones if zone.hvac_mode in {"cool", "dry", "auto"}]
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
    return HousePlan(len(active), len(demand), delivered, spec.nominal_cooling_btu_h, reason, ranked)
