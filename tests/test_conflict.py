"""Tests for the armed conflict layer: sda.conflict and its routes.

Offline: every upstream (DeepStateMap, geoBoundaries, Wikipedia, GeoConfirmed,
ACLED) is served by a fake ``http_get`` from the recorded samples in
tests/data/conflict, and the UCDP import runs on small real extracts of GED 26.1
and the 2026 candidate file. SDA_CONFLICT_DIR points at a scratch directory, so
no cache or store outside the test's tmp_path is read or written.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import shutil
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import conflict as cf
from sda.routes.conflict import router

DATA = Path(__file__).parent / "data" / "conflict"
NOW = datetime(2026, 11, 19, 22, 0, tzinfo=UTC)

GED_TOTAL_BEST = 1636
CAND_TOTAL_BEST = 30

# A box over Ukraine standing in for the land outline in route tests.
LAND_FC = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon",
        "coordinates": [[[22.0, 44.0], [41.0, 44.0], [41.0, 53.0], [22.0, 53.0], [22.0, 44.0]]],
    }}],
}
GB_META = {"simplifiedGeometryGeoJSON": "https://gb.example.test/ukr_simplified.geojson"}
GC_CONFLICTS_JSON = [
    {"shortName": "Ukraine", "url": "ukraine", "name": "Ukraine", "isPrivate": False},
    {"shortName": "Sahel", "url": "africa", "name": "Africa", "isPrivate": False},
    {"shortName": "Israel", "url": "israel", "name": "Israel/Gaza/Lebanon", "isPrivate": False},
    {"shortName": "Secret", "url": "secret", "name": "Hidden", "isPrivate": True},
]
ACLED_PAYLOAD = {
    "success": True,
    "data": [
        {"event_id_cnty": "SDN1234", "event_date": "2026-11-10", "event_type": "Battles",
         "sub_event_type": "Armed clash", "actor1": "Military Forces of Sudan (2019-)",
         "actor2": "Rapid Support Forces", "country": "Sudan", "admin1": "North Darfur",
         "location": "El Fasher", "latitude": "13.6279", "longitude": "25.3494",
         "fatalities": "12", "notes": "On 10 November 2026, <b>clashes</b> were reported.",
         "source": "Sudan Tribune", "geo_precision": "1"},
        {"event_id_cnty": "BAD1", "event_date": "2026-11-10", "latitude": "0",
         "longitude": "0"},
        {"event_id_cnty": "BAD2", "event_date": "not a date", "latitude": "10",
         "longitude": "10"},
    ],
}


def _bytes(name: str) -> bytes:
    return (DATA / name).read_bytes()


class FakeNet:
    """URL prefix -> body; the longest matching prefix wins, None means failure."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes | None] = {}
        self.calls: list[str] = []
        self.headers: list[dict[str, str] | None] = []
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.post_answer: Any = {"access_token": "test-token", "expires_in": 86400}

    def add(self, prefix: str, body: bytes | dict[str, Any] | list[Any] | None) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.routes[prefix] = body

    async def get(self, url: str, timeout_s: float = 30.0,
                  headers: dict[str, str] | None = None) -> bytes | None:
        self.calls.append(url)
        self.headers.append(headers)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                return self.routes[prefix]
        return None

    async def post(self, url: str, data: dict[str, str], timeout_s: float = 30.0) -> Any:
        self.posts.append((url, dict(data)))
        return self.post_answer

    def count(self, prefix: str) -> int:
        return sum(1 for u in self.calls if u.startswith(prefix))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SDA_CONFLICT_DIR", str(tmp_path / "conflict"))
    for var in ("ACLED_EMAIL", "ACLED_PASSWORD", "ACLED_KEY"):
        monkeypatch.delenv(var, raising=False)
    cf.clear_caches()
    cf.reset_store()
    yield tmp_path / "conflict"
    cf.clear_caches()
    cf.reset_store()


