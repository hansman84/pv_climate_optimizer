"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class ControllerEntity(Entity):
    """Base entity scoped to a config entry."""

    _attr_has_entity_name = True

    def __init__(self, controller, entry_id: str, key: str) -> None:
        self.controller = controller
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "PV Klimaregler",
            "manufacturer": "PV Klimaregler",
            "model": "PV-orientierte Hauskühlung",
            "sw_version": "0.3.4",
        }

    async def async_added_to_hass(self) -> None:
        """Register for in-memory controller changes."""
        await super().async_added_to_hass()
        self.controller.add_state_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister before HA discards this entity."""
        self.controller.remove_state_listener(self.async_write_ha_state)
        await super().async_will_remove_from_hass()

    async def async_persist_option(self, key: str, value: object) -> None:
        """Persist an interactive control via the supported config-entry API."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return
        options = dict(entry.options)
        options[key] = value
        self.hass.config_entries.async_update_entry(entry, options=options)
