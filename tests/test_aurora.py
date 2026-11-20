"""Tests for the aurora oval route (sda.routes.space, GET /api/aurora).

Offline and deterministic: the OVATION fetch is replaced with a fake that hands
back canned Python objects (so even NaN, infinities and booleans can be fed in),
and the one test that runs the real fetch helper swaps httpx.AsyncClient for a
fake client. The app under test holds only this router.
"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.routes import space

OBS = "2026-11-19T12:00:00Z"
FCST = "2026-11-19T12:30:00Z"


def feed(rows: list, **extra: object) -> dict:
    data: dict = {
        "Observation Time": OBS,
        "Forecast Time": FCST,
        "Data Format": "[Longitude, Latitude, Aurora]",
        "coordinates": rows,
        "type": "MultiPoint",
    }
    data.update(extra)
    return data


# Northern oval over Scandinavia and Alaska, a weak southern one, an equator cell.
ROWS = [
    [20, 68, 42],    # north, lon stays 20
    [200, 65, 61],   # north, lon 200 -> -160
    [359, 70, 7],    # north, lon 359 -> -1
    [180, 66, 5],    # exactly at the default threshold, lon 180 stays 180
    [10, 60, 4],     # below the default threshold
    [100, -65, 18],  # south
    [101, -66, 3],   # south, below the default threshold
    [50, 0, 0],      # equator, quiet
    [51, 10, 0],     # quiet
]


class FakeFetch:
    """Stands in for space._fetch_ovation; returns (or raises) queued answers."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self) -> object:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _fresh_cache():
    space.clear_aurora_cache()
    yield
    space.clear_aurora_cache()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(space.router)
    return TestClient(app)


def use(monkeypatch, *answers: object) -> FakeFetch:
    fake = FakeFetch(*answers)
    monkeypatch.setattr(space, "_fetch_ovation", fake)
    return fake


def expire() -> None:
    """Age the last attempt past the cache TTL, as if ten minutes went by."""
    assert space._state.attempted_at is not None
    space._state.attempted_at -= space.AURORA_CACHE_TTL_S + 1.0


# --- parsing, threshold, longitude -------------------------------------------------


def test_default_threshold_and_shape(client, monkeypatch):
    use(monkeypatch, feed(ROWS))
    resp = client.get("/api/aurora")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"observation_time", "forecast_time", "cells", "count", "max_prob",
                         "hemisphere_max", "source", "stale"}
    assert body["observation_time"] == OBS and body["forecast_time"] == FCST
    assert body["cells"] == [[20, 68, 42], [-160, 65, 61], [-1, 70, 7], [180, 66, 5],
                             [100, -65, 18]]
    assert body["count"] == 5
    assert body["max_prob"] == 61
    assert body["hemisphere_max"] == {"north": 61, "south": 18}
    assert body["source"] == "NOAA SWPC OVATION Prime"
    assert body["stale"] is False


def test_integral_values_stay_integers_on_the_wire(client, monkeypatch):
    use(monkeypatch, feed([[20.0, 68.0, 42.0], [21.5, 68.25, 12.75]]))
    resp = client.get("/api/aurora")
    assert '[20,68,42]' in resp.text.replace(" ", "")
    assert resp.json()["cells"][1] == [21.5, 68.25, 12.75]


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(1, 7), (5, 5), (7, 4), (18, 3), (42, 2), (61, 1), (62, 0), (100, 0)],
)
def test_min_threshold(client, monkeypatch, threshold, expected):
    use(monkeypatch, feed(ROWS))
    body = client.get("/api/aurora", params={"min": threshold}).json()
    assert body["count"] == expected == len(body["cells"])
    assert all(cell[2] >= threshold for cell in body["cells"])
    # the maxima describe the whole grid, whatever the threshold
    assert body["max_prob"] == 61
    assert body["hemisphere_max"] == {"north": 61, "south": 18}


