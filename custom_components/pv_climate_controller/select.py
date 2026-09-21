"""Policy diagnostic select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BEDROOM_CUTOFF_TIME, CONF_BEDROOM_QUIET_TIME, CONF_BEDROOM_START_TIME, CONF_CHILD_BEDROOM_START_TIME, CONF_ENERGY_POLICY, CONF_HOUSE_ZONES, CONF_LIVING_EVENING_END_TIME, CONF_LIVING_EVENING_START_TIME, DOMAIN, EnergyPolicy
from .blend import blend_source_candidates
from .controller import serialize_zone_config
from .entity import ControllerEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EnergyPolicySelect(controller, entry.entry_id, "energy_policy"),
        BedroomScheduleSelect(controller, entry.entry_id, "bedroom_start_time", "Schlafzimmer-Vorkühlung ab", CONF_BEDROOM_START_TIME, "master_start"),
        BedroomQuietTimeSelect(controller, entry.entry_id),
        ChildBedroomScheduleSelect(controller, entry.entry_id, "child_bedroom_start_time", "Kinderzimmer-Vorkühlung ab", CONF_CHILD_BEDROOM_START_TIME, "start"),
        ChildBedroomScheduleSelect(controller, entry.entry_id, "child_bedroom_quiet_time", "Kinderzimmer-Ruhezeit ab", CONF_BEDROOM_CUTOFF_TIME, "quiet"),
        LivingEveningScheduleSelect(controller, entry.entry_id, "living_evening_start_time", "Wohnzimmer-Abendkomfort ab", CONF_LIVING_EVENING_START_TIME, "start"),
        LivingEveningScheduleSelect(controller, entry.entry_id, "living_evening_end_time", "Wohnzimmer-Abendkomfort bis", CONF_LIVING_EVENING_END_TIME, "end"),
    ])
    zone_sources = [
        ZoneBlendSourceSelect(controller, entry.entry_id, f"zone_blend_source_select_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    ]
    if zone_sources:
        async_add_entities(zone_sources)


NONE_OPTION = "keine (nur Luft)"


class ZoneBlendSourceSelect(ControllerEntity, SelectEntity):
    """Zweitquelle der Kombi-Logik je Raum (Hauswunsch 2026-09-21).

    Im Dashboard soll die Quelle mit einem Tipp umstellbar sein.  Die Liste
    enthält alle nutzbaren Temperatursensoren (Einheit °C, nicht die eigene
    Integration) plus "keine (nur Luft)".  Die Freitext-Entity derselben
    Einstellung bleibt als Notausgang für exotische Quellen bestehen.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, entry_id: str, key: str, zone_id: str) -> None:
        super().__init__(controller, entry_id, key)
        self._zone_id = zone_id

    @property
    def _zone(self):
        return next((zone for zone in self.controller.config.house_zones if zone.zone_id == self._zone_id), None)

    @property
    def _zone_name(self) -> str:
        zone = self._zone
        return zone.name if zone is not None else self._zone_id

    @property
    def name(self) -> str:
        return f"{self._zone_name} – Zweitquelle (Kombi)"

    @property
    def options(self) -> list[str]:
        zone = self._zone
        current = "" if zone is None else getattr(zone, "blend_entity_id", "") or ""
        options = [NONE_OPTION]
        options.extend(blend_source_candidates(self.hass.states.async_all()))
        if current and current not in options:
            options.insert(1, current)
        return options

    @property
    def current_option(self) -> str:
        zone = self._zone
        current = "" if zone is None else getattr(zone, "blend_entity_id", "") or ""
        return current or NONE_OPTION

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "erklaerung": (
                "Zweite Temperaturquelle für die Regelgröße dieses Raums. "
                "\"keine (nur Luft)\" schaltet die Kombi-Logik aus."
            ),
            "anteil_pct": None if self._zone is None else getattr(self._zone, "blend_weight_pct", None),
        }

    async def async_select_option(self, option: str) -> None:
        value = "" if option == NONE_OPTION else option
        self.controller.set_zone_thermal_settings(self._zone_id, blend_entity_id=value)
        await self.async_persist_option(
            CONF_HOUSE_ZONES,
            [serialize_zone_config(zone) for zone in self.controller.config.house_zones],
        )
        self.controller.notify_state_listeners()


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
    _attr_options = [f"{hour:02d}:{minute:02d}" for hour in range(15, 24) for minute in (0, 30)]

    def __init__(self, controller, entry_id: str, key: str, name: str, option_key: str, field: str) -> None:
        super().__init__(controller, entry_id, key)
        self._attr_name = name
        self._option_key = option_key
        self._field = field

    @property
    def current_option(self) -> str:
        return self.controller.config.bedroom_start_time

    async def async_select_option(self, option: str) -> None:
        self.controller.set_bedroom_schedule(start_time=option)
        await self.async_persist_option(self._option_key, option)
        self.controller.notify_state_listeners()


class BedroomQuietTimeSelect(ControllerEntity, SelectEntity):
    """Independent quiet-time control for the master bedroom."""

    _attr_name = "Schlafzimmer-Ruhezeit ab"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [f"{hour:02d}:{minute:02d}" for hour in range(15, 24) for minute in (0, 30)]

    def __init__(self, controller, entry_id: str) -> None:
        super().__init__(controller, entry_id, "bedroom_quiet_time")

    @property
    def current_option(self) -> str:
        return self.controller.config.bedroom_quiet_time

    async def async_select_option(self, option: str) -> None:
        self.controller.set_bedroom_quiet_time(option)
        await self.async_persist_option(CONF_BEDROOM_QUIET_TIME, option)
        self.controller.notify_state_listeners()


class ChildBedroomScheduleSelect(ControllerEntity, SelectEntity):
    """Independent Kinderzimmer schedule, deliberately not shared with Schlafzimmer."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [f"{hour:02d}:{minute:02d}" for hour in range(15, 24) for minute in (0, 30)]

    def __init__(self, controller, entry_id: str, key: str, name: str, option_key: str, field: str) -> None:
        super().__init__(controller, entry_id, key)
        self._attr_name = name
        self._option_key = option_key
        self._field = field

    @property
    def current_option(self) -> str:
        return self.controller.config.child_bedroom_start_time if self._field == "start" else self.controller.config.bedroom_cutoff_time

    async def async_select_option(self, option: str) -> None:
        if self._field == "start":
            self.controller.set_child_bedroom_start_time(option)
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
