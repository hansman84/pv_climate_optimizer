from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

PACKAGE = "pv_climate_controller"
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / PACKAGE


def _load(module: str):
    sys.modules.setdefault(PACKAGE, types.ModuleType(PACKAGE)).__path__ = [str(ROOT)]
    path = ROOT / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


const = _load("const")
models = _load("models")
evaluator = _load("evaluator")
ems_adapter = _load("ems_adapter")
adapter = _load("command_adapter")
controller = _load("controller")
pilot = _load("pilot")
forecasting = _load("forecasting")
diagnostics = _load("diagnostics")
storage = _load("storage")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_shadow_mode_blocks_every_command_request() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=True)

    result = asyncio.run(command_adapter.async_request(adapter.Command("climate.confirmed", "Kühlentscheidung")))

    assert result.status == "shadow"
    assert "blockiert" in result.reason


def test_non_shadow_gate_c_is_still_not_a_write_path() -> None:
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False)

    result = asyncio.run(command_adapter.async_request(adapter.Command("climate.confirmed", "Kühlentscheidung")))

    assert result.status == "blocked"


def test_unavailable_climate_produces_safe_zone_decision() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.confirmed", "sensor.confirmed")

    decision = evaluator.evaluate_zone(zone, models.ZoneInput(temperature_c=26.0, climate_available=False))

    assert decision.state is const.ZoneState.UNAVAILABLE
    assert not decision.requested
    assert decision.reason_code == "climate_unavailable"


def test_controller_never_infers_zone_entities() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})

    assert runtime.config.zone is None
    assert runtime.evaluate(models.ZoneInput(temperature_c=26.0, climate_available=True)) is None


def test_hard_temperature_limit_has_priority() -> None:
    zone = models.ZoneConfig("living", "Wohnzimmer", "climate.confirmed", "sensor.confirmed")

    decision = evaluator.evaluate_zone(zone, models.ZoneInput(temperature_c=26.0, climate_available=True))

    assert decision.requested
    assert decision.score >= 100
    assert decision.reason_code == "hard_temperature_limit"


def test_deduplicates_confirmed_command_and_enforces_global_rate_limit() -> None:
    clock = Clock()
    calls = []

    async def fake_executor(command):
        calls.append(command)
        return True

    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock)
    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "sent"
    assert asyncio.run(command_adapter.async_request(command, fake_executor)).status == "noop"
    other = adapter.Command("climate.other", "set_temperature", 23.0)
    assert asyncio.run(command_adapter.async_request(other, fake_executor)).status == "deferred"
    assert len(calls) == 1


def test_retry_once_then_backoff() -> None:
    clock = Clock()
    calls = []

    async def failing_executor(command):
        calls.append(command)
        return False

    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)
    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    result = asyncio.run(command_adapter.async_request(command, failing_executor))
    assert result.status == "failed"
    assert result.attempts == 2
    assert len(calls) == 2
    assert asyncio.run(command_adapter.async_request(command, failing_executor)).status == "backoff"


def test_external_change_sets_override_but_matching_confirmation_does_not() -> None:
    clock = Clock()
    command_adapter = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)

    async def fake_executor(command):
        return True

    own = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    asyncio.run(command_adapter.async_request(own, fake_executor))
    assert not command_adapter.observe_external_change(own)
    assert command_adapter.observe_external_change(adapter.Command("climate.confirmed", "set_temperature", 25.0))
    assert command_adapter.is_manual_override("climate.confirmed")


def test_restart_snapshot_preserves_override_and_dedupe() -> None:
    clock = Clock()
    original = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)

    async def fake_executor(command):
        return True

    command = adapter.Command("climate.confirmed", "set_temperature", 23.0)
    asyncio.run(original.async_request(command, fake_executor))
    original.observe_external_change(adapter.Command("climate.confirmed", "set_temperature", 24.0))
    restored = adapter.ClimateCommandAdapter(shadow_mode=False, productive_enabled=True, clock=clock, global_interval_s=0, per_entity_interval_s=0)
    restored.restore_state(original.export_state())
    assert restored.is_manual_override("climate.confirmed")
    assert asyncio.run(restored.async_request(command, fake_executor)).status == "manual_override"


def test_ems_missing_or_stale_grant_fails_closed() -> None:
    grant = ems_adapter.parse_grant("2", age_s=301, stale_after_s=300)

    assert grant.stages == 0
    assert not grant.available
    assert grant.reason_code == "ems_grant_stale"


def test_ems_valid_grant_is_read_but_does_not_write() -> None:
    runtime = controller.PVClimateController.from_config({"shadow_mode": True}, {})
    grant = runtime.evaluate_ems("2", 1)

    assert grant.stages == 2
    assert grant.available
    assert asyncio.run(runtime.async_apply_last_decision()).status == "shadow"


def test_living_room_pilot_never_selects_a_different_zone() -> None:
    non_living = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.PV_PREFERRED,
        zone=models.ZoneConfig("configured_zone", "Speis", "climate.confirmed", "sensor.confirmed"),
    )
    living = models.ControllerConfig(
        shadow_mode=False,
        energy_policy=const.EnergyPolicy.PV_PREFERRED,
        zone=models.ZoneConfig("configured_zone", "Wohnzimmer", "climate.confirmed", "sensor.confirmed"),
    )

    assert pilot.living_room_pilot_eligible(non_living, 1) == (False, "pilot_living_room_only")
    assert pilot.living_room_pilot_eligible(living, 0) == (False, "ems_grant_missing")
    assert pilot.living_room_pilot_eligible(living, 1) == (True, "pilot_eligible")


def test_raw_ha_states_are_evaluated_without_a_write() -> None:
    runtime = controller.PVClimateController.from_config(
        {"shadow_mode": True, "climate_entity_id": "climate.confirmed", "temperature_entity_id": "sensor.confirmed"},
        {},
    )
    decision = runtime.evaluate_from_states(temperature_state="26.1", climate_state="off")

    assert decision is not None
    assert decision.demand
    assert runtime.last_ems_grant is not None
    assert runtime.last_ems_grant.stages == 0


def test_forecast_uses_observed_trend() -> None:
    trend = forecasting.temperature_trend_c_per_h([(0, 23.0), (1800, 23.5), (3600, 24.0)])

    assert trend == 1.0
    assert forecasting.predicted_temperature_60m(24.0, trend) == 25.0


def test_diagnostics_redacts_credentials() -> None:
    result = diagnostics.redact({"token": "abc", "nested": {"api_key": "xyz", "temperature": 24}})

    assert result == {"token": "***", "nested": {"api_key": "***", "temperature": 24}}


def test_storage_rejects_unknown_schema() -> None:
    packed = storage.pack({"manual_override_until": {"climate.confirmed": 10}})

    assert storage.unpack(packed)["manual_override_until"]["climate.confirmed"] == 10
    assert storage.unpack({"version": 99, "runtime": {"unsafe": True}}) == {}
