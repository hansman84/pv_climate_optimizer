"""Evaluation button placeholder; it performs no device action."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import ControllerEntity
from .storage import pack


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EvaluateNowButton(controller, entry.entry_id, "evaluate_now"),
        LivingRoomPilotTakeoverButton(controller, entry.entry_id, "living_room_pilot_takeover"),
        OfficePilotTakeoverButton(controller, entry.entry_id, "office_pilot_takeover"),
        SpeisPilotTakeoverButton(controller, entry.entry_id, "speis_pilot_takeover"),
        *(RoomManualTakeoverReleaseButton(controller, entry.entry_id, zone.zone_id, zone.name) for zone in controller.config.house_zones),
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
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))


class LivingRoomPilotTakeoverButton(ControllerEntity, ButtonEntity):
    """One-shot transfer of a running manual cooling session to the PV pilot."""

    _attr_name = "Wohnzimmer-Pilot übernehmen"
    _attr_icon = "mdi:account-arrow-right-outline"

    async def async_press(self) -> None:
        self.controller.request_living_room_pilot_takeover()
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))


class OfficePilotTakeoverButton(ControllerEntity, ButtonEntity):
    """One-shot transfer of an Arbeitszimmer cooling session to the PV pilot."""

    _attr_name = "Arbeitszimmer-Pilot übernehmen"
    _attr_icon = "mdi:account-arrow-right-outline"

    async def async_press(self) -> None:
        self.controller.request_office_pilot_takeover()
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))


class SpeisPilotTakeoverButton(ControllerEntity, ButtonEntity):
    """One-shot transfer of a running Speis cooling session to the PV pilot."""

    _attr_name = "Speis-Pilot übernehmen"
    _attr_icon = "mdi:account-arrow-right-outline"

    async def async_press(self) -> None:
        self.controller.request_speis_pilot_takeover()
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))


class RoomManualTakeoverReleaseButton(ControllerEntity, ButtonEntity):
    """Explicitly return one physical-remote session to the room pilot."""

    _attr_icon = "mdi:robot-happy-outline"

    def __init__(self, controller, entry_id: str, zone_id: str, room_name: str) -> None:
        super().__init__(controller, entry_id, f"{zone_id}_v2_takeover_release")
        self._zone_id = zone_id
        self._attr_name = f"{room_name} wieder an V2 übergeben"

    async def async_press(self) -> None:
        if not self.controller.release_room_manual_takeover(self._zone_id):
            return
        from . import _async_refresh_controller

        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        await _async_refresh_controller(self.hass, self.controller, store)
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))
