"""Tests for the universal search route (src/sda/routes/search.py).

Offline: the camera and peak catalogs are built from the small samples in
tests/data/search, the TLE store holds the bundled demo catalog, aircraft come
from a recorded snapshot, and the geocoder and adsb.lol calls are replaced by
fakes that count how often they are asked.
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import cameras, peaks
from sda.demo_tles import DEMO_TLE_TEXT
from sda.routes import search
from sda.routes import worldview as worldview_routes
from sda.tle_store import TLEStore

DATA = Path(__file__).resolve().parent / "data" / "search"
EPOCH_NEAR = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)


def _aircraft() -> list[dict]:
    return json.loads((DATA / "aircraft.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tle_store() -> TLEStore:
    store = TLEStore()
    assert store.load_from_text(DEMO_TLE_TEXT, source="demo") > 0
    return store


@pytest.fixture(scope="module")
def catalogs(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("search-catalogs")
    features = json.loads((DATA / "cameras.geojson").read_text(encoding="utf-8"))["features"]
    cameras.build_database(features, root / "cams" / cameras.DB_NAME, {"origin": "test"})
    peaks.build_catalog(
        DATA / "summits.txt", root / "peaks" / peaks.DB_NAME,
        names=DATA / "geonames.txt", admin1=DATA / "admin1.txt",
    )
    return root


class FakeGeocoder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.answer = json.loads((DATA / "geocode_denver.json").read_text(encoding="utf-8"))

    async def __call__(self, q: str) -> dict:
        self.calls.append(q)
        return self.answer


@pytest.fixture()
def geocoder(monkeypatch) -> FakeGeocoder:
    fake = FakeGeocoder()
    monkeypatch.setattr(worldview_routes, "proxy_geocode", fake)
    monkeypatch.setattr(search, "PLACE_GAP_S", 0.0)
    search.clear_place_cache()
    return fake


@pytest.fixture()
def client(catalogs, tle_store, geocoder, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv(cameras.CAMERA_DIR_ENV, str(catalogs / "cams"))
    monkeypatch.setenv(peaks.PEAK_DIR_ENV, str(catalogs / "peaks"))
    cameras.reset_catalog()
    peaks.reset_catalog()
    monkeypatch.setattr(search, "_store", lambda: tle_store)
    monkeypatch.setattr(search, "_aviation", lambda: None)
    search.clear_aircraft_sources()
    search.register_aircraft_source(_aircraft)
    app = FastAPI()
    app.include_router(search.router)
    with TestClient(app) as c:
        yield c
    search.clear_aircraft_sources()
    cameras.reset_catalog()
    peaks.reset_catalog()


def _get(client: TestClient, **params) -> dict:
    resp = client.get("/api/search", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- pure helpers --------------------------------------------------------------


def test_fold_and_tiers():
    assert search.fold("  C\u00e9rvino-Matterhorn!! ") == "cervino matterhorn"
    assert search.name_tier("iss zarya", "iss zarya") == search.TIER_EXACT
    assert search.name_tier("iss zarya", "iss") == search.TIER_PREFIX
    assert search.name_tier("iss zarya", "zar") == search.TIER_WORD
    assert search.name_tier("iss zarya", "arya") == search.TIER_SUBSTRING
    assert search.name_tier("iss zarya", "hubble") == -1
    assert search.canonical_id("g-eupt") == "GEUPT"


def test_query_shapes_and_lookup_paths():
    assert search.query_shape("25544")["digits"]
    assert search.query_shape("A0001")["alpha5"]
    assert search.query_shape("UAL123")["callsign"]
    assert search.query_shape("G-EUPT")["registration"]
    assert search.query_shape("N12345")["registration"]
    assert search.query_shape("a1b2c3")["hex"]
    assert not search.query_shape("Denver")["callsign"]
    assert not search.query_shape("facade")["hex"]  # six hex letters but no digit: a word
    assert search.live_lookup_path("UAL123") == "/v2/callsign/UAL123"
    assert search.live_lookup_path("g-eupt") == "/v2/reg/G-EUPT"
    assert search.live_lookup_path("A1B2C3") == "/v2/hex/a1b2c3"
    assert search.live_lookup_path("Denver") is None
    assert search.kind_order("25544")[1] == "satellite"
    assert search.kind_order("BAW12A")[1] == "aircraft"
    assert search.kind_order("Denver")[1] == "place"


def test_parse_coordinate():
    assert search.parse_coordinate("39.74, -104.99") == (39.74, -104.99)
    assert search.parse_coordinate("-33.9 151.2") == (-33.9, 151.2)
    assert search.parse_coordinate("95, 10") is None
    assert search.parse_coordinate("Denver") is None
    assert search.parse_coordinate("25544") is None


# --- satellites ------------------------------------------------------------------


def test_satellite_by_norad_id_with_position(tle_store):
    rows = search.search_satellites(tle_store, "25544", 5, when=EPOCH_NEAR)
    assert rows[0]["name"] == "ISS (ZARYA)"
    assert rows[0]["tier"] == search.TIER_EXACT
    assert rows[0]["extra"]["norad_id"] == 25544
    assert 380_000 < rows[0]["alt_m"] < 450_000  # LEO, about 420 km
    assert -52 <= rows[0]["lat"] <= 52  # inclination 51.6 deg
    assert -180 <= rows[0]["lon"] <= 180


def test_satellite_by_name_and_id_prefix(tle_store):
    by_name = search.search_satellites(tle_store, "iss", 5, when=EPOCH_NEAR)
    # "iss" is an alias of 25544, so ZARYA is exact rather than one prefix hit of many
    assert by_name[0]["name"] == "ISS (ZARYA)" and by_name[0]["tier"] == search.TIER_EXACT
    prefix = search.search_satellites(tle_store, "2554", 50, when=EPOCH_NEAR)
    assert any(r["id"] == "25544" and r["tier"] == search.TIER_PREFIX for r in prefix)
    assert search.search_satellites(tle_store, "zzzz-no-such", 5) == []


# --- aircraft --------------------------------------------------------------------


def test_aircraft_match_tiers_and_dedupe(monkeypatch):
    monkeypatch.setattr(search, "_aviation", lambda: None)
    search.clear_aircraft_sources()
    assert search._aircraft_records() == ([], False)
    search.register_aircraft_source(_aircraft)
    try:
        merged, ok = search._aircraft_records()
    finally:
        search.clear_aircraft_sources()
    assert ok
    assert [r["lat"] for r in merged if r["icao24"] == "a1b2c3"] == [40.0]  # newest fix wins
    rows = search.match_aircraft(merged, "ual123", 10)
    assert [r["id"] for r in rows] == ["a1b2c3"]  # UAL1234 has no position and is skipped
    assert rows[0]["tier"] == search.TIER_EXACT
    by_reg = search.match_aircraft(_aircraft(), "geupt", 10)
    assert by_reg[0]["name"] == "BAW12A" and by_reg[0]["tier"] == search.TIER_EXACT
    assert "G-EUPT" in by_reg[0]["detail"]
    by_hex = search.match_aircraft(_aircraft(), "a99999", 10)
    assert by_hex[0]["name"] == "A99999"  # private aircraft: no callsign or registration
    assert search.match_aircraft(_aircraft(), "UAL", 10)[0]["tier"] == search.TIER_PREFIX


def test_aircraft_endpoint_newest_fix(client):
    body = _get(client, q="UAL123", places="false")
    first = body["results"][0]
    assert first["kind"] == "aircraft" and first["lat"] == 40.0
    assert first["extra"]["registration"] == "N12345"
    assert body["sources"]["aircraft"] == "ok"


def test_live_lookup_goes_through_aviation_budget(client, monkeypatch):
    store: list[dict] = []
    asked: list[tuple[str, str]] = []

    async def fetch_v2(path: str, purpose: str = "lookup"):
        asked.append((path, purpose))
        store.append({"icao24": "a0a0a0", "callsign": "DAL45", "registration": "N801DZ",
                      "type": "A321", "lat": 33.6, "lon": -84.4, "alt_m": 3000.0,
                      "military": False, "seen_at": 1})
        return {"ac": []}

    fake = types.SimpleNamespace(aircraft_snapshot=lambda: list(store), fetch_v2=fetch_v2)
    monkeypatch.setattr(search, "_aviation", lambda: fake)

    typed = _get(client, q="DAL45", places="false")
    assert asked == [] and not [r for r in typed["results"] if r["kind"] == "aircraft"]

    entered = _get(client, q="DAL45", places="false", live="true")
    assert asked == [("/v2/callsign/DAL45", "lookup")]
    assert entered["results"][0]["name"] == "DAL45"

    # An exact hit already in the cache never spends a request.
    _get(client, q="UAL123", places="false", live="true")
    assert len(asked) == 1


# --- cameras, peaks, places ------------------------------------------------------


def test_cameras_and_peaks(client):
    cams = _get(client, q="Denver", kinds="camera")
    names = [r["name"] for r in cams["results"]]
    assert names[0] == "Denver Union Station"  # name prefix beats a city match
    assert "I-25 at Colfax" in names
    assert cams["results"][0]["extra"]["rid"] > 0
    assert all(s == "skipped" for k, s in cams["sources"].items() if k != "camera")

    whitney = _get(client, q="whitney", kinds="peak")["results"][0]
    # "Mount" is a generic summit word, so "whitney" is an exact match
    assert whitney["name"] == "Mount Whitney" and whitney["tier"] == search.TIER_EXACT
    assert "4,4" in whitney["detail"] and whitney["extra"]["peak_id"] > 0
    tahoma = _get(client, q="tahoma", kinds="peak")["results"]
    assert tahoma and tahoma[0]["name"] == "Mount Rainier"


def test_places_only_when_asked_and_cached(client, geocoder):
    typing = _get(client, q="Denver", places="false")
    assert geocoder.calls == [] and typing["sources"]["place"] == "skipped"
    entered = _get(client, q="Denver")
    assert geocoder.calls == ["Denver"]
    kinds = [r["kind"] for r in entered["results"]]
    assert kinds[0] == "place" and entered["results"][0]["name"] == "Denver"
    assert "camera" in kinds
    _get(client, q="denver ")
    assert geocoder.calls == ["Denver"]  # same folded query: served from the cache


def test_coordinate_query_skips_the_geocoder(client, geocoder):
    body = _get(client, q="39.74, -104.99")
    assert body["results"][0]["kind"] == "coordinate"
    assert body["results"][0]["lat"] == 39.74 and body["results"][0]["lon"] == -104.99
    assert geocoder.calls == []


def test_every_kind_keeps_a_share(client):
    body = _get(client, q="Denver", limit=4)
    kinds = {r["kind"] for r in body["results"]}
    assert {"place", "camera"} <= kinds
    assert body["count"] == len(body["results"]) <= 4


def test_digit_query_puts_satellites_first(client):
    body = _get(client, q="25544", places="false")
    assert body["results"][0]["kind"] == "satellite"
    assert body["results"][0]["name"] == "ISS (ZARYA)"


def test_missing_catalogs_report_unavailable(tmp_path, tle_store, geocoder, monkeypatch):
    monkeypatch.setenv(cameras.CAMERA_DIR_ENV, str(tmp_path / "none-c"))
    monkeypatch.setenv(peaks.PEAK_DIR_ENV, str(tmp_path / "none-p"))
    cameras.reset_catalog()
    peaks.reset_catalog()
    monkeypatch.setattr(search, "_store", lambda: tle_store)
    monkeypatch.setattr(search, "_aviation", lambda: None)
    search.clear_aircraft_sources()
    app = FastAPI()
    app.include_router(search.router)
    try:
        with TestClient(app) as c:
            body = _get(c, q="whitney", places="false")
    finally:
        cameras.reset_catalog()
        peaks.reset_catalog()
    assert body["sources"]["camera"] == "unavailable"
    assert body["sources"]["peak"] == "unavailable"
    assert body["sources"]["aircraft"] == "unavailable"
    assert body["results"] == []


def test_validation(client):
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    assert client.get("/api/search", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/api/search", params={"q": "x" * 121}).status_code == 422


# --- ranking: aliases, summit words, minor places ---------------------------


def test_summit_words_count_as_exact():
    assert search.summit_tier("mount everest", "everest") == search.TIER_EXACT
    assert search.summit_tier("monte rosa", "rosa") == search.TIER_EXACT
    assert search.summit_tier("everest north summit", "everest") == search.TIER_PREFIX
    assert search.summit_tier("mount everest", "mount everest") == search.TIER_EXACT
    assert search.summit_tier("mount", "everest") == -1


def test_iss_alias_puts_zarya_first(tle_store):
    rows = search.search_satellites(tle_store, "iss", 10, when=EPOCH_NEAR)
    assert rows and rows[0]["id"] == "25544" and rows[0]["tier"] == search.TIER_EXACT
    same_tier = [int(r["id"]) for r in rows if r["tier"] == search.TIER_PREFIX]
    assert same_tier == sorted(same_tier)  # oldest object of a family first


def test_minor_exact_places_drop_a_tier_and_importance_orders(monkeypatch):
    import asyncio

    answer = {"results": [
        {"name": "Everest, Brown County, Kansas, United States", "type": "administrative",
         "lat": 39.68, "lon": -95.42, "importance": 0.438},
        {"name": "Mount Everest, Khumjung, Nepal", "type": "peak",
         "lat": 27.988, "lon": 86.925, "importance": 0.614},
        {"name": "Everest, Cass County, North Dakota, United States", "type": "hamlet",
         "lat": 46.99, "lon": -97.06, "importance": 0.133},
        {"name": "Paris, Ile-de-France, France", "type": "administrative",
         "lat": 48.85, "lon": 2.35},
    ]}

    async def fake(q):
        return answer

    monkeypatch.setattr(worldview_routes, "proxy_geocode", fake)
    monkeypatch.setattr(search, "PLACE_GAP_S", 0.0)
    search.clear_place_cache()
    rows, status = asyncio.run(search.search_places("everest", 8))
    assert status == "ok"
    got = [(r["name"], r["tier"]) for r in rows]
    assert got[:3] == [
        ("Mount Everest", search.TIER_EXACT),
        ("Everest", search.TIER_PREFIX),  # Kansas, importance 0.438
        ("Everest", search.TIER_PREFIX),  # North Dakota, 0.133
    ]
    assert rows[1]["detail"].endswith("Kansas, United States")
    search.clear_place_cache()
