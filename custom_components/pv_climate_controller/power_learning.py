"""Conservative learning from the measured shared outdoor-unit power."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median


@dataclass(frozen=True, slots=True)
class PowerEstimate:
    zone_id: str
    incremental_w: float | None
    sample_count: int
    data_quality: str


@dataclass(slots=True)
class OutdoorPowerLearner:
    """Learn only stable active-set medians; never invent per-room watts."""

    samples: dict[tuple[str, ...], list[float]] = field(default_factory=dict)
    _active_set: tuple[str, ...] | None = None
    _active_set_since: float | None = None
    _last_sample_at: float | None = None

    def observe(self, active_zone_ids: tuple[str, ...], power_w: float | None, now: float) -> bool:
        """Store a sample only after the same operating set ran for five minutes.

        Compressor ramping and the first minutes after a set-point change are not
        representative of a room's incremental electrical demand.
        """
        if power_w is None or power_w < 0 or power_w > 20_000:
            return False
        key = tuple(sorted(active_zone_ids))
        if key != self._active_set:
            self._active_set = key
            self._active_set_since = now
            return False
        if self._active_set_since is None or now - self._active_set_since < 300:
            return False
        if self._last_sample_at is not None and now - self._last_sample_at < 300:
            return False
        values = self.samples.setdefault(key, [])
        values.append(round(power_w, 1))
        self.samples[key] = values[-24:]
        self._last_sample_at = now
        return True

    def estimate(self, zone_id: str, active_zone_ids: tuple[str, ...]) -> PowerEstimate:
        base = tuple(sorted(active_zone_ids))
        with_zone = tuple(sorted((*base, zone_id)))
        base_samples, with_samples = self.samples.get(base, []), self.samples.get(with_zone, [])
        count = min(len(base_samples), len(with_samples))
        if count < 3:
            return PowerEstimate(zone_id, None, count, "insufficient_history")
        incremental = max(0.0, median(with_samples) - median(base_samples))
        # Add 15 % to avoid spending the last watts of export on a noisy estimate.
        return PowerEstimate(zone_id, round(incremental * 1.15, 1), count, "learned")

    def export_state(self) -> dict[str, list[float]]:
        return {"|".join(key): values for key, values in self.samples.items()}

    def restore_state(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        restored: dict[tuple[str, ...], list[float]] = {}
        for key, values in raw.items():
            if not isinstance(key, str) or not isinstance(values, list):
                continue
            valid = [float(value) for value in values if isinstance(value, (int, float)) and 0 <= float(value) <= 20_000]
            if valid:
                restored[tuple(filter(None, key.split("|")))] = valid[-24:]
        self.samples = restored
