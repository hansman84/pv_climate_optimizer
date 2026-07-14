"""Read-only Gate C safety switch representation."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_EXPORT_POWER_POSITIVE, CONF_HOUSE_ZONES, CONF_LIVING_ROOM_PILOT_ENABLED, CONF_SHADOW_MODE, DOMAIN
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ShadowModeSwitch(controller, entry.entry_id, "shadow_mode"),
        LivingRoomPilotSwitch(controller, entry.entry_id, "living_room_pilot"),
        ExportPowerPositiveSwitch(controller, entry.entry_id, "export_power_positive"),
    ]
    entities.extend(
        ClimateTemperatureFallbackSwitch(controller, entry.entry_id, f"temperature_fallback_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    async_add_entities(entities)


class ShadowModeSwitch(ControllerEntity, SwitchEntity):
    _attr_name = "Shadow Mode"

    @property
    def is_on(self) -> bool:
        return self.controller.config.shadow_mode

    async def async_turn_on(self, **kwargs) -> None:
        """Re-enable Shadow Mode; direct climate commands remain hard locked."""
        self.controller.set_shadow_mode(True)
        await self.async_persist_option(CONF_SHADOW_MODE, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        """Leave Shadow Mode; the dedicated pilot switch remains a second gate."""
        self.controller.set_shadow_mode(False)
        await self.async_persist_option(CONF_SHADOW_MODE, False)
        self.controller.notify_state_listeners()


class LivingRoomPilotSwitch(ControllerEntity, SwitchEntity):
    """Explicit productive gate for the confirmed Wohnzimmer pilot only."""

    _attr_name = "Wohnzimmer-Pilot aktiv"

    @property
    def is_on(self) -> bool:
        return self.controller.config.living_room_pilot_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_living_room_pilot_enabled(True)
        await self.async_persist_option(CONF_LIVING_ROOM_PILOT_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_living_room_pilot_enabled(False)
        await self.async_persist_option(CONF_LIVING_ROOM_PILOT_ENABLED, False)
        self.controller.notify_state_listeners()


class ExportPowerPositiveSwitch(ControllerEntity, SwitchEntity):
    """Expose the selected net-meter sign convention without changing its source."""

    _attr_name = "Netzeinspeisung positiv"

    @property
    def is_on(self) -> bool:
        return self.controller.config.export_power_positive

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_export_power_positive(True)
        await self.async_persist_option(CONF_EXPORT_POWER_POSITIVE, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_export_power_positive(False)
        await self.async_persist_option(CONF_EXPORT_POWER_POSITIVE, False)
        self.controller.notify_state_listeners()


class ClimateTemperatureFallbackSwitch(ControllerEntity, SwitchEntity):
    """Opt-in fallback from a failed external room sensor to climate telemetry."""

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        return next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone else self._zone_id} – Klima-Temperatur als Fallback"

    @property
    def is_on(self) -> bool:
        zone = self._zone
        return bool(zone and zone.use_climate_temperature_fallback)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        self.controller.set_zone_temperature_fallback(self._zone_id, enabled)
        zones = [
            {
                "zone_id": zone.zone_id, "name": zone.name,
                "climate_entity_id": zone.climate_entity_id,
                "temperature_entity_id": zone.temperature_entity_id,
                "cooling_power_entity_id": zone.cooling_power_entity_id,
                "comfort_temperature": zone.comfort_temperature,
                "hard_max_temperature": zone.hard_max_temperature,
                "priority": zone.priority,
                "use_climate_temperature_fallback": zone.use_climate_temperature_fallback,
            }
            for zone in self.controller.config.house_zones
        ]
        await self.async_persist_option(CONF_HOUSE_ZONES, zones)
        self.controller.notify_state_listeners()
