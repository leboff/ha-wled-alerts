"""Enable switches: one per alert (mute) and one per zone (pause the whole zone)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import WledAlertsEntity
from .manager import AlertRuntime, ZoneRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the enable switch."""
    async_add_entities([EnabledSwitch(entry, entry.runtime_data)])


class EnabledSwitch(WledAlertsEntity, SwitchEntity, RestoreEntity):
    """Turn an alert (or a whole zone) off without deleting it."""

    _attr_translation_key = "enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, runtime: AlertRuntime | ZoneRuntime) -> None:
        """Initialise."""
        super().__init__(entry, "enabled")
        self._runtime = runtime

    async def async_added_to_hass(self) -> None:
        """Restore the last on/off."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            if isinstance(self._runtime, AlertRuntime):
                dismissed = (last.attributes or {}).get("dismissed")
                self._runtime.restore(enabled=last.state == "on", dismissed=bool(dismissed))
            else:
                self._runtime.set_enabled(last.state == "on")

    @property
    def is_on(self) -> bool:
        """Enabled?"""
        return self._runtime.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Persist the dismissed flag across restarts via the restore cache."""
        if isinstance(self._runtime, AlertRuntime):
            return {"dismissed": self._runtime.dismissed}
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        self._runtime.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        self._runtime.set_enabled(False)
        self.async_write_ha_state()
