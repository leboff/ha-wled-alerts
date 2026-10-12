"""Dismiss button (only for alerts that allow dismissing)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import register_adder
from .entity import WledAlertsEntity
from .manager import AlertRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """A dismiss button for each dismissible alert."""

    @callback
    def add(alert: AlertRuntime) -> None:
        if alert.dismissible:
            async_add_entities([DismissButton(entry, alert)], config_subentry_id=alert.entry_id)

    register_adder(entry, add)


class DismissButton(WledAlertsEntity, ButtonEntity):
    """Hide the alert until its condition clears (then it re-arms by itself)."""

    _attr_translation_key = "dismiss"

    def __init__(self, entry: ConfigEntry, alert: AlertRuntime) -> None:
        """Initialise."""
        super().__init__(entry, alert, "dismiss")
        self._alert = alert

    async def async_press(self) -> None:
        """Dismiss."""
        self._alert.dismiss()
