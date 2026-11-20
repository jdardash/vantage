"""Tests for the maritime layer (sda.maritime) and its routes.

The AIS feed is a list of rows shaped like sda.routes.worldview's cache, handed in
through monkeypatch; the stream itself is never opened. Sanctions come from the
recorded OFAC and UK samples in tests/data/sanctions. Times are explicit epochs, so
nothing here waits.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import maritime, sanctions
from sda.routes.maritime import router

DATA = Path(__file__).resolve().parent / "data" / "sanctions"

# 2026-11-19 12:00:00 UTC
T0 = 1795089600.0

LANES = {
    "gibraltar": [(-5.60, 35.95), (-5.90, 35.93), (-5.40, 35.97)],
    "hormuz": [(56.40, 26.55), (56.20, 26.45), (56.70, 26.45)],
    "bab_el_mandeb": [(43.36, 12.60), (43.45, 12.45), (43.30, 12.75)],
    "suez": [(32.31, 31.25), (32.30, 30.60), (32.38, 30.33), (32.55, 29.95)],
    "malacca": [(101.60, 2.35), (101.50, 2.30)],
    "panama": [(-79.92, 9.27), (-79.68, 9.07), (-79.59, 9.00), (-79.565, 8.95)],
    "bosporus": [(29.056, 41.085), (29.08, 41.17), (29.00, 41.02), (29.12, 41.21)],
    "dover": [(1.50, 51.02), (1.60, 51.05), (1.70, 51.10)],
    "taiwan_strait": [(120.25, 24.95), (119.90, 25.20), (120.60, 24.80)],
}


def row(mmsi: str, lat: float, lon: float, received: float, sog=12.0, cog=90.0, nav=0,
        name="") -> dict:
    return {"mmsi": mmsi, "name": name, "lat": lat, "lon": lon, "sog_kn": sog,
            "cog_deg": cog, "heading_deg": cog, "nav_status": nav,
            "time_utc": "", "received": received}


def gib(t: float) -> tuple[float, float]:
    """(lat, lon) a fraction t along the Gibraltar monitor's axis."""
    (lon_a, lat_a), (lon_b, lat_b) = maritime.CHOKEPOINT_BY_ID["gibraltar"].axis
    return lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t


@pytest.fixture
def tracker():
    t = maritime.reset_tracker()
    yield t
    maritime.reset_tracker()


@pytest.fixture
def store():
    s = sanctions.SanctionsStore(DATA / "never-written", allow_network=False)
    ofac = sanctions.parse_ofac((DATA / "sdn_sample.csv").read_text(encoding="utf-8"))
    with (DATA / "uk_sanctions_sample.csv").open(encoding="utf-8-sig", newline="") as fh:
        uk = sanctions.parse_uk(fh)
    s._install({"ofac": ofac, "uk": uk}, {}, time.time())
    sanctions.set_store(s)
    yield s
    sanctions.set_store(None)


# --- geometry ---------------------------------------------------------------------


def test_destination_and_distance_on_the_sphere():
    lat, lon = maritime.destination(0.0, 0.0, 90.0, 111.19508)
    assert abs(lat) < 1e-9 and lon == pytest.approx(1.0, abs=1e-5)
    lat, lon = maritime.destination(10.0, 20.0, 0.0, 111.19508)
    assert lat == pytest.approx(11.0, abs=1e-5) and lon == pytest.approx(20.0, abs=1e-9)
    lat, lon = maritime.destination(0.0, 179.9, 90.0, 22.239)
    assert lon == pytest.approx(-179.9, abs=1e-3)  # wraps at the antimeridian
    assert maritime.distance_km(0, 0, 0, 1) == pytest.approx(111.195, abs=1e-3)


def test_point_in_polygon():
    square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    assert maritime.point_in_polygon(0.5, 0.5, square)
    assert not maritime.point_in_polygon(1.5, 0.5, square)
    assert not maritime.point_in_polygon(0.5, -0.1, square)