@pytest.fixture
def net(home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNet:
    fake = FakeNet()
    last = json.loads(_bytes("deepstate_last.json"))
    fake.add(cf.DS_LAST_URL, last)
    fake.add(cf.DS_LIST_URL, _bytes("deepstate_history.json"))
    fake.add(cf.DS_BASE + "/1649004375/geojson", _bytes("deepstate_2022.json"))
    fake.add(cf.GB_UKR_META_URL, GB_META)
    fake.add(GB_META["simplifiedGeometryGeoJSON"], LAND_FC)
    fake.add(cf.WIKI_API, _bytes("wiki_sudan.json"))
    fake.add(cf.GC_CONFLICTS_URL, GC_CONFLICTS_JSON)
    fake.add("https://geoconfirmed.org/api/Map/export/csv", _bytes("geoconfirmed_90d.csv"))
    fake.add(cf.ACLED_READ_URL, ACLED_PAYLOAD)

    async def no_wait(host: str, gap_s: float) -> None:
        return None

    monkeypatch.setattr(cf, "http_get", fake.get)
    monkeypatch.setattr(cf, "http_post_form", fake.post)
    monkeypatch.setattr(cf, "paced", no_wait)
    monkeypatch.setattr(cf, "utc_now", lambda: NOW)
    return fake


@pytest.fixture
def client(net: FakeNet) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _rect(w: float, s: float, e: float, n: float, step: float = 0.25) -> np.ndarray:
    """A closed counter-clockwise rectangle with a vertex every *step* degrees."""
    pts: list[tuple[float, float]] = []
    nx = max(1, round((e - w) / step))
    ny = max(1, round((n - s) / step))
    pts += [(w + (e - w) * i / nx, s) for i in range(nx)]
    pts += [(e, s + (n - s) * i / ny) for i in range(ny)]
    pts += [(e - (e - w) * i / nx, n) for i in range(nx)]
    pts += [(w, n - (n - s) * i / ny) for i in range(ny)]
    pts.append(pts[0])
    return np.asarray(pts, dtype=np.float64)


def _ged_inputs(folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    zpath = folder / "ged261-csv.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(DATA / "ucdp_ged.csv", "GEDEvent_v26_1.csv")
    cand = folder / "GEDEvent_v26_01_26_09.csv"
    shutil.copy(DATA / "ucdp_candidate.csv", cand)
    return zpath, cand


@pytest.fixture
def store(home: Path) -> cf.HistoryStore:
    zpath, cand = _ged_inputs(home / "download")
    report = cf.build_history([(zpath, "UCDP GED 26.1"), (cand, "UCDP Candidate")], cf.db_path())
    assert report.events == 15
    return cf.get_store()


# --------------------------------------------------------------------------
# Paths and helpers
# --------------------------------------------------------------------------


def test_conflict_dir_follows_the_environment(home: Path) -> None:
    assert cf.conflict_dir() == home
    assert cf.db_path() == home / "conflict.sqlite"
    assert cf.cache_dir() == home / "cache"


def test_text_and_url_helpers() -> None:
    assert cf.clean_text("<b>A</b>  &amp;\n B") == "A & B"
    assert cf.clean_text("x" * 50, 10) == "xxxxxxx..."
    assert cf.clean_text(None) == ""
    assert cf.find_urls("(1/2) https://x.com/a/1, https://t.me/b/2; https://x.com/a/1") == [
        "https://x.com/a/1", "https://t.me/b/2"]
    assert cf.safe_url("https://ok.example/x") == "https://ok.example/x"
    assert cf.safe_url("javascript:alert(1)") == ""
    assert cf.safe_url("https://bad example") == ""
    assert cf.valid_point(0.0, 0.0) is False
    assert cf.valid_point(91.0, 0.0) is False
    assert cf.valid_point(48.5, 37.1) is True


def test_haversine_on_the_mean_sphere() -> None:
    assert cf.haversine_km(0, 0, 1, 0) == pytest.approx(111.195, abs=0.01)
    assert cf.line_length_km([[0, 0], [0, 1], [0, 2]]) == pytest.approx(222.39, abs=0.02)


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def _brute_in_ring(x: float, y: float, ring: np.ndarray) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring[:-1], ring[1:], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def test_banded_point_in_ring_matches_a_plain_ray_cast() -> None:
    rng = random.Random(7)
    star = []
    for k in range(40):
        r = 3.0 if k % 2 == 0 else 1.2
        a = 2 * math.pi * k / 40
        star.append((30 + r * math.cos(a), 48 + r * math.sin(a)))
    star.append(star[0])
    ring = np.asarray(star)
    lon = np.asarray([rng.uniform(26, 34) for _ in range(800)])
    lat = np.asarray([rng.uniform(44, 52) for _ in range(800)])
    want = [_brute_in_ring(x, y, ring) for x, y in zip(lon, lat, strict=True)]
    assert cf.points_in_ring(lon, lat, ring).tolist() == want
    assert cf.points_in_ring(lon, lat, ring, band_deg=0.05, block=7).tolist() == want
    assert 0 < sum(want) < 800


def test_polygon_holes_are_outside() -> None:
    poly = cf.Polygon(_rect(0, 0, 10, 10, 5), [_rect(4, 4, 6, 6, 1)])
    got = poly.contains(np.array([2.0, 5.0, 12.0]), np.array([2.0, 5.0, 5.0]))
    assert got.tolist() == [True, False, False]


def test_runs_join_across_the_ring_start() -> None:
    flags = np.array([True, True, False, False, True])
    assert cf.runs_on_ring(flags) == [(4, 3)]
    assert cf.runs_on_ring(np.array([False, False])) == []
    assert cf.runs_on_ring(np.array([True, True, True])) == [(0, 3)]


def test_line_of_contact_follows_the_free_side_only() -> None:
    land = [cf.Polygon(_rect(0, 40, 10, 48))]
    held = [cf.Polygon(_rect(6, 39.5, 10.5, 48.5))]
    lines = cf.contact_lines(held, land)
    assert len(lines) == 1
    line = lines[0]
    assert all(p[0] == pytest.approx(6.0) for p in line)
    lats = sorted(p[1] for p in line)
    assert lats[0] == pytest.approx(40.0, abs=0.3) and lats[-1] == pytest.approx(48.0, abs=0.3)
    assert cf.line_length_km(line) == pytest.approx(8 * 111.195, abs=60.0)


def test_edges_shared_by_two_held_areas_are_not_front() -> None:
    land = [cf.Polygon(_rect(0, 40, 10, 48))]
    held = [cf.Polygon(_rect(6, 39.5, 10.5, 48.5)), cf.Polygon(_rect(4, 40, 6, 42))]
    lines = cf.contact_lines(held, land)
    total = sum(cf.line_length_km(ln) for ln in lines)
    alone = sum(cf.line_length_km(ln)
                for ln in cf.contact_lines([cf.Polygon(_rect(6, 39.5, 10.5, 48.5))], land))
    assert total > alone
    for ln in lines:
        for lon, lat in ln:
            # Nothing along the shared edge at lon 6 south of 42, nor on the coast at lat 40.
            assert not (lon == pytest.approx(6.0) and lat < 41.9)


def test_a_thin_coastal_sliver_is_not_front() -> None:
    land = [cf.Polygon(_rect(0, 40, 10, 48))]
    # The held area stops about 1.2 km short of the east coast.
    held = [cf.Polygon(np.asarray([[0, 40], [9.985, 40], [9.985, 44], [9.985, 48], [0, 48],
                                   [0, 40]], dtype=np.float64))]
    assert cf.contact_lines(held, land) == []
    loose = cf.contact_lines(held, land, reach_km=1.0)
    assert loose and all(p[0] == pytest.approx(9.985) for ln in loose for p in ln)


def test_no_land_means_no_derived_line() -> None:
    assert cf.contact_lines([cf.Polygon(_rect(0, 0, 1, 1))], []) == []


# --------------------------------------------------------------------------
# DeepStateMap
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kind", "note"), [
    ("Окуповано /// Occupied /// geoJSON.status.occupied\n", "held", ""),
    ("x /// y /// geoJSON.territories.crimea", "held_2014", ""),
    ("x /// y /// geoJSON.territories.ORDLO", "held_2014", ""),
    ("x /// y /// geoJSON.status.unknown", "contested", ""),
    ("x /// y /// geoJSON.status.dismissed_at {{at:25.03}}", "retaken", "25.03"),
    ("x /// y /// geoJSON.territories.transnistria", None, ""),
    ("x /// y /// geoJSON.units.storm-z", None, ""),
    ("x /// y /// geoJSON.status.attack_direction", None, ""),
    (None, None, ""),
])
def test_deepstate_kinds(name: str | None, kind: str | None, note: str) -> None:
    assert cf.deepstate_kind(name) == (kind, note)


