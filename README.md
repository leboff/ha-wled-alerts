# WLED Alerts for Home Assistant

Turn part of a WLED strip into a status light. Each alert is a Home Assistant helper with a template. While the template is true, a colored block lights up in that alert's zone of the strip.

- **Zones** say *where*: which WLED controller, which LEDs, which end the most important alert sits at, and brightness (with an optional night mode). You can have several zones, on one strip or on different strips.
- **Alerts** say *when* and *how*: a template, an optional "only after X minutes", color, solid/pulse/flash, priority, and whether it can be dismissed. An alert can also take over the whole strip, which suits a doorbell.

Everything is set up in the UI under **Settings → Devices & services → Helpers → Create helper → WLED Alerts**. To edit something, use the gear icon. To remove it, delete it.

## How it behaves

- Alerts stack from the anchor end of the zone, most important first. If they don't all fit, blocks shrink (down to 2 LEDs) and the least important ones are left out.
- Only the segments it creates are touched. They're WLED segments named `wla:…`, drawn on top of whatever the strip is doing, so your own effect keeps running everywhere else.
- If the strip is **off** when an alert fires, it turns the strip on, shows only the alerts, and turns it off again (exactly as it was) once they clear.
- If you **turn the strip off yourself** while alerts are showing, those alerts stay dark. A *new* alert will light it up again.
- It re-checks the strip every 30 s, so a WLED reboot or a preset that wipes segments gets fixed by itself.

## Alert kinds

| Kind | Template returns | Shows |
|---|---|---|
| Condition | true / false | while true (after the optional delay) |
| Momentary | a value that changes when the event happens, e.g. `{{ states('event.doorbell') }}` | for "Show for" after each change |

Momentary alerts ignore the value they see at startup, and ignore a sensor dropping out (`unavailable`) and coming back with the same value.

## Entities

Each alert gets:
- `binary_sensor.<alert>`: on while active. Its attributes include `showing`, `dismissed`, `color` and `template_error`.
- `switch.<alert>_enabled`: mute the alert without deleting it.
- `button.<alert>_dismiss`: only if "can be dismissed" is on. Hides the alert until its condition clears.

Each zone gets:
- `sensor.<zone>_alerts_showing`: how many alerts are lit, with the list in its attributes (handy for dashboards).
- `switch.<zone>_enabled`: pause the zone.

## Install

HACS → ⋮ → Custom repositories → `https://github.com/leboff/ha-wled-alerts` (Integration). Install it, restart Home Assistant, then add helpers.

Requires WLED 0.14+ (tested on 16.0.1). The core WLED integration is optional: if it's installed, it lists your devices in the zone form, and the light entities WLED creates for our segments are hidden automatically.

## Notes

- WLED creates one segment per lit alert (plus one marker while it has the strip "borrowed"). Most ESP32 builds allow 32 segments.
- If the WLED is in a group with **Sync send** on, the alert segments are sent to the other strips too.