@pytest.mark.parametrize("bad", ["0", "101", "-5", "abc", "5.5"])
def test_min_out_of_range_is_422(client, monkeypatch, bad):
    fake = use(monkeypatch, feed(ROWS))
    assert client.get("/api/aurora", params={"min": bad}).status_code == 422
    assert fake.calls == 0


def test_longitude_wrap(client, monkeypatch):
    rows = [[lon, 65, 50] for lon in (0, 90, 179, 180, 181, 270, 359, 360, -180, -45)]
    use(monkeypatch, feed(rows))
    lons = [cell[0] for cell in client.get("/api/aurora").json()["cells"]]
    assert lons == [0, 90, 179, 180, -179, -90, -1, 0, -180, -45]
    assert all(-180 <= lon <= 180 for lon in lons)


def test_hemispheres_ignore_the_equator_but_max_does_not(client, monkeypatch):
    use(monkeypatch, feed([[0, 0, 9], [1, 1, 6], [2, -1, 8]]))
    body = client.get("/api/aurora").json()
    assert body["max_prob"] == 9
    assert body["hemisphere_max"] == {"north": 6, "south": 8}


def test_full_grid(client, monkeypatch):
    # The real product: 360 x 181 one-degree cells, most of them zero.
    def chance(lon: int, lat: int) -> int:
        return 30 if 60 <= abs(lat) <= 70 and lon % 2 == 0 else 0

    rows = [[lon, lat, chance(lon, lat)] for lon in range(360) for lat in range(-90, 91)]
    use(monkeypatch, feed(rows))
    body = client.get("/api/aurora").json()
    assert body["count"] == 180 * 11 * 2
    assert body["hemisphere_max"] == {"north": 30, "south": 30}
    assert all(-180 <= c[0] <= 180 for c in body["cells"])


# --- malformed input -------------------------------------------------------------


def test_malformed_rows_are_skipped(client, monkeypatch):
    rows = [
        None, "20,68,42", 42, {"lon": 1}, [], [1, 2], [1, 2, 3, 4],
        ["x", 68, 40],                 # not a number
        [True, 68, 40],                # a flag is not a reading
        [20, 68, False],
        [float("nan"), 68, 40],
        [20, float("inf"), 40],
        [20, 68, float("-inf")],
        [-9999.9, 68, 40],             # SWPC-style fill value
        [20, 91, 40], [20, -91, 40],   # latitude out of range
        [361, 68, 40], [-181, 68, 40], # longitude out of range
        [20, 68, 100.5], [20, 68, -1], # probability out of range
        [20, 68, None],
        ["30", "69", "45"],            # numeric strings are readings
        [31, 69, 46],
    ]
    use(monkeypatch, feed(rows))
    resp = client.get("/api/aurora")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cells"] == [[30, 69, 45], [31, 69, 46]]
    assert body["max_prob"] == 46


def test_bad_times_become_empty_strings(client, monkeypatch):
    use(monkeypatch, feed(ROWS, **{"Observation Time": 12345, "Forecast Time": None}))
    body = client.get("/api/aurora").json()
    assert body["observation_time"] == "" and body["forecast_time"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not json",
        {},
        {"coordinates": "x"},
        {"coordinates": None},
        {"coordinates": []},
        {"coordinates": [[None, None, None], ["a", "b", "c"], [1, 2]]},
        [[0, 60, 50]],  # rows without the wrapping object
    ],
)
def test_malformed_feed_is_502_not_500(client, monkeypatch, payload):
    use(monkeypatch, payload)
    resp = client.get("/api/aurora")
    assert resp.status_code == 502
    assert "unavailable" in resp.json()["detail"]


def test_fetch_exception_is_502(client, monkeypatch):
    use(monkeypatch, RuntimeError("network down"))
    assert client.get("/api/aurora").status_code == 502


# --- cache -----------------------------------------------------------------------


