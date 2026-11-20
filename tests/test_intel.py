"""Tests for country intelligence (sda.intel and sda.routes.intel).

Offline: every upstream answer comes from recorded samples in tests/data/intel
(Natural Earth 1:110m for 14 countries, World Bank, WHO Disease Outbreak News,
abuse.ch Feodo Tracker, IODA alerts and one real GDELT 2.0 export file). The
application's own feeds (USGS quakes, FIRMS fires, NWS alerts, EONET) are
replaced with small hand-made lists. The clock is pinned to
2026-11-20T05:00Z, a Friday.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import intel
from sda.routes import intel as intel_routes

DATA = Path(__file__).resolve().parent / "data" / "intel"
NOW = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)
NEWEST = "20261120050000"  # listed in lastupdate.txt, answers 404 (as it did live)
HELD = "20261120044500"  # the recorded export file


def _load(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _index() -> intel.CountryIndex:
    return intel.CountryIndex(intel.parse_countries(_load("ne_countries.geojson")))


QUAKES = [
    {"id": "q1", "magnitude": 6.0, "time": "2026-11-20 03:10 UTC",
     "title": "M 6.0 - central California", "lon": -120.5, "lat": 36.0},
    {"id": "q2", "magnitude": 4.8, "time": "2026-11-20 01:00 UTC",
     "title": "M 4.8 - Qatar", "lon": 51.2, "lat": 25.3},
    {"id": "q3", "magnitude": 5.5, "time": "2026-11-18 10:00 UTC",
     "title": "M 5.5 - too old", "lon": 138.5, "lat": 36.0},
    {"id": "q4", "magnitude": 5.0, "time": "2026-11-20 02:00 UTC",
     "title": "M 5.0 - Mid-Atlantic Ridge", "lon": -30.0, "lat": 0.0},
]
FIRES = (
    [{"lat": 25.2, "lon": 51.3, "acq_date": "2026-11-20", "acq_time": "0130", "frp_mw": 12.5}]
    + [{"lat": -2.0 - i * 0.1, "lon": 23.0 + i * 0.1, "acq_date": "2026-11-19",
        "acq_time": "1200", "frp_mw": 3.0} for i in range(10)]
    + [{"lat": -2.5, "lon": 23.5, "acq_date": "2026-11-18", "acq_time": "2300", "frp_mw": 1.0}]
)


def _box(lon: float, lat: float) -> list[float]:
    return [lon - 0.5, lat - 0.5, lon + 0.5, lat - 0.5, lon + 0.5, lat + 0.5,
            lon - 0.5, lat + 0.5, lon - 0.5, lat - 0.5]


ALERTS = [
    {"event": "Blizzard Warning", "headline": "Blizzard Warning for Kansas",
     "severity": "Extreme", "rings": [_box(-98.5, 38.5)]},
    {"event": "Winter Storm Warning", "headline": "Winter Storm Warning",
     "severity": "Severe", "rings": [_box(-97.5, 38.5)]},
    {"event": "High Wind Warning", "headline": "High Wind Warning",
     "severity": "Severe", "rings": [_box(-98.5, 37.5)]},
    {"event": "Frost Advisory", "headline": "Frost Advisory",
     "severity": "Moderate", "rings": [_box(-96.5, 38.5)]},
    {"event": "No shape", "headline": "dropped", "severity": "Extreme", "rings": []},
]


def _eonet(eid: str, title: str, cat: str, lon: float, lat: float, date: str) -> Any:
    return SimpleNamespace(id=eid, title=title, categories=[SimpleNamespace(title=cat)],
                           point=(lon, lat), date=date)


EONET = (
    _eonet("E1", "Wildfire near Wichita", "Wildfires", -98.4, 38.4, "2026-11-19T20:00:00Z"),
    _eonet("E2", "Snow in Lapland", "Snow", 25.0, 62.0, "2026-11-19T12:00:00Z"),
    _eonet("E3", "Old event", "Floods", 7.5, 46.8, "2026-10-01T00:00:00Z"),
)


class Feeds:
    """The fake upstream: records every URL and can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.fires_key = True

    def count(self, prefix: str) -> int:
        return sum(1 for u in self.calls if u.startswith(prefix))

    async def json(self, url: str, purpose: str, timeout_s: float = 20.0) -> object:
        self.calls.append(url)
        if purpose in self.fail:
            return None
        if url == intel.NE_URL:
            return _load("ne_countries.geojson")
        if url == intel.WB_COUNTRIES_URL:
            return _load("wb_countries.json")
        if url.startswith("https://api.worldbank.org/v2/country/all/indicator/"):
            indicator = url.split("/indicator/")[1].split("?")[0]
            return _load(f"wb_{indicator}.json")
        if url == intel.WHO_URL:
            return _load("who_don.json")
        if url == intel.FEODO_URL:
            return _load("feodo.json")
        if url.startswith("https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"):
            return _load("ioda_alerts.json")
        return None

    async def text(self, url: str, purpose: str, timeout_s: float = 20.0) -> str | None:
        self.calls.append(url)
        if purpose in self.fail or url != intel.GDELT_LASTUPDATE_URL:
            return None
        return (DATA / "gdelt_lastupdate.txt").read_text(encoding="utf-8")

    async def bytes(self, url: str, purpose: str, timeout_s: float = 30.0) -> bytes | None:
        self.calls.append(url)
        if purpose in self.fail or url != intel.GDELT_EXPORT_URL.format(stamp=HELD):
            return None
        return (DATA / "gdelt_export.CSV.zip").read_bytes()

    async def quakes(self) -> dict[str, Any]:
        self.calls.append("local:quakes")
        return {"earthquakes": QUAKES, "count": len(QUAKES)}

    async def fires(self, days: int, source: str, south: float, west: float,
                    north: float, east: float) -> dict[str, Any]:
        self.calls.append("local:fires")
        if not self.fires_key:
            return {"fires": [], "count": 0, "key_configured": False}
        return {"fires": FIRES, "count": len(FIRES), "key_configured": True}

    async def weather(self) -> dict[str, Any]:
        self.calls.append("local:weather")
        return {"alerts": ALERTS, "count": len(ALERTS)}

    async def eonet(self, days: int, status: str) -> tuple[tuple[Any, ...] | None, bool]:
        self.calls.append("local:eonet")
        return EONET, False


