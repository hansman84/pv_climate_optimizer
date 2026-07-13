"""Redacted diagnostics suitable for export."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


SENSITIVE_KEYS = {"token", "password", "secret", "authorization", "api_key"}


def redact(value: Any) -> Any:
    """Recursively redact values whose keys can contain credentials."""
    if isinstance(value, dict):
        return {key: "***" if key.casefold() in SENSITIVE_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(hass: "HomeAssistant", entry: "ConfigEntry") -> dict[str, Any]:
    """Export configuration and planner state without credentials or writes."""
    controller = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    plan = None if controller is None else controller.last_house_plan
    return redact({
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "runtime": {
            "controller_state": None if controller is None else controller.state.value,
            "last_energy": None if controller is None else {
                "pv_power_w": controller.last_energy.pv_power_w,
                "export_power_w": controller.last_energy.export_power_w,
                "pv_forecast_power_w": controller.last_energy.pv_forecast_power_w,
            },
            "house_plan": None if plan is None else {
                "active_zone_count": plan.active_zone_count,
                "thermal_demand_count": plan.thermal_demand_count,
                "observed_cooling_btu_h": plan.observed_cooling_btu_h,
                "nominal_budget_btu_h": plan.nominal_budget_btu_h,
                "reason": plan.reason,
                "recommended_zone_ids": list(plan.recommended_zone_ids),
            },
        },
    })
