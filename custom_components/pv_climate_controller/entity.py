"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class ControllerEntity(Entity):
    """Base entity scoped to a config entry."""

    _attr_has_entity_name = True

    def __init__(self, controller, entry_id: str, key: str) -> None:
        self.controller = controller
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry_id)}, "name": "PV Klimaregler"}