# Maps before mid-2024 have no geoJSON key: free text (Ukrainian alone in 2022)
# and a fill colour. Names below are the site's own wording, colours its own.
@pytest.mark.parametrize(("name", "fill", "kind", "note"), [
    ("Окуповано /// Occupied", "#a52714", "held", ""),
    ("Окуповано", "#a52714", "held", ""),
    ("Окупований Крим /// Occupied Crimea", "#880e4f", "held_2014", ""),
    ("Крим", "#000000", "held_2014", ""),
    ("ОРДЛО /// CADR and CALR", "#880e4f", "held_2014", ""),
    ("Острів Тузла /// Occupied Tuzla Island", "#880e4f", "held_2014", ""),
    ("Придністров'я", "#880e4f", None, ""),
    ("Окупована Абхазія. /// Occupied Abkhazia.", "#ff5252", None, ""),
    ("Звільнено 25.03 /// Liberated 25.03", "#0f9d58", "retaken", "25.03"),
    ("Звільнено 31.03-02.04", "#0288d1", "retaken", "31.03-02.04"),
    ("Звільнено /// Taken back", "#0F9D58 ", "retaken", ""),
    ("Статус невідомий /// Unknown status", "#bcaaa4", "contested", ""),
    ("Під питанням /// Contested", "#bdbdbd", "contested", ""),
    ("Сєвєродонецьк. Території, що окуповано.", "#bcaaa4", "contested", ""),
    ("Рубіжне. Території зайняті ворогом", None, "held", ""),
    ("Звільнено", None, "retaken", ""),
    ("Direction of attack", None, None, ""),
    ("PMC Wagner", "#123456", None, ""),
])
def test_deepstate_kinds_before_mid_2024(name: str, fill: str | None, kind: str | None,
                                         note: str) -> None:
    assert cf.deepstate_kind(name, fill) == (kind, note)


