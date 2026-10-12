"""Shared entity base."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .manager import AlertRuntime, ZoneRuntime, signal_update


class WledAlertsEntity(Entity):
    """Zone entities hang off the zone device; alert entities off a per-alert device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: ZoneRuntime | AlertRuntime, key: str) -> None:
        """Initialise."""
        self._entry = entry
        self._signal_id = runtime.entry_id
        self._attr_unique_id = f"{runtime.entry_id}_{key}"
        if isinstance(runtime, ZoneRuntime):
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry.entry_id)},
                name=entry.title,
                manufacturer="WLED Alerts",
                model=f"Zone · LEDs {runtime.start}-{runtime.start + runtime.length - 1}",
                entry_type=DeviceEntryType.SERVICE,
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, runtime.entry_id)},
                name=runtime.name,
                manufacturer="WLED Alerts",
                model="Alert",
                entry_type=DeviceEntryType.SERVICE,
                via_device=(DOMAIN, entry.entry_id),
            )

    async def async_added_to_hass(self) -> None:
        """Re-render when the runtime says something changed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_update(self._signal_id), self._updated)
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()
