"""WLED Alerts: show Home Assistant alerts as colored blocks on a WLED strip.

A config entry is a *zone* (a WLED controller + a stretch of LEDs). Each alert is a
config *subentry* of its zone ("Add alert" on the integration page).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .const import CONF_HOST, DOMAIN, SUBENTRY_ALERT
from .manager import AlertRuntime, Manager, ZoneRuntime
from .wled import Layout, WledError, WledStrip

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

AlertAdder = Callable[[AlertRuntime], None]


@dataclass
class ZoneData:
    """Everything one zone entry owns at runtime."""

    zone: ZoneRuntime
    options: dict[str, Any]
    title: str
    # subentry_id -> (title, data) as last set up; used to tell add / edit / delete apart
    alerts: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    # one per platform: adds that platform's entities for a new alert
    adders: list[AlertAdder] = field(default_factory=list)


WledAlertsConfigEntry = ConfigEntry[ZoneData]


def get_manager(hass: HomeAssistant) -> Manager:
    """The single shared manager (zones on the same strip share one WledStrip)."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = Manager(hass)
    return hass.data[DOMAIN]


def _alert_subentries(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    return {k: s for k, s in entry.subentries.items() if s.subentry_type == SUBENTRY_ALERT}


async def async_setup_entry(hass: HomeAssistant, entry: WledAlertsConfigEntry) -> bool:
    """Set up a zone and its alerts."""
    if entry.options.get("entry_type") == "alert":  # left over from v0.1.0
        _LOGGER.error(
            "'%s' is a v0.1.0 alert entry. Alerts now live under a zone: delete this entry "
            "and use 'Add alert' on the zone instead",
            entry.title,
        )
        return False

    manager = get_manager(hass)
    zone = ZoneRuntime(manager, entry)
    manager.zones[entry.entry_id] = zone
    data = ZoneData(zone=zone, options=dict(entry.options), title=entry.title)
    entry.runtime_data = data

    for sub in _alert_subentries(entry).values():
        manager.alerts[sub.subentry_id] = AlertRuntime(manager, entry.entry_id, sub)
        data.alerts[sub.subentry_id] = (sub.title, dict(sub.data))

    try:
        await manager.strip(entry.options[CONF_HOST]).async_info()
    except WledError:
        pass  # offline strip: entities still load, the 30 s reconcile keeps retrying

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    zone.async_start()
    for sid in data.alerts:
        manager.alerts[sid].async_start()
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    manager.async_start()
    manager.async_schedule_refresh()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: WledAlertsConfigEntry) -> None:
    """Zone edited, or an alert added / edited / deleted."""
    data = entry.runtime_data
    manager = get_manager(hass)
    if dict(entry.options) != data.options or entry.title != data.title:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return

    subs = _alert_subentries(entry)
    for sid, sub in subs.items():
        if sid in data.alerts and data.alerts[sid] != (sub.title, dict(sub.data)):
            # An alert was edited. Reloading the zone is simplest and safe: the strip
            # isn't cleared on reload, so nothing visibly blinks.
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return

    for sid in set(data.alerts) - set(subs):  # deleted (HA already removed its entities)
        data.alerts.pop(sid)
        if (alert := manager.alerts.pop(sid, None)) is not None:
            alert.async_stop()

    for sid in set(subs) - set(data.alerts):  # added
        sub = subs[sid]
        alert = AlertRuntime(manager, entry.entry_id, sub)
        manager.alerts[sid] = alert
        data.alerts[sid] = (sub.title, dict(sub.data))
        for add in data.adders:
            add(alert)
        alert.async_start()

    manager.async_schedule_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: WledAlertsConfigEntry) -> bool:
    """Unload a zone (on reload the LEDs are left as they are, so nothing blinks)."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    manager = get_manager(hass)
    data = entry.runtime_data
    for sid in data.alerts:
        if (alert := manager.alerts.pop(sid, None)) is not None:
            alert.async_stop()
    zone: ZoneRuntime = manager.zones.pop(entry.entry_id)
    zone.async_stop()
    if entry.disabled_by is not None:
        await manager.async_refresh_zone(zone, clear=True)
    if manager.empty:
        manager.async_shutdown()
        hass.data.pop(DOMAIN, None)
    else:
        manager.async_schedule_refresh()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Zone deleted: take its blocks off the strip and give the strip back."""
    host = entry.options.get(CONF_HOST)
    if not host:
        return
    manager: Manager | None = hass.data.get(DOMAIN)
    strip = manager.strip(host) if manager else WledStrip(hass, host)
    try:
        await strip.async_apply(ZoneRuntime.key_for(entry.entry_id), Layout(()), brightness_pct=100)
    except WledError as err:
        _LOGGER.warning("Couldn't clear WLED alert segments on %s: %s", host, err)


@callback
def register_adder(entry: WledAlertsConfigEntry, adder: AlertAdder) -> None:
    """Platforms call this so alerts added later get their entities too."""
    entry.runtime_data.adders.append(adder)
    manager = get_manager_from_entry(entry)
    for sid in entry.runtime_data.alerts:
        adder(manager.alerts[sid])


def get_manager_from_entry(entry: WledAlertsConfigEntry) -> Manager:
    """Manager that owns this entry's zone."""
    return entry.runtime_data.zone.manager