def test_parse_the_older_map_format() -> None:
    parsed = cf.parse_deepstate(json.loads(_bytes("deepstate_2022.json")))
    assert parsed is not None
    counts: dict[str, int] = {}
    for area in parsed["areas"]:
        counts[area["kind"]] = counts.get(area["kind"], 0) + 1
    # Crimea arrives as a GeometryCollection of two polygons.
    assert counts == {"held_2014": 4, "held": 2, "contested": 2, "retaken": 3}
    assert sorted(a["note"] for a in parsed["areas"] if a["note"]) == ["25.03", "31.03-02.04"]


def test_parse_deepstate_sample() -> None:
    parsed = cf.parse_deepstate(json.loads(_bytes("deepstate_last.json")))
    assert parsed is not None
    assert parsed["id"] == 1795038105
    kinds = [a["kind"] for a in parsed["areas"]]
    assert set(kinds) <= set(cf.DS_KINDS)
    assert {"held", "held_2014"} <= set(kinds)
    for area in parsed["areas"]:
        ring = area["rings"][0]
        assert ring[0] == ring[-1] and len(ring) >= 4
    bare = cf.parse_deepstate(json.loads(_bytes("deepstate_last.json"))["map"])
    assert bare is not None and len(bare["areas"]) == len(kinds)
    assert cf.parse_deepstate({"nope": 1}) is None
    assert cf.parse_deepstate([]) is None


def test_deepstate_history_and_days() -> None:
    hist = cf.parse_deepstate_history(json.loads(_bytes("deepstate_history.json")) + [
        {"id": 5, "status": True}, {"id": 1795100000, "status": False}])
    assert [h["id"] for h in hist] == [1648989208, 1649004375, 1794773750, 1794945179,
                                       1795038105]
    days = cf.daily_snapshots(hist)
    assert days[0]["date"] == "2022-04-03" and days[0]["id"] == 1649004375
    assert cf.snapshot_for_date(hist, date(2022, 4, 2)) is None
    got = cf.snapshot_for_date(hist, date(2022, 4, 3))
    assert got is not None and got["id"] == 1649004375
    latest = cf.snapshot_for_date(hist, date(2030, 1, 1))
    assert latest is not None and latest["id"] == 1795038105


def test_parse_land_keeps_every_part() -> None:
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 0]]], [[[5, 5], [6, 5], [6, 6], [5, 5]]]]}},
        {"type": "Feature", "geometry": None}]}
    assert len(cf.parse_land(fc)) == 2
    assert cf.parse_land(None) == []


def test_frontline_latest_is_cached_and_falls_back_to_disk(net: FakeNet, home: Path) -> None:
    first = asyncio.run(cf.ukraine_frontline())
    assert first["error"] == ""
    assert first["snapshot_id"] == 1795038105
    assert first["snapshot_at"].startswith("2026-11-18")
    assert first["line_method"] == "derived"
    assert first["lines"], "the occupied areas inside the land box have a free side"
    assert first["counts"]["held"] >= 1
    calls = net.count(cf.DS_LAST_URL)
    asyncio.run(cf.ukraine_frontline())
    assert net.count(cf.DS_LAST_URL) == calls  # memory cache
    # A new process with a fresh disk copy does not call out either.
    cf._memo.clear()
    net.add(cf.DS_LAST_URL, None)
    again = asyncio.run(cf.ukraine_frontline())
    assert again["stale"] is False and again["areas"]
    # Once the copy is older than the TTL and the upstream fails, the old copy is served.
    cf._memo.clear()
    old = time.time() - cf.DS_TTL_S - 60
    os.utime(cf.cache_dir() / "deepstate" / "last.json", (old, old))
    stale = asyncio.run(cf.ukraine_frontline())
    assert stale["stale"] is True and stale["areas"] and stale["error"] == ""


def test_frontline_without_any_source_reports_an_error(net: FakeNet) -> None:
    net.add(cf.DS_LAST_URL, None)
    out = asyncio.run(cf.ukraine_frontline())
    assert out["error"]
    assert out["areas"] == [] and out["lines"] == []
    assert "DeepStateMap" in out["attribution"]


def test_frontline_without_a_land_outline_falls_back_to_outlines(net: FakeNet) -> None:
    net.add(cf.GB_UKR_META_URL, None)
    out = asyncio.run(cf.ukraine_frontline())
    assert out["areas"] and out["lines"] == [] and out["line_method"] == "outline"


