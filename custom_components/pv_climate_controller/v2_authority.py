"""Pure V1/V2 ownership state machine.

This module does not import Home Assistant and has no command path.  Its only
job is to make a future handoff fail closed: at most one controller authority
may request a command for a room, and both are frozen while ownership changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ControlAuthority(StrEnum):
    V1_ACTIVE = "v1_active"
    V2_SHADOW = "v2_shadow"
    HANDOFF_PENDING = "handoff_pending"
    V2_ACTIVE = "v2_active"
    ROLLBACK_PENDING = "rollback_pending"


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    room_id: str
    authority: ControlAuthority
    reason_code: str
    reason_text: str

    @property
    def v1_may_write(self) -> bool:
        return self.authority in {ControlAuthority.V1_ACTIVE, ControlAuthority.V2_SHADOW}

    @property
    def v2_may_write(self) -> bool:
        return self.authority is ControlAuthority.V2_ACTIVE


@dataclass(frozen=True, slots=True)
class HandoffReadiness:
    """Dashboard-safe precondition result; it changes no authority itself."""

    room_id: str
    ready: bool
    blocker_codes: tuple[str, ...]

    @property
    def reason_text(self) -> str:
        if self.ready:
            return "V2-Übergabe ist vorbereitet; der beobachtete Zustand kann ohne Sollwertsprung übernommen werden."
        return "V2-Übergabe gesperrt: " + ", ".join(self.blocker_codes)


class RoomAuthorityRegistry:
    """Persistable room ownership with explicit, no-command handoff states."""

    def __init__(self, states: dict[str, ControlAuthority] | None = None) -> None:
        self._states = dict(states or {})

    def decision_for(self, room_id: str) -> AuthorityDecision:
        state = self._states.get(room_id, ControlAuthority.V1_ACTIVE)
        return AuthorityDecision(room_id, state, state.value, self._reason_text(state))

    def enable_shadow(self, room_id: str) -> AuthorityDecision:
        state = self._states.get(room_id, ControlAuthority.V1_ACTIVE)
        if state not in {ControlAuthority.V1_ACTIVE, ControlAuthority.V2_SHADOW}:
            return self.decision_for(room_id)
        self._states[room_id] = ControlAuthority.V2_SHADOW
        return self.decision_for(room_id)

    def disable_shadow(self, room_id: str) -> AuthorityDecision:
        """Cancel comparison ownership before any handoff starts."""
        if self._states.get(room_id, ControlAuthority.V1_ACTIVE) is ControlAuthority.V2_SHADOW:
            self._states[room_id] = ControlAuthority.V1_ACTIVE
        return self.decision_for(room_id)

    def begin_handoff(self, room_id: str, *, preconditions_met: bool) -> AuthorityDecision:
        state = self._states.get(room_id, ControlAuthority.V1_ACTIVE)
        if state is not ControlAuthority.V2_SHADOW or not preconditions_met:
            return AuthorityDecision(room_id, state, "handoff_preconditions_not_met", "Übergabe bleibt gesperrt: V2 muss im Shadow-Vergleich stabil und vollständig sein.")
        self._states[room_id] = ControlAuthority.HANDOFF_PENDING
        return self.decision_for(room_id)

    def activate_v2(self, room_id: str, *, observed_state_aligned: bool) -> AuthorityDecision:
        if self._states.get(room_id) is not ControlAuthority.HANDOFF_PENDING or not observed_state_aligned:
            return AuthorityDecision(room_id, self._states.get(room_id, ControlAuthority.V1_ACTIVE), "handoff_state_not_aligned", "Übergabe bleibt eingefroren: beobachteter Klima-Zustand ist nicht eindeutig übernommen.")
        self._states[room_id] = ControlAuthority.V2_ACTIVE
        return self.decision_for(room_id)

    def begin_rollback(self, room_id: str) -> AuthorityDecision:
        if self._states.get(room_id) is not ControlAuthority.V2_ACTIVE:
            return self.decision_for(room_id)
        self._states[room_id] = ControlAuthority.ROLLBACK_PENDING
        return self.decision_for(room_id)

    def complete_rollback(self, room_id: str, *, observed_state_aligned: bool) -> AuthorityDecision:
        if self._states.get(room_id) is not ControlAuthority.ROLLBACK_PENDING or not observed_state_aligned:
            return AuthorityDecision(room_id, self._states.get(room_id, ControlAuthority.V1_ACTIVE), "rollback_state_not_aligned", "Rückfall bleibt eingefroren: V1 hat den beobachteten Zustand noch nicht sicher übernommen.")
        self._states[room_id] = ControlAuthority.V1_ACTIVE
        return self.decision_for(room_id)

    def export_state(self) -> dict[str, str]:
        return {room_id: state.value for room_id, state in self._states.items()}

    @classmethod
    def restore(cls, raw: object) -> "RoomAuthorityRegistry":
        if not isinstance(raw, dict):
            return cls()
        states: dict[str, ControlAuthority] = {}
        for room_id, value in raw.items():
            if not isinstance(room_id, str) or not isinstance(value, str):
                continue
            try:
                state = ControlAuthority(value)
            except ValueError:
                continue
            # A process crash while both paths are frozen must remain frozen.
            states[room_id] = state
        return cls(states)

    @staticmethod
    def _reason_text(state: ControlAuthority) -> str:
        return {
            ControlAuthority.V1_ACTIVE: "V1 führt; V2 ist nicht schreibberechtigt.",
            ControlAuthority.V2_SHADOW: "V1 führt; V2 vergleicht ausschließlich im Shadow Mode.",
            ControlAuthority.HANDOFF_PENDING: "Übergabe läuft: V1 und V2 senden keine neuen Raumbefehle.",
            ControlAuthority.V2_ACTIVE: "V2 führt; V1 darf für diesen Raum keine neuen Befehle senden.",
            ControlAuthority.ROLLBACK_PENDING: "Rückfall läuft: V1 und V2 senden keine neuen Raumbefehle.",
        }[state]
