"""Test fixtures: a fake WLED that mimics firmware 16.x JSON-API segment handling."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

pytest_plugins = "pytest_homeassistant_custom_component"

HOST = "192.168.1.50"
EFFECTS = ["Solid", "Blink", "Breathe", "Wipe", "Percent"]


class FakeWled:
    """Enough of WLED's /json/state behaviour to test segment management."""

    def __init__(self, leds: int = 106, maxseg: int = 32) -> None:
        self.leds = leds
        self.maxseg = maxseg
        self.on = False
        self.bri = 212
        self.segs: list[dict[str, Any]] = [self._seg(0, 0, leds, n="", col=[[177, 255, 99]], fx=218)]
        self.posts: list[dict[str, Any]] = []
        self.offline = False

    @staticmethod
    def _seg(id_: int, start: int, stop: int, **kw: Any) -> dict[str, Any]:
        base = {"id": id_, "start": start, "stop": stop, "n": "", "on": True, "bri": 255,
                "fx": 0, "sx": 128, "ix": 128, "pal": 0, "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]]}
        base.update(kw)
        return base

    def state(self) -> dict[str, Any]:
        return {"on": self.on, "bri": self.bri, "transition": 7, "ps": -1, "pl": -1,
                "seg": copy.deepcopy([s for s in self.segs if s["stop"] > 0])}

    def apply(self, payload: dict[str, Any]) -> None:
        self.posts.append(copy.deepcopy(payload))
        if "bri" in payload:
            self.bri = payload["bri"]
        if "on" in payload:
            self.on = bool(payload["on"])
        for op in payload.get("seg", []):
            sid = op["id"]
            assert sid < self.maxseg, f"segment id {sid} >= maxseg"
            existing = next((s for s in self.segs if s["id"] == sid), None)
            if existing is None:
                if op.get("stop", -1) <= 0:
                    continue  # WLED ignores empty new segments
                # WLED appends at the END of the list (id = count), regardless of requested id
                new_id = len(self.segs)
                assert new_id == sid, f"WLED would create id {new_id}, caller asked {sid}"
                existing = self._seg(new_id, 0, self.leds)
                self.segs.append(existing)
            if op.get("stop") == 0:
                existing["stop"] = 0
                continue
            for k, v in op.items():
                if k != "id":
                    existing[k] = v
        # purge trailing deleted segments like WLED does (keeps ids dense at the end)
        while len(self.segs) > 1 and self.segs[-1]["stop"] == 0:
            self.segs.pop()

    def ours(self) -> list[dict[str, Any]]:
        return [s for s in self.segs if s["stop"] > 0 and str(s.get("n", "")).startswith("wla:")]

    def blocks(self) -> list[tuple[int, int, list[int], int]]:
        """(start, stop, rgb, fx) of alert blocks, ordered by start."""
        return sorted(
            (s["start"], s["stop"], list(s["col"][0][:3]), s["fx"])
            for s in self.ours() if not s["n"].startswith("wla:~")
        )


@pytest.fixture
def fake_wled() -> FakeWled:
    """A fresh fake strip."""
    return FakeWled()


@pytest.fixture
def wled_http(aioclient_mock, fake_wled: FakeWled):
    """Route HTTP calls for HOST to the fake strip."""

    async def info(method, url, data):
        if fake_wled.offline:
            return AiohttpClientMockResponse(method, url, exc=TimeoutError())
        return AiohttpClientMockResponse(method, url, json={
            "ver": "16.0.1", "name": "Test WLED", "mac": "aabbccddeeff",
            "leds": {"count": fake_wled.leds, "maxseg": fake_wled.maxseg}})

    async def state(method, url, data):
        if fake_wled.offline:
            return AiohttpClientMockResponse(method, url, exc=TimeoutError())
        if method.lower() == "post":
            body = data if isinstance(data, dict) else json.loads(data)
            fake_wled.apply(body)
        return AiohttpClientMockResponse(method, url, json=fake_wled.state())

    async def eff(method, url, data):
        return AiohttpClientMockResponse(method, url, json=EFFECTS)

    aioclient_mock.get(f"http://{HOST}/json/info", side_effect=info)
    aioclient_mock.get(f"http://{HOST}/json/eff", side_effect=eff)
    aioclient_mock.get(f"http://{HOST}/json/state", side_effect=state)
    aioclient_mock.post(f"http://{HOST}/json/state", side_effect=state)
    return aioclient_mock


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ from this repo."""
    yield
