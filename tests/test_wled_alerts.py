"""End-to-end tests against a fake WLED."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.wled_alerts.const import DOMAIN

from .conftest import HOST, FakeWled

RED, BLUE, WHITE = [255, 0, 0], [0, 0, 255], [255, 255, 255]


async def add_zone(hass: HomeAssistant, **opts) -> MockConfigEntry:
    options = {"entry_type": "zone", "host": HOST, "start": 88, "length": 18, "anchor": "end",
               "block_size": 4, "gap": 1, "brightness": 100, "night_brightness": 25, **opts}
    entry = MockConfigEntry(domain=DOMAIN, title="Coffee bar", options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def add_alert(hass: HomeAssistant, title: str, template: str, **opts) -> MockConfigEntry:
    options = {"entry_type": "alert", "active_template": template, "mode": "state", "color": RED,
               "style": "solid", "priority": 50, "dismissible": False, "full_strip": False, **opts}
    entry = MockConfigEntry(domain=DOMAIN, title=title, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


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
    assert await hass.config_entries.async_remove(zone.entry_id)
    await hass.async_block_till_done()
    assert fake_wled.ours() == []
    assert fake_wled.on is False


async def test_two_zones_same_strip(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    left = await add_zone(hass, start=0, length=10, anchor="start")
    right = await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}", zones=[right.entry_id])
    await add_alert(hass, "Y", "{{ is_state('input_boolean.x','on') }}", color=BLUE)  # all zones
    await settle(hass)
    blocks = fake_wled.blocks()
    assert (0, 4, BLUE, 0) in blocks  # Y in left zone
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
async def test_flow_zone_then_alert(hass, wled_http, fake_wled: FakeWled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    # alert before any zone -> abort
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "alert"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_zones"

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "zone"})
    assert result["type"] is FlowResultType.FORM
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Bar", "host": HOST, "start": 100, "length": 18, "anchor": "end", "block_size": 4,
        "gap": 1, "brightness": 100, "night_brightness": 25})
    assert bad["errors"] == {"length": "out_of_range"}
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Bar", "host": HOST, "start": 88, "length": 18, "anchor": "end", "block_size": 4,
        "gap": 1, "brightness": 100, "night_brightness": 25})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["options"]["start"] == 88 and isinstance(done["options"]["start"], int)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "alert"})
    with pytest.raises(InvalidData):  # HA's template selector rejects syntax errors itself
        await hass.config_entries.flow.async_configure(result["flow_id"], {
            "name": "Oops", "active_template": "{{ is_state('a' }}", "mode": "state", "color": RED,
            "style": "solid", "priority": 5, "dismissible": False, "full_strip": False})
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Garage", "active_template": "{{ true }}", "mode": "state", "color": RED,
        "style": "pulse", "priority": 5, "dismissible": False, "full_strip": False})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await settle(hass)
    assert len(fake_wled.blocks()) == 1


async def test_flow_cannot_connect(hass, wled_http, fake_wled: FakeWled):
    fake_wled.offline = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "zone"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Bar", "host": HOST, "start": 0, "length": 10, "anchor": "end", "block_size": 4,
        "gap": 1, "brightness": 100, "night_brightness": 25})
    assert result["errors"] == {"host": "cannot_connect"}


async def test_options_flow_edits_alert(hass, wled_http, fake_wled: FakeWled):
    hass.states.async_set("input_boolean.x", "on")
    await add_zone(hass)
    alert = await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await settle(hass)
    result = await hass.config_entries.options.async_init(alert.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "active_template": "{{ is_state('input_boolean.x','on') }}", "mode": "state", "color": BLUE,
        "style": "pulse", "priority": 3, "dismissible": True, "full_strip": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await settle(hass)
    assert fake_wled.blocks() == [(102, 106, BLUE, 2)]  # Breathe
    assert hass.states.get("button.x_dismiss") is not None


async def test_user_turns_strip_off_while_alert_lit(hass, wled_http, fake_wled: FakeWled):
    """Switching the strip off by hand silences current alerts; a NEW alert lights it again."""
    hass.states.async_set("input_boolean.x", "on")
    hass.states.async_set("input_boolean.y", "off")
    await add_zone(hass)
    await add_alert(hass, "X", "{{ is_state('input_boolean.x','on') }}")
    await add_alert(hass, "Y", "{{ is_state('input_boolean.y','on') }}", color=BLUE)
    await settle(hass)
    assert fake_wled.on is True
    fake_wled.on = False  # someone hits the WLED app / HA light off
    await settle(hass, 31)
    assert fake_wled.on is False
    assert fake_wled.ours() == []
    assert fake_wled.segs[0]["on"] is True  # their segment restored for next time
    hass.states.async_set("input_boolean.y", "on")  # a new alert
    await settle(hass)
    assert fake_wled.on is True
    assert len(fake_wled.blocks()) == 2
