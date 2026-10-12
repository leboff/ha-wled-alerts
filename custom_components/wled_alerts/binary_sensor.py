"""Alert "active" binary sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import WledAlertsEntity
from .manager import AlertRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the alert's sensor."""
    async_add_entities([AlertActiveSensor(entry, entry.runtime_data)])


class AlertActiveSensor(WledAlertsEntity, BinarySensorEntity):
    """On while the alert is showing (or would show, if muted/dismissed)."""

    _attr_name = None  # the device name, e.g. "Garage open"
    _attr_icon = "mdi:alarm-light-outline"

    def __init__(self, entry: ConfigEntry, alert: AlertRuntime) -> None:
        """Initialise."""
        super().__init__(entry, "active")
        self._alert = alert

    @property
    def is_on(self) -> bool:
        """Condition met (after any delay)."""
        return self._alert.active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Everything a dashboard might want."""
        a = self._alert
        return {
            "showing": a.shown,
            "condition": a.raw,
            "enabled": a.enabled,
            "dismissed": a.dismissed,
            "dismissible": a.dismissible,
            "color": list(a.color),
            "style": a.style,
            "priority": a.priority,
            "active_since": a.active_since.isoformat() if a.active_since else None,
            "template_result": a.last_result,
            "template_error": a.error,
        }
