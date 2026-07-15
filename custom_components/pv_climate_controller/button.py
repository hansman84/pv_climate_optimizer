"""Evaluation button placeholder; it performs no device action."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EvaluateNowButton(controller, entry.entry_id, "evaluate_now"),
        LivingRoomPilotTakeoverButton(controller, entry.entry_id, "living_room_pilot_takeover"),
    ])


class EvaluateNowButton(ControllerEntity, ButtonEntity):
    _attr_name = "Jetzt auswerten"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        """Evaluate configured HA states; this button does not send commands."""
        # Reuse the integration refresh so a button press refreshes *all*
        # configured zones, not merely the legacy first-zone fields.
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)


class LivingRoomPilotTakeoverButton(ControllerEntity, ButtonEntity):
    """One-shot transfer of a running manual cooling session to the PV pilot."""

    _attr_name = "Wohnzimmer-Pilot übernehmen"
    _attr_icon = "mdi:account-arrow-right-outline"

    async def async_press(self) -> None:
        self.controller.request_living_room_pilot_takeover()
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)