def test_the_nine_chokepoints():
    assert [cp.id for cp in maritime.CHOKEPOINTS] == [
        "gibraltar", "hormuz", "bab_el_mandeb", "suez", "malacca", "panama", "bosporus",
        "dover", "taiwan_strait"]
    for cp in maritime.CHOKEPOINTS:
        for lon, lat in LANES[cp.id]:
            assert cp.contains(lon, lat), (cp.id, lon, lat)
        a, b = cp.axis
        assert cp.along(*a) == pytest.approx(0.0, abs=1e-9)
        assert cp.along(*b) == pytest.approx(1.0, abs=1e-9)
        d = cp.as_dict()
        assert d["polygon"][0] == list(cp.polygon[0]) and len(d["center"]) == 2
    for a, b in itertools.combinations(maritime.CHOKEPOINTS, 2):
        assert not any(b.contains(*p) for p in a.polygon), (a.id, b.id)


def test_ais_field_cleaning():
    assert maritime.clean_sog(102.3) is None and maritime.clean_sog(-1) is None
    assert maritime.clean_sog("12.5") == 12.5
    assert maritime.clean_cog(360) is None and maritime.clean_cog(359.9) == 359.9
    assert maritime.clean_nav(15) == 15 and maritime.clean_nav(16) is None
    assert maritime.clean_nav(True) is None


def test_dead_reckoning_follows_course_and_speed():
    track = {"lat": 0.0, "lon": 0.0, "sog_kn": 10.0, "cog_deg": 90.0, "nav_status": 0,
             "received": T0}
    lat, lon, run = maritime.dead_reckon(track, T0 + 3600)
    assert run == pytest.approx(18.52)
    assert lon == pytest.approx(18.52 / 111.19508, abs=1e-6) and abs(lat) < 1e-9
    _, _, capped = maritime.dead_reckon(track, T0 + 24 * 3600)
    assert capped == pytest.approx(10 * 1.852 * 6)
    assert maritime.dead_reckon({**track, "nav_status": 1}, T0 + 3600) is None
    assert maritime.dead_reckon({**track, "sog_kn": 2.0}, T0 + 3600) is None
    assert maritime.dead_reckon({**track, "cog_deg": None}, T0 + 3600) is None


# --- tracker ------------------------------------------------------------------------


def test_dark_ship_after_thirty_minutes(tracker):
    tracker.ingest([row("1", 10.0, 60.0, T0, sog=12, cog=45),
                    row("2", 10.5, 60.5, T0, sog=0.1, nav=1)], now=T0)
    assert [t["mmsi"] for t in tracker.live(T0 + 600)] == ["1", "2"]
    assert tracker.dark(T0 + 600) == []
    dark = tracker.dark(T0 + 40 * 60)
    assert [d["mmsi"] for d in dark] == ["1"]  # the anchored ship is not estimated
    d = dark[0]
    assert d["silent_s"] == pytest.approx(2400)
    assert d["run_km"] == pytest.approx(12 * 1.852 * 40 / 60)
    assert d["est_lat"] > 10.0 and d["est_lon"] > 60.0
    assert tracker.dark(T0 + 7 * 3600) == []
    tracker.ingest([], now=T0 + 8 * 3600)
    assert tracker.tracks == {}  # forgotten once past the window


def test_old_and_repeated_reports_are_ignored(tracker):
    assert tracker.ingest([row("1", 10.0, 60.0, T0)], now=T0) == 1
    assert tracker.ingest([row("1", 10.0, 60.0, T0)], now=T0 + 2) == 0
    assert tracker.ingest([row("1", 11.0, 60.0, T0 - 5)], now=T0 + 4) == 0
    assert tracker.tracks["1"]["lat"] == 10.0
    assert tracker.ingest([{"mmsi": "x"}, "junk", row("3", 95.0, 0.0, T0)], now=T0) == 0


def test_track_limit_evicts_the_quietest(tracker, monkeypatch):
    monkeypatch.setattr(maritime, "TRACK_LIMIT", 3)
    tracker.ingest([row(str(i), 1.0, float(i), T0 + i) for i in range(5)], now=T0 + 5)
    assert list(tracker.tracks) == ["2", "3", "4"]


