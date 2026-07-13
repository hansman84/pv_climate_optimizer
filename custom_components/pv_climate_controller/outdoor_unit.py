"""Verified limits of the installed Hisense multi-split outdoor unit."""

from __future__ import annotations

from dataclasses import dataclass

BTU_H_PER_KW = 3412.142


@dataclass(frozen=True, slots=True)
class OutdoorUnitSpec:
    """Limits used for planning, never for direct device control."""

    model: str
    max_indoor_units: int
    nominal_cooling_kw: float
    min_cooling_kw: float
    max_cooling_kw: float
    cooling_outdoor_min_c: float
    cooling_outdoor_max_c: float

    @property
    def nominal_cooling_btu_h(self) -> float:
        return self.nominal_cooling_kw * BTU_H_PER_KW

    @property
    def max_cooling_btu_h(self) -> float:
        return self.max_cooling_kw * BTU_H_PER_KW


HISENSE_5AMW125U4RTA = OutdoorUnitSpec(
    model="5AMW125U4RTA",
    max_indoor_units=5,
    nominal_cooling_kw=12.5,
    min_cooling_kw=3.8,
    max_cooling_kw=15.3,
    cooling_outdoor_min_c=-15,
    cooling_outdoor_max_c=48,
)