def test_frontline_for_a_past_day_uses_the_update_list(net: FakeNet) -> None:
    out = asyncio.run(cf.ukraine_frontline(date(2022, 4, 3)))
    assert out["error"] == ""
    assert out["snapshot_id"] == 1649004375
    assert out["requested"] == "2022-04-03"
    assert out["counts"]["held"] == 2 and out["counts"]["held_2014"] == 4
    assert out["line_method"] == "derived" and out["lines"]
    n = net.count(cf.DS_BASE + "/1649004375/geojson")
    assert n == 1
    cf._memo.clear()
    asyncio.run(cf.ukraine_frontline(date(2022, 4, 3)))
    assert net.count(cf.DS_BASE + "/1649004375/geojson") == n  # immutable on disk
    early = asyncio.run(cf.ukraine_frontline(date(2022, 3, 1)))
    assert early["error"] and early["areas"] == []


# --------------------------------------------------------------------------
# Wikipedia detailed maps
# --------------------------------------------------------------------------


def test_lua_comments_are_stripped_outside_strings() -> None:
    text = 'a = "x -- y" -- gone\n--[[ block\n still ]] b = 1\nc = \'p--q\''
    out = cf.strip_lua_comments(text)
    assert '"x -- y"' in out and "gone" not in out and "block" not in out
    assert "b = 1" in out and "'p--q'" in out


def test_caption_legend_drops_prefixes() -> None:
    caption = ("*[[File:Location dot red.svg|11px]] Under control of the [[Sudanese Armed "
               "Forces]]\n*[[File:Location dot blue.svg|9px]] Rapid Support Forces\n")
    legend = cf.parse_caption_legend(caption)
    assert legend["location dot red.svg"] == "Sudanese Armed Forces"
    assert legend["location dot blue.svg"] == "Rapid Support Forces"


def test_color_words_and_marker_colours() -> None:
    assert cf.color_words("red-lime") == ["red", "lime"]
    assert cf.control_color("location dot red.svg") == "red"
    assert cf.control_color("fighter-jet-red-icon.svg") is None


def test_parse_sudan_module() -> None:
    parsed_api = cf.parse_wiki_api(json.loads(_bytes("wiki_sudan.json")))
    assert parsed_api is not None
    content, revid, stamp = parsed_api
    assert revid == 1385747157 and stamp == "2026-11-15T01:33:01Z"
    parsed = cf.parse_control_module(content)
    parties = {p["party"]: p for p in parsed["legend"]}
    assert parties["Sudanese Armed Forces"]["color"] == "red"
    assert parties["Rapid Support Forces"]["color"] == "blue"
    assert parties["Sudanese Armed Forces"]["count"] == 13
    marks = parsed["marks"]
    assert len(marks) == 15
    contested = [mk for mk in marks if mk["kind"] == "contested"]
    assert [mk["name"].split(" ")[0] for mk in contested] == ["Babanusa"]
    assert contested[0]["p"] == [0, 1]
    for mk in marks:
        assert -90 <= mk["lat"] <= 90 and -180 <= mk["lon"] <= 180
    assert cf.parse_wiki_api({"query": {}}) is None


def test_control_map_is_cached_and_credited(net: FakeNet) -> None:
    out = asyncio.run(cf.control_map("sudan"))
    assert out["error"] == "" and out["revid"] == 1385747157
    assert out["revised_at"] == "2026-11-15T01:33:01Z"
    assert "1385747157" in out["url"]
    assert "CC BY-SA" in out["attribution"]
    n = net.count(cf.WIKI_API)
    asyncio.run(cf.control_map("sudan"))
    assert net.count(cf.WIKI_API) == n
    net.add(cf.WIKI_API, None)
    assert asyncio.run(cf.control_map("myanmar"))["error"]


def test_theatre_list_reports_every_theatre() -> None:
    slugs = [t["slug"] for t in cf.theatre_list()]
    assert slugs[0] == "ukraine"
    assert set(cf.THEATRE_BY_SLUG) <= set(slugs)


# --------------------------------------------------------------------------
# GeoConfirmed
# --------------------------------------------------------------------------


def test_parse_geoconfirmed_sample() -> None:
    conflicts = cf.parse_geoconfirmed_conflicts(GC_CONFLICTS_JSON)
    assert "Secret" not in conflicts
    events = cf.parse_geoconfirmed_csv(_bytes("geoconfirmed_90d.csv").decode("utf-8-sig"),
                                       conflicts)
    assert len(events) == 8  # one undated row dropped, one repeated row kept once
    assert len({e["id"] for e in events}) == 8
    sahel = [e for e in events if e["conflict"] == "Sahel"]
    assert sahel and all(e["url"].startswith("https://geoconfirmed.org/africa/") for e in sahel)
    for e in events:
        assert e["date"] and e["url"].startswith("https://geoconfirmed.org/")
        assert all(u.startswith("https://") for u in e["sources"])


