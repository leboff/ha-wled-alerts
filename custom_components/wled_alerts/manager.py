"""Runtime: watches alert templates and keeps every zone's LEDs in sync."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_call_later,
    async_track_state_change_event,
    async_track_template_result,
    async_track_time_interval,
)
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.util import dt as dt_util

from .const import (
    ANCHOR_START,
    CONF_ANCHOR,
    CONF_BLOCK_SIZE,
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_DELAY,
    CONF_DISMISSIBLE,
    CONF_FULL_STRIP,
    CONF_GAP,
    CONF_HOST,
    CONF_LENGTH,
    CONF_MODE,
    CONF_NIGHT_BRIGHTNESS,
    CONF_NIGHT_ENTITY,
    CONF_PRIORITY,
    CONF_SHOW_FOR,
    CONF_START,
    CONF_STYLE,
    CONF_TEMPLATE,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR,
    DEFAULT_GAP,
    DEFAULT_NIGHT_BRIGHTNESS,
    DEFAULT_PRIORITY,
    DOMAIN,
    FALSY_VALUES,
    MODE_MOMENTARY,
    POLL_SECONDS,
    STYLE_SOLID,
)
from .wled import Layout, WledError, WledStrip, plan_layout

_LOGGER = logging.getLogger(__name__)


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for one entry's entities."""
    return f"{DOMAIN}_update_{entry_id}"


