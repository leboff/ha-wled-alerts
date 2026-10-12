"""Shared entity base."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_ENTRY_TYPE, DOMAIN, TYPE_ZONE
from .manager import signal_update


class WledAlertsEntity(Entity):
    """Every entity of one zone/alert hangs off a device named after it."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        """Initialise."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        is_zone = entry.options.get(CONF_ENTRY_TYPE) == TYPE_ZONE
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="WLED Alerts",
            model="Zone" if is_zone else "Alert",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Re-render when the runtime says something changed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_update(self._entry.entry_id), self._updated)
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