def test_geoconfirmed_events_cache_and_fallback(net: FakeNet) -> None:
    out = asyncio.run(cf.geoconfirmed_events())
    assert len(out["events"]) == 8 and out["error"] == ""
    export = [u for u in net.calls if "/api/Map/export/csv" in u]
    assert len(export) == 1
    cf._memo.clear()
    net.add("https://geoconfirmed.org/api/Map/export/csv", None)
    old = time.time() - cf.GC_TTL_S - 60
    os.utime(cf.cache_dir() / "geoconfirmed.json", (old, old))
    stale = asyncio.run(cf.geoconfirmed_events())
    assert stale["stale"] is True and len(stale["events"]) == 8


def test_an_expired_copy_is_answered_at_once_and_refreshed_behind_it(net: FakeNet) -> None:
    asyncio.run(cf.geoconfirmed_events())
    cf._memo.clear()
    old = time.time() - cf.GC_TTL_S - 60
    os.utime(cf.cache_dir() / "geoconfirmed.json", (old, old))
    before = len([u for u in net.calls if "/api/Map/export/csv" in u])

    async def scenario() -> tuple[dict[str, Any], int, dict[str, Any]]:
        first = await cf.geoconfirmed_events()
        asked = len([u for u in net.calls if "/api/Map/export/csv" in u])
        await asyncio.gather(*list(cf._gc_refresh))
        return first, asked, await cf.geoconfirmed_events()

    first, asked_before_refresh, after = asyncio.run(scenario())
    # the stale copy came back without waiting on the export
    assert first["stale"] is True and len(first["events"]) == 8
    assert asked_before_refresh == before
    # the background refresh then replaced it
    assert after["stale"] is False and len(after["events"]) == 8
    assert len([u for u in net.calls if "/api/Map/export/csv" in u]) == before + 1
    assert not cf._gc_refresh


def test_geoconfirmed_down_with_no_copy(net: FakeNet) -> None:
    net.add("https://geoconfirmed.org/api/Map/export/csv", None)
    out = asyncio.run(cf.geoconfirmed_events())
    assert out["events"] == [] and out["error"]


# --------------------------------------------------------------------------
# ACLED
# --------------------------------------------------------------------------


def test_acled_without_an_account_says_what_to_set(net: FakeNet) -> None:
    out = asyncio.run(cf.acled_events())
    assert out["configured"] is False
    assert "ACLED_EMAIL" in out["error"] and "ACLED_PASSWORD" in out["error"]
    assert not net.posts


