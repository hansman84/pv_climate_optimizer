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

    @property
    def signature(self) -> tuple[str, str, str | float | None]:
        return (self.entity_id, self.action, self.value)


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
        global_interval_s: float = 30.0,
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
        self._last_signature: dict[str, tuple[str, str, str | float | None]] = {}
        self._backoff_until: dict[str, float] = {}
        self._pending: dict[str, tuple[tuple[str, str, str | float | None], float]] = {}
        self._manual_override_until: dict[str, float] = {}

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def is_manual_override(self, entity_id: str) -> bool:
        return self._manual_override_until.get(entity_id, 0.0) > self._clock()

    def observe_external_change(self, command: Command, *, override_duration_s: float = 7200.0) -> bool:
        """Record an override only if the state change is not our pending command."""
        now = self._clock()
        pending = self._pending.get(command.entity_id)
        if pending and pending[0] == command.signature:
            self._pending.pop(command.entity_id, None)
            return False
        self._manual_override_until[command.entity_id] = now + override_duration_s
        return True

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
        if self._last_global_at is not None and now - self._last_global_at < self._global_interval_s:
            return CommandResult("deferred", "Globales Befehlsintervall noch nicht erreicht.")
        if now - self._last_entity_at.get(command.entity_id, float("-inf")) < self._per_entity_interval_s:
            return CommandResult("deferred", "Gerätebezogenes Befehlsintervall noch nicht erreicht.")

        for attempt in (1, 2):
            accepted = await executor(command)
            if accepted:
                sent_at = self._clock()
                self._last_global_at = sent_at
                self._last_entity_at[command.entity_id] = sent_at
                self._last_signature[command.entity_id] = command.signature
                self._pending[command.entity_id] = (command.signature, sent_at)
                return CommandResult("sent", "Befehl angenommen; Cloud-Bestätigung steht aus.", attempt)
        self._backoff_until[command.entity_id] = self._clock() + self._backoff_s
        return CommandResult("failed", "Bestätigung fehlgeschlagen; Backoff aktiv.", 2)