def _seconds(value: Any, default: float = 0) -> float:
    """Duration selector dict (or number) -> seconds."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return timedelta(**{k: float(v) for k, v in value.items() if v}).total_seconds()
    return default


class AlertRuntime:
    """One alert: its template, timers and on/off logic."""

    def __init__(self, manager: Manager, zone_id: str, subentry: ConfigSubentry) -> None:
        """Initialise."""
        self.manager = manager
        self.hass = manager.hass
        self.entry_id = subentry.subentry_id  # unique id of this alert
        self.zone_id = zone_id
        self.name = subentry.title
        opts = subentry.data
        self.template_str: str = opts[CONF_TEMPLATE]
        self.momentary = opts.get(CONF_MODE) == MODE_MOMENTARY
        self.delay = _seconds(opts.get(CONF_DELAY))
        self.show_for = _seconds(opts.get(CONF_SHOW_FOR), 30)
        self.color = tuple(int(c) for c in (opts.get(CONF_COLOR) or DEFAULT_COLOR))
        self.style = opts.get(CONF_STYLE, STYLE_SOLID)
        self.priority = int(opts.get(CONF_PRIORITY, DEFAULT_PRIORITY))
        self.dismissible = bool(opts.get(CONF_DISMISSIBLE, False))
        self.full_strip = bool(opts.get(CONF_FULL_STRIP, False))

        self.raw = False  # template condition right now
        self.active = False  # raw held for `delay` (state) / inside show window (momentary)
        self.active_since: datetime | None = None
        self.dismissed = False
        self.enabled = True
        self.last_result: str | None = None
        self.error: str | None = None

        self.true_since: datetime | None = None  # when the condition became true
        self.restored_true_since: datetime | None = None  # from before an HA restart / reload
        self._info: Any = None
        self._first = True
        self._last_truthy: str | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._unsub: Callable[[], None] | None = None

    # ---------------------------------------------------------------- lifecycle
    @callback
    def async_start(self) -> None:
        """Start watching the template."""
        info = async_track_template_result(
            self.hass,
            [TrackTemplate(Template(self.template_str, self.hass), None)],
            self._on_result,
        )
        self._info = info
        info.async_refresh()
        self._unsub = info.async_remove

    @callback
    def async_stop(self) -> None:
        """Stop everything."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._cancel_timer()

    # ---------------------------------------------------------------- state
    @property
    def shown(self) -> bool:
        """Is this alert lighting LEDs right now?"""
        return self.enabled and self.active and not self.dismissed

    def applies_to(self, zone_entry_id: str) -> bool:
        """Does this alert belong to that zone?"""
        return self.zone_id == zone_entry_id

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Mute / unmute this alert."""
        self.enabled = enabled
        self._changed()

    @callback
    def dismiss(self) -> None:
        """Hide until the condition clears (or the momentary window ends)."""
        if self.momentary:
            self._set_active(False)
            self._cancel_timer()
        else:
            self.dismissed = True
        self._changed()

    @callback
    def restore(self, *, enabled: bool | None = None, dismissed: bool | None = None) -> None:
        """Restore persisted flags after a restart."""
        if enabled is not None:
            self.enabled = enabled
        if dismissed is not None and not self.momentary:
            self.dismissed = dismissed and self.raw
        self._changed()

    # ---------------------------------------------------------------- internals
    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    def _set_active(self, active: bool) -> None:
        if active != self.active:
            self.active = active
            self.active_since = dt_util.utcnow() if active else None

    @callback
    def _changed(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry_id))
        self.manager.async_schedule_refresh()

    @callback
    def _on_result(self, event: Event | None, updates: list[TrackTemplateResult]) -> None:
        result = updates[-1].result
        if isinstance(result, TemplateError):
            self.error = str(result)
            _LOGGER.warning("WLED alert %s: template error: %s", self.name, result)
            text = "unavailable"
        else:
            self.error = None
            text = str(result).strip()
        prev = self.last_result
        self.last_result = text
        first, self._first = self._first, False

        if self.momentary:
            truthy = text.lower() not in FALSY_VALUES
            if truthy and not first:
                prev_truthy = prev is not None and prev.lower() not in FALSY_VALUES
                recovering = (prev or "").lower() in ("unavailable", "unknown") and text == self._last_truthy
                if (not prev_truthy and not recovering) or (prev_truthy and text != prev):
                    self._fire()
            if truthy:
                self._last_truthy = text
            self.raw = truthy
        else:
            raw = False if isinstance(result, TemplateError) else result_as_boolean(result)
            if raw and not self.raw:
                self._cancel_timer()
                now = dt_util.utcnow()
                # On the first evaluation (startup, alert just created) the condition may
                # have been true for a while already: count the delay from then.
                self.true_since = self._initial_true_since(now) if first else now
                remaining = self.delay - (now - self.true_since).total_seconds()
                if self.delay > 0 and remaining > 0:
                    self._timer = async_call_later(self.hass, remaining, self._delay_done)
                else:
                    self._set_active(True)
            elif not raw:
                self._cancel_timer()
                self.true_since = None
                self._set_active(False)
                self.dismissed = False
            self.raw = raw
        self._changed()

    def _initial_true_since(self, now: datetime) -> datetime:
        """Best guess of when an already-true condition became true.

        Prefer what we saved before a restart/reload. Otherwise use the most recent
        last_changed of the entities the template reads: the condition can't have
        turned true before its inputs last changed, so this never fires early.
        """
        if self.restored_true_since is not None and self.restored_true_since <= now:
            return self.restored_true_since
        entities = set()
        if self._info is not None:
            try:
                entities = set(self._info.listeners.get("entities") or ())
            except (AssertionError, AttributeError):
                entities = set()
        changed = [s.last_changed for e in entities if (s := self.hass.states.get(e)) is not None]
        return min(max(changed), now) if changed else now

    @callback
    def _delay_done(self, _now: datetime) -> None:
        self._timer = None
        if self.raw:
            self._set_active(True)
            self._changed()

    def _fire(self) -> None:
        self._cancel_timer()
        self._set_active(True)
        self.active_since = dt_util.utcnow()
        self._timer = async_call_later(self.hass, self.show_for, self._window_done)

    @callback
    def _window_done(self, _now: datetime) -> None:
        self._timer = None
        self._set_active(False)
        self._changed()


class ZoneRuntime:
    """One zone: a stretch of LEDs on one WLED strip."""

    def __init__(self, manager: Manager, entry: ConfigEntry) -> None:
        """Initialise."""
        self.manager = manager
        self.entry_id = entry.entry_id
        self.name = entry.title
        opts = entry.options
        self.host: str = opts[CONF_HOST]
        self.start = int(opts.get(CONF_START, 0))
        self.length = int(opts[CONF_LENGTH])
        self.anchor = opts.get(CONF_ANCHOR, ANCHOR_START)
        self.block_size = int(opts.get(CONF_BLOCK_SIZE, DEFAULT_BLOCK_SIZE))
        self.gap = int(opts.get(CONF_GAP, DEFAULT_GAP))
        self.brightness = int(opts.get(CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS))
        self.night_entity: str | None = opts.get(CONF_NIGHT_ENTITY) or None
        self.night_brightness = int(opts.get(CONF_NIGHT_BRIGHTNESS, DEFAULT_NIGHT_BRIGHTNESS))
        self.enabled = True
        self.shown: list[AlertRuntime] = []
        self.error: str | None = None
        self.last_layout: Layout | None = None
        self._unsub: Callable[[], None] | None = None

    @staticmethod
    def key_for(entry_id: str) -> str:
        """Short stable key used in WLED segment names (names are max 32 chars)."""
        return entry_id[-6:]

    @property
    def key(self) -> str:
        """This zone's segment-name key."""
        return self.key_for(self.entry_id)

    @property
    def night(self) -> bool:
        """Is the zone in night mode right now?"""
        if not self.night_entity:
            return False
        state = self.manager.hass.states.get(self.night_entity)
        return bool(state and state.state == "on")

    @callback
    def async_start(self) -> None:
        """Watch the night entity."""
        if self.night_entity:

            @callback
            def _night(_event: Event[EventStateChangedData]) -> None:
                self.manager.async_schedule_refresh()

            self._unsub = async_track_state_change_event(self.manager.hass, [self.night_entity], _night)

    @callback
    def async_stop(self) -> None:
        """Stop watching."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Pause / resume the whole zone."""
        self.enabled = enabled
        self.manager.async_schedule_refresh()


