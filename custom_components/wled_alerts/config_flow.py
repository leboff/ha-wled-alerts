"""Config flow: "Create helper → WLED Alerts" offers a Zone or an Alert."""

from __future__ import annotations

from typing import Any

try:  # HA 2026.9+ ships probatio; older releases use voluptuous (same API)
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import selector
from homeassistant.helpers.template import Template

from .const import (
    ANCHOR_END,
    ANCHOR_START,
    CONF_ANCHOR,
    CONF_BLOCK_SIZE,
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_DELAY,
    CONF_DISMISSIBLE,
    CONF_ENTRY_TYPE,
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
    CONF_ZONES,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR,
    DEFAULT_GAP,
    DEFAULT_NIGHT_BRIGHTNESS,
    DEFAULT_PRIORITY,
    DOMAIN,
    MODE_MOMENTARY,
    MODE_STATE,
    STYLE_FLASH,
    STYLE_PULSE,
    STYLE_SOLID,
    TYPE_ALERT,
    TYPE_ZONE,
)
from .wled import WledError, WledStrip

CONF_NAME = "name"


def _wled_hosts(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    """Offer the WLED devices HA already knows about."""
    return [
        selector.SelectOptionDict(value=e.data["host"], label=f"{e.title} ({e.data['host']})")
        for e in hass.config_entries.async_entries("wled")
        if e.data.get("host")
    ]


def _zones(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    return [
        selector.SelectOptionDict(value=e.entry_id, label=e.title)
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.options.get(CONF_ENTRY_TYPE) == TYPE_ZONE
    ]


def zone_schema(hass: HomeAssistant, *, with_name: bool) -> vol.Schema:
    """Form for a zone."""
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME)] = selector.TextSelector()
    fields.update(
        {
            vol.Required(CONF_HOST): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_wled_hosts(hass), custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_START, default=0): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=4096, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_LENGTH, default=18): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=4096, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_ANCHOR, default=ANCHOR_START): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[ANCHOR_START, ANCHOR_END], translation_key="anchor")
            ),
            vol.Required(CONF_BLOCK_SIZE, default=DEFAULT_BLOCK_SIZE): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=50, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_GAP, default=DEFAULT_GAP): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=20, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_BRIGHTNESS, default=DEFAULT_BRIGHTNESS): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=100, step=1, unit_of_measurement="%")
            ),
            vol.Optional(CONF_NIGHT_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["schedule", "input_boolean", "binary_sensor", "switch"])
            ),
            vol.Required(CONF_NIGHT_BRIGHTNESS, default=DEFAULT_NIGHT_BRIGHTNESS): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=100, step=1, unit_of_measurement="%")
            ),
        }
    )
    return vol.Schema(fields)


def alert_schema(hass: HomeAssistant, *, with_name: bool) -> vol.Schema:
    """Form for an alert."""
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME)] = selector.TextSelector()
    fields.update(
        {
            vol.Required(CONF_TEMPLATE): selector.TemplateSelector(),
            vol.Required(CONF_MODE, default=MODE_STATE): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[MODE_STATE, MODE_MOMENTARY], translation_key="mode")
            ),
            vol.Optional(CONF_DELAY): selector.DurationSelector(),
            vol.Optional(CONF_SHOW_FOR, default={"seconds": 30}): selector.DurationSelector(),
            vol.Required(CONF_COLOR, default=DEFAULT_COLOR): selector.ColorRGBSelector(),
            vol.Required(CONF_STYLE, default=STYLE_SOLID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[STYLE_SOLID, STYLE_PULSE, STYLE_FLASH], translation_key="style")
            ),
            vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=100, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_DISMISSIBLE, default=False): selector.BooleanSelector(),
            vol.Required(CONF_FULL_STRIP, default=False): selector.BooleanSelector(),
            vol.Optional(CONF_ZONES): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_zones(hass), multiple=True)
            ),
        }
    )
    return vol.Schema(fields)


async def validate_zone(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Reach the WLED and check the LED range fits."""
    errors: dict[str, str] = {}
    strip = WledStrip(hass, str(data[CONF_HOST]))
    try:
        await strip.async_info()
    except WledError:
        return {CONF_HOST: "cannot_connect"}
    start, length = int(data[CONF_START]), int(data[CONF_LENGTH])
    if strip.length and start + length > strip.length:
        errors[CONF_LENGTH] = "out_of_range"
    return errors


def validate_alert(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Catch template syntax errors before saving."""
    try:
        Template(str(data[CONF_TEMPLATE]), hass).ensure_valid()
    except TemplateError:
        return {CONF_TEMPLATE: "invalid_template"}
    return {}


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Selectors return floats; store ints where we mean ints."""
    out = dict(data)
    for key in (CONF_START, CONF_LENGTH, CONF_BLOCK_SIZE, CONF_GAP, CONF_BRIGHTNESS, CONF_NIGHT_BRIGHTNESS, CONF_PRIORITY):
        if key in out and out[key] is not None:
            out[key] = int(out[key])
    if CONF_COLOR in out:
        out[CONF_COLOR] = [int(c) for c in out[CONF_COLOR]]
    return out


class WledAlertsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a zone or an alert."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick what to create."""
        return self.async_show_menu(step_id="user", menu_options=[TYPE_ZONE, TYPE_ALERT])

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A stretch of LEDs on a WLED strip."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await validate_zone(self.hass, user_input)
            if not errors:
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(
                    title=name, data={}, options={CONF_ENTRY_TYPE: TYPE_ZONE, **_clean(user_input)}
                )
        schema = zone_schema(self.hass, with_name=True)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="zone", data_schema=schema, errors=errors)

    async def async_step_alert(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One thing to show on the LEDs."""
        if not _zones(self.hass):
            return self.async_abort(reason="no_zones")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_alert(self.hass, user_input)
            if not errors:
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(
                    title=name, data={}, options={CONF_ENTRY_TYPE: TYPE_ALERT, **_clean(user_input)}
                )
        schema = alert_schema(self.hass, with_name=True)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="alert", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WledAlertsOptionsFlow:
        """Edit an existing zone or alert."""
        return WledAlertsOptionsFlow()


class WledAlertsOptionsFlow(OptionsFlowWithReload):
    """Edit a zone or alert (the gear icon)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the same form, pre-filled."""
        kind = self.config_entry.options.get(CONF_ENTRY_TYPE)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = (
                await validate_zone(self.hass, user_input)
                if kind == TYPE_ZONE
                else validate_alert(self.hass, user_input)
            )
            if not errors:
                return self.async_create_entry(data={CONF_ENTRY_TYPE: kind, **_clean(user_input)})
        schema = (
            zone_schema(self.hass, with_name=False)
            if kind == TYPE_ZONE
            else alert_schema(self.hass, with_name=False)
        )
        schema = self.add_suggested_values_to_schema(schema, user_input or dict(self.config_entry.options))
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