def test_cache_reused_within_ttl(client, monkeypatch):
    fake = use(monkeypatch, feed(ROWS))
    first = client.get("/api/aurora").json()
    again = client.get("/api/aurora").json()
    other = client.get("/api/aurora", params={"min": 40}).json()
    assert fake.calls == 1
    assert first == again
    assert other["count"] == 2


def test_refetch_after_ttl(client, monkeypatch):
    fake = use(monkeypatch, feed(ROWS), feed([[20, 68, 90]]))
    assert client.get("/api/aurora").json()["max_prob"] == 61
    expire()
    body = client.get("/api/aurora").json()
    assert fake.calls == 2
    assert body["max_prob"] == 90 and body["cells"] == [[20, 68, 90]]
    assert body["stale"] is False


def test_stale_on_error_keeps_last_good_copy(client, monkeypatch):
    fake = use(monkeypatch, feed(ROWS), None, feed([[20, 68, 90]]))
    good = client.get("/api/aurora").json()
    expire()
    stale = client.get("/api/aurora")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["cells"] == good["cells"]
    # a failed attempt also waits out the TTL: no second request to SWPC
    assert client.get("/api/aurora").json()["stale"] is True
    assert fake.calls == 2
    # and the next good fetch clears the flag
    expire()
    fresh = client.get("/api/aurora").json()
    assert fake.calls == 3
    assert fresh["stale"] is False and fresh["max_prob"] == 90


def test_malformed_feed_after_good_one_is_stale_not_error(client, monkeypatch):
    use(monkeypatch, feed(ROWS), {"coordinates": "garbage"})
    client.get("/api/aurora")
    expire()
    resp = client.get("/api/aurora")
    assert resp.status_code == 200 and resp.json()["stale"] is True
    assert resp.json()["count"] == 5


def test_502_on_first_failure_and_no_retry_within_ttl(client, monkeypatch):
    fake = use(monkeypatch, None, feed(ROWS))
    assert client.get("/api/aurora").status_code == 502
    assert client.get("/api/aurora").status_code == 502
    assert fake.calls == 1
    expire()
    resp = client.get("/api/aurora")
    assert resp.status_code == 200 and resp.json()["stale"] is False
    assert fake.calls == 2


def test_concurrent_requests_share_one_fetch(monkeypatch):
    calls = 0

    async def slow_fetch() -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return feed(ROWS)

    monkeypatch.setattr(space, "_fetch_ovation", slow_fetch)

    async def many() -> list:
        return await asyncio.gather(*(space._current() for _ in range(5)))

    results = asyncio.run(many())
    assert calls == 1
    assert all(product is not None and not stale for product, stale in results)


# --- the real fetch helper, against a fake httpx client ---------------------------


class _FakeResponse:
    def __init__(self, status_code: int, data: object) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> object:
        return self._data


def _fake_client(status_code: int, data: object, seen: list):
    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            seen.append(("init", kwargs.get("timeout")))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def get(self, url, **kwargs):
            seen.append(("get", url))
            return _FakeResponse(status_code, data)

    return _Client


def test_upstream_error_status_is_502(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(503, {}, seen))
    assert client.get("/api/aurora").status_code == 502
    assert ("get", space.OVATION_URL) in seen
    assert ("init", space.OVATION_TIMEOUT_S) in seen


def test_upstream_ok_goes_through_the_parser(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(200, feed(ROWS), seen))
    body = client.get("/api/aurora").json()
    assert body["count"] == 5
    assert seen.count(("get", space.OVATION_URL)) == 1


# --- the parser on its own ---------------------------------------------------------


def test_parser_keeps_cells_from_one_percent():
    product = space._parse_ovation(feed(ROWS))
    assert product is not None
    assert min(cell[2] for cell in product.cells) == 3
    assert len(product.cells) == 7  # the two zero cells are dropped
    assert math.isclose(product.north_max, 61) and math.isclose(product.south_max, 18)