@pytest.fixture
def feeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Feeds]:
    monkeypatch.setenv(intel.INTEL_DIR_ENV, str(tmp_path))
    intel.reset_state()
    monkeypatch.setattr(intel, "_utcnow", lambda: NOW)
    f = Feeds()
    monkeypatch.setattr(intel, "_fetch_json", f.json)
    monkeypatch.setattr(intel, "_fetch_text", f.text)
    monkeypatch.setattr(intel, "_fetch_bytes", f.bytes)
    monkeypatch.setattr(intel.wv, "proxy_earthquakes", f.quakes)
    monkeypatch.setattr(intel.wv, "proxy_fires", f.fires)
    monkeypatch.setattr(intel.wv, "proxy_weather_alerts", f.weather)
    monkeypatch.setattr(intel.eonet_routes, "_current", f.eonet)
    yield f
    intel.reset_state()


@pytest.fixture
def client(feeds: Feeds) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(intel_routes.router)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_haversine_and_intel_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert intel.haversine_km(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(343.5, abs=1)
    assert intel.haversine_km(0, 0, 0, 180) == pytest.approx(math.pi * intel.EARTH_RADIUS_KM)
    monkeypatch.setenv(intel.INTEL_DIR_ENV, str(tmp_path))
    assert intel.intel_dir() == tmp_path
    monkeypatch.delenv(intel.INTEL_DIR_ENV)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert intel.intel_dir() == tmp_path / "local" / "sda" / "intel"


# ---------------------------------------------------------------------------
# Borders and point-in-country
# ---------------------------------------------------------------------------


def test_parse_countries_from_natural_earth_sample() -> None:
    idx = _index()
    assert len(idx.countries) == 14
    assert [c.name for c in idx.countries] == sorted(c.name for c in idx.countries)
    fra = idx.lookup("FRA")
    assert fra is not None and fra.iso2 == "FR" and len(fra.polygons) == 3
    assert idx.lookup("fr") is fra and idx.lookup(" fra ") is fra
    assert idx.lookup("XX") is None and idx.lookup("") is None
    che = idx.by_iso3["CHE"]
    assert che.brief() == {"iso3": "CHE", "iso2": "CH", "name": "Switzerland",
                           "lat": che.label_lat, "lon": che.label_lon}
    assert che.pop_year == 2019 and che.continent == "Europe"


@pytest.mark.parametrize(("lat", "lon", "iso3"), [
    (46.95, 7.44, "CHE"),  # Bern
    (25.29, 51.53, "QAT"),  # Doha
    (48.86, 2.35, "FRA"),  # Paris
    (-1.29, 36.82, "KEN"),  # Nairobi
    (-2.5, 23.5, "COD"),
    (36.0, 139.0, "JPN"),  # Gunma, inland Honshu
    (39.0, -98.0, "USA"),
    (0.0, -30.0, None),  # Atlantic
    (95.0, 0.0, None),  # not a latitude
])
def test_locate(lat: float, lon: float, iso3: str | None) -> None:
    idx = _index()
    for memo in (False, True, True):
        found = idx.locate(lat, lon, memo=memo)
        assert (found.iso3 if found else None) == iso3


def _square(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def test_holes_merges_and_code_fallbacks() -> None:
    geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"NAME": "Outer", "ISO_A3": "-99", "ADM0_A3": "OUT", "ISO_A2": "OU"},
         "geometry": {"type": "Polygon",
                      "coordinates": [_square(0, 0, 10, 10), _square(4, 4, 6, 6)]}},
        {"type": "Feature", "properties": {"NAME": "Inner", "ISO_A3_EH": "INN", "ISO_A2": "IN"},
         "geometry": {"type": "Polygon", "coordinates": [_square(4, 4, 6, 6)]}},
        {"type": "Feature", "properties": {"NAME": "Outer islands", "ISO_A3": "OUT"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[_square(20, 0, 21, 1)]]}},
        {"type": "Feature", "properties": {"NAME": "No code"},
         "geometry": {"type": "Polygon", "coordinates": [_square(30, 0, 31, 1)]}},
        {"type": "Feature", "properties": {"NAME": "No shape", "ISO_A3": "NOS"},
         "geometry": None},
    ]}
    idx = intel.CountryIndex(intel.parse_countries(geo))
    assert sorted(idx.by_iso3) == ["INN", "OUT"]
    out = idx.by_iso3["OUT"]
    assert len(out.polygons) == 2 and out.bbox == (0, 0, 21, 10)
    assert out.main_bbox == (0, 0, 10, 10)
    assert idx.locate(5, 5).iso3 == "INN"  # inside the hole
    assert idx.locate(2, 2).iso3 == "OUT"
    assert idx.locate(0.5, 20.5).iso3 == "OUT"
    assert idx.locate(0.5, 30.5) is None
    assert intel.parse_countries({"features": "nope"}) == []
    assert intel.parse_countries(None) == []


