"""Safety diagnostics."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ShadowModeBinarySensor(controller, entry.entry_id, "shadow_mode_active"),
        PVSurplusAvailableBinarySensor(controller, entry.entry_id, "pv_surplus_available"),
    ])


class ShadowModeBinarySensor(ControllerEntity, BinarySensorEntity):
    _attr_name = "Shadow Mode aktiv"

    @property
    def is_on(self) -> bool:
        return self.controller.config.shadow_mode


class PVSurplusAvailableBinarySensor(ControllerEntity, BinarySensorEntity):
    """Purely diagnostic indication based on normalized export power."""

    _attr_name = "PV-Überschuss verfügbar"

    @property
    def is_on(self) -> bool:
        export_power = self.controller.last_energy.export_power_w
        return export_power is not None and export_power >= self.controller.config.min_pv_surplus_w

    @property
    def extra_state_attributes(self) -> dict[str, float | str]:
        export_power = self.controller.last_energy.export_power_w
        if export_power is None:
            reason = "Keine gültige Netzeinspeisungsquelle konfiguriert."
        elif export_power < self.controller.config.min_pv_surplus_w:
            reason = "PV-Mindestüberschuss noch nicht erreicht."
        else:
            reason = "PV-Mindestüberschuss erreicht."
        return {"minimum_surplus_w": self.controller.config.min_pv_surplus_w, "reason": reason}
