"""Guarded command adapter.

This module deliberately has no Home Assistant service import. A future
transport supplies the executor only after an explicit production gate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Command:
    """A normalized, deduplicatable desired state."""

    entity_id: str
    action: str
    value: str | float | None = None
    urgent: bool = False
    fan_mode: str | None = None
    batch_window: bool = False

    @property
    def signature(self) -> tuple[str, str, str | float | None, str | None]:
        return (self.entity_id, self.action, self.value, self.fan_mode)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """A recorder-friendly result; no external state is implied by status."""

    status: str
    reason: str
    attempts: int = 0


Executor = Callable[[Command], Awaitable[bool]]
Clock = Callable[[], float]


class ClimateCommandAdapter:
    """The sole future write boundary, fail-closed until production is enabled."""

    def __init__(
        self,
        *,
        shadow_mode: bool = True,
        productive_enabled: bool = False,
        clock: Clock | None = None,
        global_interval_s: float = 60.0,
        per_entity_interval_s: float = 300.0,
        backoff_s: float = 900.0,
    ) -> None:
        self._shadow_mode = shadow_mode
        self._productive_enabled = productive_enabled
        self._clock = clock or __import__("time").monotonic
        self._global_interval_s = global_interval_s
        self._per_entity_interval_s = per_entity_interval_s
        self._backoff_s = backoff_s
        self._last_global_at: float | None = None
        self._last_entity_at: dict[str, float] = {}
        self._last_signature: dict[str, tuple[str, str, str | float | None, str | None]] = {}
        self._backoff_until: dict[str, float] = {}
        self._pending: dict[str, tuple[tuple[str, str, str | float | None, str | None], float]] = {}
        self._manual_override_until: dict[str, float] = {}

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def set_operating_mode(self, *, shadow_mode: bool, productive_enabled: bool) -> None:
        """Update the explicit runtime gate without loosening any rate limits."""
        self._shadow_mode = shadow_mode
        self._productive_enabled = productive_enabled

    def is_manual_override(self, entity_id: str) -> bool:
        return self._manual_override_until.get(entity_id, 0.0) > self._clock()

    def manual_override_remaining_s(self, entity_id: str) -> int:
        """Return the visible remaining room takeover time, rounded up."""
        remaining = self._manual_override_until.get(entity_id, 0.0) - self._clock()
        return max(0, int(remaining + 0.999))

    def clear_manual_override(self, entity_id: str) -> None:
        """Return exactly one room to the controller without touching its state."""
        self._manual_override_until.pop(entity_id, None)

    def handoff_blockers(self, entity_id: str) -> tuple[str, ...]:
        """Return non-destructive reasons why a controller handoff is unsafe."""
        now = self._clock()
        blockers: list[str] = []
        if self._manual_override_until.get(entity_id, 0.0) > now:
            blockers.append("manual_override_active")
        if entity_id in self._pending:
            blockers.append("command_ack_pending")
        if self._backoff_until.get(entity_id, 0.0) > now:
            blockers.append("command_backoff_active")
        return tuple(blockers)

    def confirm_observed_climate_state(
        self,
        entity_id: str,
        *,
        hvac_mode: str | None,
        target_temperature_c: float | None,
    ) -> bool:
        """Clear a pending command only when the device reports its exact result.

        A service acceptance is not a device acknowledgement.  This narrow
        comparison is shared by V1 and V2 so a handoff or rollback never relies
        on an optimistic command send.
        """
        pending = self._pending.get(entity_id)
        if pending is None:
            return False
        _, action, value, _ = pending[0]
        target_matches = isinstance(value, (int, float)) and target_temperature_c is not None and float(value) == target_temperature_c
        acknowledged = (
            (action == "pilot_start" and hvac_mode == "cool" and target_matches)
            or (action == "pilot_adjust" and target_matches)
            or (action == "pilot_stop" and hvac_mode == "off")
        )
        if acknowledged:
            self._pending.pop(entity_id, None)
        return acknowledged

    def observe_external_change(self, command: Command, *, override_duration_s: float = 7200.0) -> bool:
        """Record an override only if the state change is not our pending command."""
        now = self._clock()
        pending = self._pending.get(command.entity_id)
        if pending and pending[0] == command.signature:
            self._pending.pop(command.entity_id, None)
            return False
        self._manual_override_until[command.entity_id] = now + override_duration_s
        return True

    def observe_climate_state(
        self,
        entity_id: str,
        *,
        hvac_mode: str | None,
        target_temperature_c: float | None,
        override_duration_s: float = 7200.0,
    ) -> bool:
        """Adopt a physical or foreign climate change as a timed room takeover.

        Infrared remotes and vendor apps do not carry a Home Assistant user
        context.  Compare their reported state with our pending/confirmed
        command instead of relying on that context.  A short pending grace
        absorbs the device's intermediate state reports during our own
        turn-on/mode/temperature service sequence.
        """
        now = self._clock()

        def matches(signature: tuple[str, str, str | float | None, str | None]) -> bool:
            _, action, value, _ = signature
            target_matches = isinstance(value, (int, float)) and target_temperature_c is not None and float(value) == target_temperature_c
            return (
                (action == "pilot_stop" and hvac_mode == "off")
                or (action == "pilot_start" and hvac_mode == "cool" and target_matches)
                or (action == "pilot_adjust" and target_matches)
            )

        pending = self._pending.get(entity_id)
        if pending is not None:
            signature, sent_at = pending
            if matches(signature):
                self._pending.pop(entity_id, None)
                return False
            if now - sent_at <= 30.0:
                return False
        confirmed = self._last_signature.get(entity_id)
        if confirmed is not None and matches(confirmed):
            return False
        self._pending.pop(entity_id, None)
        self._manual_override_until[entity_id] = now + override_duration_s
        return True

    def invalidate_confirmed_signature(self, command: Command) -> None:
        """Allow a re-send when the device has demonstrably drifted from it.

        A remembered command is not proof that a cloud-controlled climate unit
        still has that value.  Callers may clear only the exact signature after
        comparing the reported device state with the intended setpoint.
        """
        if self._last_signature.get(command.entity_id) == command.signature:
            self._last_signature.pop(command.entity_id, None)
        pending = self._pending.get(command.entity_id)
        if pending is not None and pending[0] == command.signature:
            self._pending.pop(command.entity_id, None)

    def export_state(self) -> dict[str, Any]:
        """Return a serializable, secret-free restart snapshot."""
        return {
            "last_global_at": self._last_global_at,
            "last_entity_at": self._last_entity_at,
            "last_signature": {key: list(value) for key, value in self._last_signature.items()},
            "backoff_until": self._backoff_until,
            "manual_override_until": self._manual_override_until,
        }

    def restore_state(self, saved: dict[str, Any]) -> None:
        """Restore only safe timestamps and signatures after a restart."""
        self._last_global_at = saved.get("last_global_at")
        self._last_entity_at = dict(saved.get("last_entity_at", {}))
        self._last_signature = {key: tuple(value) for key, value in saved.get("last_signature", {}).items()}
        self._backoff_until = dict(saved.get("backoff_until", {}))
        self._manual_override_until = dict(saved.get("manual_override_until", {}))

    async def async_request(self, command: Command, executor: Executor | None = None) -> CommandResult:
        """Deduplicate and guard a command; retries exactly once only when enabled."""
        now = self._clock()
        if self._shadow_mode:
            return CommandResult("shadow", f"Shadow Mode blockiert {command.action} für {command.entity_id}.")
        if not self._productive_enabled:
            return CommandResult("blocked", "Produktivmodus ist nicht freigegeben.")
        if executor is None:
            return CommandResult("blocked", "Kein freigegebener Transport vorhanden.")
        if self.is_manual_override(command.entity_id):
            return CommandResult("manual_override", "Manueller Override ist aktiv.")
        if self._backoff_until.get(command.entity_id, 0.0) > now:
            return CommandResult("backoff", "Gerät befindet sich nach Fehler in Backoff.")
        if self._last_signature.get(command.entity_id) == command.signature:
            return CommandResult("noop", "Identischer bestätigter Befehl wird nicht wiederholt.")
        if command.entity_id in self._pending:
            return CommandResult("deferred", "Gerätebestätigung für den vorherigen Befehl steht noch aus.")
        if not command.batch_window and self._last_global_at is not None and now - self._last_global_at < self._global_interval_s:
            return CommandResult("deferred", "Globales Befehlsintervall noch nicht erreicht.")
        if not command.urgent and now - self._last_entity_at.get(command.entity_id, float("-inf")) < self._per_entity_interval_s:
            return CommandResult("deferred", "Gerätebezogenes Befehlsintervall noch nicht erreicht.")

        # Reserve the command *before* calling Home Assistant.  A climate
        # service call may synchronously emit state callbacks which re-enter
        # this adapter; without this reservation they could issue another V2
        # step between turn_on, mode selection and set_temperature.
        previous_global_at = self._last_global_at
        previous_entity_at = self._last_entity_at.get(command.entity_id)
        self._last_global_at = now
        self._last_entity_at[command.entity_id] = now
        self._pending[command.entity_id] = (command.signature, now)
        for attempt in (1, 2):
            accepted = await executor(command)
            if accepted:
                sent_at = self._clock()
                self._last_global_at = sent_at
                self._last_entity_at[command.entity_id] = sent_at
                self._last_signature[command.entity_id] = command.signature
                self._pending[command.entity_id] = (command.signature, sent_at)
                return CommandResult("sent", "Befehl angenommen; Cloud-Bestätigung steht aus.", attempt)
        if self._pending.get(command.entity_id, (None,))[0] == command.signature:
            self._pending.pop(command.entity_id, None)
        self._last_global_at = previous_global_at
        if previous_entity_at is None:
            self._last_entity_at.pop(command.entity_id, None)
        else:
            self._last_entity_at[command.entity_id] = previous_entity_at
        self._backoff_until[command.entity_id] = self._clock() + self._backoff_s
        return CommandResult("failed", "Bestätigung fehlgeschlagen; Backoff aktiv.", 2)