def test_simplify_and_borders_payload() -> None:
    ring = intel.Ring([(i / 10, 0.0) for i in range(100)] + [(9.9, 5.0), (0.0, 5.0)])
    flat = intel.simplify_ring(ring, 1.0)
    assert len(flat) % 2 == 0
    lons = flat[0::2]
    assert lons[:3] == [0.0, 1.0, 2.0]
    tiny = intel.Ring([(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)])
    assert intel.simplify_ring(tiny, 0.2) == []
    kept = intel.simplify_ring(tiny, 0.2, keep_small=True)
    assert len(kept) == 8 and kept[2] == 0.01

    idx = _index()
    payload = intel.borders_payload(idx, 0.2)
    assert payload["count"] == 14
    assert payload["attribution"] == intel.BORDERS_ATTRIBUTION
    total = 0
    for country in payload["countries"]:
        assert country["polygons"], country["iso3"]
        for rings in country["polygons"]:
            for r in rings:
                assert len(r) % 2 == 0 and len(r) >= 6
                total += len(r) // 2
    assert total == payload["points"]


# ---------------------------------------------------------------------------
# Time zones and exchanges
# ---------------------------------------------------------------------------


def test_zones_and_local_time() -> None:
    assert intel.parse_iso6709("+404251-0740023") == pytest.approx(
        (40 + 42 / 60 + 51 / 3600, -(74 + 0 / 60 + 23 / 3600)))
    assert intel.parse_iso6709("+3518+13942") == pytest.approx((35.3, 139.7))
    assert intel.parse_iso6709("nonsense") is None
    assert intel.zone_for_country("US", 40.7, -74.0) == "America/New_York"
    assert intel.zone_for_country("us", 34.05, -118.25) == "America/Los_Angeles"
    assert intel.zone_for_country("CH", 46.95, 7.44) == "Europe/Zurich"
    assert intel.zone_for_country("ZZ", 0, 0) is None
    assert intel.local_time("Asia/Kolkata", NOW) == {
        "zone": "Asia/Kolkata", "time": "10:30", "date": "2026-11-20", "weekday": "Fri",
        "utc_offset": "+05:30"}
    st_johns = intel.local_time("America/St_Johns", NOW)
    assert st_johns["time"] == "01:30" and st_johns["utc_offset"] == "-03:30"
    assert intel.local_time("Not/AZone", NOW) is None


def _ex(mic: str) -> intel.Exchange:
    return next(e for e in intel.EXCHANGES if e.mic == mic)


