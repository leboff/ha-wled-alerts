"""Zone status sensor: how many alerts are showing, and which."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import WledAlertsEntity
from .manager import ZoneRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the zone sensor."""
    async_add_entities([ZoneSensor(entry, entry.runtime_data)])


class ZoneSensor(WledAlertsEntity, SensorEntity):
    """Number of alerts lit in this zone."""

    _attr_translation_key = "showing"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, entry: ConfigEntry, zone: ZoneRuntime) -> None:
        """Initialise."""
        super().__init__(entry, "showing")
        self._zone = zone

    @property
    def native_value(self) -> int:
        """How many alerts are on the LEDs."""
        return len(self._zone.shown)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Details for dashboards (e.g. a tablet chip row)."""
        z = self._zone
        return {
            "alerts": [
                {"name": a.name, "color": list(a.color), "style": a.style, "priority": a.priority,
                 "dismissible": a.dismissible, "entry_id": a.entry_id}
                for a in z.shown
            ],
            "host": z.host,
            "leds": f"{z.start}-{z.start + z.length - 1}",
            "anchor": z.anchor,
            "night": z.night,
            "enabled": z.enabled,
            "error": z.error,
        }