def test_transit_counted_once_per_passage(tracker):
    t = T0
    for frac in (0.05, 0.3, 0.55, 0.7, 0.9, 0.95):
        lat, lon = gib(frac)
        t += 600
        tracker.ingest([row("100", lat, lon, t)], now=t)
    stats = {s["id"]: s for s in tracker.chokepoint_stats(t)}
    assert stats["gibraltar"]["transits_total"] == 1
    assert stats["gibraltar"]["transits_today"] == 1
    assert stats["gibraltar"]["by_day"] == {"2026-11-19": 1}
    assert stats["gibraltar"]["inside_now"] == 1
    assert sum(s["transits_total"] for s in stats.values()) == 1

    # Back the other way after more than an hour away: a new passage.
    t += 2 * 3600
    for frac in (0.9, 0.5, 0.1):
        lat, lon = gib(frac)
        t += 600
        tracker.ingest([row("100", lat, lon, t, cog=270)], now=t)
    assert tracker.transits_total["gibraltar"] == 2


def test_crossings_and_anchored_ships_are_not_transits(tracker):
    t = T0
    # A ferry from Tarifa to Tangier: across the strait, not along it.
    for lat in (36.04, 35.98, 35.92, 35.86, 35.84):
        t += 300
        tracker.ingest([row("ferry", lat, -5.60, t, cog=180)], now=t)
    # A tanker at anchor, swinging.
    for k in range(6):
        t += 120
        tracker.ingest([row("anchor", 35.95 + 0.001 * k, -5.45, t, sog=0.2, nav=1)], now=t)
    assert tracker.transits_total["gibraltar"] == 0
    stats = {s["id"]: s for s in tracker.chokepoint_stats(t)}
    assert stats["gibraltar"]["inside_now"] == 2
    assert stats["gibraltar"]["under_way_now"] == 1  # the ferry


def test_day_buckets_are_utc_and_bounded(tracker):
    days = tracker.transits_by_day["dover"]
    for i in range(20):
        days[f"2026-10-{i + 1:02d}"] = 1
    tracker.ingest([], now=T0)
    assert len(days) == maritime.DAY_HISTORY
    assert min(days) == "2026-10-07"
    assert maritime.utc_day(T0) == "2026-11-19"


# --- sanctions on tracks -----------------------------------------------------------------


def test_sanction_flags_by_mmsi_and_name(store):
    listed = maritime.sanction_flags({"mmsi": "572469210", "name": "WHATEVER"}, store)
    assert listed["sanction"]["programme"] == "IRAN"
    assert listed["sanction"]["name"] == "ARTAVIL" and listed["sanction"]["imo"] == "9187629"
    assert "name_match" not in listed
    lead = maritime.sanction_flags({"mmsi": "123456789", "name": "Galaxy"}, store)
    assert "sanction" not in lead
    assert lead["name_match"]["name"] == "GALAXY" and lead["name_match"]["listed"] is False
    assert "not confirmed" in lead["name_match"]["note"]
    assert maritime.sanction_flags({"mmsi": "123456789", "name": "EVER GIVEN"}, store) == {}


# --- routes --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tracker, store) -> TestClient:
    touched: list[str] = []
    monkeypatch.setattr(maritime, "touch_stream", touched.append)
    monkeypatch.setattr(maritime, "ais_status",
                        lambda: {"connected": True, "error": None, "cached_vessels": 3})
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.touched = touched  # type: ignore[attr-defined]
    return c


def test_without_the_key_polygons_still_draw(client, monkeypatch):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    body = client.get("/api/maritime/chokepoints").json()
    assert body["key_configured"] is False
    assert "AISSTREAM_API_KEY" in body["error"]
    assert len(body["chokepoints"]) == 9
    assert all(len(cp["polygon"]) >= 4 for cp in body["chokepoints"])
    assert body["chokepoints"][0]["inside_now"] is None
    vessels = client.get("/api/maritime/vessels").json()
    assert vessels["vessels"] == [] and vessels["dark"] == []
    assert "AISSTREAM_API_KEY" in vessels["error"]
    one = client.get("/api/maritime/chokepoints/suez")
    assert one.status_code == 200 and one.json()["vessels"] == []
    assert client.get("/api/maritime/chokepoints/atlantis").status_code == 404
    assert client.touched == []  # no stream without a key


