"""End-to-end tests against a fake WLED."""

from __future__ import annotations

from datetime import timedelta

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.wled_alerts.const import DOMAIN

from .conftest import HOST, FakeWled

RED, BLUE, WHITE = [255, 0, 0], [0, 0, 255], [255, 255, 255]


ZONE: dict[str, MockConfigEntry] = {}


async def add_zone(hass: HomeAssistant, title: str = "Coffee bar", **opts) -> MockConfigEntry:
    options = {"host": HOST, "start": 88, "length": 18, "anchor": "end",
               "block_size": 4, "gap": 1, "brightness": 100, "night_brightness": 25, **opts}
    entry = MockConfigEntry(domain=DOMAIN, title=title, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ZONE["last"] = entry
    return entry


async def add_alert(hass: HomeAssistant, title: str, template: str, zone: MockConfigEntry | None = None,
                    **opts) -> str:
    """Add an alert subentry to a running zone (like the UI's "Add alert"). Returns its id."""
    zone = zone or ZONE["last"]
    data = {"active_template": template, "mode": "state", "color": RED,
            "style": "solid", "priority": 50, "dismissible": False, "full_strip": False, **opts}
    sub = ConfigSubentry(data=MappingProxyType(data), subentry_type="alert", title=title, unique_id=None)
    hass.config_entries.async_add_subentry(zone, sub)
    await hass.async_block_till_done()
    return sub.subentry_id


async def settle(hass: HomeAssistant, seconds: float = 1) -> None:
    """Advance time, then let the 0.4 s refresh debounce fire too."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds + 2))
    await hass.async_block_till_done()


async def test_condition_alert_lights_and_clears(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("cover.garage", "closed")
    await add_zone(hass)
    await add_alert(hass, "Garage open", "{{ is_state('cover.garage','open') }}")
    await settle(hass)
    assert fake_wled.blocks() == []
    assert fake_wled.on is False

    hass.states.async_set("cover.garage", "open")
    await settle(hass)
    # anchored at the END of LEDs 88..105: block 102..106
    assert fake_wled.blocks() == [(102, 106, RED, 0)]
    assert fake_wled.on is True  # borrowed the strip from off
    # the user's own segment was switched off while we borrow
    assert fake_wled.segs[0]["on"] is False
    assert hass.states.get("binary_sensor.garage_open").state == "on"
    assert hass.states.get("sensor.coffee_bar_alerts_showing").state == "1"

    hass.states.async_set("cover.garage", "closed")
    await settle(hass)
    assert fake_wled.blocks() == []
    assert fake_wled.ours() == []  # marker removed too
    assert fake_wled.on is False  # given back
    assert fake_wled.bri == 212  # original master brightness restored
    assert fake_wled.segs[0]["on"] is True  # user's segment restored for next time


async def test_strip_already_on_is_left_alone(hass, wled_http, fake_wled: FakeWled):
    fake_wled.on = True
    hass.states.async_set("lock.door", "locked")
    await add_zone(hass)
    await add_alert(hass, "Door", "{{ is_state('lock.door','unlocked') }}")
    hass.states.async_set("lock.door", "unlocked")
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, RED, 0)]
    assert fake_wled.segs[0]["on"] is True  # user's effect keeps running under our block
    assert fake_wled.bri == 212
    hass.states.async_set("lock.door", "locked")
    await settle(hass)
    assert fake_wled.blocks() == []
    assert fake_wled.on is True  # we never turned it off


async def test_delay(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("cover.garage", "closed")
    await add_zone(hass)
    await add_alert(hass, "Garage open", "{{ is_state('cover.garage','open') }}", delay={"minutes": 5})
    hass.states.async_set("cover.garage", "open")
    await settle(hass, 60)
    assert fake_wled.blocks() == []
    assert hass.states.get("binary_sensor.garage_open").state == "off"
    await settle(hass, 301)
    assert fake_wled.blocks() == [(102, 106, RED, 0)]
    # closing before the delay elapses cancels it
    hass.states.async_set("cover.garage", "closed")
    await settle(hass)
    hass.states.async_set("cover.garage", "open")
    await settle(hass, 120)
    assert fake_wled.blocks() == []


async def test_priority_order_and_shrink(hass, wled_http, fake_wled: FakeWled):
    await add_zone(hass)
    for i in range(6):
        hass.states.async_set(f"input_boolean.a{i}", "on")
        await add_alert(hass, f"A{i}", f"{{{{ is_state('input_boolean.a{i}','on') }}}}",
                        priority=10 * (6 - i), color=[i * 40, 0, 0])
    await settle(hass)
    blocks = fake_wled.blocks()
    # 6 alerts in 18 LEDs: shrink to 2-LED blocks with 1 gap -> 6*2 + 5 = 17 fits
    assert len(blocks) == 6
    assert all(stop - start == 2 for start, stop, _c, _f in blocks)
    # most important (A5, priority 10, color 200) sits at the very end
    assert blocks[-1] == (104, 106, [200, 0, 0], 0)
    assert blocks[0][2] == [0, 0, 0]  # least important (A0, priority 60) furthest in


async def test_overflow_drops_least_important(hass, wled_http, fake_wled: FakeWled):
    await add_zone(hass, length=8)
    for i in range(4):
        hass.states.async_set(f"input_boolean.a{i}", "on")
        await add_alert(hass, f"A{i}", f"{{{{ is_state('input_boolean.a{i}','on') }}}}", priority=i + 1)
    await settle(hass)
    # 8 LEDs: 2+1+2+1+2 = 8 -> 3 alerts fit, the 4th (priority 4) is dropped
    assert len(fake_wled.blocks()) == 3
    assert hass.states.get("sensor.coffee_bar_alerts_showing").state == "3"


async def test_momentary_doorbell_full_strip(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("event.doorbell", "2026-10-11T18:13:05+00:00", {"event_type": "ring"})
    await add_zone(hass)
    await add_alert(hass, "Doorbell", "{{ states('event.doorbell') }}", mode="momentary",
                    show_for={"seconds": 20}, color=WHITE, style="flash", full_strip=True, priority=1)
    await settle(hass)
    assert fake_wled.blocks() == []  # existing value at startup is NOT a new ring

    hass.states.async_set("event.doorbell", "2026-10-12T19:00:00+00:00", {"event_type": "ring"})
    await settle(hass)
    assert fake_wled.blocks() == [(0, 106, WHITE, 1)]  # whole strip, Blink
    await settle(hass, 21)
    assert fake_wled.blocks() == []
    assert fake_wled.on is False

    # going unavailable and back to the same value is not a new ring
    hass.states.async_set("event.doorbell", "unavailable")
    await settle(hass)
    hass.states.async_set("event.doorbell", "2026-10-12T19:00:00+00:00")
    await settle(hass)
    assert fake_wled.blocks() == []


async def test_dismiss_and_rearm(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("sensor.humidifier_water", "empty")
    await add_zone(hass)
    await add_alert(hass, "Humidifier empty", "{{ is_state('sensor.humidifier_water','empty') }}",
                    dismissible=True)
    await settle(hass)
    assert len(fake_wled.blocks()) == 1
    await hass.services.async_call("button", "press", {"entity_id": "button.humidifier_empty_dismiss"}, blocking=True)
    await settle(hass)
    assert fake_wled.blocks() == []
    assert hass.states.get("binary_sensor.humidifier_empty").attributes["dismissed"] is True
    # refilled, then empty again -> shows again
    hass.states.async_set("sensor.humidifier_water", "ok")
    await settle(hass)
    hass.states.async_set("sensor.humidifier_water", "empty")
    await settle(hass)
    assert len(fake_wled.blocks()) == 1


async def test_enable_switch_mutes(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    assert len(fake_wled.blocks()) == 1
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.x_enabled"}, blocking=True)
    await settle(hass)
    assert fake_wled.blocks() == []
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.x_enabled"}, blocking=True)
    await settle(hass)
    assert len(fake_wled.blocks()) == 1


async def test_night_brightness(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("schedule.night", "off")
    hass.states.async_set("input_boolean.x", "on")
    await add_zone(hass, night_entity="schedule.night", night_brightness=25)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    assert fake_wled.ours()[-1]["bri"] == 255
    hass.states.async_set("schedule.night", "on")
    await settle(hass)
    block = next(s for s in fake_wled.ours() if s["n"].endswith(":0"))
    assert block["bri"] == round(255 * 0.25)


async def test_user_takes_strip_while_borrowed(hass, wled_http, fake_wled: FakeWled):
    """If someone turns their own scene on while we borrowed the strip, don't turn it off on them."""
    hass.states.async_set("input_boolean.x", "on")
    await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    assert fake_wled.on is True
    fake_wled.segs[0]["on"] = True  # e.g. they hit a preset / the game-day automation ran
    hass.states.async_set("input_boolean.x", "off")
    await settle(hass)
    assert fake_wled.on is True
    assert fake_wled.ours() == []


async def test_reconcile_after_wled_reboot(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    # power blip: WLED comes back with only its boot preset
    fake_wled.segs = [fake_wled._seg(0, 0, 106)]
    fake_wled.on = True
    await settle(hass, 31)  # poll interval
    assert len(fake_wled.blocks()) == 1


async def test_offline_strip_does_not_break_setup(hass, wled_http, fake_wled: FakeWled):
    fake_wled.offline = True
    hass.states.async_set("input_boolean.x", "on")
    zone = await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    assert zone.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.coffee_bar_alerts_showing").attributes["error"]
    fake_wled.offline = False
    await settle(hass, 31)
    assert len(fake_wled.blocks()) == 1
    assert hass.states.get("sensor.coffee_bar_alerts_showing").attributes["error"] is None


async def test_unload_zone_cleans_strip(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    zone = await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    assert fake_wled.ours()
    await hass.config_entries.async_reload(zone.entry_id)  # reload must NOT blink the strip
    await hass.async_block_till_done()
    assert fake_wled.ours()
    assert await hass.config_entries.async_remove(zone.entry_id)
    await hass.async_block_till_done()
    assert fake_wled.ours() == []
    assert fake_wled.on is False


async def test_two_zones_same_strip(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    left = await add_zone(hass, start=0, length=10, anchor="start")
    right = await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}", zone=right)
    await add_alert(hass, "Y", "{{ is_state('input_boolean.x','on') }}", zone=right, color=BLUE)
    await add_alert(hass, "Y left", "{{ is_state('input_boolean.x','on') }}", zone=left, color=BLUE)
    await settle(hass)
    blocks = fake_wled.blocks()
    assert (0, 4, BLUE, 0) in blocks  # left zone
    assert (102, 106, RED, 0) in blocks and (97, 101, BLUE, 0) in blocks  # X then Y on the right (same priority, X first by time)
    hass.states.async_set("input_boolean.x", "off")
    await settle(hass)
    assert fake_wled.ours() == []
    assert fake_wled.on is False
    assert left.state is ConfigEntryState.LOADED


async def test_template_error_is_inactive(hass, wled_http, fake_wled: FakeWled):
    await add_zone(hass)
    await add_alert(hass, "Bad", "{{ states('sensor.x') | int / 0 }}")
    await settle(hass)
    assert fake_wled.blocks() == []
    assert hass.states.get("binary_sensor.bad").attributes["template_error"]


# ------------------------------------------------------------------ config flow
ZONE_FORM = {"name": "Bar", "host": HOST, "start": 88, "length": 18, "anchor": "end", "block_size": 4,
             "gap": 1, "brightness": 100, "night_brightness": 25}
ALERT_FORM = {"name": "Garage", "active_template": "{{ true }}", "mode": "state", "color": RED,
              "style": "pulse", "priority": 5, "dismissible": False, "full_strip": False}


async def test_flow_zone_then_alert(hass, wled_http, fake_wled: FakeWled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {**ZONE_FORM, "start": 100})
    assert bad["errors"] == {"length": "out_of_range"}
    done = await hass.config_entries.flow.async_configure(result["flow_id"], ZONE_FORM)
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["options"]["start"] == 88 and isinstance(done["options"]["start"], int)
    await hass.async_block_till_done()
    zone = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.subentries.async_init((zone.entry_id, "alert"), context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    with pytest.raises(InvalidData):  # HA's template selector rejects syntax errors itself
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {**ALERT_FORM, "active_template": "{{ is_state('a' }}"})
    done = await hass.config_entries.subentries.async_configure(result["flow_id"], ALERT_FORM)
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, RED, 2)]
    assert hass.states.get("binary_sensor.garage").state == "on"
    assert hass.states.get("switch.garage_enabled") is not None


async def test_flow_cannot_connect(hass, wled_http, fake_wled: FakeWled):
    fake_wled.offline = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**ZONE_FORM, "start": 0, "length": 10})
    assert result["errors"] == {"host": "cannot_connect"}


