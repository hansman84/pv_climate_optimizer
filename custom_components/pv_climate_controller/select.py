"""Policy diagnostic select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BEDROOM_CUTOFF_TIME, CONF_BEDROOM_START_TIME, CONF_ENERGY_POLICY, CONF_LIVING_EVENING_END_TIME, CONF_LIVING_EVENING_START_TIME, DOMAIN, EnergyPolicy
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EnergyPolicySelect(controller, entry.entry_id, "energy_policy"),
        BedroomScheduleSelect(controller, entry.entry_id, "bedroom_start_time", "Schlafraum-Modus ab", CONF_BEDROOM_START_TIME, "start"),
        BedroomScheduleSelect(controller, entry.entry_id, "bedroom_cutoff_time", "Schlafraum-Ruhezeit ab", CONF_BEDROOM_CUTOFF_TIME, "cutoff"),
        LivingEveningScheduleSelect(controller, entry.entry_id, "living_evening_start_time", "Wohnzimmer-Abendkomfort ab", CONF_LIVING_EVENING_START_TIME, "start"),
        LivingEveningScheduleSelect(controller, entry.entry_id, "living_evening_end_time", "Wohnzimmer-Abendkomfort bis", CONF_LIVING_EVENING_END_TIME, "end"),
    ])


class EnergyPolicySelect(ControllerEntity, SelectEntity):
    _attr_name = "Energiepolitik"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [item.value for item in EnergyPolicy]

    @property
    def current_option(self) -> str:
        return self.controller.config.energy_policy.value

    async def async_select_option(self, option: str) -> None:
        """Persist a policy selection and refresh the device card."""
        self.controller.set_energy_policy(EnergyPolicy(option))
        await self.async_persist_option(CONF_ENERGY_POLICY, option)
        self.controller.notify_state_listeners()


class BedroomScheduleSelect(ControllerEntity, SelectEntity):
    """Touch-friendly time choices for the sleeping-room schedule."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["15:00", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30", "19:00"]

    def __init__(self, controller, entry_id: str, key: str, name: str, option_key: str, field: str) -> None:
        super().__init__(controller, entry_id, key)
        self._attr_name = name
        self._option_key = option_key
        self._field = field

    @property
    def current_option(self) -> str:
        return self.controller.config.bedroom_start_time if self._field == "start" else self.controller.config.bedroom_cutoff_time

    async def async_select_option(self, option: str) -> None:
        if self._field == "start":
            self.controller.set_bedroom_schedule(start_time=option)
        else:
            self.controller.set_bedroom_schedule(cutoff_time=option)
        await self.async_persist_option(self._option_key, option)
        self.controller.notify_state_listeners()


class LivingEveningScheduleSelect(ControllerEntity, SelectEntity):
    """Touch-friendly occupied-evening schedule for the living room."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [f"{hour:02d}:{minute:02d}" for hour in range(18, 24) for minute in (0, 30)]

    def __init__(self, controller, entry_id: str, key: str, name: str, option_key: str, field: str) -> None:
        super().__init__(controller, entry_id, key)
        self._attr_name = name
        self._option_key = option_key
        self._field = field

    @property
    def current_option(self) -> str:
        return self.controller.config.living_evening_start_time if self._field == "start" else self.controller.config.living_evening_end_time

    async def async_select_option(self, option: str) -> None:
        if self._field == "start":
            self.controller.set_living_evening_schedule(start_time=option)
        else:
            self.controller.set_living_evening_schedule(end_time=option)
        await self.async_persist_option(self._option_key, option)
        self.controller.notify_state_listeners()