def test_vessels_with_sanctions_and_dark_estimates(client, monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "test-key-not-returned")
    now = time.time()
    lat, lon = gib(0.5)
    rows = [
        row("111111111", 40.0, 10.0, now - 30, name="PLAIN SHIP"),
        row("572469210", lat, lon, now - 10, name="ARTAVIL"),
        row("222222222", 41.0, 11.0, now - 60, name="GALAXY"),
        row("333333333", 42.0, 12.0, now - 45 * 60, sog=14, cog=0, name="QUIET ONE"),
    ]
    monkeypatch.setattr(maritime, "ais_rows", lambda: rows)
    res = client.get("/api/maritime/vessels")
    assert res.status_code == 200
    assert "test-key-not-returned" not in res.text
    body = res.json()
    assert client.touched == ["test-key-not-returned"]
    assert body["key_configured"] is True and body["sanctions_loaded"] is True
    names = [v["name"] for v in body["vessels"]]
    assert names[0] == "ARTAVIL" and names[1] == "GALAXY"  # listed, then name lead
    artavil = body["vessels"][0]
    assert artavil["sanction"]["programme"] == "IRAN"
    assert "NATIONAL IRANIAN TANKER" in artavil["sanction"]["remarks"]
    assert artavil["chokepoint"] == "gibraltar"
    assert body["vessels"][1]["name_match"]["listed"] is False
    assert "sanction" not in body["vessels"][2] and "name_match" not in body["vessels"][2]
    assert [d["mmsi"] for d in body["dark"]] == ["333333333"]
    dark = body["dark"][0]
    assert dark["estimate"] is True and dark["label"].startswith("EST QUIET ONE (no AIS 4")
    assert dark["lat"] > dark["last_lat"] and dark["lon"] == pytest.approx(12.0)
    assert body["sanctioned_count"] == 1
    assert body["rules"]["dark_after_s"] == 1800.0

    boxed = client.get("/api/maritime/vessels",
                       params={"south": 39, "north": 40.5, "west": 9, "east": 10.5}).json()
    assert [v["mmsi"] for v in boxed["vessels"]] == ["111111111"] and boxed["dark"] == []
    assert client.get("/api/maritime/vessels",
                      params={"south": 10, "north": 5}).status_code == 422


def test_chokepoint_counts_with_the_key(client, monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "k")
    now = time.time()
    lat, lon = gib(0.5)
    monkeypatch.setattr(maritime, "ais_rows", lambda: [
        row("572469210", lat, lon, now - 10, name="ARTAVIL"),
        row("444444444", 30.33, 32.38, now - 20, name="CANAL SHIP")])
    body = client.get("/api/maritime/chokepoints").json()
    stats = {cp["id"]: cp for cp in body["chokepoints"]}
    assert stats["gibraltar"]["inside_now"] == 1 and stats["gibraltar"]["sanctioned_now"] == 1
    assert stats["suez"]["inside_now"] == 1 and stats["suez"]["sanctioned_now"] == 0
    assert stats["hormuz"]["inside_now"] == 0
    assert body["counting_since"].endswith("Z") and body["day"]
    detail = client.get("/api/maritime/chokepoints/gibraltar").json()
    assert [v["name"] for v in detail["vessels"]] == ["ARTAVIL"]
    assert detail["vessels"][0]["sanction"]["source"] == "OFAC SDN"


# --- the shared stream and the sampler ------------------------------------------------------


def test_touch_stream_keeps_the_shared_subscription_alive(monkeypatch, tracker):
    from sda.routes import worldview

    started: list[str] = []
    monkeypatch.setattr(worldview, "_ais_ensure_stream", started.append)
    monkeypatch.setitem(worldview._AIS_STATE, "last_request", 0.0)
    maritime.touch_stream("abc")  # no running loop: no sampler task, no error
    assert started == ["abc"]
    assert worldview._AIS_STATE["last_request"] > 0.0
    maritime.stop_sampler()


def test_sampler_folds_the_cache_until_idle(monkeypatch, tracker):
    from sda.routes import worldview

    monkeypatch.setattr(worldview, "_ais_ensure_stream", lambda key: None)
    monkeypatch.setattr(maritime, "SAMPLE_EVERY_S", 0.01)
    monkeypatch.setattr(maritime, "IDLE_STOP_S", 0.2)
    now = time.time()
    monkeypatch.setattr(maritime, "ais_rows", lambda: [row("9", 1.0, 1.0, now)])

    async def run() -> None:
        maritime.touch_stream("abc")
        task = maritime._SAMPLER["task"]
        assert isinstance(task, asyncio.Task)
        await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert maritime.TRACKER.samples >= 2
    assert "9" in maritime.TRACKER.tracks
    assert maritime._SAMPLER["task"] is None
    maritime.stop_sampler()
