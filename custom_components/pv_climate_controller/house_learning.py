"""Persisted, explainable observations for the whole-house cooling model."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median


@dataclass(frozen=True, slots=True)
class HouseObservation:
    """One stable five-minute operating point; never a synthetic datapoint."""

    timestamp: float
    local_hour: int
    active_zone_ids: tuple[str, ...]
    outdoor_power_w: float
    pv_power_w: float | None
    export_power_w: float | None
    outdoor_temperature_c: float | None
    irradiance_w_m2: float | None


@dataclass(slots=True)
class HouseLearningModel:
    """A bounded observation store used for transparent rather than opaque learning."""

    observations: list[HouseObservation] = field(default_factory=list)

    def observe(
        self,
        *,
        timestamp: float,
        local_hour: int,
        active_zone_ids: tuple[str, ...],
        outdoor_power_w: float | None,
        pv_power_w: float | None,
        export_power_w: float | None,
        outdoor_temperature_c: float | None,
        irradiance_w_m2: float | None,
    ) -> bool:
        """Store only a validated, already-stable compressor measurement."""
        if outdoor_power_w is None or not 0 <= outdoor_power_w <= 20_000:
            return False
        self.observations.append(HouseObservation(
            timestamp=timestamp,
            local_hour=max(0, min(23, int(local_hour))),
            active_zone_ids=tuple(sorted(active_zone_ids)),
            outdoor_power_w=round(outdoor_power_w, 1),
            pv_power_w=pv_power_w,
            export_power_w=export_power_w,
            outdoor_temperature_c=outdoor_temperature_c,
            irradiance_w_m2=irradiance_w_m2,
        ))
        self.observations = self.observations[-288:]  # 24 h at a five-minute cadence.
        return True

    def summaries(self) -> list[dict[str, object]]:
        """Return interpretable power envelopes per observed active-room set."""
        groups: dict[tuple[str, ...], list[HouseObservation]] = {}
        for item in self.observations:
            groups.setdefault(item.active_zone_ids, []).append(item)
        result: list[dict[str, object]] = []
        for zone_ids, items in groups.items():
            watts = sorted(item.outdoor_power_w for item in items)
            result.append({
                "active_zone_ids": list(zone_ids),
                "sample_count": len(items),
                "median_power_w": round(median(watts), 1),
                "p90_power_w": watts[max(0, round((len(watts) - 1) * 0.9))],
                "local_hours": sorted({item.local_hour for item in items}),
                "latest_outdoor_temperature_c": items[-1].outdoor_temperature_c,
                "latest_irradiance_w_m2": items[-1].irradiance_w_m2,
            })
        return sorted(result, key=lambda item: (-int(item["sample_count"]), str(item["active_zone_ids"])))

    def export_state(self, now: float) -> list[list[object]]:
        """Persist age, not absolute clock timestamps, across a restart."""
        return [
            [round(now - item.timestamp, 3), item.local_hour, list(item.active_zone_ids), item.outdoor_power_w,
             item.pv_power_w, item.export_power_w, item.outdoor_temperature_c, item.irradiance_w_m2]
            for item in self.observations if 0 <= now - item.timestamp <= 86400
        ]

    def restore_state(self, raw: object, now: float) -> None:
        if not isinstance(raw, list):
            return
        restored: list[HouseObservation] = []
        for item in raw:
            if not isinstance(item, list) or len(item) != 8 or not isinstance(item[2], list):
                continue
            try:
                age, hour, power = float(item[0]), int(item[1]), float(item[3])
                optional = [None if value is None else float(value) for value in item[4:]]
            except (TypeError, ValueError):
                continue
            if not 0 <= age <= 86400 or not 0 <= power <= 20_000:
                continue
            zones = tuple(sorted(value for value in item[2] if isinstance(value, str)))
            restored.append(HouseObservation(now - age, max(0, min(23, hour)), zones, power, *optional))
        self.observations = restored[-288:]
