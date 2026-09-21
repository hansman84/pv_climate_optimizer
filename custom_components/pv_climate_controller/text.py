"""Text-Entities: optionale zweite Temperaturquelle je Raum (Kombi-Logik).

Hauswunsch 2026-09-21: Die Regelgröße eines Raums kann aus zwei Quellen
gemischt werden - der Luft (Primärquelle des Raums) und einer zweiten Quelle
(z. B. das Taster-Mittel des Loxone-Raumreglers im Wohnzimmer).  Diese Entity
ist der Ort, an dem die zweite Quelle *optional* dazugegeben wird: einfach die
Entity-ID eintragen (leer = aus).  Der Anteil wird mit
``number.<raum>_zweitquelle_anteil`` eingestellt.
"""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_HOUSE_ZONES, DOMAIN
from .controller import serialize_zone_config
from .entity import ControllerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ZoneBlendSourceText(controller, entry.entry_id, f"zone_blend_source_{index}", zone.zone_id)
        for index, zone in enumerate(controller.config.house_zones, start=1)
    )


class ZoneBlendSourceText(ControllerEntity, TextEntity):
    """Entity-ID der zweiten Temperaturquelle dieses Raums (leer = aus)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min = 0
    _attr_native_max = 255
    _attr_icon = "mdi:thermometer-plus"

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
        return f"{self._zone_name} – Zweitquelle (Entity-ID)"

    @property
    def native_value(self) -> str:
        zone = self._zone
        return "" if zone is None else str(getattr(zone, "blend_entity_id", "") or "")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        info = self.controller.last_blend_info.get(self._zone_id) or {}
        return {
            "erklaerung": (
                "Entity-ID einer zweiten Temperaturquelle (z. B. das Taster-Mittel des "
                "Loxone-Raumreglers). Leer lassen = Kombi-Logik aus. Der Anteil wird mit "
                "'Zweitquelle-Anteil' eingestellt."
            ),
            "aktuelle_regelgroesse_c": info.get("value_c"),
            "luft_c": info.get("primary_temperature_c"),
            "zweitquelle_c": info.get("second_temperature_c"),
            "zweitquelle_aktiv": info.get("second_used"),
            "begruendung": info.get("reason"),
        }

    async def async_set_value(self, value: str) -> None:
        """Setzen/leeren der zweiten Quelle - mit Tippfehler-Schutz."""
        candidate = (value or "").strip()
        if candidate and self.hass.states.get(candidate) is None:
            raise ValueError(f"Entity nicht gefunden: {candidate}")
        self.controller.set_zone_thermal_settings(self._zone_id, blend_entity_id=candidate)
        await self.async_persist_option(
            CONF_HOUSE_ZONES,
            [serialize_zone_config(zone) for zone in self.controller.config.house_zones],
        )
        self.controller.notify_state_listeners()