async def test_reconfigure_alert(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    zone = await add_zone(hass)
    sid = await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    result = await hass.config_entries.subentries.async_init(
        (zone.entry_id, "alert"), context={"source": "reconfigure", "subentry_id": sid})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "X", "active_template": "{{ is_state('input_boolean.x','on') }}", "mode": "state", "color": BLUE,
        "style": "pulse", "priority": 3, "dismissible": True, "full_strip": False})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, BLUE, 2)]  # Breathe
    assert hass.states.get("button.x_dismiss") is not None


async def test_delete_alert(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    zone = await add_zone(hass)
    sid = await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await add_alert(hass, "Y", "{{ is_state('input_boolean.x','on') }}", color=BLUE, priority=60)
    await settle(hass)
    assert len(fake_wled.blocks()) == 2
    hass.config_entries.async_remove_subentry(zone, sid)
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, BLUE, 0)]
    assert hass.states.get("binary_sensor.x") is None
    assert hass.states.get("binary_sensor.y").state == "on"


async def test_edit_zone_moves_blocks(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    zone = await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    result = await hass.config_entries.options.async_init(zone.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        k: v for k, v in {**ZONE_FORM, "start": 0, "anchor": "start"}.items() if k != "name"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(0, 4, RED, 0)]
    assert hass.states.get("binary_sensor.x").state == "on"


async def test_adding_alert_does_not_reload_zone(hass, wled_http, fake_wled: FakeWled):
    """Adding an alert must not reset other alerts' running delay timers."""
    hass.states.async_set("cover.garage", "closed")
    zone = await add_zone(hass)
    await add_alert(hass, "Garage open", "{{ is_state('cover.garage','open') }}", delay={"minutes": 5})
    hass.states.async_set("cover.garage", "open")
    await settle(hass, 200)
    await add_alert(hass, "Other", "{{ false }}")
    assert zone.state is ConfigEntryState.LOADED
    assert fake_wled.blocks() == []  # still inside the 5 minutes
    await settle(hass, 301)  # fake clock jumps are relative to real now: 301 s after opening
    assert fake_wled.blocks() == [(102, 106, RED, 0)]
    assert hass.states.get("binary_sensor.other").state == "off"


async def test_v010_alert_entry_refuses_cleanly(hass, wled_http, fake_wled: FakeWled):
    old = MockConfigEntry(domain=DOMAIN, title="Old", options={"entry_type": "alert", "active_template": "{{ true }}"})
    old.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(old.entry_id)
    assert fake_wled.ours() == []


async def test_delay_counts_from_when_condition_became_true(hass, wled_http, fake_wled: FakeWled, freezer):
    """Alert created while the door has already been open 8 min: lights after 2 more, not 10."""
    freezer.move_to("2026-10-12 18:00:00+00:00")
    hass.states.async_set("binary_sensor.patio", "on")
    freezer.move_to("2026-10-12 18:08:00+00:00")
    await add_zone(hass)
    await add_alert(hass, "Door", "{{ is_state('binary_sensor.patio','on') }}", delay={"minutes": 10})
    await settle(hass)
    assert fake_wled.blocks() == []
    freezer.move_to("2026-10-12 18:10:05+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, RED, 0)]


async def test_delay_survives_reload(hass, wled_http, fake_wled: FakeWled, freezer):
    """The countdown keeps its start time across a reload/restart (restored from the sensor)."""
    freezer.move_to("2026-10-12 18:00:00+00:00")
    hass.states.async_set("binary_sensor.patio", "off")
    zone = await add_zone(hass)
    await add_alert(hass, "Door", "{{ is_state('binary_sensor.patio','on') }}", delay={"minutes": 10})
    hass.states.async_set("binary_sensor.patio", "on")
    await hass.async_block_till_done()
    freezer.move_to("2026-10-12 18:07:00+00:00")
    # after a restart every entity's last_changed is "now"; simulate that
    hass.states.async_set("binary_sensor.patio", "on", force_update=True)
    await hass.config_entries.async_reload(zone.entry_id)
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == []
    freezer.move_to("2026-10-12 18:10:05+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, RED, 0)]
