"""Tests for the orbit-visuals routes (sda.routes.orbitviz).

Offline. The SATCAT side reads tests/data/orbitviz/satcat_sample.json: 36 real
Spacebook rows recorded on 2026-11-19 (orbital fields dropped), chosen to cover
pieces of the curated debris events (on orbit and re-entered), the first objects
of 1957 and 1958, and one uncurated debris cloud. The visible-pass side uses the
bundled demo catalog (sda.demo_tles), whose elements date from 2026-03-22.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import satcat
from sda.demo_tles import DEMO_TLE_TEXT
from sda.passes import (
    CIVIL_TWILIGHT_DEG,
    Observer,
    is_sunlit,
    solar_position_eci_km,
    sun_elevation_deg,
)
from sda.propagator import build_satrec, datetime_to_jd
from sda.routes import orbitviz
from sda.routes.orbitviz import (
    DEBRIS_EVENTS,
    MIN_MEAN_MOTION_REV_DAY,
    STD_MAGNITUDES,
    TYPE_CODES,
    YEAR_ALPHABET,
    YEAR_BASE,
    apparent_magnitude,
    build_attributes,
    build_debris,
    build_history,
    candidate_tles,
    encode_year,
    find_visible_passes,
    launch_designator,
    mean_motion_rev_day,
    phase_factor,
    router,
    satcat_entries,
    standard_magnitude,
    sunlit_mask,
)
from sda.satcat import SatcatStore
from sda.tle_store import TLEStore

DATA = Path(__file__).resolve().parent / "data" / "orbitviz"
SAMPLE_ROWS = json.loads((DATA / "satcat_sample.json").read_text(encoding="utf-8"))
NOW = 1_795_000_000.0  # 2026-11-18T08:26:40Z

LONDON = Observer(name="london", latitude_deg=51.5, longitude_deg=0.0, altitude_km=0.0)
DEMO_START = datetime(2026, 3, 22, tzinfo=UTC)  # the demo elements' epoch day

# A geostationary set (mean motion 1.0027 rev/day): never a naked-eye candidate.
GEO_TLE = (
    "INTELSAT 901\n"
    "1 26824U 01024A   26081.50000000  .00000000  00000+0  00000+0 0  9990\n"
    "2 26824   0.0150  80.0000 0002000 100.0000 260.0000  1.00270000 90009\n"
)


def _offline_download(url: str, dest: Path, timeout: float) -> None:
    raise satcat.SatcatFetchError("offline test")


def write_store(directory: Path, rows: list[object] | None) -> SatcatStore:
    """A store over a recorded cache (or an empty directory), never the network."""
    directory.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        (directory / satcat.SATCAT_FILE).write_text(json.dumps(rows), encoding="utf-8")
        meta = {"source": "spacebook", "url": "recorded", "fetched_at": satcat._iso(NOW),
                "records": len(rows)}
        (directory / "satcat.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return SatcatStore(directory, download=_offline_download, spacetrack=None,
                       clock=lambda: NOW, min_records=1, details_network=False)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(tmp_path / "env-cache"))
    satcat.set_store(None)
    orbitviz.clear_cache()
    yield
    satcat.set_store(None)
    orbitviz.clear_cache()


@pytest.fixture
def store(tmp_path) -> SatcatStore:
    s = write_store(tmp_path / "satcat", SAMPLE_ROWS)
    s.ensure_cache_loaded()
    satcat.set_store(s)
    return s


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def demo_store() -> TLEStore:
    s = TLEStore()
    s.load_from_text(DEMO_TLE_TEXT)
    return s


# --- packing ----------------------------------------------------------------------


def test_year_alphabet_costs_one_json_byte_per_year():
    assert len(json.dumps(YEAR_ALPHABET)) == len(YEAR_ALPHABET) + 2
    assert '"' not in YEAR_ALPHABET and "\\" not in YEAR_ALPHABET
    assert len(set(YEAR_ALPHABET)) == len(YEAR_ALPHABET)
    assert encode_year(0) == YEAR_ALPHABET[0]
    assert encode_year(1957) == YEAR_ALPHABET[1]
    assert YEAR_ALPHABET.index(encode_year(2026)) + YEAR_BASE == 2026
    assert encode_year(1900) == encode_year(2200) == YEAR_ALPHABET[0]


def test_launch_designator():
    assert launch_designator("1999-025ABC") == "1999-025"
    assert launch_designator("1993-036E") == "1993-036"
    assert launch_designator("") == ""
    assert launch_designator("99025A") == ""


def test_attributes_decode_back_to_the_entries(store):
    entries = satcat_entries(store)
    body = build_attributes(entries, on_orbit=True)
    on = sorted(e.norad_id for e in entries if e.on_orbit)
    ids = [int(v) for v in np.cumsum(body["id_deltas"])]
    assert ids == on and body["count"] == len(on)
    by_id = {e.norad_id: e for e in entries}
    owners = [o["code"] for o in body["owners"]]
    for i, nid in enumerate(ids):
        e = by_id[nid]
        assert body["types"][i] == TYPE_CODES.get(e.object_type, "U")
        assert owners[body["owner_index"][i]] == e.owner
        pos = body["year_alphabet"].index(body["years"][i])
        assert (body["year_base"] + pos if pos else 0) == e.launch_year
    counts = [o["count"] for o in body["owners"]]
    assert counts == sorted(counts, reverse=True) and sum(counts) == len(on)
    assert body["type_codes"]["D"] == "DEBRIS" and body["rcs_codes"]["L"] == "LARGE"
    everything = build_attributes(entries, on_orbit=False)
    assert everything["count"] == len(entries) > len(on)


def test_history_matches_a_brute_force_count(store):
    entries = satcat_entries(store)
    body = build_history(entries, 2026)
    assert body["years"][0] == 1957 and body["years"][-1] == 2026
    for i, year in enumerate(body["years"]):
        expected = 0
        for e in entries:
            if not e.launch_year or e.launch_year > year:
                continue
            decay = int(e.decay_date[:4]) if e.decay_date else None
            if decay is None or max(decay, e.launch_year) > year:
                expected += 1
        assert body["total"][i] == expected, year
        assert sum(body["in_orbit"][k][i] for k in body["in_orbit"]) == expected
    # 1957: Sputnik 1 is up at the year end, its rocket body came down in December.
    assert body["in_orbit"]["PAYLOAD"][0] == 1
    assert body["in_orbit"]["ROCKET BODY"][0] == 0
    # 1958: Sputnik 1 has re-entered, Vanguard 1 is still in orbit today.
    assert body["in_orbit"]["PAYLOAD"][1] == 1
    assert body["launched"][0] == 2 and body["decayed"][0] == 1 and body["decayed"][1] == 1
    assert body["no_launch_date"] == 0


def test_debris_events_count_pieces_by_launch(store):
    body = build_debris(satcat_entries(store), top=5, min_pieces=2)
    events = {ev["key"]: ev for ev in body["events"]}
    assert [ev["key"] for ev in body["events"]] == [ev.key for ev in DEBRIS_EVENTS]
    fy = events["fengyun-1c"]
    assert (fy["catalogued"], fy["on_orbit"], fy["decayed"]) == (9, 6, 3)
    assert fy["ids"] == [25733, 29733, 29734, 29735, 29736, 29737]
    pair = events["cosmos-2251-iridium-33"]
    assert pair["designators"] == ["1993-036", "1997-051"]
    assert (pair["catalogued"], pair["on_orbit"], pair["decayed"]) == (8, 6, 2)
    asat = events["cosmos-1408"]
    assert (asat["on_orbit"], asat["decayed"]) == (3, 2)
    assert events["microsat-r"]["catalogued"] == 0
    assert [f["key"] for f in body["found"]] == ["launch-1970-025"]
    found = body["found"][0]
    assert found["name"] == "THORAD AGENA D" and found["on_orbit"] == 4
    assert found["curated"] is False and found["event_year"] is None
    lean = build_debris(satcat_entries(store), top=5, min_pieces=5, include_ids=False)
    assert lean["found"] == [] and all(ev["ids"] == [] for ev in lean["events"])


# --- routes over the SATCAT store ------------------------------------------------------


def test_attribute_history_and_debris_routes(store, client):
    attrs = client.get("/api/orbitviz/attributes")
    assert attrs.status_code == 200
    body = attrs.json()
    assert body["count"] == sum(1 for r in SAMPLE_ROWS if not r.get("decayDate"))
    assert body["source"] == "spacebook" and body["attribution"]
    history = client.get("/api/orbitviz/history").json()
    assert history["first_year"] == 1957 and history["last_year"] >= 2026
    debris = client.get("/api/orbitviz/debris", params={"top": 3, "min_pieces": 2}).json()
    assert debris["events"][0]["key"] == "fengyun-1c" and debris["min_pieces"] == 2
    assert client.get("/api/orbitviz/debris", params={"top": 99}).status_code == 422


def test_tables_are_built_once_per_catalog(store):
    calls = []

    def build():
        calls.append(1)
        return {"n": len(calls)}

    first = orbitviz._cached("probe", store, (1,), build)
    again = orbitviz._cached("probe", store, (1,), build)
    other = orbitviz._cached("probe", store, (2,), build)
    assert first is again and other["n"] == 2 and len(calls) == 2
    orbitviz.clear_cache()
    assert orbitviz._cached("probe", store, (2,), build)["n"] == 3


def test_empty_satcat_answers_503_with_a_hint(tmp_path, client):
    satcat.set_store(write_store(tmp_path / "empty", None))
    for path in ("/api/orbitviz/attributes", "/api/orbitviz/history", "/api/orbitviz/debris"):
        res = client.get(path)
        assert res.status_code == 503, path
        detail = res.json()["detail"]
        assert detail["message"] == "SATCAT not loaded" and detail["hint"]


# --- visible tonight ----------------------------------------------------------------


def test_phase_law_and_magnitude_scale():
    assert phase_factor(math.pi / 2) == pytest.approx(1.0)
    assert phase_factor(0.0) == pytest.approx(math.pi)
    assert phase_factor(math.pi) == pytest.approx(0.0, abs=1e-12)
    sat = [7000.0, 0.0, 0.0]
    sun_side = [1.5e8, 0.0, 0.0]
    # Observer 1000 km away at right angles to the Sun: the standard magnitude.
    assert apparent_magnitude(2.0, sat, [7000.0, -1000.0, 0.0], sun_side) \
        == pytest.approx(2.0, abs=1e-6)
    # Twice the range is 5 log10(2) fainter.
    assert apparent_magnitude(2.0, sat, [7000.0, -2000.0, 0.0], sun_side) \
        == pytest.approx(2.0 + 5 * math.log10(2), abs=1e-6)
    # Full phase (Sun behind the observer, in line) is 2.5 log10(pi) brighter.
    assert apparent_magnitude(2.0, sat, [6000.0, 0.0, 0.0], [-1.5e8, 0.0, 0.0]) \
        == pytest.approx(2.0 - 2.5 * math.log10(math.pi), abs=1e-6)


def test_standard_magnitude_fallbacks(store):
    assert standard_magnitude(25544, store) == (STD_MAGNITUDES[25544], "table")
    assert standard_magnitude(5, store) == (6.5, "estimate from SATCAT radar size")
    assert standard_magnitude(57286, store)[0] == 8.0       # SMALL debris
    assert standard_magnitude(44714, store)[0] == 4.5       # LARGE payload
    assert standard_magnitude(1, store) == (3.5, "estimate from SATCAT type and radar size")
    assert standard_magnitude(2, store) == (5.5, "default")  # no published size
    assert standard_magnitude(99999, None) == (5.5, "default")


def test_candidates_take_requested_table_then_groups_and_skip_slow_orbits():
    app = demo_store()
    low = [t.norad_id for t in app.get_all()
           if mean_motion_rev_day(t) >= MIN_MEAN_MOTION_REV_DAY
           and t.norad_id not in STD_MAGNITUDES]
    pick = low[-1]
    tles, sources = candidate_tles(app, {"visual": DEMO_TLE_TEXT, "stations": GEO_TLE},
                                   [pick, 26824])
    ids = [t.norad_id for t in tles]
    assert ids[0] == pick and ids[1:4] == list(STD_MAGNITUDES)
    assert 26824 not in ids  # geostationary: below the mean-motion floor
    assert all(mean_motion_rev_day(t) >= MIN_MEAN_MOTION_REV_DAY for t in tles)
    assert sorted(ids) == sorted(low + list(STD_MAGNITUDES))
    assert sources == {"requested": 1, "table": 3, "visual": len(low) - 1}
    assert all(t is app.get(t.norad_id) for t in tles)  # the app catalog's copy wins
    few, _ = candidate_tles(app, {"visual": DEMO_TLE_TEXT}, [], limit=5)
    assert len(few) == 5
    bad, bad_sources = candidate_tles(TLEStore(), {"visual": "garbage", "stations": None}, [])
    assert bad == [] and bad_sources == {}


def test_visible_passes_are_sunlit_under_a_dark_sky():
    iss = demo_store().get(25544)
    assert iss is not None
    passes, dark = find_visible_passes([iss], LONDON, DEMO_START, 48, 10.0, 4.5, 20, None)
    assert dark > 0 and passes
    mags = [p["magnitude"] for p in passes]
    assert mags == sorted(mags) and max(mags) <= 4.5
    for p in passes:
        start = datetime.fromisoformat(p["visible_start"]["time"])
        end = datetime.fromisoformat(p["visible_end"]["time"])
        bright = datetime.fromisoformat(p["brightest_time"])
        assert start <= bright <= end and p["visible_seconds"] >= 0
        assert p["visible_peak"]["elevation_deg"] <= p["max_elevation_deg"] + 0.5
        assert p["std_magnitude_source"] == "table"
        # Independent check at the brightest moment with sda.passes itself.
        assert sun_elevation_deg(LONDON, bright) < CIVIL_TWILIGHT_DEG
        jd, fr = datetime_to_jd(bright)
        err, pos, _ = build_satrec(iss).sgp4(jd, fr)
        assert err == 0 and is_sunlit(pos, solar_position_eci_km(bright))


def test_vectorized_shadow_matches_the_scalar_test():
    tle = demo_store().get(25544)
    assert tle is not None
    times = [DEMO_START + timedelta(minutes=7 * k) for k in range(40)]
    site = orbitviz._site(times, LONDON)
    _, pos, _ = build_satrec(tle).sgp4_array(site.jd, site.fr)
    mask = sunlit_mask(np.asarray(pos), site.sun_hat)
    for k, moment in enumerate(times):
        assert bool(mask[k]) == is_sunlit(pos[k], solar_position_eci_km(moment))
    assert mask.any() and not mask.all()


def test_no_dark_sky_means_no_passes():
    # Svalbard at midsummer: the Sun never drops 6 deg below the horizon.
    north = Observer(latitude_deg=78.2, longitude_deg=15.6)
    iss = demo_store().get(25544)
    assert iss is not None
    passes, dark = find_visible_passes([iss], north, datetime(2026, 6, 21, tzinfo=UTC), 24,
                                       10.0, 4.5, 20, None)
    assert passes == [] and dark == 0


def test_visible_route(monkeypatch, client):
    monkeypatch.setattr(orbitviz, "_tle_store", demo_store)
    monkeypatch.setattr(orbitviz, "_group_text", lambda group: None)
    payload = {"observer": {"latitude_deg": 51.5, "longitude_deg": 0.0, "altitude_km": 0.0},
               "start": DEMO_START.isoformat(), "hours": 48, "norad_ids": [25544]}
    res = client.post("/api/orbitviz/visible", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["candidate_sources"] == {"requested": 1, "table": 2}
    assert body["candidates"] == 3 and body["dark_minutes"] > 0
    assert body["count"] == len(body["passes"]) > 0
    assert body["passes"][0]["norad_id"] == 25544 and "estimates" in body["method"]
    assert client.post("/api/orbitviz/visible", json={**payload, "hours": 49}).status_code == 422
    too_many = {**payload, "norad_ids": list(range(1, 52))}
    assert client.post("/api/orbitviz/visible", json=too_many).status_code == 422


def test_visible_route_without_candidates_is_503(monkeypatch, client):
    monkeypatch.setattr(orbitviz, "_tle_store", TLEStore)
    monkeypatch.setattr(orbitviz, "_group_text", lambda group: None)
    res = client.post("/api/orbitviz/visible",
                      json={"observer": {"latitude_deg": 51.5, "longitude_deg": 0.0}})
    assert res.status_code == 503
