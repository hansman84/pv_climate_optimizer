"""Read-only Gate C safety switch representation."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BEDROOM_CUTOFF_ENABLED, CONF_BEDROOM_MODE_ENABLED, CONF_BEDROOM_QUIET_ENABLED, CONF_EXPORT_POWER_POSITIVE, CONF_HOUSE_ZONES, CONF_LIVING_ROOM_PILOT_ENABLED, CONF_MANUAL_OVERRIDE_ENABLED, CONF_SHADOW_MODE, CONF_V2_SHADOW_ENABLED, DOMAIN
from .controller import serialize_zone_config
from .entity import ControllerEntity
from .storage import pack


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ShadowModeSwitch(controller, entry.entry_id, "shadow_mode"),
        V2ShadowSwitch(controller, entry.entry_id, "v2_shadow"),
        LivingRoomPilotSwitch(controller, entry.entry_id, "living_room_pilot"),
        ManualOverrideSwitch(controller, entry.entry_id, "manual_override"),
        BedroomModeSwitch(controller, entry.entry_id, "bedroom_mode"),
        BedroomCutoffSwitch(controller, entry.entry_id, "child_bedroom_quiet"),
        BedroomQuietSwitch(controller, entry.entry_id, "bedroom_quiet"),
        ExportPowerPositiveSwitch(controller, entry.entry_id, "export_power_positive"),
    ]
    entities.extend(
        ZonePilotSwitch(controller, entry.entry_id, f"zone_pilot_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )
    entities.extend(
        V2RoomControlSwitch(controller, entry.entry_id, f"v2_room_control_{index}", zone.zone_id)
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


class V2ShadowSwitch(ControllerEntity, SwitchEntity):
    """Enable the comparison runner; it has no route to climate services."""

    _attr_name = "V2 Shadow-Vergleich"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.config.v2_shadow_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_v2_shadow_enabled(True)
        await self.async_persist_option(CONF_V2_SHADOW_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_v2_shadow_enabled(False)
        await self.async_persist_option(CONF_V2_SHADOW_ENABLED, False)
        self.controller.notify_state_listeners()


class V2RoomControlSwitch(ControllerEntity, SwitchEntity):
    """One explicit room handoff with a one-switch V1 failback."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        return next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone else self._zone_id} – V2-Steuerung"

    @property
    def is_on(self) -> bool:
        return self.controller.v2_authority_for(self._zone_id).v2_may_write

    async def _async_persist_authority(self) -> None:
        store = self.hass.data[DOMAIN].get("_learning_stores", {}).get(self._entry_id)
        if store is not None:
            await store.async_save(pack(self.controller.export_learning_state()))

    def _observed_state_is_aligned(self) -> bool:
        zone = self._zone
        room = next((item for item in self.controller.last_v2_room_inputs if item.policy.room_id == self._zone_id), None)
        state = None if zone is None else self.hass.states.get(zone.climate_entity_id)
        if room is None or state is None:
            return False
        if room.observed_hvac_mode != state.state:
            return False
        observed_target = state.attributes.get("temperature")
        return room.observed_target_temperature_c == observed_target

    async def async_turn_on(self, **kwargs) -> None:
        """Take ownership only after a current, approved V2 comparison."""
        self.controller.enable_v2_room_shadow(self._zone_id)
        readiness = self.controller.v2_handoff_readiness(self._zone_id)
        if not readiness.ready or not self._observed_state_is_aligned():
            self.controller.disable_v2_room_shadow(self._zone_id)
            await self._async_persist_authority()
            self.controller.notify_state_listeners()
            reason = readiness.reason_text if not readiness.ready else "V2-Übergabe gesperrt: beobachteter Klima-Zustand hat sich geändert."
            raise HomeAssistantError(reason)
        pending = self.controller.begin_v2_handoff(self._zone_id, preconditions_met=True)
        active = self.controller.activate_v2_authority(
            self._zone_id,
            observed_state_aligned=pending.authority.value == "handoff_pending" and self._observed_state_is_aligned(),
        )
        await self._async_persist_authority()
        self.controller.notify_state_listeners()
        if not active.v2_may_write:
            raise HomeAssistantError(active.reason_text)
        # The plan used to prove this handoff is still current and the
        # observed device state was compared immediately above.  Send that
        # one plan through the shared adapter now instead of waiting for an
        # unrelated state update or the periodic refresh.  This preserves one
        # writer while making a user-approved V2 handoff operational.
        plan = self.controller.v2_command_plan_for(self._zone_id)
        if plan is None:
            self.controller.failback_v2_to_v1(self._zone_id)
            await self._async_persist_authority()
            self.controller.notify_state_listeners()
            raise HomeAssistantError("V2-Übergabe zurückgenommen: freigegebener Befehl ist nicht mehr vorhanden.")
        from . import _pilot_service_executor

        result = await self.controller.async_apply_v2_command(plan, _pilot_service_executor(self.hass))
        if result.status in {"failed", "blocked", "shadow", "authority_blocked", "invalid", "manual_override", "backoff"}:
            self.controller.failback_v2_to_v1(self._zone_id)
            await self._async_persist_authority()
            self.controller.notify_state_listeners()
            raise HomeAssistantError(f"V2-Übergabe zurückgenommen: {result.reason}")

    async def async_turn_off(self, **kwargs) -> None:
        """Return this room to V1 without racing an in-flight V2 command."""
        if not self._observed_state_is_aligned():
            raise HomeAssistantError("V1-Rückfall gesperrt: beobachteter Klima-Zustand hat sich geändert.")
        pending = self.controller.begin_v1_rollback(self._zone_id)
        if pending.authority.value != "rollback_pending":
            raise HomeAssistantError(pending.reason_text)
        await self._async_persist_authority()
        self.controller.notify_state_listeners()
        # When no V2 command is waiting for cloud/device confirmation, the
        # failback is immediate.  Otherwise _async_refresh_controller completes
        # it after the exact observed device state has arrived.
        zone = self._zone
        if zone is not None and "command_ack_pending" not in self.controller.command_adapter.handoff_blockers(zone.climate_entity_id):
            restored = self.controller.complete_v1_rollback(self._zone_id, observed_state_aligned=True)
            await self._async_persist_authority()
            self.controller.notify_state_listeners()
            if not restored.v1_may_write:
                raise HomeAssistantError(restored.reason_text)