class Manager:
    """Owns every zone, alert and strip."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise."""
        self.hass = hass
        self.zones: dict[str, ZoneRuntime] = {}
        self.alerts: dict[str, AlertRuntime] = {}
        self.strips: dict[str, WledStrip] = {}
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=0.4, immediate=False, function=self.async_refresh
        )
        self._poll: CALLBACK_TYPE | None = None
        self._ready = False

    def strip(self, host: str) -> WledStrip:
        """Shared strip object per host."""
        key = host.strip().lower()
        if key not in self.strips:
            self.strips[key] = WledStrip(self.hass, host)
        return self.strips[key]

    @callback
    def async_start(self) -> None:
        """Begin periodic reconciling (fixes WLED reboots / presets wiping our segments)."""
        if self._poll is None:

            @callback
            def _tick(_now: datetime) -> None:
                self.async_schedule_refresh()

            self._poll = async_track_time_interval(self.hass, _tick, timedelta(seconds=POLL_SECONDS))
        self._ready = True

    @callback
    def async_shutdown(self) -> None:
        """Stop polling."""
        if self._poll:
            self._poll()
            self._poll = None
        self._debouncer.async_cancel()

    @property
    def empty(self) -> bool:
        """Nothing left to manage."""
        return not self.zones

    @callback
    def async_schedule_refresh(self) -> None:
        """Ask for a (debounced) refresh of every zone."""
        if self._ready:
            self.hass.async_create_task(self._debouncer.async_call(), eager_start=True)

    def ordered_alerts(self, zone: ZoneRuntime) -> list[AlertRuntime]:
        """Shown alerts for a zone, most important first."""
        shown = [a for a in self.alerts.values() if a.shown and a.applies_to(zone.entry_id)]
        return sorted(
            shown,
            key=lambda a: (not a.full_strip, a.priority, a.active_since or dt_util.utcnow(), a.name),
        )

    async def async_refresh(self) -> None:
        """Recompute and push every zone."""
        for zone in list(self.zones.values()):
            await self.async_refresh_zone(zone)

    async def async_refresh_zone(self, zone: ZoneRuntime, *, clear: bool = False) -> None:
        """Push one zone's layout to its strip."""
        strip = self.strip(zone.host)
        alerts = [] if clear or not zone.enabled else self.ordered_alerts(zone)
        try:
            if strip.length is None:
                await strip.async_info()
            layout = plan_layout(
                [
                    {"id": a.entry_id, "color": a.color, "style": a.style, "full_strip": a.full_strip}
                    for a in alerts
                ],
                zone_start=zone.start,
                zone_length=zone.length,
                strip_length=strip.length or zone.start + zone.length,
                anchor=zone.anchor,
                block_size=zone.block_size,
                gap=zone.gap,
            )
            pct = zone.night_brightness if zone.night else zone.brightness
            await strip.async_apply(zone.key, layout, brightness_pct=pct)
            zone.error = None
        except WledError as err:
            if zone.error != str(err):
                _LOGGER.warning("WLED alerts zone %s: %s", zone.name, err)
            zone.error = str(err)
            layout = zone.last_layout or Layout(())
        shown_ids = {b.alert_id for b in layout.blocks}
        zone.shown = [a for a in alerts if a.entry_id in shown_ids]
        zone.last_layout = layout
        if not clear:
            async_dispatcher_send(self.hass, signal_update(zone.entry_id))
        self._hide_wled_segment_entities(strip)

    @callback
    def _hide_wled_segment_entities(self, strip: WledStrip) -> None:
        """The core WLED integration adds a light entity per segment; hide the ones we created."""
        if not strip.mac or not strip.alert_segment_ids:
            return
        registry = er.async_get(self.hass)
        mac = strip.mac.lower().replace(":", "")
        for entry in list(registry.entities.values()):
            if entry.platform != "wled" or entry.domain != "light" or entry.hidden_by is not None:
                continue
            uid = entry.unique_id.lower()
            prefix, _, seg = uid.rpartition("_")
            if prefix == mac and seg.isdigit() and int(seg) in strip.alert_segment_ids:
                registry.async_update_entity(entry.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
