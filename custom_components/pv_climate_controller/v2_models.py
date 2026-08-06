"""Pure V2 coordination contracts.

These types intentionally contain no Home Assistant imports and no service
transport.  V2 uses them first in shadow mode while V1 remains productive.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CandidateAction(StrEnum):
    """The only desired room actions a V2 candidate may request."""

    START = "start"
    ADJUST = "adjust"
    HOLD = "hold"
    STOP = "stop"


class DecisionState(StrEnum):
    """A dashboard-visible result for every V2 room candidate."""

    APPROVED_STEP = "approved_step"
    WAITING_FOR_OBSERVATION = "waiting_for_observation"
    BLOCKED_WITH_ESCALATION = "blocked_with_escalation"
    COMFORT_RISK_ALERT = "comfort_risk_alert"
    NOT_REQUESTED = "not_requested"


class InputQuality(StrEnum):
    """Quality gate for an explicitly configured input source."""

    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class InputValue:
    """One normalized, auditable source value.

    The snapshot adapter, rather than a room algorithm, decides quality.  This
    prevents stale or guessed source data from accidentally becoming a normal
    cooling candidate.
    """

    source_entity_id: str | None
    value: float | bool | str | None
    unit: str | None
    age_s: float | None
    quality: InputQuality
    reason_code: str

    def __post_init__(self) -> None:
        if self.age_s is not None and self.age_s < 0:
            raise ValueError("age_s cannot be negative")
        if not self.reason_code.strip():
            raise ValueError("every input needs a reason code")

    @property
    def is_valid(self) -> bool:
        return self.quality is InputQuality.VALID


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """Read-only V2 input boundary; it deliberately contains no HA objects."""

    observed_at: str
    room_temperature: InputValue
    climate_available: InputValue
    pv_export_w: InputValue
    outdoor_unit_power_w: InputValue
    outdoor_temperature: InputValue
    heat_pump_priority: InputValue
    automation_enabled: InputValue
    vacation_active: InputValue
    cooling_season_allowed: InputValue

    def __post_init__(self) -> None:
        try:
            datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from error

    @property
    def critical_inputs_valid(self) -> bool:
        return all(
            value.is_valid
            for value in (
                self.room_temperature,
                self.climate_available,
                self.automation_enabled,
                self.vacation_active,
                self.cooling_season_allowed,
            )
        )

    @property
    def critical_input_issues(self) -> tuple[InputValue, ...]:
        """Return the critical sources that currently prevent a V2 decision."""
        return tuple(
            value
            for value in (
                self.room_temperature,
                self.climate_available,
                self.automation_enabled,
                self.vacation_active,
                self.cooling_season_allowed,
            )
            if not value.is_valid
        )


@dataclass(frozen=True, slots=True)
class RoomEstimate:
    """An explainable thermal estimate, never a climate command."""

    room_id: str
    temperature_c: float | None
    trend_c_per_h: float | None
    predicted_temperature_60m_c: float | None
    confidence: float
    comfort_reserve_c: float | None
    thermal_factors: tuple[str, ...]
    reason_code: str

    def __post_init__(self) -> None:
        if not self.room_id.strip() or not self.reason_code.strip():
            raise ValueError("room_id and reason_code are required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0 and 1")


@dataclass(frozen=True, slots=True)
class V2RoomInput:
    """The explicit inputs required to form one shadow candidate."""

    policy: "RoomPolicy"
    snapshot: InputSnapshot
    estimate: RoomEstimate
    eligibility: EligibilityDecision
    comfort_temperature_c: float
    hard_max_temperature_c: float
    required_budget_w: float | None
    observed_hvac_mode: str | None = None
    observed_target_temperature_c: float | None = None
    pilot_min_target_temperature_c: float | None = None
    pilot_max_target_temperature_c: float | None = None
    target_temperature_step_c: float | None = None
    observed_fan_mode: str | None = None
    supported_fan_modes: tuple[str, ...] = ()
    evening_comfort_active: bool = False
    scheduled_target_temperature_c: float | None = None

    def __post_init__(self) -> None:
        if not 5.0 <= self.comfort_temperature_c <= 50.0:
            raise ValueError("comfort_temperature_c is implausible")
        if self.hard_max_temperature_c < self.comfort_temperature_c:
            raise ValueError("hard maximum cannot be below comfort temperature")
        if self.required_budget_w is not None and self.required_budget_w < 0:
            raise ValueError("required_budget_w cannot be negative")
        if self.pilot_min_target_temperature_c is not None and self.pilot_max_target_temperature_c is not None:
            if self.pilot_min_target_temperature_c > self.pilot_max_target_temperature_c:
                raise ValueError("pilot minimum cannot exceed pilot maximum")
        if self.target_temperature_step_c is not None and self.target_temperature_step_c <= 0:
            raise ValueError("target_temperature_step_c must be positive")
        if self.scheduled_target_temperature_c is not None and not 5.0 <= self.scheduled_target_temperature_c <= 50.0:
            raise ValueError("scheduled target temperature is implausible")


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Central gate result kept separate from room-comfort estimation."""

    allowed: bool
    reason_code: str
    reason_text: str
    next_review_at: str | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip() or not self.reason_text.strip():
            raise ValueError("every eligibility result needs an explainable reason")