class LivingRoomPilotSwitch(ControllerEntity, SwitchEntity):
    """Explicit productive gate for the confirmed Wohnzimmer pilot only."""

    _attr_name = "PV-Pilot aktiv"

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


class ManualOverrideSwitch(ControllerEntity, SwitchEntity):
    """Allow a user to opt out of manual release while the pilot is enabled."""

    _attr_name = "Manuelle Übernahme erlaubt"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.config.manual_override_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_manual_override_enabled(True)
        await self.async_persist_option(CONF_MANUAL_OVERRIDE_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_manual_override_enabled(False)
        await self.async_persist_option(CONF_MANUAL_OVERRIDE_ENABLED, False)
        self.controller.notify_state_listeners()

class BedroomModeSwitch(ControllerEntity, SwitchEntity):
    """Separate switch for the late-afternoon sleeping-room strategy."""

    _attr_name = "Schlafraum-Modus aktiv"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.config.bedroom_mode_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_bedroom_mode_enabled(True)
        await self.async_persist_option(CONF_BEDROOM_MODE_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_bedroom_mode_enabled(False)
        await self.async_persist_option(CONF_BEDROOM_MODE_ENABLED, False)
        self.controller.notify_state_listeners()


class BedroomCutoffSwitch(ControllerEntity, SwitchEntity):
    """Independent quiet-time switch for the Kinderzimmer."""

    _attr_name = "Kinderzimmer-Ruhezeit aktiv"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.config.bedroom_cutoff_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_bedroom_cutoff_enabled(True)
        await self.async_persist_option(CONF_BEDROOM_CUTOFF_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_bedroom_cutoff_enabled(False)
        await self.async_persist_option(CONF_BEDROOM_CUTOFF_ENABLED, False)
        self.controller.notify_state_listeners()


class BedroomQuietSwitch(ControllerEntity, SwitchEntity):
    """Independent quiet-time enable switch for the master bedroom."""

    _attr_name = "Schlafzimmer-Ruhezeit aktiv"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.controller.config.bedroom_quiet_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.set_bedroom_quiet_enabled(True)
        await self.async_persist_option(CONF_BEDROOM_QUIET_ENABLED, True)
        self.controller.notify_state_listeners()

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.set_bedroom_quiet_enabled(False)
        await self.async_persist_option(CONF_BEDROOM_QUIET_ENABLED, False)
        self.controller.notify_state_listeners()

class ZonePilotSwitch(ControllerEntity, SwitchEntity):
    """Explicit per-room permission for productive pilot commands."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        return next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)

    @property
    def name(self) -> str:
        zone = self._zone
        return f"{zone.name if zone else self._zone_id} – Pilot aktiv"

    @property
    def is_on(self) -> bool:
        zone = self._zone
        return bool(zone and zone.pilot_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        self.controller.set_zone_pilot_enabled(self._zone_id, enabled)
        await self.async_persist_option(
            CONF_HOUSE_ZONES,
            [serialize_zone_config(zone) for zone in self.controller.config.house_zones],
        )
        self.controller.notify_state_listeners()

class ExportPowerPositiveSwitch(ControllerEntity, SwitchEntity):
    """Expose the selected net-meter sign convention without changing its source."""

    _attr_name = "Netzeinspeisung positiv"
    _attr_entity_category = EntityCategory.CONFIG

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
        self._attr_entity_category = EntityCategory.CONFIG

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
        zones = [serialize_zone_config(zone) for zone in self.controller.config.house_zones]
        await self.async_persist_option(CONF_HOUSE_ZONES, zones)
        self.controller.notify_state_listeners()
