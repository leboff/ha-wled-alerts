"""Constants for WLED Alerts."""

from __future__ import annotations

DOMAIN = "wled_alerts"

CONF_ENTRY_TYPE = "entry_type"
TYPE_ZONE = "zone"
TYPE_ALERT = "alert"

# Zone options
CONF_HOST = "host"
CONF_START = "start"
CONF_LENGTH = "length"
CONF_ANCHOR = "anchor"
CONF_BLOCK_SIZE = "block_size"
CONF_GAP = "gap"
CONF_BRIGHTNESS = "brightness"
CONF_NIGHT_ENTITY = "night_entity"
CONF_NIGHT_BRIGHTNESS = "night_brightness"

ANCHOR_START = "start"
ANCHOR_END = "end"

# Alert options
CONF_TEMPLATE = "active_template"
CONF_MODE = "mode"
CONF_DELAY = "delay"
CONF_SHOW_FOR = "show_for"
CONF_COLOR = "color"
CONF_STYLE = "style"
CONF_PRIORITY = "priority"
CONF_DISMISSIBLE = "dismissible"
CONF_ZONES = "zones"
CONF_FULL_STRIP = "full_strip"
CONF_SHOW_AT_NIGHT = "show_at_night"

MODE_STATE = "state"
MODE_MOMENTARY = "momentary"

STYLE_SOLID = "solid"
STYLE_PULSE = "pulse"
STYLE_FLASH = "flash"

# WLED effect per style: (effect name, fallback id, speed, intensity)
STYLE_EFFECTS: dict[str, tuple[str, int, int, int]] = {
    STYLE_SOLID: ("Solid", 0, 128, 128),
    STYLE_PULSE: ("Breathe", 2, 170, 128),
    STYLE_FLASH: ("Blink", 1, 215, 128),
}

DEFAULT_BLOCK_SIZE = 4
DEFAULT_GAP = 1
DEFAULT_BRIGHTNESS = 100
DEFAULT_NIGHT_BRIGHTNESS = 25
DEFAULT_PRIORITY = 50
DEFAULT_COLOR = [255, 0, 0]
MIN_BLOCK = 2
MAX_SLOTS = 12

# Our WLED segments are named "wla:<zone key>:<slot>", so we can always find them
# again even if presets or other tools renumber segments.
SEGMENT_PREFIX = "wla:"

POLL_SECONDS = 30
HTTP_TIMEOUT = 8

FALSY_VALUES = {"", "none", "unknown", "unavailable", "false", "off", "0", "no"}
