"""Talk to a WLED controller and lay alert blocks out on a strip.

Rules:
* We only create, change or delete segments whose name starts with ``wla:<zone>:``.
  Everything else on the strip (your own segments and effects, other automations)
  is left alone while the strip is on.
* Our segments are appended after yours, so WLED draws them on top. Later segments
  overwrite earlier ones where they overlap, so nothing needs to move for an alert
  to show.
* If the strip is off when an alert appears, we turn it on, switch your segments
  off, and add a marker segment (``wla:~borrowed``) recording which ones we
  switched off. The marker lives on the WLED itself, so an HA restart can't lose
  it. When the last alert clears, we restore those segments and turn the strip off
  again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ANCHOR_END, HTTP_TIMEOUT, MAX_SLOTS, MIN_BLOCK, SEGMENT_PREFIX, STYLE_EFFECTS

_LOGGER = logging.getLogger(__name__)

BORROW_PREFIX = f"{SEGMENT_PREFIX}~b:"  # wla:~b:<bri>:<id,id,...>  (WLED names: max 32 chars)


class WledError(Exception):
    """WLED could not be reached or rejected a request."""


@dataclass(frozen=True)
class Block:
    """One alert block to draw."""

    alert_id: str
    start: int
    stop: int
    color: tuple[int, int, int]
    style: str


@dataclass(frozen=True)
class Layout:
    """What a zone should look like right now."""

    blocks: tuple[Block, ...]
    full_strip: bool = False


def plan_layout(
    alerts: list[dict[str, Any]],
    *,
    zone_start: int,
    zone_length: int,
    strip_length: int,
    anchor: str,
    block_size: int,
    gap: int,
) -> Layout:
    """Compute block positions for the active alerts.

    ``alerts`` must already be sorted, most important first. Each item has
    ``id``, ``color``, ``style`` and ``full_strip``.

    The first alert sits at the anchor end of the zone. If the zone can't fit every
    alert at ``block_size``, blocks shrink down to MIN_BLOCK; anything that still
    doesn't fit is dropped (least important first).
    """
    if not alerts:
        return Layout(())

    for alert in alerts:
        if alert.get("full_strip"):
            return Layout(
                (Block(alert["id"], 0, strip_length, tuple(alert["color"]), alert["style"]),),
                full_strip=True,
            )

    zone_start = max(0, min(zone_start, strip_length - 1))
    zone_length = max(1, min(zone_length, strip_length - zone_start))
    gap = max(0, gap)
    count = min(len(alerts), MAX_SLOTS)
    size = max(1, block_size)
    while count and count * size + (count - 1) * gap > zone_length:
        if size > MIN_BLOCK:
            size -= 1
        else:
            count -= 1
    if count == 0:
        count, size, gap = 1, zone_length, 0

    blocks: list[Block] = []
    for index, alert in enumerate(alerts[:count]):
        offset = index * (size + gap)
        if anchor == ANCHOR_END:
            stop = zone_start + zone_length - offset
            start = stop - size
        else:
            start = zone_start + offset
            stop = start + size
        blocks.append(Block(alert["id"], start, stop, tuple(alert["color"]), alert["style"]))
    return Layout(tuple(blocks))


def _parse_borrow(name: str) -> tuple[int, list[int]]:
    """Decode ``wla:~b:<bri>:<id,id,...>``."""
    try:
        _p, _b, bri, ids = name.split(":", 3)
        return int(bri), [int(x) for x in ids.split(",") if x != ""]
    except ValueError:
        return 128, []


class WledStrip:
    """A WLED controller, shared by every zone that uses it."""

    def __init__(self, hass: HomeAssistant, host: str) -> None:
        """Initialise."""
        self.hass = hass
        self.host = host.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
        self.lock = asyncio.Lock()
        self._effects: list[str] = []
        self.length: int | None = None
        self.max_segments: int = 32
        self.name: str | None = None
        self.mac: str | None = None
        self.version: str | None = None
        # Segment ids currently used by this integration (alert blocks + borrow marker).
        self.alert_segment_ids: set[int] = set()
        # zone key -> alert ids the user switched the strip off on (don't re-light for these)
        self.suppressed: dict[str, set[str]] = {}

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        session = async_get_clientsession(self.hass)
        url = f"http://{self.host}{path}"
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                resp = await session.request(method, url, json=payload)
                if resp.status >= 400:
                    raise WledError(f"{method} {url} -> HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise WledError(f"{method} {url} failed: {err}") from err

    async def async_info(self) -> dict[str, Any]:
        """Fetch /json/info and cache basic facts."""
        info = await self._request("GET", "/json/info")
        leds = info.get("leds") or {}
        self.length = int(leds.get("count") or 0) or self.length
        self.max_segments = int(leds.get("maxseg") or self.max_segments)
        self.name = info.get("name")
        self.mac = info.get("mac")
        self.version = info.get("ver")
        if not self._effects:
            try:
                self._effects = list(await self._request("GET", "/json/eff"))
            except WledError:
                self._effects = []
        return info

    def effect_id(self, style: str) -> int:
        """Map a style to a WLED effect id by name, so firmware renumbering doesn't break us."""
        name, fallback, _sx, _ix = STYLE_EFFECTS[style]
        return self._effects.index(name) if name in self._effects else fallback

    async def async_apply(self, zone_key: str, layout: Layout, *, brightness_pct: int) -> None:
        """Make the strip's ``wla:<zone_key>:*`` segments match ``layout``."""
        async with self.lock:
            if self.length is None:
                await self.async_info()
            state = await self._request("GET", "/json/state")
            segments: list[dict[str, Any]] = state.get("seg") or []
            prefix = f"{SEGMENT_PREFIX}{zone_key}:"
            name_of = {s["id"]: str(s.get("n") or "") for s in segments}
            ours = sorted(i for i, n in name_of.items() if n.startswith(prefix))
            borrow = next((s for s in segments if name_of[s["id"]].startswith(BORROW_PREFIX)), None)
            other_alert_segs = [
                i for i, n in name_of.items()
                if n.startswith(SEGMENT_PREFIX) and not n.startswith(prefix) and not n.startswith(BORROW_PREFIX)
            ]
            user_segs = [s for s in segments if not name_of[s["id"]].startswith(SEGMENT_PREFIX)]

            wanted = list(layout.blocks)
            next_free = max((s["id"] for s in segments), default=-1) + 1
            reuse = list(ours)
            ids: list[int] = []
            for _ in wanted:
                if reuse:
                    ids.append(reuse.pop(0))
                else:
                    ids.append(next_free)
                    next_free += 1

            ops: list[dict[str, Any]] = []
            payload: dict[str, Any] = {}
            strip_on = bool(state.get("on"))
            wanted_ids = {b.alert_id for b in wanted}

            # Someone switched the strip off while our alerts were lit: respect that.
            # Give the strip back as it was, and stay dark until a *new* alert appears.
            if not strip_on and (ours or borrow is not None) and wanted:
                self.suppressed[zone_key] = wanted_ids | self.suppressed.get(zone_key, set())
            if not wanted:
                self.suppressed.pop(zone_key, None)
            suppressed = self.suppressed.get(zone_key)
            if suppressed is not None and not strip_on:
                if wanted_ids <= suppressed:
                    wanted, ids = [], []
                    reuse = list(ours)
                else:
                    self.suppressed.pop(zone_key, None)  # something new: light up again

            # --- borrowing the strip from "off" --------------------------------------
            need_borrow = bool(wanted) and not strip_on and borrow is None
            if need_borrow:
                hidden = [s["id"] for s in user_segs if s.get("on", True)]
                marker = f"{BORROW_PREFIX}{int(state.get('bri') or 128)}:{','.join(map(str, hidden))}"
                if len(marker) > 32:  # WLED segment names max out at 32 chars
                    marker = f"{BORROW_PREFIX}{int(state.get('bri') or 128)}:" + ",".join(
                        str(i) for i in hidden if i < 10
                    )
                ops.extend({"id": i, "on": False} for i in hidden)
                ops.append({"id": next_free, "start": 0, "stop": 1, "n": marker, "on": False,
                            "fx": 0, "col": [[0, 0, 0]], "sel": False})
                next_free += 1
                payload.update({"on": True, "bri": 255})

            if ids and max(ids + [next_free - 1]) >= self.max_segments:
                raise WledError(f"{self.host}: out of WLED segments (max {self.max_segments})")

            seg_bri = max(1, min(255, round(255 * brightness_pct / 100)))
            current = {s["id"]: s for s in segments}
            for slot, (seg_id, block) in enumerate(zip(ids, wanted, strict=True)):
                _n, _f, speed, intensity = STYLE_EFFECTS[block.style]
                fx = self.effect_id(block.style)
                cur = current.get(seg_id)
                if cur is not None and (
                    cur.get("start"), cur.get("stop"), cur.get("n"), cur.get("on"), cur.get("bri"),
                    cur.get("fx"), cur.get("sx"), list((cur.get("col") or [[]])[0][:3]),
                ) == (block.start, block.stop, f"{prefix}{slot}", True, seg_bri, fx, speed, list(block.color)):
                    continue  # already exactly right: don't resend (it would restart the effect)
                ops.append({
                    "id": seg_id, "start": block.start, "stop": block.stop, "n": f"{prefix}{slot}",
                    "on": True, "bri": seg_bri, "fx": fx, "sx": speed,
                    "ix": intensity, "pal": 0, "col": [list(block.color), [0, 0, 0], [0, 0, 0]],
                    "grp": 1, "spc": 0, "of": 0, "rev": False, "mi": False, "frz": False,
                    "sel": False, "bm": 0,
                })
            ops.extend({"id": i, "stop": 0} for i in reuse)

            # --- giving the strip back ------------------------------------------------
            if not wanted and borrow is not None and not other_alert_segs:
                bri, hidden = _parse_borrow(name_of[borrow["id"]])
                ops.append({"id": borrow["id"], "stop": 0})
                # If any of the user's segments is on again, someone else (a person, a
                # preset, another automation) started using the strip while we had it.
                # Leave it on and theirs; otherwise put everything back and turn it off.
                in_use = state.get("on") and any(s.get("on", True) for s in user_segs)
                if not in_use:
                    ops.extend({"id": i, "on": True} for i in hidden if i in name_of)
                    payload.update({"on": False, "bri": bri})

            if not ops and not payload:
                self._remember_ids(segments, ops)
                return
            # WLED appends a new segment at index == current count, in array order, so
            # new ids must be sent in ascending order or they land on the wrong index.
            ops.sort(key=lambda op: op["id"])
            payload["seg"] = ops
            _LOGGER.debug("WLED %s zone %s -> %s", self.host, zone_key, payload)
            await self._request("POST", "/json/state", {**payload, "v": True})
            self._remember_ids(segments, ops)

    def _remember_ids(self, segments: list[dict[str, Any]], ops: list[dict[str, Any]]) -> None:
        """Track which segment ids belong to us (so their HA light entities can be hidden)."""
        ids = {s["id"] for s in segments if str(s.get("n") or "").startswith(SEGMENT_PREFIX)}
        for op in ops:
            if op.get("stop") == 0:
                ids.discard(op["id"])
            elif str(op.get("n") or "").startswith(SEGMENT_PREFIX):
                ids.add(op["id"])
        self.alert_segment_ids = ids