@dataclass(frozen=True, slots=True)
class RoomPolicy:
    """Explicit room role and fixed V2 modulation priority.

    A lower ``modulation_priority`` always wins normal modulation budget.
    The coordinator never infers this value from a German room name.
    """

    room_id: str
    display_name: str
    modulation_priority: int

    def __post_init__(self) -> None:
        if not self.room_id.strip():
            raise ValueError("room_id is required")
        if not self.display_name.strip():
            raise ValueError("display_name is required")
        if self.modulation_priority < 1:
            raise ValueError("modulation_priority starts at 1")


@dataclass(frozen=True, slots=True)
class RoomCandidate:
    """An explainable request from a room estimator; never a command."""

    policy: RoomPolicy
    action: CandidateAction
    required_budget_w: float
    comfort_gap_c: float
    confidence: float
    reason_code: str
    reason_text: str
    safety_override: bool = False
    next_review_at: str | None = None
    target_before_c: float | None = None
    target_after_c: float | None = None
    expected_grid_impact_w: float | None = None

    def __post_init__(self) -> None:
        if self.required_budget_w < 0:
            raise ValueError("required_budget_w cannot be negative")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0 and 1")
        if not self.reason_code.strip() or not self.reason_text.strip():
            raise ValueError("every candidate needs an explainable reason")

    @property
    def requests_modulation(self) -> bool:
        """Return whether the house must schedule one explicit control step."""
        return self.action in {CandidateAction.START, CandidateAction.ADJUST, CandidateAction.STOP}


@dataclass(frozen=True, slots=True)
class RoomDecision:
    """One visible response for one input candidate."""

    room_id: str
    state: DecisionState
    reason_code: str
    reason_text: str
    selected_action: CandidateAction | None = None
    next_review_at: str | None = None


@dataclass(frozen=True, slots=True)
class HouseDecision:
    """A pure, recorder-friendly V2 house decision."""

    room_decisions: tuple[RoomDecision, ...]
    approved_room_ids: tuple[str, ...]
    reserved_budget_w: float
    available_budget_w: float


@dataclass(frozen=True, slots=True)
class V2AuditRecord:
    """Secret-free comparison record for Shadow Mode and later handoff review."""

    observed_at: str
    input_fingerprint: str
    room_id: str
    v1_reason_code: str | None
    v2_decision: RoomDecision
    confidence: float
    authority: str
    observed_effect: str | None = None

    def __post_init__(self) -> None:
        if not self.input_fingerprint.strip() or not self.room_id.strip():
            raise ValueError("audit records require input_fingerprint and room_id")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0 and 1")

    def as_dict(self) -> dict[str, Any]:
        """Return recorder/store-safe primitive data without HA state objects."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class V2CommandPlan:
    """A V2 command request, not an executor and not a service call."""

    room_id: str
    action: CandidateAction
    target_temperature_c: float | None
    reason_code: str
    reason_text: str
    fan_mode: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {CandidateAction.START, CandidateAction.ADJUST, CandidateAction.STOP}:
            raise ValueError("V2 command plans require start, adjust, or stop")
        if self.action in {CandidateAction.START, CandidateAction.ADJUST} and self.target_temperature_c is None:
            raise ValueError("start and adjust require a target temperature")
        if not self.room_id.strip() or not self.reason_code.strip() or not self.reason_text.strip():
            raise ValueError("V2 command plans need room_id and an explainable reason")
