"""WLED Alerts: show Home Assistant alerts as colored blocks on a WLED strip."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, CONF_HOST, DOMAIN, TYPE_ALERT, TYPE_ZONE
from .manager import AlertRuntime, Manager, ZoneRuntime

ZONE_PLATFORMS = [Platform.SENSOR, Platform.SWITCH]
ALERT_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SWITCH, Platform.BUTTON]


def get_manager(hass: HomeAssistant) -> Manager:
    """The single shared manager."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = Manager(hass)
    return hass.data[DOMAIN]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a zone or an alert."""
    manager = get_manager(hass)
    kind = entry.options.get(CONF_ENTRY_TYPE)
    if kind == TYPE_ZONE:
        zone = ZoneRuntime(manager, entry)
        manager.zones[entry.entry_id] = zone
        entry.runtime_data = zone
        try:
            await manager.strip(entry.options[CONF_HOST]).async_info()
        except Exception:  # noqa: BLE001 - offline strip: entities still load, refresh retries
            pass
        await hass.config_entries.async_forward_entry_setups(entry, ZONE_PLATFORMS)
        zone.async_start()
    elif kind == TYPE_ALERT:
        alert = AlertRuntime(manager, entry)
        manager.alerts[entry.entry_id] = alert
        entry.runtime_data = alert
        await hass.config_entries.async_forward_entry_setups(entry, ALERT_PLATFORMS)
        alert.async_start()
    else:
        return False
    manager.async_start()
    manager.async_schedule_refresh()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a zone or an alert."""
    manager = get_manager(hass)
    kind = entry.options.get(CONF_ENTRY_TYPE)
    platforms = ZONE_PLATFORMS if kind == TYPE_ZONE else ALERT_PLATFORMS
    ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if not ok:
        return False
    if kind == TYPE_ZONE:
        zone: ZoneRuntime = manager.zones.pop(entry.entry_id)
        zone.async_stop()
        # Take our blocks off the strip (and give it back if we had turned it on).
        await manager.async_refresh_zone(zone, clear=True)
    else:
        alert: AlertRuntime = manager.alerts.pop(entry.entry_id)
        alert.async_stop()
        manager.async_schedule_refresh()
    if manager.empty:
        manager.async_shutdown()
        hass.data.pop(DOMAIN, None)
    return True