def test_acled_with_an_account(net: FakeNet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACLED_EMAIL", "analyst@example.test")
    monkeypatch.setenv("ACLED_KEY", "s3cret-value")
    out = asyncio.run(cf.acled_events(14))
    assert out["configured"] is True and out["error"] == ""
    assert len(out["events"]) == 1
    ev = out["events"][0]
    assert ev["id"] == "SDN1234" and ev["fatalities"] == 12
    assert "<b>" not in ev["text"]
    assert net.posts[0][1]["password"] == "s3cret-value"
    read_at = next(i for i, u in enumerate(net.calls) if u.startswith(cf.ACLED_READ_URL))
    assert net.headers[read_at] == {"Authorization": "Bearer test-token"}
    assert "s3cret-value" not in json.dumps(out)
    assert "s3cret-value" not in net.calls[read_at]


def test_acled_refused_credentials(net: FakeNet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACLED_EMAIL", "analyst@example.test")
    monkeypatch.setenv("ACLED_PASSWORD", "wrong")
    net.post_answer = {"error": "invalid_grant"}
    out = asyncio.run(cf.acled_events())
    assert out["configured"] is True and out["error"] and out["events"] == []


# --------------------------------------------------------------------------
# UCDP history
# --------------------------------------------------------------------------


def test_ged_row_parsing() -> None:
    row = {"id": "7615", "year": "1989", "latitude": "12.6", "longitude": "32.8",
           "type_of_violence": "3", "best": "1000", "low": "-5", "conflict_new_id": "480"}
    parsed = cf.parse_ged_row(row, "GED")
    assert parsed is not None
    rec = dict(zip(cf._GED_COLS, parsed, strict=True))
    assert rec["best"] == 1000
    assert cf.parse_ged_row({**row, "latitude": ""}, "GED") is None
    assert cf.ucdp_url(3, 480) == "https://ucdp.uu.se/onesided/480"
    assert cf.ucdp_url(1, 12) == "https://ucdp.uu.se/conflict/12"


def test_build_history_store(store: cf.HistoryStore, home: Path) -> None:
    s = store.summary()
    assert s["available"] is True
    assert s["events"] == 15
    assert s["deaths"] == GED_TOTAL_BEST + CAND_TOTAL_BEST
    assert (s["year_min"], s["year_max"]) == (1989, 2026)
    years = {y["year"]: y for y in store.years()}
    assert years[2026]["events"] == 3 and years[2026]["deaths"] == CAND_TOTAL_BEST
    conn = sqlite3.connect(cf.db_path())
    try:
        for cell in cf.DENSITY_CELLS:
            n, deaths = conn.execute("SELECT SUM(n), SUM(deaths) FROM density WHERE cell = ?",
                                     (cell,)).fetchone()
            assert (n, deaths) == (15, GED_TOTAL_BEST + CAND_TOTAL_BEST)
    finally:
        conn.close()
    assert not (home / "conflict.sqlite.new").exists()
    assert not (home / "conflict.sqlite.building").exists()


def test_later_inputs_never_refill_covered_years(home: Path) -> None:
    zpath, cand = _ged_inputs(home / "download")
    again = home / "again.csv"
    shutil.copy(DATA / "ucdp_ged.csv", again)
    report = cf.build_history([(zpath, "GED"), (cand, "Cand"), (again, "Again")],
                              home / "x.sqlite")
    assert report.events == 15 and report.skipped >= 12
    assert report.years == (1989, 2026)


def test_density_events_and_get(store: cf.HistoryStore) -> None:
    cell, cells = store.density(-180, -90, 180, 90, 4.0, 1946, 2100)
    assert cell == 4.0 and sum(c["events"] for c in cells) == 15
    _, state = store.density(-180, -90, 180, 90, 4.0, 1946, 2100, (1,))
    _, others = store.density(-180, -90, 180, 90, 4.0, 1946, 2100, (2, 3))
    assert sum(c["events"] for c in state) + sum(c["events"] for c in others) == 15
    rows, total = store.events(-180, -90, 180, 90, 1946, 2100, (), 5)
    assert total == 15 and len(rows) == 5
    bests = [r["best"] for r in rows]
    assert bests == sorted(bests, reverse=True)
    assert rows[0]["url"].startswith("https://ucdp.uu.se/")
    rows, total = store.events(-180, -90, 180, 90, 2026, 2026, (), 50)
    assert total == 3
    first = rows[0]["id"]
    one = store.get(first)
    assert one is not None and one["id"] == first and one["type_name"]
    assert store.get(999999999) is None


def test_a_store_from_an_older_schema_asks_for_a_new_import(store: cf.HistoryStore) -> None:
    assert store.available
    cf.reset_store()
    conn = sqlite3.connect(cf.db_path())
    try:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema'")
        conn.commit()
    finally:
        conn.close()
    old = cf.get_store()
    assert old.available is False
    s = old.summary()
    assert s["available"] is False and "older version" in s["hint"]


def test_density_across_the_antimeridian(store: cf.HistoryStore) -> None:
    _, everything = store.density(-180, -90, 180, 90, 4.0, 1946, 2100)
    _, wrapped = store.density(170, -90, 169.9, 90, 4.0, 1946, 2100)
    assert sum(c["events"] for c in wrapped) == sum(c["events"] for c in everything)


def test_discover_urls_and_labels() -> None:
    page = ('<a href="https://ucdp.uu.se/downloads/ged/ged251-csv.zip">'
            '<a href="https://ucdp.uu.se/downloads/ged/ged261-csv.zip">'
            '<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_01_26_08.csv">'
            '<a href="https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_01_26_09.csv">')
    ged, cand = cf.discover_ucdp_urls(page)
    assert ged.endswith("/ged261-csv.zip")
    assert cand.endswith("/GEDEvent_v26_01_26_09.csv")
    assert cf.discover_ucdp_urls("") == (cf.UCDP_GED_FALLBACK, cf.UCDP_CANDIDATE_FALLBACK)
    assert "26.1" in cf.dataset_label(Path("ged261-csv.zip"))
    assert "Candidate" in cf.dataset_label(Path("GEDEvent_v26_01_26_09.csv"))


def test_cli_import_and_status(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = home / "files"
    _ged_inputs(folder)
    db = home / "cli.sqlite"
    assert cf.main(["import", "--from", str(folder), "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "15" in out and out.isascii()
    assert cf.main(["status", "--db", str(db)]) == 0
    assert "15" in capsys.readouterr().out
    assert cf.main(["status", "--db", str(home / "missing.sqlite")]) != 0
    db2 = home / "nocand.sqlite"
    assert cf.main(["import", "--from", str(folder), "--no-candidate", "--db", str(db2)]) == 0
    assert "12" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_summary_route(client: TestClient) -> None:
    body = client.get("/api/conflict/summary").json()
    assert body["history"]["available"] is False
    assert "python -m sda.conflict import" in body["history"]["hint"]
    assert body["acled_configured"] is False and "ACLED_EMAIL" in body["acled_hint"]
    assert body["theatres"][0]["slug"] == "ukraine"
    assert {"frontline", "control", "events", "history", "acled"} <= set(body["attribution"])


def test_frontline_routes(client: TestClient) -> None:
    latest = client.get("/api/conflict/frontline/ukraine").json()
    assert latest["snapshot_id"] == 1795038105 and latest["lines"]
    past = client.get("/api/conflict/frontline/ukraine", params={"date": "2022-04-03"}).json()
    assert past["snapshot_id"] == 1649004375
    future = client.get("/api/conflict/frontline/ukraine", params={"date": "2026-12-01"}).json()
    assert future["snapshot_id"] == 1795038105
    assert client.get("/api/conflict/frontline/ukraine",
                      params={"date": "2021-05-01"}).status_code == 422
    assert client.get("/api/conflict/frontline/ukraine",
                      params={"date": "yesterday"}).status_code == 422
    dates = client.get("/api/conflict/frontline/ukraine/dates").json()
    assert dates["first"] == "2022-04-03" and dates["last"] == "2026-11-18"
    assert dates["updates"] == 5 and dates["error"] == ""


def test_control_routes(client: TestClient) -> None:
    sudan = client.get("/api/conflict/control/sudan").json()
    assert len(sudan["marks"]) == 15 and sudan["error"] == ""
    assert client.get("/api/conflict/control/atlantis").status_code == 404


def test_event_routes(client: TestClient) -> None:
    body = client.get("/api/conflict/events").json()
    assert body["count"] == 8 and body["error"] == ""
    assert set(body["events"][0]) == set(cf.GC_COMPACT)
    name, n = next(iter(body["conflicts"].items()))
    one = client.get("/api/conflict/events", params={"conflict": name}).json()
    assert one["count"] == n
    recent = client.get("/api/conflict/events", params={"days": 7}).json()
    assert recent["count"] == 0  # the sample ends on 2026-08-25
    ident = body["events"][0]["id"]
    detail = client.get(f"/api/conflict/events/{ident.upper()}").json()
    assert detail["id"] == ident and detail["url"] and "attribution" in detail
    assert client.get("/api/conflict/events/00000000-0000").status_code == 404
    assert client.get("/api/conflict/events", params={"days": 91}).status_code == 422


def test_acled_routes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    off = client.get("/api/conflict/acled").json()
    assert off["configured"] is False and off["events"] == [] and off["count"] == 0
    monkeypatch.setenv("ACLED_EMAIL", "analyst@example.test")
    monkeypatch.setenv("ACLED_PASSWORD", "pw")
    on = client.get("/api/conflict/acled").json()
    assert on["count"] == 1 and set(on["events"][0]) == set(cf.ACLED_COMPACT)
    one = client.get("/api/conflict/acled/SDN1234").json()
    assert one["id"] == "SDN1234" and "attribution" in one
    assert client.get("/api/conflict/acled/NOPE").status_code == 404


def test_history_routes_before_and_after_import(client: TestClient, home: Path) -> None:
    assert client.get("/api/conflict/history/summary").json()["available"] is False
    for path in ("/api/conflict/history/years", "/api/conflict/history/density",
                 "/api/conflict/history/events", "/api/conflict/history/7615"):
        r = client.get(path)
        assert r.status_code == 503 and "sda.conflict import" in r.json()["detail"]
    zpath, cand = _ged_inputs(home / "download")
    cf.build_history([(zpath, "UCDP GED 26.1"), (cand, "UCDP Candidate")], cf.db_path())
    cf.reset_store()
    assert client.get("/api/conflict/history/summary").json()["events"] == 15
    years = client.get("/api/conflict/history/years").json()
    assert sum(y["events"] for y in years) == 15
    dens = client.get("/api/conflict/history/density",
                      params={"cell": 4, "types": "1,2,3"}).json()
    assert dens["events"] == 15 and dens["deaths"] == GED_TOTAL_BEST + CAND_TOTAL_BEST
    ev = client.get("/api/conflict/history/events", params={"limit": 2}).json()
    assert ev["count"] == 2 and ev["total"] == 15 and ev["capped"] is True
    ident = ev["events"][0]["id"]
    assert client.get(f"/api/conflict/history/{ident}").json()["id"] == ident
    assert client.get("/api/conflict/history/1").status_code == 404
    bad = [{"types": "4"}, {"types": "a"}, {"year_from": 2000, "year_to": 1990},
           {"bbox": "1,2,3"}]
    for params in bad:
        assert client.get("/api/conflict/history/density", params=params).status_code == 422
