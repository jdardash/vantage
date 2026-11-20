"""Tests for the weather routes (sda.routes.weather): GET /api/weather/wind and
GET /api/weather/cyclones.

Offline and deterministic. Every upstream fetch helper (_fetch_open_meteo,
_fetch_nhc_status, _fetch_nhc_layer, _fetch_gdacs) is replaced with a fake that
hands back canned Python objects, the wall clock the wind field is served for is
pinned through weather._wall_time, and pacing and cache ages are moved by editing
the monotonic stamps in the module state rather than by waiting. The two tests
that run a real fetch helper swap httpx.AsyncClient for a fake client. The app
under test holds only this router.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.routes import weather

WIND = "/api/weather/wind"
CYCLONES = "/api/weather/cyclones"

#: The wall clock every test runs at: 2026-11-19 15:30 UTC.
NOW = datetime(2026, 11, 19, 15, 30, tzinfo=UTC).timestamp()
#: First hour of each fake series: the current hour minus one, as past_hours=1 gives.
T0 = int(NOW // 3600 * 3600 - 3600)
HOURS = weather.WIND_PAST_HOURS + weather.WIND_FORECAST_HOURS


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    weather.clear_wind_cache()
    weather.clear_cyclone_cache()
    monkeypatch.setattr(weather, "_wall_time", lambda: NOW)
    yield
    weather.clear_wind_cache()
    weather.clear_cyclone_cache()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(weather.router)
    return TestClient(app)


# ==========================================================================
# Wind
# ==========================================================================


class FakeOpenMeteo:
    """Stands in for weather._fetch_open_meteo: one block per place asked for.

    speed(lat, lon, hour) and direction(lat, lon, hour) shape the field; *fail*
    decides per call (1-based) whether the whole request fails; places in *bad*
    come back snapped too far away, so they are answered but unusable.
    """

    def __init__(self, speed=None, direction=None, fail=None, bad=()) -> None:
        self.calls: list[list[tuple[float, float]]] = []
        self.speed = speed or (lambda lat, lon, k: 10.0 + k)
        self.direction = direction or (lambda lat, lon, k: 270.0)
        self.fail = fail
        self.bad = set(bad)

    def block(self, lat: float, lon: float) -> dict:
        snapped_lat = lat + 0.03 if lat < 90 else lat
        # Open-Meteo answers 180 W with the 180 E column, which is the same place
        snapped_lon = 179.97 if lon == -180 else lon - 0.02
        if (lat, lon) in self.bad:
            snapped_lat = lat + 10.0
        return {
            "latitude": snapped_lat,
            "longitude": snapped_lon,
            "utc_offset_seconds": 0,
            "hourly_units": {"time": "unixtime", "wind_speed_10m": "m/s"},
            "hourly": {
                "time": [T0 + k * 3600 for k in range(HOURS)],
                "wind_speed_10m": [self.speed(lat, lon, k) for k in range(HOURS)],
                "wind_direction_10m": [self.direction(lat, lon, k) for k in range(HOURS)],
            },
        }

    async def __call__(self, points: list[tuple[float, float]]) -> object:
        self.calls.append(list(points))
        if self.fail is not None and self.fail(len(self.calls), points):
            return None
        blocks = [self.block(lat, lon) for lat, lon in points]
        return blocks if len(blocks) > 1 else blocks[0]


def use_wind(monkeypatch, **kwargs) -> FakeOpenMeteo:
    fake = FakeOpenMeteo(**kwargs)
    monkeypatch.setattr(weather, "_fetch_open_meteo", fake)
    return fake


def next_pass(seconds: float | None = None) -> None:
    """Age the last pass as if the pacing interval went by."""
    assert weather._wind.pass_at is not None
    weather._wind.pass_at -= (weather.WIND_PASS_INTERVAL_S if seconds is None else seconds) + 1


def expire_grid() -> None:
    """Age the complete grid past its TTL; its last pass is as old."""
    assert weather._wind.good is not None and weather._wind.pass_at is not None
    weather._wind.good.built_at -= weather.WIND_TTL_S + 1
    weather._wind.pass_at -= weather.WIND_TTL_S + 1


def build_all(client: TestClient) -> dict:
    body = client.get(WIND).json()
    for _ in range(10):
        if body["complete"]:
            return body
        next_pass()
        body = client.get(WIND).json()
    raise AssertionError("the wind grid never completed")


def at(body: dict, lat: float, lon: float) -> tuple[float, float]:
    row = round((body["la1"] - lat) / body["dy"])
    col = round((lon - body["lo1"]) / body["dx"]) % body["nx"]
    i = row * body["nx"] + col
    return body["u"][i], body["v"][i]


# --- grid layout and the request budget -------------------------------------------


def test_grid_layout_and_fetch_order():
    assert (weather.WIND_NX, weather.WIND_NY, weather.WIND_POINTS) == (72, 31, 2232)
    assert weather._node_point(0) == (75.0, -180.0)
    assert weather._node_point(71) == (75.0, 175.0)
    assert weather._node_point(72) == (70.0, -180.0)
    assert weather._node_point(2231) == (-75.0, 175.0)
    order = weather._ORDER
    assert sorted(order) == list(range(2232))
    # the 10-degree subgrid comes first and is exactly one pass
    assert len(weather._COARSE) == weather.WIND_PASS_POINTS == 576
    assert set(order[:576]) == weather._COARSE
    for i in order[:576]:
        lat, lon = weather._node_point(i)
        assert (75 - lat) % 10 == 0 and (lon + 180) % 10 == 0


def test_request_budget_fits_the_free_tier():
    # Open-Meteo counts each place as one call: a pass stays under 600 a minute,
    # and two builds a day stay under half of the 10,000 a day.
    assert weather.WIND_PASS_POINTS < 600
    assert weather.WIND_PASS_INTERVAL_S >= 60
    builds_per_day = 86400 / weather.WIND_TTL_S
    assert weather.WIND_POINTS * builds_per_day <= 5000
    # an hour holds at most one build: under the 5000 an hour
    assert weather.WIND_POINTS <= 5000
    assert weather.WIND_PASS_POINTS % weather.WIND_REQUEST_POINTS == 0


def test_uv_follows_the_meteorological_convention():
    def uv(direction):
        u, v = weather._uv(10.0, direction)
        return round(u, 9) + 0.0, round(v, 9) + 0.0

    assert uv(0) == (0.0, -10.0)    # from the north blows south
    assert uv(90) == (-10.0, 0.0)   # from the east blows west
    assert uv(180) == (0.0, 10.0)   # from the south blows north
    assert uv(270) == (10.0, 0.0)   # a westerly blows east


# --- the build: provisional first pass, pacing, completion ---------------------------


def test_first_request_serves_a_provisional_whole_globe_field(client, monkeypatch):
    fake = use_wind(monkeypatch)
    resp = client.get(WIND)
    assert resp.status_code == 200
    body = resp.json()
    # one pass: the 10-degree subgrid in four requests of 144 places
    assert [len(c) for c in fake.calls] == [144, 144, 144, 144]
    asked = {p for c in fake.calls for p in c}
    assert asked == {weather._node_point(i) for i in weather._COARSE}
    assert body["complete"] is False and body["resolution_deg"] == 10
    assert body["filled"] == 576 and body["total"] == 2232
    assert (body["nx"], body["ny"], body["lo1"], body["la1"], body["dx"], body["dy"]) == (
        72, 31, -180.0, 75.0, 5.0, 5.0)
    assert len(body["u"]) == len(body["v"]) == 2232
    assert body["units"] == "m/s" and body["fetched_at"] is None
    assert body["stale"] is False and body["refreshing"] is False
    assert body["valid_time"] == "2026-11-19T15:30:00Z"
    assert "Open-Meteo" in body["source"] and "CC BY 4.0" in body["source"]
    # 15:30 is 1.5 hours into each series: 11.5 m/s everywhere, filled points too
    assert set(body["u"]) == {11.5}
    assert all(abs(v) < 1e-9 for v in body["v"])
    assert body["max_speed"] == 11.5


def test_passes_are_paced_and_complete_the_grid(client, monkeypatch):
    fake = use_wind(monkeypatch)
    first = client.get(WIND).json()
    # inside the pacing interval nothing more is fetched
    again = client.get(WIND).json()
    assert len(fake.calls) == 4 and again == first
    sizes = []
    for n in range(3):
        next_pass()
        body = client.get(WIND).json()
        sizes.append([len(c) for c in fake.calls[4 + 4 * n:]])
        assert body["complete"] is (n == 2)
    assert sizes == [[144, 144, 144, 144], [144, 144, 144, 144], [144, 144, 144, 72]]
    assert body["resolution_deg"] == 5 and body["filled"] == 2232
    assert body["fetched_at"] == "2026-11-19T15:30:00Z"
    assert body["refreshing"] is False
    # every place asked for exactly once across the four passes
    asked = [p for c in fake.calls for p in c]
    assert len(asked) == len(set(asked)) == 2232
    # a complete grid is not rebuilt inside its twelve hours
    next_pass()
    client.get(WIND)
    assert len(fake.calls) == 16


def test_values_land_on_the_right_grid_points(client, monkeypatch):
    # speed rises 0.5 m/s per column; northern rows blow east, southern rows north
    use_wind(
        monkeypatch,
        speed=lambda lat, lon, k: 10.0 + (lon + 180) / 10,
        direction=lambda lat, lon, k: 270.0 if lat >= 0 else 180.0,
    )
    body = build_all(client)
    assert at(body, 75, -180) == (10.0, 0.0)
    assert at(body, 0, 0) == (28.0, 0.0)
    u, v = at(body, -45, 175)
    assert (round(u, 6) + 0.0, v) == (0.0, 45.5)
    assert at(body, 35, 5)[0] == 28.5
    assert body["max_speed"] == 45.5


def test_time_interpolation_and_the_end_of_the_forecast(client, monkeypatch):
    use_wind(monkeypatch)
    build_all(client)
    when = {"t": float(T0 + 10 * 3600)}
    monkeypatch.setattr(weather, "_wall_time", lambda: when["t"])
    assert client.get(WIND).json()["u"][0] == 20.0
    when["t"] = T0 + 2.25 * 3600
    assert client.get(WIND).json()["u"][0] == 12.25
    # before the series: clamped to its first hour
    when["t"] = T0 - 7200
    assert client.get(WIND).json()["u"][0] == 10.0
    # past the last forecast hour (plus one hour of grace): clamped and flagged stale
    last = T0 + (HOURS - 1) * 3600
    when["t"] = last + 3600
    body = client.get(WIND).json()
    assert body["u"][0] == 10.0 + HOURS - 1 and body["stale"] is False
    when["t"] = last + 3601
    assert client.get(WIND).json()["stale"] is True


def test_rebuild_after_ttl_keeps_serving_the_old_grid(client, monkeypatch):
    fake = use_wind(monkeypatch)
    build_all(client)
    expire_grid()
    fake.speed = lambda lat, lon, k: 20.0
    body = client.get(WIND).json()
    assert len(fake.calls) == 20  # a new coarse pass went out
    assert body["complete"] is True and body["refreshing"] is True
    assert body["u"][0] == 11.5  # still the old grid
    for _ in range(3):
        next_pass()
        body = client.get(WIND).json()
    assert body["refreshing"] is False and body["u"][0] == 20.0
    assert len(fake.calls) == 32


def test_failed_rebuild_pass_is_stale_not_an_error(client, monkeypatch):
    fake = use_wind(monkeypatch)
    build_all(client)
    expire_grid()
    fake.fail = lambda n, points: True
    resp = client.get(WIND)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale"] is True and body["complete"] is True and body["refreshing"] is True
    assert body["u"][0] == 11.5
    # the next attempt waits five minutes, not one
    calls = len(fake.calls)
    next_pass()
    client.get(WIND)
    assert len(fake.calls) == calls
    fake.fail = None
    next_pass(weather.WIND_RETRY_S)
    assert client.get(WIND).json()["stale"] is False
    assert len(fake.calls) == calls + 4


def test_first_pass_failure_is_502_and_retries_after_five_minutes(client, monkeypatch):
    fake = use_wind(monkeypatch, fail=lambda n, points: True)
    resp = client.get(WIND)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Open-Meteo wind grid unavailable"
    assert len(fake.calls) == 4
    assert client.get(WIND).status_code == 502
    next_pass()
    assert client.get(WIND).status_code == 502
    assert len(fake.calls) == 4  # no retry inside five minutes
    fake.fail = None
    next_pass(weather.WIND_RETRY_S)
    resp = client.get(WIND)
    assert resp.status_code == 200 and resp.json()["stale"] is False
    assert len(fake.calls) == 8


def test_partial_first_pass_waits_for_the_missing_coarse_points(client, monkeypatch):
    fake = use_wind(monkeypatch, fail=lambda n, points: n == 2)
    assert client.get(WIND).status_code == 502  # a quarter of the 10-degree grid is missing
    missing = fake.calls[1]
    next_pass()
    body = client.get(WIND).json()
    # the next pass starts with the places whose request failed
    assert fake.calls[4] == missing
    assert body["complete"] is False and body["resolution_deg"] == 10
    assert body["filled"] == 1008  # 432 + 576


def test_unusable_place_is_dropped_after_two_misses_and_filled(client, monkeypatch):
    bad = (15.0, 0.0)  # a 10-degree point
    fake = use_wind(monkeypatch, bad=[bad])
    body = client.get(WIND).json()
    # answered but unusable still counts as answered: the field is served, the hole filled
    assert body["complete"] is False and body["filled"] == 575
    assert at(body, 15, 0) == (11.5, 0.0)
    next_pass()
    client.get(WIND)
    assert fake.calls[4][0] == bad  # asked again first in the next pass
    body = build_all(client)
    assert body["filled"] == 2231 and body["complete"] is True
    assert sum(c.count(bad) for c in fake.calls) == 2
    assert at(body, 15, 0) == (11.5, 0.0)


def test_wrong_block_count_counts_as_a_failed_request(client, monkeypatch):
    async def short(points):
        return [FakeOpenMeteo().block(*points[0])]

    monkeypatch.setattr(weather, "_fetch_open_meteo", short)
    assert client.get(WIND).status_code == 502
    assert weather._wind.stale is True
    assert weather._wind.build is not None and not weather._wind.build.misses


def test_fetch_exception_is_a_failed_pass(client, monkeypatch):
    async def boom(points):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(weather, "_fetch_open_meteo", boom)
    assert client.get(WIND).status_code == 502
    assert weather._wind.pass_failed is True


def test_concurrent_requests_share_one_pass(monkeypatch):
    calls = 0

    async def slow(points):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return [FakeOpenMeteo().block(lat, lon) for lat, lon in points]

    monkeypatch.setattr(weather, "_fetch_open_meteo", slow)

    async def many():
        return await asyncio.gather(*(weather.weather_wind() for _ in range(5)))

    results = asyncio.run(many())
    assert calls == 4
    assert all(r.filled == 576 and r.complete is False for r in results)


# --- parsing one place's block and filling holes -------------------------------------


def _block(**hourly) -> dict:
    data = {
        "time": [T0, T0 + 3600, T0 + 7200],
        "wind_speed_10m": [5.0, 6.0, 7.0],
        "wind_direction_10m": [270.0, 270.0, 270.0],
    }
    data.update(hourly)
    return {"latitude": 10.02, "longitude": 20.01, "hourly": data}


def test_parse_block_accepts_a_good_series():
    node = weather._parse_block(_block(), 10.0, 20.0)
    assert node is not None and node.t0 == T0
    assert [round(u, 9) for u in node.u] == [5.0, 6.0, 7.0]


@pytest.mark.parametrize(
    "block",
    [
        None,
        "text",
        {"latitude": 13.0, "longitude": 20.0, "hourly": _block()["hourly"]},  # snapped too far
        {"latitude": 10.0, "longitude": 23.0, "hourly": _block()["hourly"]},
        {"latitude": "x", "longitude": 20.0, "hourly": _block()["hourly"]},
        {"latitude": 10.0, "longitude": 20.0},
        _block(time=[T0, T0 + 3600, T0 + 9000]),  # not hourly
        _block(time=[str(T0), str(T0 + 3600), str(T0 + 7200)]),  # iso strings, not unixtime
        _block(time=[T0 + 0.5, T0 + 3600.5, T0 + 7200.5]),
        _block(wind_speed_10m=[5.0, 6.0]),  # length mismatch
        _block(wind_direction_10m="270"),
        _block(time=[]),
        _block(wind_speed_10m=[None, None, None]),  # no usable hour at all
        _block(wind_speed_10m=[True, True, True]),
    ],
)
def test_parse_block_refuses_junk(block):
    assert weather._parse_block(block, 10.0, 20.0) is None


def test_parse_block_wraps_the_antimeridian_and_marks_bad_hours():
    block = _block(wind_speed_10m=[5.0, None, 500.0], wind_direction_10m=[270, 270, 270])
    block["longitude"] = 179.98
    node = weather._parse_block(block, 10.0, -180.0)
    assert node is not None
    assert round(node.u[0], 9) == 5.0
    assert math.isnan(node.u[1]) and math.isnan(node.u[2])
    # between a good hour and a missing one the good hour is used, not calm
    assert weather._node_at(node, T0 + 1800)[0] == pytest.approx(5.0)
    # with both hours missing the point is a hole, which _fill fills from its neighbours
    assert math.isnan(weather._node_at(node, T0 + 5400)[0])
    assert weather._parse_block(_block(wind_direction_10m=[400, 270, 270]), 10.0, 20.0) is not None


def test_fill_is_bilinear_on_the_ten_degree_subgrid():
    us = [math.nan] * weather.WIND_POINTS
    vs = [math.nan] * weather.WIND_POINTS
    for i in weather._COARSE:
        row, col = divmod(i, weather.WIND_NX)
        us[i], vs[i] = float(col), float(row)

    def idx(row, col):
        return row * weather.WIND_NX + col

    weather._fill(us, vs)
    assert not any(math.isnan(x) for x in us + vs)
    assert us[idx(0, 3)] == 3.0       # between two along a row
    assert vs[idx(5, 4)] == 5.0       # between two along a column
    assert us[idx(5, 7)] == 7.0       # the centre of four
    assert vs[idx(5, 7)] == 5.0
    assert us[idx(0, 71)] == 35.0     # columns wrap: between 70 and 0


def test_fill_leaves_calm_where_nothing_is_near():
    us = [math.nan] * weather.WIND_POINTS
    vs = [math.nan] * weather.WIND_POINTS
    weather._fill(us, vs)
    assert set(us) == {0.0} and set(vs) == {0.0}


# --- the real fetch helper, against a fake httpx client -------------------------------


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


def test_open_meteo_request_shape(monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(200, [{}, {}], seen))
    points = [weather._node_point(i) for i in weather._ORDER[:144]]
    answer = asyncio.run(weather._fetch_open_meteo(points))
    assert answer == [{}, {}]
    assert ("init", weather.WIND_TIMEOUT_S) in seen
    url = next(v for k, v in seen if k == "get")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == weather.OPEN_METEO_URL
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["latitude"].split(",")[:2] == ["75", "75"]
    assert q["longitude"].split(",")[:3] == ["-180", "-170", "-160"]
    assert len(q["latitude"].split(",")) == 144
    assert q["hourly"] == "wind_speed_10m,wind_direction_10m"
    assert (q["wind_speed_unit"], q["timeformat"], q["timezone"]) == ("ms", "unixtime", "GMT")
    assert (q["past_hours"], q["forecast_hours"]) == ("1", "36")
    assert q["cell_selection"] == "nearest"
    assert "key" not in url.lower()
    assert len(url) < 2048  # stays a short URL with 144 places


def test_open_meteo_error_status_is_a_failed_pass(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(429, {"error": True}, seen))
    assert client.get(WIND).status_code == 502
    assert sum(1 for k, _ in seen if k == "get") == 4


# ==========================================================================
# Cyclones
# ==========================================================================

MILTON = {
    "id": "al142026",
    "binNumber": "AT4",
    "name": "Milton",
    "classification": "HU",
    "intensity": "120",
    "pressure": "929",
    "latitude": "23.4N",
    "longitude": "86.8W",
    "latitudeNumeric": 23.4,
    "longitudeNumeric": -86.8,
    "movementDir": 90,
    "movementSpeed": 10,
    "lastUpdate": "2026-11-19T15:00:00.000Z",
    "publicAdvisory": {
        "advNum": "015",
        "issuance": "2026-11-19T15:00:00.000Z",
        "url": "https://www.nhc.noaa.gov/text/refresh/MIATCPAT4+shtml/191500.shtml",
    },
    "forecastAdvisory": {
        "advNum": "015",
        "issuance": "2026-11-19T15:00:00.000Z",
        "url": "https://www.nhc.noaa.gov/text/refresh/MIATCMAT4+shtml/191500.shtml",
    },
}
RAYMOND = {
    "id": "ep152026",
    "name": "Raymond",
    "classification": "TS",
    "intensity": "45",
    "pressure": "1000",
    "latitudeNumeric": 16.0,
    "longitudeNumeric": -105.5,
    "movementDir": 300,
    "movementSpeed": 12,
    "lastUpdate": "2026-11-19T15:00:00.000Z",
    "publicAdvisory": {"url": "http://evil.example/advisory"},
    "forecastAdvisory": {
        "advNum": "010",
        "issuance": "2026-11-19T15:00:00.000Z",
        "url": "https://www.nhc.noaa.gov/text/refresh/MIATCMEP5+shtml/191500.shtml",
    },
}
NHC = {
    "activeStorms": [
        MILTON,
        RAYMOND,
        {**MILTON, "id": "xx012026", "name": "BadId"},
        {**MILTON, "id": "al152026", "name": ""},
        {**MILTON, "id": "al162026", "name": "Nadine", "latitudeNumeric": 95},
        {**MILTON, "name": "Milton again"},  # same id twice
        "junk",
    ]
}


def _pts(source: str, tau: int, wind: int, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"idp_source": source, "tau": tau, "maxwind": wind, "advisnum": "15"},
    }


GIS = {
    5: {
        "type": "FeatureCollection",
        "features": [
            _pts("al142026-015_5day_pts", 24, 90, -80.0, 27.0),
            _pts("al142026-015_5day_pts", 0, 120, -86.8, 23.4),
            _pts("al142026-015_5day_pts", 12, 110, -84.0, 25.0),
            _pts("ep152026-009_5day_pts", 0, 45, -105.5, 16.0),
            _pts("bogus", 0, 45, 0.0, 0.0),
        ],
    },
    6: {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.8, 23.4], [-84.0, 25.0], [-80.0, 27.0]],
                },
                "properties": {"idp_source": "al142026-015_5day_lin"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-105.5, 16.0], [-108, 17]]},
                "properties": {"idp_source": "ep152026-009_5day_lin"},
            },
        ],
    },
    7: {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-87.0, 23.0], [-80.0, 26.0], [-79.0, 28.0], [-86.0, 24.0], [-87.0, 23.0]]
                    ],
                },
                "properties": {"idp_source": "al142026-015_5day_pgn"},
            }
        ],
    },
}


def _gdacs_event(eventid, name, lon, lat, severity, **props) -> dict:
    base = {
        "eventtype": "TC",
        "eventid": eventid,
        "episodeid": 12,
        "eventname": name,
        "name": f"Tropical Cyclone {name}",
        "alertlevel": "Orange",
        "iscurrent": "true",
        "country": "Philippines",
        "fromdate": "2026-11-15T00:00:00",
        "todate": "2026-11-19T12:00:00",
        "datemodified": "2026-11-19T12:00:00",
        "source": "JTWC",
        "severitydata": {"severity": severity, "severitytext": "x", "severityunit": "km/h"},
        "url": {
            "report": f"https://www.gdacs.org/report.aspx?eventid={eventid}&eventtype=TC",
            "geometry": "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry",
        },
    }
    base.update(props)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": base}


GDACS = {
    "type": "FeatureCollection",
    "features": [
        _gdacs_event(1001, "MILTON-26", -86.5, 23.0, 222),  # NHC already has it, by name
        _gdacs_event(1002, "KONG-REY-26", 125.0, 15.0, 213),
        _gdacs_event(1003, "OTHER-26", -86.0, 23.5, 100),  # within 500 km of Milton
        _gdacs_event(1004, "OLD-26", 60.0, -15.0, 100, iscurrent="false"),
        _gdacs_event(1005, "STALE-26", 60.0, -15.0, 100, datemodified="2026-10-01T00:00:00",
                     todate="2026-10-01T00:00:00"),
        _gdacs_event(1006, "QUAKE", 60.0, -15.0, 100, eventtype="EQ"),
        _gdacs_event(1007, "BELAL-26", 57.5, -20.0, 100, alertlevel="green",
                     url={"report": "https://evil.example/report"}),
        _gdacs_event(1002, "KONG-REY-26", 125.0, 15.0, 213),  # listed twice
        _gdacs_event(1008, "KNOTS-26", 150.0, 20.0, 70, severitydata={
            "severity": 70, "severityunit": "kts"}),
        {"type": "Feature", "geometry": None, "properties": {"eventtype": "TC"}},
        "junk",
    ],
}


class Feeds:
    """Fakes for the three cyclone fetch helpers, each with a call count."""

    def __init__(self, nhc=NHC, gis=GIS, gdacs=GDACS) -> None:
        self.nhc, self.gis, self.gdacs = nhc, dict(gis), gdacs
        self.calls = {"nhc": 0, "gis": 0, "gdacs": 0}

    async def fetch_nhc(self):
        self.calls["nhc"] += 1
        return _answer(self.nhc)

    async def fetch_layer(self, layer_id):
        self.calls["gis"] += 1
        return _answer(self.gis.get(layer_id))

    async def fetch_gdacs(self):
        self.calls["gdacs"] += 1
        return _answer(self.gdacs)


def _answer(value):
    if isinstance(value, BaseException):
        raise value
    return value


def use_feeds(monkeypatch, **kwargs) -> Feeds:
    feeds = Feeds(**kwargs)
    monkeypatch.setattr(weather, "_fetch_nhc_status", feeds.fetch_nhc)
    monkeypatch.setattr(weather, "_fetch_nhc_layer", feeds.fetch_layer)
    monkeypatch.setattr(weather, "_fetch_gdacs", feeds.fetch_gdacs)
    return feeds


def age_cyclones(seconds: float) -> None:
    assert weather._cyclones.attempted_at is not None
    weather._cyclones.attempted_at -= seconds + 1


def storms_by_id(body: dict) -> dict:
    return {s["id"]: s for s in body["storms"]}


@pytest.mark.parametrize(
    ("kt", "level"),
    [(None, "UNK"), (0, "TD"), (33, "TD"), (34, "TS"), (63, "TS"), (64, "C1"), (82, "C1"),
     (83, "C2"), (95, "C2"), (96, "C3"), (112, "C3"), (113, "C4"), (136, "C4"), (137, "C5"),
     (180, "C5")],
)
def test_saffir_simpson_boundaries(kt, level):
    assert weather.intensity_for(kt) == level


def test_cyclones_merge_nhc_and_gdacs(client, monkeypatch):
    feeds = use_feeds(monkeypatch)
    resp = client.get(CYCLONES)
    assert resp.status_code == 200
    body = resp.json()
    assert [s["id"] for s in body["storms"]] == [
        "al142026", "ep152026", "gdacs-1002", "gdacs-1007", "gdacs-1008"]
    assert body["count"] == 5
    assert body["sources"] == {"nhc": "ok", "gdacs": "ok"}
    assert body["stale"] is False
    assert "NHC" in body["source"] and "GDACS" in body["source"]
    assert feeds.calls == {"nhc": 1, "gis": 3, "gdacs": 1}


def test_nhc_storm_fields_and_forecast_geometry(client, monkeypatch):
    use_feeds(monkeypatch)
    milton = storms_by_id(client.get(CYCLONES).json())["al142026"]
    assert milton["name"] == "Milton" and milton["source"] == "NHC" and milton["agency"] == "NHC"
    assert milton["basin"] == "AL" and milton["classification"] == "HU"
    assert milton["category"] == "Hurricane (category 4)" and milton["intensity"] == "C4"
    assert milton["wind_kt"] == 120 and milton["wind_basis"] == "current advisory"
    assert milton["pressure_hpa"] == 929
    assert milton["movement_dir_deg"] == 90
    assert milton["movement_speed_kt"] == 9  # 10 mph
    assert milton["advisory"] == "15"
    assert milton["issued_at"] == "2026-11-19T15:00:00Z"
    assert milton["updated_at"] == "2026-11-19T15:00:00Z"
    assert milton["url"] == MILTON["publicAdvisory"]["url"]
    assert (milton["lon"], milton["lat"]) == (-86.8, 23.4)
    assert milton["track"] == [[[-86.8, 23.4], [-84.0, 25.0], [-80.0, 27.0]]]
    assert len(milton["cone"]) == 1 and len(milton["cone"][0]) == 5
    assert [p["tau_h"] for p in milton["forecast_points"]] == [0, 12, 24]
    assert [p["wind_kt"] for p in milton["forecast_points"]] == [120, 110, 90]
    assert milton["geometry_status"] == "current" and milton["geometry_advisory"] == "15"


def test_older_gis_advisory_and_url_host_filter(client, monkeypatch):
    use_feeds(monkeypatch)
    raymond = storms_by_id(client.get(CYCLONES).json())["ep152026"]
    assert raymond["category"] == "Tropical storm" and raymond["intensity"] == "TS"
    assert raymond["agency"] == "NHC" and raymond["basin"] == "EP"
    # the public advisory URL is not on www.nhc.noaa.gov, so the forecast one is used
    assert raymond["url"] == RAYMOND["forecastAdvisory"]["url"]
    assert raymond["geometry_status"] == "older advisory"
    assert raymond["geometry_advisory"] == "9" and raymond["advisory"] == "10"
    assert raymond["cone"] == [] and len(raymond["track"]) == 1


def test_gdacs_events_outside_nhc(client, monkeypatch):
    use_feeds(monkeypatch)
    by_id = storms_by_id(client.get(CYCLONES).json())
    kong = by_id["gdacs-1002"]
    assert kong["source"] == "GDACS" and kong["agency"] == "JTWC"
    assert kong["wind_kt"] == 115 and kong["intensity"] == "C4"  # 213 km/h
    assert kong["wind_basis"] == "event peak"
    assert kong["category"] == "Hurricane/typhoon strength (category 4)"
    assert kong["alert_level"] == "Orange" and kong["country"] == "Philippines"
    assert kong["url"] == "https://www.gdacs.org/report.aspx?eventid=1002&eventtype=TC"
    assert kong["issued_at"] == "2026-11-15T00:00:00Z"
    assert kong["geometry_status"] == "none" and kong["track"] == []
    belal = by_id["gdacs-1007"]
    assert belal["url"] is None and belal["alert_level"] == "Green"
    assert belal["wind_kt"] == 54 and belal["intensity"] == "TS"  # 100 km/h
    assert by_id["gdacs-1008"]["wind_kt"] == 70  # already in knots


def test_gis_failure_marks_geometry_unavailable(client, monkeypatch):
    use_feeds(monkeypatch, gis={5: GIS[5], 6: None, 7: GIS[7]})
    body = client.get(CYCLONES).json()
    nhc = [s for s in body["storms"] if s["source"] == "NHC"]
    assert len(nhc) == 2
    assert all(s["geometry_status"] == "unavailable" and s["track"] == [] for s in nhc)
    assert body["sources"]["nhc"] == "ok"


def test_gis_exception_marks_geometry_unavailable(client, monkeypatch):
    use_feeds(monkeypatch, gis={5: RuntimeError("timeout"), 6: GIS[6], 7: GIS[7]})
    body = client.get(CYCLONES).json()
    assert storms_by_id(body)["al142026"]["geometry_status"] == "unavailable"


def test_no_gis_queries_on_a_quiet_ocean(client, monkeypatch):
    feeds = use_feeds(monkeypatch, nhc={"activeStorms": []})
    body = client.get(CYCLONES).json()
    assert feeds.calls["gis"] == 0
    assert body["sources"] == {"nhc": "ok", "gdacs": "ok"}
    # nothing from NHC to dedupe against: the GDACS Milton is listed
    assert "gdacs-1001" in storms_by_id(body)


def test_502_when_both_fail_then_retry_after_five_minutes(client, monkeypatch):
    feeds = use_feeds(monkeypatch, nhc=None, gdacs=None)
    resp = client.get(CYCLONES)
    assert resp.status_code == 502
    assert resp.json()["detail"] == "NHC and GDACS cyclone feeds unavailable"
    assert client.get(CYCLONES).status_code == 502
    assert feeds.calls == {"nhc": 1, "gis": 0, "gdacs": 1}
    feeds.nhc, feeds.gdacs = NHC, GDACS
    age_cyclones(weather.CYCLONE_RETRY_S)
    resp = client.get(CYCLONES)
    assert resp.status_code == 200 and resp.json()["stale"] is False
    assert feeds.calls["nhc"] == 2


def test_one_source_down_is_flagged(client, monkeypatch):
    use_feeds(monkeypatch, nhc=RuntimeError("down"))
    body = client.get(CYCLONES).json()
    assert body["sources"] == {"nhc": "unavailable", "gdacs": "ok"}
    assert body["stale"] is True
    assert "gdacs-1001" in storms_by_id(body)


def test_last_good_copy_is_kept_and_flagged_stale(client, monkeypatch):
    feeds = use_feeds(monkeypatch)
    client.get(CYCLONES)
    age_cyclones(weather.CYCLONE_TTL_S)
    feeds.gdacs = {"type": "nonsense"}
    body = client.get(CYCLONES).json()
    assert body["sources"] == {"nhc": "ok", "gdacs": "stale"}
    assert body["stale"] is True
    assert "gdacs-1002" in storms_by_id(body)


def test_cache_holds_for_fifteen_minutes(client, monkeypatch):
    feeds = use_feeds(monkeypatch)
    first = client.get(CYCLONES).json()
    age_cyclones(weather.CYCLONE_RETRY_S)  # a healthy cache ignores the retry interval
    assert client.get(CYCLONES).json() == first
    assert feeds.calls["nhc"] == 1
    age_cyclones(weather.CYCLONE_TTL_S)
    client.get(CYCLONES)
    assert feeds.calls == {"nhc": 2, "gis": 6, "gdacs": 2}


@pytest.mark.parametrize(
    "bad",
    [None, [], "text", {"activeStorms": "none"}, {"storms": []}],
)
def test_malformed_nhc_is_unavailable(client, monkeypatch, bad):
    use_feeds(monkeypatch, nhc=bad)
    body = client.get(CYCLONES).json()
    assert body["sources"]["nhc"] == "unavailable"


@pytest.mark.parametrize(
    "bad",
    [None, [], {"type": "FeatureCollection"}, {"features": []}, {"type": "Feature"}],
)
def test_malformed_gdacs_is_unavailable(client, monkeypatch, bad):
    use_feeds(monkeypatch, gdacs=bad)
    body = client.get(CYCLONES).json()
    assert body["sources"]["gdacs"] == "unavailable"


def test_concurrent_cyclone_requests_share_one_refresh(monkeypatch):
    feeds = Feeds()
    calls = {"n": 0}

    async def slow_nhc():
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return NHC

    monkeypatch.setattr(weather, "_fetch_nhc_status", slow_nhc)
    monkeypatch.setattr(weather, "_fetch_nhc_layer", feeds.fetch_layer)
    monkeypatch.setattr(weather, "_fetch_gdacs", feeds.fetch_gdacs)

    async def many():
        return await asyncio.gather(*(weather.weather_cyclones() for _ in range(5)))

    results = asyncio.run(many())
    assert calls["n"] == 1 and feeds.calls["gdacs"] == 1
    assert all(r.count == 5 for r in results)


def test_gdacs_name_suffix_and_distance_dedupe():
    nhc = weather._parse_nhc({"activeStorms": [MILTON]})
    gdacs = weather._parse_gdacs(GDACS, datetime.fromtimestamp(NOW, tz=UTC))
    assert nhc is not None and gdacs is not None
    merged = weather._merge(nhc, gdacs)
    ids = [s.id for s in merged]
    assert "gdacs-1001" not in ids  # MILTON-26 is Milton
    assert "gdacs-1003" not in ids  # 60 km from Milton
    assert weather._base_name("Kong-rey-26") == "KONG-REY"


def test_nhc_and_gis_request_shapes(monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(200, {"activeStorms": []}, seen))
    asyncio.run(weather._fetch_nhc_status())
    asyncio.run(weather._fetch_nhc_layer(7))
    asyncio.run(weather._fetch_gdacs())
    urls = [v for k, v in seen if k == "get"]
    assert urls[0] == weather.NHC_STATUS_URL
    layer = urlsplit(urls[1])
    assert layer.path.endswith("/7/query")
    q = {k: v[0] for k, v in parse_qs(layer.query).items()}
    assert (q["f"], q["outSR"], q["where"]) == ("geojson", "4326", "1=1")
    assert "idp_source" in q["outFields"]
    assert urls[2] == weather.GDACS_URL
    assert all(v == weather.CYCLONE_TIMEOUT_S for k, v in seen if k == "init")


# ==========================================================================
# Source rules
# ==========================================================================


def test_route_module_text_rules():
    text = Path(weather.__file__).read_text(encoding="utf-8")
    assert all(ord(ch) < 127 for ch in text), "weather.py must stay ASCII"
    for word in ("WGS84", "ECEF", "datetime.now()"):
        assert word not in text