def test_exchange_hours() -> None:
    riyadh_sunday = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)  # 11:00 in Riyadh
    s = intel.exchange_status(_ex("XSAU"), riyadh_sunday)
    assert (s["open"], s["state"], s["next"], s["days"]) == (
        True, "open", "closes 15:00 local", "Sun-Thu")
    s = intel.exchange_status(_ex("XSAU"), NOW)  # Friday 08:00 in Riyadh
    assert (s["open"], s["state"], s["next"]) == (
        False, "non-trading day", "opens Sun 10:00 local")
    s = intel.exchange_status(_ex("XJPX"), datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
    assert (s["open"], s["state"], s["next"], s["local_time"]) == (
        False, "midday break", "opens 12:30 local", "12:00")
    s = intel.exchange_status(_ex("XNYS"), NOW)  # midnight in New York
    assert (s["state"], s["next"]) == ("closed", "opens 09:30 local")
    s = intel.exchange_status(_ex("XNYS"), datetime(2026, 11, 20, 22, 0, tzinfo=UTC))
    assert (s["state"], s["next"], s["days"]) == ("closed", "opens Mon 09:30 local", "Mon-Fri")
    # Daylight saving follows the zone: 07:30 UTC is 08:30 in a London July.
    assert intel.exchange_status(_ex("XLON"), datetime(2026, 7, 1, 7, 30, tzinfo=UTC))["open"]
    assert not intel.exchange_status(_ex("XLON"), datetime(2026, 12, 1, 7, 30, tzinfo=UTC))["open"]
    assert [r["mic"] for r in intel.exchanges_for("ch", NOW)] == ["XSWX"]
    assert intel.exchanges_for("ZZ", NOW) == []


def test_exchange_table_is_consistent() -> None:
    mics = [e.mic for e in intel.EXCHANGES]
    assert len(mics) == len(set(mics))
    for e in intel.EXCHANGES:
        ZoneInfo(e.tz)
        assert len(e.iso2) == 2 and e.days in (intel.MON_FRI, intel.SUN_THU)
        bounds = [intel._minutes(t) for span in e.sessions for t in span]
        assert bounds == sorted(bounds) and len(set(bounds)) == len(bounds), e.mic


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_world_bank_parsers() -> None:
    pop = intel.parse_wb_indicator(_load("wb_SP.POP.TOTL.json"))
    assert pop["CHE"] == {"value": 9092436.0, "year": 2025}
    countries = intel.parse_wb_countries(_load("wb_countries.json"))
    assert "WLD" not in countries and "AFE" not in countries
    assert countries["CHE"]["capital"] == "Bern"
    assert countries["CHE"]["lat"] == pytest.approx(46.948)
    assert intel.parse_wb_indicator({"message": "bad"}) == {}
    assert intel.parse_wb_indicator([{}, [{"countryiso3code": "CHE", "value": None,
                                          "date": "2025"}]]) == {}
    assert intel.parse_wb_countries(None) == {}


def test_who_titles_and_country_matching() -> None:
    assert intel.split_who_title("Chikungunya virus disease - Switzerland") == (
        "Chikungunya virus disease", "Switzerland")
    assert intel.split_who_title("Middle East respiratory syndrome " + chr(0x2013) + " Qatar") == (
        "Middle East respiratory syndrome", "Qatar")
    assert intel.split_who_title("Ebola disease, Uganda") == ("Ebola disease", "Uganda")
    assert intel.split_who_title("Avian Influenza in Cambodia") == (
        "Avian Influenza", "Cambodia")
    assert intel.split_who_title("Mystery") == ("Mystery", "")
    names = sorted([("papua new guinea", "PNG"), ("guinea", "GIN"),
                    ("guinea-bissau", "GNB"), ("nigeria", "NGA"), ("niger", "NER")],
                   key=lambda kv: -len(kv[0]))
    assert intel.match_countries("Papua New Guinea and Guinea", names) == ["PNG", "GIN"]
    assert intel.match_countries("Guinea-Bissau", names) == ["GNB"]
    assert intel.match_countries("Nigeria", names) == ["NGA"]
    assert intel.match_countries("Niger and Nigeria", names) == ["NER", "NGA"]
    assert intel.match_countries("Nothing here", names) == []
    # Accents fold away on both sides.
    accented = "C" + chr(0xF4) + "te d'Ivoire"
    assert intel.match_countries(accented, [("cote d'ivoire", "CIV")]) == ["CIV"]
    curacao = "Cura" + chr(0xE7) + "ao"
    cuw = intel.CountryIndex(intel.parse_countries({"features": [
        {"properties": {"NAME": curacao, "ISO_A3": "CUW", "ISO_A2": "CW"},
         "geometry": {"type": "Polygon", "coordinates": [_square(0, 0, 1, 1)]}}]}))
    assert intel.match_countries("Dengue - Curacao", cuw.name_table()) == ["CUW"]
    assert intel.match_countries("Dengue - " + curacao, cuw.name_table()) == ["CUW"]


def test_parse_who_sample() -> None:
    items = intel.parse_who(_load("who_don.json"), _index())
    assert len(items) == 12
    assert [o["published"] for o in items] == sorted((o["published"] for o in items),
                                                     reverse=True)
    first = items[0]
    assert first["disease"] == "Chikungunya virus disease"
    assert [c["iso3"] for c in first["countries"]] == ["CHE"]
    assert first["url"] == f"{intel.WHO_ITEM_URL}/2026-DON627"
    by_id = {o["id"]: o for o in items}
    assert [c["iso3"] for c in by_id["2026-DON625"]["countries"]] == ["COD", "KEN", "NGA"]
    assert by_id["2026-DON624"]["countries"] == []  # "WHO African Region"
    assert by_id["2026-DON620"]["countries"] == []  # Russia is not in the sample
    saudi = by_id["middle-east-respiratory-syndrome-coronavirus---kingdom-of-saudi-arabia"]
    assert [c["iso3"] for c in saudi["countries"]] == ["SAU"]
    hostile = intel.parse_who({"value": [
        {"Title": "X - France", "ItemDefaultUrl": "javascript:alert(1)",
         "PublicationDateAndTime": "2026-11-01T00:00:00Z"},
        {"Title": "", "ItemDefaultUrl": "/a"}, "junk"]}, _index())
    assert len(hostile) == 1 and hostile[0]["url"] == intel.WHO_LIST_URL
    assert intel.parse_who([], None) == []


def test_parse_feodo_counts_and_drops_addresses() -> None:
    raw = _load("feodo.json")
    agg = intel.parse_feodo(raw + [{"country": "", "malware": "X"}, {"country": "U1"}, "junk"])
    assert agg["US"] == {"servers": 3, "online": 1, "malware": {"Emotet": 1, "QakBot": 2},
                         "last_online": "2026-03-12"}
    assert agg["GB"]["servers"] == 1 and agg["JP"]["online"] == 0
    assert sorted(agg) == ["GB", "JP", "US"]
    dumped = json.dumps(agg)
    for row in raw:
        assert row["ip_address"] not in dumped
    assert intel.parse_feodo({"not": "a list"}) == {}


def test_ioda_alerts_and_outage_aggregation() -> None:
    alerts = intel.parse_ioda_alerts(_load("ioda_alerts.json"))
    assert len(alerts) == 24
    assert [a["time"] for a in alerts] == sorted(a["time"] for a in alerts)
    out = intel.aggregate_outages(alerts)
    fi = out["FI"]
    assert (fi["critical"], fi["warning"], fi["ongoing"], fi["datasources"]) == (
        1, 0, True, ["gtr"])
    assert fi["url"] == "https://ioda.inetintel.cc.gatech.edu/country/FI"
    assert fi["last"] == "2026-11-19T07:30:00Z"
    vi = out["VI"]  # bgp still critical, ping-slash24 recovered
    assert (vi["critical"], vi["ongoing"], vi["datasources"]) == (2, True, ["bgp", "ping-slash24"])
    assert out["MC"]["ongoing"] is False
    made = intel.aggregate_outages([
        {"iso2": "ZZ", "name": "Z", "datasource": "bgp", "level": "warning", "time": 10,
         "value": 1, "history": 2},
        {"iso2": "ZZ", "name": "Z", "datasource": "bgp", "level": "critical", "time": 20,
         "value": 1, "history": 2},
        {"iso2": "ZZ", "name": "Z", "datasource": "gtr", "level": "warning", "time": 30,
         "value": 1, "history": 2},
        {"iso2": "ZZ", "name": "Z", "datasource": "gtr", "level": "normal", "time": 40,
         "value": 1, "history": 2},
    ])
    assert (made["ZZ"]["critical"], made["ZZ"]["warning"], made["ZZ"]["datasources"],
            made["ZZ"]["alerts"]) == (1, 1, ["bgp", "gtr"], 3)
    assert intel.parse_ioda_alerts({"data": [{"entity": {"code": "F"}, "level": "critical",
                                              "time": 1}, {"level": "odd"}]}) == []


GDELT_KEYS = {"id", "date", "lat", "lon", "place", "country_fips", "event_code", "root_code",
              "event", "quad_class", "goldstein", "mentions", "sources", "articles", "tone",
              "source_url"}


def test_gdelt_export_parsing() -> None:
    assert intel.parse_gdelt_lastupdate(
        (DATA / "gdelt_lastupdate.txt").read_text(encoding="utf-8")) == NEWEST
    assert intel.parse_gdelt_lastupdate(None) is None
    assert intel.parse_gdelt_lastupdate("garbage") is None
    assert intel.slot_stamps(NEWEST, 3) == [NEWEST, HELD, "20261120043000"]
    assert intel.slot_stamps("", 2) == []
    text = intel.unzip_export((DATA / "gdelt_export.CSV.zip").read_bytes())
    assert text is not None and len(text.splitlines()) == 60
    assert intel.unzip_export(b"not a zip") is None
    rows = intel.parse_gdelt_export(text)
    assert len(rows) == 60
    assert all(set(r) == GDELT_KEYS for r in rows)  # no actor names
    first = rows[0]
    assert first["date"] == "2026-11-20T04:45:00Z" and first["root_code"] == "07"
    assert first["event"] == "aid" and first["quad_class"] == 2
    assert first["source_url"].startswith("https://www.wrhi.com/")
    assert len(intel.parse_gdelt_export(text, max_rows=5)) == 5

    cols = text.splitlines()[0].split("\t")
    hostile = list(cols)
    hostile[60] = "javascript:alert(1)"
    no_place = list(cols)
    no_place[56] = ""
    rows = intel.parse_gdelt_export("\n".join(["\t".join(hostile), "\t".join(no_place),
                                               "short\tline"]))
    assert len(rows) == 1 and rows[0]["source_url"] == ""


def test_fetch_gdelt_events_uses_the_previous_file_when_the_newest_404s(feeds: Feeds) -> None:
    rows = asyncio.run(intel.fetch_gdelt_events(2))
    assert len(rows) == 60 and set(rows[0]) == GDELT_KEYS
    newest_url = intel.GDELT_EXPORT_URL.format(stamp=NEWEST)
    assert feeds.count(newest_url) == 1
    assert intel.gdelt_status() == {"newest": "2026-11-20T05:00:00Z", "files": 1,
                                    "hours": 0.0, "error": ""}
    # Within five minutes lastupdate.txt is not read again, and the missing
    # file waits out its two-minute retry.
    assert len(asyncio.run(intel.fetch_gdelt_events(2))) == 60
    assert feeds.count(intel.GDELT_LASTUPDATE_URL) == 1
    assert feeds.count(newest_url) == 1
    intel._state.gdelt.failed[NEWEST] -= intel.GDELT_RETRY_MISSING_S + 1
    asyncio.run(intel.fetch_gdelt_events(2))
    assert feeds.count(newest_url) == 2
    assert len(asyncio.run(intel.fetch_gdelt_events(1))) == 0  # only the 404 slot
    assert len(asyncio.run(intel.fetch_gdelt_events(99))) == 60  # clamped to 8 slots


def test_fetch_gdelt_events_when_gdelt_is_down(feeds: Feeds) -> None:
    feeds.fail.add("gdelt")
    assert asyncio.run(intel.fetch_gdelt_events()) == []
    assert intel.gdelt_status()["error"] == "GDELT lastupdate.txt unavailable"


# ---------------------------------------------------------------------------
# Instability and convergence
# ---------------------------------------------------------------------------


def test_instability_formula() -> None:
    zero = intel.instability_score({})
    assert zero["score"] == 0 and zero["level"] == "low"
    assert set(zero["terms"]) == {"unrest_volume", "unrest_share", "news_tone",
                                  "internet_outages", "earthquakes", "fires",
                                  "weather_alerts", "outbreaks"}
    shown = "\n".join(intel.INSTABILITY_FORMULA)
    assert all(term in shown for term in zero["terms"])
    top = intel.instability_score({
        "gdelt_events": 100, "gdelt_unrest": 100, "gdelt_tone": -10, "outage_critical": 3,
        "quake_max_mag": 8.0, "fires": 5000, "weather_extreme": 3, "outbreaks": 5})
    assert top["score"] == 100 and top["level"] == "severe"
    assert top["terms"] == {"unrest_volume": 20, "unrest_share": 10, "news_tone": 15,
                            "internet_outages": 20, "earthquakes": 10, "fires": 10,
                            "weather_alerts": 5, "outbreaks": 10}
    mid = intel.instability_score({"gdelt_events": 40, "gdelt_unrest": 6, "gdelt_tone": -4.0})
    assert mid["terms"]["unrest_volume"] == 8.43
    assert mid["terms"]["unrest_share"] == 5.0 and mid["terms"]["news_tone"] == 7.5
    assert mid["score"] == 20.9 and mid["level"] == "guarded"
    # Under 20 events the share and tone terms stay at zero.
    thin = intel.instability_score({"gdelt_events": 19, "gdelt_unrest": 19, "gdelt_tone": -9})
    assert thin["terms"]["unrest_share"] == 0 and thin["terms"]["news_tone"] == 0
    assert thin["terms"]["unrest_volume"] == 12.98
    calm = intel.instability_score({"gdelt_events": 50, "gdelt_tone": 2.0,
                                    "quake_max_mag": 4.0, "outage_warning": 1})
    assert calm["terms"]["news_tone"] == 0 and calm["terms"]["earthquakes"] == 0
    assert calm["terms"]["internet_outages"] == 3.33
    assert [intel.instability_level(x) for x in (100, 80, 79.9, 60, 40, 20, 19.9, 0)] == [
        "severe", "severe", "high", "high", "elevated", "guarded", "low", "low"]


def _pt(kind: str, lat: float, lon: float, iso3: str = "", label: str = "") -> dict[str, Any]:
    return {"kind": kind, "lat": lat, "lon": lon, "iso3": iso3, "label": label or kind}


def test_convergence_cells() -> None:
    points = [
        _pt("quake", 25.3, 51.2, "QAT"), _pt("fire", 25.2, 51.3, "QAT"),
        _pt("fire", 25.1, 51.1, "QAT"),
        _pt("eonet", 62.0, 25.0, "FIN"),  # one located kind: never a ring
        _pt("quake", 0.5, -29.5), _pt("eonet", 0.6, -29.6), _pt("gdelt", 0.7, -29.7),
        _pt("weather", 38.5, -98.5, "USA"), _pt("eonet", 38.4, -98.4, "USA"),
    ]
    wide = {"QAT": {"outbreak"}, "FIN": {"outage", "outbreak"}}
    cells = intel.convergence_cells(points, wide, 2.0, 3)
    assert [c["countries"] for c in cells] == [["QAT"], []]
    qat = cells[0]
    assert qat["kinds"] == ["quake", "fire", "outbreak"]
    assert qat["labels"] == ["earthquakes", "fires", "disease outbreak (country-wide)"]
    assert qat["counts"] == {"quake": 1, "fire": 2, "outbreak": 0}
    assert qat["events"] == 4 and qat["score"] == 25 * 3 + 2 * 4
    assert qat["cell"] == [50.0, 24.0, 52.0, 26.0]
    assert qat["lat"] == pytest.approx(25.2) and set(qat["samples"]) == {"quake", "fire"}
    sea = cells[1]
    assert sea["kinds"] == ["quake", "eonet", "gdelt"] and sea["score"] == 75 + 6
    two = intel.convergence_cells(points, wide, 2.0, 2)
    assert sorted(tuple(c["countries"]) for c in two) == [(), ("QAT",), ("USA",)]
    assert intel.convergence_cells(points, wide, 2.0, 4) == []
    many = [_pt("quake", 10.1, 10.1, label=f"q{i}") for i in range(20)] + [
        _pt("fire", 10.2, 10.2), _pt("gdelt", 10.3, 10.3)]
    big = intel.convergence_cells(many, {}, 2.0, 3)[0]
    assert big["score"] == 100 and big["samples"]["quake"] == ["q0", "q1", "q2"]


def test_country_wide_kinds() -> None:
    snap = {"outages": {"FIN": {"critical": 1, "warning": 0}, "XXX": {"critical": 0,
                                                                       "warning": 0}},
            "outbreaks_active": [{"countries": [{"iso3": "FIN"}, {"iso3": "QAT"}]}]}
    assert intel.country_wide_kinds(snap) == {"FIN": {"outage", "outbreak"},
                                              "QAT": {"outbreak"}}


def test_cached_keeps_the_last_good_value(feeds: Feeds) -> None:
    value, error = asyncio.run(intel.cyber())
    assert value["US"]["servers"] == 3 and error == ""
    feeds.fail.add("feodo")
    assert asyncio.run(intel.cyber())[0] is value  # still fresh: no new request
    assert feeds.count(intel.FEODO_URL) == 1
    intel._state.cached["feodo"].attempted_at -= intel.FEODO_TTL_S + 1
    value2, error2 = asyncio.run(intel.cyber())
    assert value2 is value and error2 == "feodo unavailable"
    assert feeds.count(intel.FEODO_URL) == 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_summary_fetches_nothing(client: TestClient, feeds: Feeds) -> None:
    body = client.get("/api/intel/summary").json()
    assert feeds.calls == []
    assert body["borders"]["loaded"] is False and body["generated"] == ""
    assert body["formula"] == list(intel.INSTABILITY_FORMULA)
    assert body["note"].startswith("This application's own heuristic")
    assert [lv["level"] for lv in body["levels"]] == [
        "severe", "high", "elevated", "guarded", "low"]


def test_borders_download_once_and_load_from_disk(client: TestClient, feeds: Feeds,
                                                  tmp_path: Path) -> None:
    body = client.get("/api/intel/countries").json()
    assert body["count"] == 14
    assert {"iso3", "iso2", "name", "lat", "lon", "bbox"} <= set(body["countries"][0])
    assert feeds.count(intel.NE_URL) == 1
    saved = tmp_path / intel.BORDERS_FILE
    assert saved.is_file()
    assert json.loads(saved.read_text(encoding="utf-8"))["source"] == intel.NE_URL
    intel.reset_state()
    assert client.get("/api/intel/countries").json()["count"] == 14
    assert feeds.count(intel.NE_URL) == 1  # read back from disk
    b = client.get("/api/intel/borders", params={"tol": 0.2}).json()
    assert b["count"] == 14 and b["tol_deg"] == 0.2
    assert client.get("/api/intel/borders", params={"tol": 5}).status_code == 422


def test_borders_unavailable_answers_503_and_waits(client: TestClient, feeds: Feeds) -> None:
    feeds.fail.add("borders")
    r = client.get("/api/intel/countries")
    assert r.status_code == 503 and "did not download" in r.json()["detail"]
    assert client.get("/api/intel/country/CHE").status_code == 503
    assert client.get("/api/intel/instability").status_code == 503
    assert client.get("/api/intel/country-at", params={"lat": 1, "lon": 1}).status_code == 503
    assert feeds.count(intel.NE_URL) == 1  # retried after ten minutes, not per request
    assert client.get("/api/intel/summary").json()["borders"]["error"]


def test_country_at(client: TestClient) -> None:
    r = client.get("/api/intel/country-at", params={"lat": 46.95, "lon": 7.44})
    assert r.json()["iso3"] == "CHE"
    assert client.get("/api/intel/country-at", params={"lat": 0, "lon": -30}).status_code == 404
    assert client.get("/api/intel/country-at", params={"lat": 95, "lon": 0}).status_code == 422


def test_instability_route_scores_every_feed(client: TestClient, feeds: Feeds) -> None:
    body = client.get("/api/intel/instability").json()
    rows = {r["iso3"]: r for r in body["countries"]}
    assert [r["score"] for r in body["countries"]] == sorted(
        (r["score"] for r in body["countries"]), reverse=True)
    assert body["countries"][0]["iso3"] == "COD"
    cod = rows["COD"]["terms"]
    assert cod["outbreaks"] == 10 and cod["fires"] == 3.15
    assert rows["COD"]["inputs"]["fires"] == 10  # the old fire is outside the window
    qat = rows["QAT"]
    assert qat["terms"]["outbreaks"] == 5 and qat["terms"]["earthquakes"] == 1.2
    assert qat["terms"]["fires"] == 0.91 and qat["score"] == 7.1
    assert qat["inputs"]["gdelt_events"] == 2
    usa = rows["USA"]
    assert usa["terms"]["earthquakes"] == 6 and usa["terms"]["weather_alerts"] == 3.33
    assert usa["inputs"]["weather_alerts"] == 4 and usa["inputs"]["eonet"] == 1
    assert usa["inputs"]["gdelt_events"] == 3 and usa["terms"]["news_tone"] == 0
    assert rows["FIN"]["terms"]["internet_outages"] == 6.67
    assert rows["KEN"]["score"] == rows["NGA"]["score"] == 10
    assert rows["CHE"]["score"] == rows["FRA"]["score"] == 5
    assert "JPN" not in rows  # its only quake is older than a day
    assert body["formula"] == list(intel.INSTABILITY_FORMULA)
    src = body["sources"]
    assert src["fires"]["ok"] and src["who"]["count"] == 12 and src["feodo"]["count"] == 5
    assert src["gdelt"]["ok"] and src["quakes"]["count"] == 4
    assert src["weather"]["note"] == "NWS alerts cover the United States only"
    # Cached: a second read within five minutes fetches nothing new.
    before = len(feeds.calls)
    client.get("/api/intel/instability")
    client.get("/api/intel/outages")
    assert len(feeds.calls) == before


def test_missing_feeds_say_so(client: TestClient, feeds: Feeds) -> None:
    feeds.fail |= {"who outbreaks", "ioda", "feodo", "gdelt"}
    feeds.fires_key = False
    body = client.get("/api/intel/instability").json()
    src = body["sources"]
    assert not src["who"]["ok"] and src["who"]["note"] == "who unavailable"
    assert not src["ioda"]["ok"] and not src["feodo"]["ok"] and not src["gdelt"]["ok"]
    assert not src["fires"]["ok"] and "FIRMS_MAP_KEY" in src["fires"]["note"]
    rows = {r["iso3"]: r for r in body["countries"]}
    assert rows["USA"]["terms"]["earthquakes"] == 6  # the local feeds still count
    assert "COD" not in rows
    assert client.get("/api/intel/outages").json()["error"] == "ioda unavailable"


def test_country_card(client: TestClient) -> None:
    card = client.get("/api/intel/country/CHE").json()
    assert card["name"] == "Switzerland" and card["iso2"] == "CH"
    assert card["capital"] == {"name": "Bern", "lat": pytest.approx(46.948),
                               "lon": pytest.approx(7.44821)}
    assert card["region"] == "Europe & Central Asia" and card["income_group"] == "High income"
    ind = {i["key"]: i for i in card["indicators"]}
    assert ind["population"]["value"] == 9092436 and ind["population"]["year"] == 2025
    assert ind["gdp_usd"]["value"] == pytest.approx(1.0435e12, rel=1e-3)
    assert ind["gdp_per_capita_usd"]["id"] == "NY.GDP.PCAP.CD"
    assert ind["inflation_pct"]["value"] == pytest.approx(0.154, abs=1e-3)
    assert card["local_time"] == {"zone": "Europe/Zurich", "time": "06:00",
                                  "date": "2026-11-20", "weekday": "Fri",
                                  "utc_offset": "+01:00"}
    assert [(e["mic"], e["state"], e["next"]) for e in card["exchanges"]] == [
        ("XSWX", "closed", "opens 09:00 local")]
    assert card["events_24h"]["outbreaks_30d"] == 1 and card["events_24h"]["quakes"] == 0
    inst = card["instability"]
    assert inst["score"] == 5 and inst["terms"]["outbreaks"] == 5
    assert inst["formula"] == list(intel.INSTABILITY_FORMULA) and inst["note"]
    assert [o["id"] for o in card["outbreaks"]] == ["2026-DON627"]
    assert card["cyber"] is None and card["outages"] is None
    assert 400000 <= card["view"]["height_m"] <= 12000000
    assert card["cameras"]["lat"] == pytest.approx(46.948)
    assert 100 <= card["cameras"]["radius_km"] <= 2000
    assert card["attribution"][0] == intel.BORDERS_ATTRIBUTION
    assert client.get("/api/intel/country/ch").json()["iso3"] == "CHE"


def test_country_card_with_feeds(client: TestClient) -> None:
    qat = client.get("/api/intel/country/QAT").json()
    assert qat["events_24h"]["quakes"] == 1 and qat["events_24h"]["fires"] == 1
    assert qat["events_24h"]["gdelt_events"] == 2
    assert qat["local_time"]["zone"] == "Asia/Qatar" and qat["local_time"]["time"] == "08:00"
    assert qat["exchanges"][0]["state"] == "non-trading day"  # Friday in Doha
    usa = client.get("/api/intel/country/US").json()
    assert usa["cyber"]["servers"] == 3 and usa["cyber"]["online"] == 1
    fin = client.get("/api/intel/country/FIN").json()
    assert fin["outages"]["critical"] == 1 and fin["events_24h"]["outage_sources"] == 1
    assert fin["exchanges"][0]["mic"] == "XHEL"
    assert client.get("/api/intel/country/ZZZ").status_code == 404
    assert client.get("/api/intel/country/1x").status_code == 422


def test_outbreaks_cyber_outages_routes(client: TestClient) -> None:
    month = client.get("/api/intel/outbreaks", params={"days": 30}).json()
    assert month["count"] == 6 and month["error"] == ""
    assert client.get("/api/intel/outbreaks").json()["count"] == 12
    cyber = client.get("/api/intel/cyber").json()
    assert cyber["countries"][0]["iso3"] == "USA"
    assert {r["iso3"] for r in cyber["countries"]} == {"USA", "GBR", "JPN"}
    assert (cyber["servers"], cyber["online"]) == (5, 1)
    assert all("lat" in r and "lon" in r for r in cyber["countries"])
    outages = client.get("/api/intel/outages").json()
    assert [r["iso3"] for r in outages["countries"]] == ["FIN"]
    assert outages["countries"][0]["url"].endswith("/country/FI")


def test_convergence_route(client: TestClient) -> None:
    body = client.get("/api/intel/convergence").json()
    assert body["count"] == 1 and body["window_h"] == 24 and body["min_kinds"] == 3
    cell = body["cells"][0]
    assert cell["kinds"] == ["quake", "fire", "outbreak"] and cell["countries"] == ["QAT"]
    assert cell["score"] == 75 + 2 * 3
    assert body["method"] == intel.CONVERGENCE_METHOD
    two = client.get("/api/intel/convergence", params={"min_kinds": 2}).json()
    countries = [c["countries"] for c in two["cells"]]
    assert ["USA"] in countries and ["FIN"] not in countries
    assert client.get("/api/intel/convergence", params={"cell_deg": 0.1}).status_code == 422


def test_exchanges_route(client: TestClient) -> None:
    body = client.get("/api/intel/exchanges", params={"country": "sa"}).json()
    assert [r["mic"] for r in body["exchanges"]] == ["XSAU"]
    assert body["at"] == "2026-11-20T05:00:00Z" and body["open"] == 0
    everything = client.get("/api/intel/exchanges").json()
    assert everything["count"] == len(intel.EXCHANGES)
    assert everything["open"] == sum(1 for r in everything["exchanges"] if r["open"])
    assert client.get("/api/intel/exchanges", params={"country": "USA"}).status_code == 422


def test_module_text_stays_plain() -> None:
    for path in (Path(intel.__file__), Path(intel_routes.__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path
        assert all(ord(ch) < 128 for ch in text), path
