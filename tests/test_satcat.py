"""Tests for the SATCAT store (sda.satcat) and its routes (sda.routes.satcat).

Offline: the store takes its downloader, Space-Track source and clock as
arguments, so every fetch here is a fake that writes a recorded sample into the
temp file the store hands it. The sample rows are real Spacebook rows recorded on
2026-11-19 (orbital fields trimmed), plus a few hand-made rows for the cases the
real catalog does not show in a handful of objects.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import satcat, spacetrack
from sda.routes.satcat import router
from sda.satcat import (
    ATTRIBUTION,
    DETAILS_URL,
    SATCAT_URL,
    SatcatFetchError,
    SatcatStore,
    cache_is_fresh,
    decode_norad,
    details_from_row,
    entry_from_row,
    iter_json_array,
    iter_json_file,
    parse_satcat,
    row_from_spacetrack,
)

NOW = 1_795_000_000.0  # 2026-11-18T08:26:40Z, a whole second so ISO round trips exactly
HOUR = 3600.0

# --- recorded sample --------------------------------------------------------------

SATCAT_ROWS: list[object] = [
    {"id": "bf72c797-cee3-45b2-8de1-5bc16ac62ea8", "intlId": "1998-067A", "name": "ISS (ZARYA)",
     "noradId": 25544, "launchDate": "1998-11-20T00:00:00", "objectType": "PAYLOAD",
     "owner": "CIS", "launchSite": "TTMTR", "period": 92.94, "rcsSize": "LARGE"},
    {"id": "6e3cc260-6b5a-4c7c-a58a-e74484dd2583", "intlId": "1998-067B", "name": "SL-13 R/B",
     "noradId": 25545, "launchDate": "1998-11-20T00:00:00", "decayDate": "1998-11-27T00:00:00",
     "objectType": "ROCKET BODY", "owner": "CIS", "launchSite": "TTMTR", "rcsSize": ""},
    {"id": "22842c32-848a-4655-9809-e8ab858e5cdc", "intlId": "1998-067XT", "name": "DUPLEX",
     "noradId": 66906, "launchDate": "1998-11-20T00:00:00", "objectType": "PAYLOAD",
     "owner": "US", "launchSite": "TTMTR", "rcsSize": "MEDIUM"},
    {"id": "c0ea0892-1ba1-42c4-b5cc-7419c17d85ba", "intlId": "1999-025X",
     "name": "FENGYUN 1C DEB", "noradId": 29733, "launchDate": "1999-05-10T00:00:00",
     "objectType": "DEBRIS", "owner": "PRC", "launchSite": "TSC", "rcsSize": "MEDIUM"},
    {"id": "2c8478be-f9b9-44d9-a41e-e87e7c73650d", "intlId": "1999-025Y",
     "name": "FENGYUN 1C DEB", "noradId": 29734, "launchDate": "1999-05-10T00:00:00",
     "objectType": "DEB", "owner": "PRC", "launchSite": "TSC", "rcsSize": "small"},
    {"id": "253bc397-c7e1-4b91-ab97-8b0a2b648ff0", "intlId": "2021-035A",
     "name": "CSS (TIANHE-1)", "noradId": 48274, "launchDate": "2021-04-29T00:00:00",
     "objectType": "PAYLOAD", "owner": "PRC", "launchSite": "WSC", "rcsSize": "LARGE"},
    {"id": "264945b5-6747-4d7f-8aab-d8408cfc0d64", "intlId": "2017-042C", "name": "OBJECT C",
     "noradId": 42827, "launchDate": "2017-07-14T00:00:00", "objectType": "TBA",
     "owner": "TBD", "launchSite": "TTMTR", "rcsSize": "MEDIUM"},
    {"id": "856bad8c-c469-4059-a9a9-960002f8b5c7", "intlId": "", "name": "81008 - Analyst Sat",
     "noradId": 81008, "objectType": "", "owner": "", "launchSite": "", "rcsSize": ""},
    {"id": "be246613-a39a-43cd-9d89-7a404976ccfb", "intlId": "2026-067CY", "name": "SARAMAGO",
     "noradId": 100001, "launchDate": "2026-03-30T00:00:00", "objectType": "PAYLOAD",
     "owner": "POR", "launchSite": "AFWTR", "rcsSize": "MEDIUM"},
    # junk a real reply could carry: none of it may become a record
    "not a row", {"name": "no id"}, {"noradId": 0}, {"noradId": "garbage"}, None,
]
SATCAT_COUNT = 9
ON_ORBIT = [25544, 29733, 29734, 42827, 48274, 66906, 81008, 100001]

DETAILS_ROWS: list[object] = [
    {"noradId": 25544, "discosMsg": {"comspocDerivedMessage": {
        "adoptedMass": 450000.0, "adoptedSpan": 108, "hbrMeanMeters": 36.48893}},
     "jMcDowellMsg": {"jcat": "S25544", "name": "Zarya", "parent": "S25545", "status": "GRP",
                      "owner": "JSC", "manufacturer": "KHRR", "bus": "77KS", "mass": 20281,
                      "totalMass": 20351, "span": 23.9}},
    {"noradId": 29733, "discosMsg": {"comspocDerivedMessage": {
        "adoptedMass": 5, "adoptedSpan": 10, "hbrMeanMeters": 5.0}},
     "jMcDowellMsg": {"name": "deb FY-1C", "parent": "S25730", "owner": "CASC", "mass": 0,
                      "totalMass": 0, "span": 0.0}},
    {"noradId": 29734, "discosMsg": {"comspocDerivedMessage": {
        "adoptedMass": 5, "adoptedSpan": 10}},
     "jMcDowellMsg": {"parent": "S25730", "owner": "CASC", "totalMass": 2, "span": 0.2}},
    {"noradId": 66906, "discosMsg": {"comspocDerivedMessage": {
        "adoptedMass": 1.0, "adoptedSpan": 0.1}},
     "jMcDowellMsg": {"parent": "A11620*", "owner": "CUA", "manufacturer": "CUA",
                      "bus": "Cubesat 6U", "totalMass": 10, "span": 0.3}},
    {"noradId": 48274, "discosMsg": None,
     "jMcDowellMsg": {"owner": "CMSA", "mass": 22500, "span": -1}},
    {"noradId": "x"}, 7,
]

SPACETRACK_ROWS = [
    {"NORAD_CAT_ID": "25544", "SATNAME": "ISS (ZARYA)", "INTLDES": "1998-067A",
     "OBJECT_TYPE": "PAYLOAD", "COUNTRY": "CIS", "SITE": "TTMTR", "LAUNCH": "1998-11-20",
     "DECAY": None, "RCS_SIZE": "LARGE"},
    {"NORAD_CAT_ID": "25545", "OBJECT_NAME": "SL-13 R/B", "OBJECT_ID": "1998-067B",
     "OBJECT_TYPE": "ROCKET BODY", "COUNTRY": "CIS", "SITE": "TTMTR", "LAUNCH": "1998-11-20",
     "DECAY": "1998-11-27", "RCS_SIZE": None},
    {"NORAD_CAT_ID": "nope"},
]


def as_bytes(rows: object) -> bytes:
    return json.dumps(rows, indent=1).encode("utf-8")


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeNet:
    """Stands in for http_download: writes a recorded body, or raises."""

    def __init__(self, routes: dict[str, bytes | Exception] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path, timeout: float) -> None:
        self.calls.append(url)
        body = self.routes.get(url)
        if isinstance(body, Exception):
            raise body
        if body is None:
            raise SatcatFetchError(f"HTTP 503 from {url}")
        dest.write_bytes(body)


def write_cache(directory: Path, name: str, rows: object, fetched: float | None,
                source: str = "spacebook") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(as_bytes(rows))
    if fetched is not None:
        meta = {"source": source, "url": "recorded", "fetched_at": satcat._iso(fetched),
                "records": 0}
        path.with_name(path.stem + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def make_store(directory: Path, net: FakeNet | None = None, **kwargs) -> SatcatStore:
    kwargs.setdefault("spacetrack", None)
    kwargs.setdefault("clock", Clock())
    kwargs.setdefault("min_records", 1)
    kwargs.setdefault("details_network", False)
    return SatcatStore(directory, download=net or FakeNet(), **kwargs)


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("*.tmp"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the process-wide store at a temp dir and forget it afterwards."""
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(tmp_path / "env-cache"))
    monkeypatch.delenv(satcat.OFFLINE_ENV, raising=False)
    monkeypatch.delenv(satcat.DETAILS_ENV, raising=False)
    satcat.set_store(None)
    yield
    satcat.set_store(None)


# --- streaming parser ---------------------------------------------------------------

TRICKY = (
    '[ {"a": "x], [y", "b": "{\\"q\\": 1}"}, 12345678901234567, -1.5e3, "tail \\u00e9",'
    ' [1, [2, []]], {"n": null, "t": true}, false ,"", 0 ]'
)


@pytest.mark.parametrize("chunk", [1, 16, 17, 23, 1 << 20])
def test_stream_matches_json_load_at_any_chunk_size(chunk):
    assert list(iter_json_array(io.StringIO(TRICKY), chunk)) == json.loads(TRICKY)


def test_numbers_cut_at_the_buffer_edge_are_read_whole():
    numbers = [10**15 + i for i in range(40)]
    text = json.dumps(numbers, separators=(",", ":"))
    for chunk in (16, 17, 18, 19):
        assert list(iter_json_array(io.StringIO(text), chunk)) == numbers


@pytest.mark.parametrize("text", ["[]", "  [ ]  ", "\n[\n]\n", "﻿[]"])
def test_empty_arrays(text):
    assert list(iter_json_array(io.StringIO(text))) == []


def test_byte_order_mark_is_skipped(tmp_path):
    assert list(iter_json_array(io.StringIO("﻿[1, 2]"))) == [1, 2]
    path = tmp_path / "bom.json"
    path.write_text('[{"noradId": 5}]', encoding="utf-8-sig")
    assert list(iter_json_file(path, 16)) == [{"noradId": 5}]


@pytest.mark.parametrize("text", ["", "{}", '"x"', "[1, 2", "[1 2]", "[1,]", "[1]]x", "[] x",
                                  "<html>busy</html>", '[{"a": 1}, {"b":'])
def test_malformed_input_raises_value_error(text):
    with pytest.raises(ValueError):
        list(iter_json_array(io.StringIO(text), 16))


def test_parse_satcat_shares_repeated_strings():
    rows = [{"noradId": 40000 + i, "name": "FENGYUN 1C DEB", "owner": "PRC",
             "launchDate": "1999-05-10T00:00:00", "objectType": "DEBRIS"} for i in range(3000)]
    text = json.dumps(rows)
    entries = parse_satcat(iter_json_array(io.StringIO(text), 4096))
    assert len(entries) == 3000
    a, b = entries[40000], entries[42999]
    # one string object per distinct value, not 3,000 copies
    assert a.name is b.name and a.owner is b.owner and a.launch_date is b.launch_date
    assert a.launch_year == 1999


# --- row decoding ---------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "want"), [
    (25544, 25544), ("25544", 25544), ("025544", 25544), (" 5 ", 5), (25544.0, 25544),
    ("A0001", 100001), ("a0001", 100001), ("Z9999", 339999), ("J0000", 180000),
    (0, None), (-3, None), (340000, None), (25544.5, None), (True, None), (None, None),
    ("I0001", None), ("O1234", None), ("A001", None), ("abc", None), ("", None), ([1], None),
])
def test_decode_norad(raw, want):
    assert decode_norad(raw) == want


def test_entry_from_row_normalizes():
    rows = {r["noradId"]: r for r in SATCAT_ROWS if isinstance(r, dict) and "noradId" in r}
    iss = entry_from_row(rows[25544])
    assert iss is not None
    assert (iss.name, iss.intl_id, iss.owner, iss.object_type, iss.launch_date,
            iss.launch_year, iss.launch_site, iss.decay_date, iss.rcs_size) == (
        "ISS (ZARYA)", "1998-067A", "CIS", "PAYLOAD", "1998-11-20", 1998, "TTMTR", "", "LARGE")
    assert iss.on_orbit and iss.guid == "bf72c797-cee3-45b2-8de1-5bc16ac62ea8"
    deb = entry_from_row(rows[29734])
    assert deb is not None and deb.object_type == "DEBRIS" and deb.rcs_size == "SMALL"
    rb = entry_from_row(rows[25545])
    assert rb is not None and rb.decay_date == "1998-11-27" and not rb.on_orbit
    assert rb.rcs_size == "UNKNOWN"
    tbd = entry_from_row(rows[42827])
    assert tbd is not None and tbd.object_type == "UNKNOWN"
    analyst = entry_from_row(rows[81008])
    assert analyst is not None
    assert (analyst.owner, analyst.launch_date, analyst.launch_year, analyst.intl_id) == (
        "", "", 0, "")
    odd = entry_from_row({"noradId": "5", "name": " VANGUARD\x00 1\x1b ", "intlId": "1958-002b",
                          "owner": "-", "launchDate": "soon", "objectType": 7, "rcsSize": 3})
    assert odd is not None
    assert (odd.name, odd.intl_id, odd.owner, odd.launch_date, odd.object_type,
            odd.rcs_size) == ("VANGUARD 1", "1958-002B", "", "", "UNKNOWN", "UNKNOWN")
    unnamed = entry_from_row({"noradId": 77, "intlId": "not-an-id"})
    assert unnamed is not None and unnamed.name == "NORAD 77" and unnamed.intl_id == ""
    for junk in ("x", None, {}, {"noradId": 0}, {"noradId": "nope"}, {"noradId": 400000}):
        assert entry_from_row(junk) is None


def test_details_size_source_and_placeholder():
    got = {}
    for row in DETAILS_ROWS:
        parsed = details_from_row(row)
        if parsed is not None:
            got[parsed[0]] = parsed[1]
    assert sorted(got) == [25544, 29733, 29734, 48274, 66906]
    iss = got[25544]
    # COMSPOC's whole-station values, not GCAT's Zarya module
    assert (iss.mass_kg, iss.span_m, iss.size_source) == (450000.0, 108.0, "COMSPOC")
    assert (iss.manufacturer, iss.bus, iss.operator, iss.parent) == (
        "KHRR", "77KS", "JSC", "S25545")
    # the 5 kg / 10 m placeholder is no size at all, and GCAT has none either
    assert (got[29733].mass_kg, got[29733].span_m, got[29733].size_source) == (None, None, "")
    # ... or GCAT has one, and fills in behind it
    assert (got[29734].mass_kg, got[29734].span_m, got[29734].size_source) == (2.0, 0.2, "GCAT")
    assert (got[66906].mass_kg, got[66906].span_m) == (1.0, 0.1)
    assert got[66906].parent == "A11620*"
    # no COMSPOC message: GCAT mass, and a negative span is dropped
    assert (got[48274].mass_kg, got[48274].span_m, got[48274].size_source) == (
        22500.0, None, "GCAT")
    mixed = details_from_row({"noradId": 9, "discosMsg": {"comspocDerivedMessage": {
        "adoptedMass": 12.5}}, "jMcDowellMsg": {"span": 1.5, "totalMass": True}})
    assert mixed is not None
    assert (mixed[1].mass_kg, mixed[1].span_m, mixed[1].size_source) == (
        12.5, 1.5, "COMSPOC and GCAT")


def test_row_from_spacetrack_maps_fields():
    row = row_from_spacetrack(SPACETRACK_ROWS[1])
    assert row is not None
    entry = entry_from_row(row)
    assert entry is not None
    assert (entry.norad_id, entry.name, entry.intl_id, entry.object_type, entry.owner,
            entry.launch_site, entry.launch_date, entry.decay_date, entry.rcs_size) == (
        25545, "SL-13 R/B", "1998-067B", "ROCKET BODY", "CIS", "TTMTR", "1998-11-20",
        "1998-11-27", "UNKNOWN")
    assert row_from_spacetrack(SPACETRACK_ROWS[2]) is None
    assert row_from_spacetrack("x") is None


# --- cache freshness ---------------------------------------------------------------------


def test_cache_is_fresh():
    assert cache_is_fresh(None, NOW) is False
    assert cache_is_fresh(NOW, NOW) is True
    assert cache_is_fresh(NOW - 23.9 * HOUR, NOW) is True
    assert cache_is_fresh(NOW - 24 * HOUR, NOW) is False
    assert cache_is_fresh(NOW - 2 * HOUR, NOW, max_age_s=HOUR) is False
    # a little clock skew is tolerated, a file from the far future is not
    assert cache_is_fresh(NOW + 0.5 * HOUR, NOW) is True
    assert cache_is_fresh(NOW + 2 * HOUR, NOW) is False


def test_fresh_cache_loads_with_no_network(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    assert store.ensure_loaded(allow_network=True) == "fresh"
    assert net.calls == []
    assert len(store) == SATCAT_COUNT
    assert store.provenance() == {"source": "spacebook", "fetched_at": satcat._iso(NOW - HOUR),
                                  "age_hours": 1.0, "stale": False, "attribution": ATTRIBUTION}


def test_file_age_falls_back_to_mtime(tmp_path):
    path = write_cache(tmp_path, "satcat.json", SATCAT_ROWS, None)
    os.utime(path, (NOW - 2 * HOUR, NOW - 2 * HOUR))
    store = make_store(tmp_path)
    assert store.ensure_loaded(allow_network=True) == "fresh"
    os.utime(path, (NOW - 30 * HOUR, NOW - 30 * HOUR))
    old = make_store(tmp_path)
    assert old.ensure_loaded(allow_network=False) == "offline"
    assert len(old) == SATCAT_COUNT and old.provenance()["stale"] is True


def test_future_timestamp_counts_as_stale(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW + 5 * HOUR)
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    assert store.ensure_loaded(allow_network=True) == "downloaded"
    assert net.calls == [SATCAT_URL]


def test_download_writes_cache_and_meta(tmp_path):
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    assert store.ensure_loaded(allow_network=True) == "downloaded"
    assert len(store) == SATCAT_COUNT
    meta = json.loads((tmp_path / "satcat.meta.json").read_text(encoding="utf-8"))
    assert meta == {"source": "spacebook", "url": SATCAT_URL, "fetched_at": satcat._iso(NOW),
                    "records": SATCAT_COUNT}
    assert (tmp_path / "satcat.json").read_bytes() == as_bytes(SATCAT_ROWS)
    assert leftovers(tmp_path) == []
    # within the day nothing is fetched again
    assert store.ensure_loaded(allow_network=True) == "fresh"
    assert net.calls == [SATCAT_URL]
    # a new process the next morning reads the cache, then refreshes it once
    later = make_store(tmp_path, net, clock=Clock(NOW + 25 * HOUR))
    later.ensure_cache_loaded()
    assert len(later) == SATCAT_COUNT and net.calls == [SATCAT_URL]
    assert later.ensure_loaded(allow_network=True) == "downloaded"
    assert len(net.calls) == 2


def test_offline_with_nothing_cached_is_empty_with_a_hint(tmp_path, monkeypatch):
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    assert store.ensure_loaded(allow_network=False) == "offline"
    monkeypatch.setenv(satcat.OFFLINE_ENV, "1")
    assert satcat.network_allowed() is False
    assert store.ensure_loaded() == "offline"
    assert net.calls == [] and len(store) == 0
    status = store.status()
    assert status["available"] is False and status["count"] == 0
    assert satcat.SATCAT_DIR_ENV in status["hint"] and "refresh" in status["hint"]
    assert str(tmp_path) not in json.dumps(status)
    monkeypatch.setenv(satcat.OFFLINE_ENV, "0")
    assert satcat.network_allowed() is True


def test_failed_refresh_keeps_the_stale_copy_and_backs_off(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - 30 * HOUR)
    net = FakeNet()  # every download fails
    clock = Clock()
    store = make_store(tmp_path, net, clock=clock)
    assert store.ensure_loaded(allow_network=True) == "stale"
    assert len(store) == SATCAT_COUNT
    assert store.provenance()["stale"] is True
    assert store.status()["last_error"].startswith("Spacebook: HTTP 503")
    assert store.ensure_loaded(allow_network=True) == "backoff"
    assert len(net.calls) == 1
    clock.t += HOUR + 1  # one hour after the first failure
    assert store.ensure_loaded(allow_network=True) == "stale"
    clock.t += HOUR + 1  # the second failure waits two hours
    assert store.ensure_loaded(allow_network=True) == "backoff"
    clock.t += HOUR
    assert store.ensure_loaded(allow_network=True) == "stale"
    assert len(net.calls) == 3
    assert leftovers(tmp_path) == []
    # the wait never exceeds a day; Spacebook is back
    net.routes[SATCAT_URL] = as_bytes(SATCAT_ROWS)
    clock.t += 24 * HOUR
    assert store.ensure_loaded(allow_network=True) == "downloaded"
    assert store.provenance()["stale"] is False and store.status()["last_error"] == ""


def test_short_or_broken_reply_never_replaces_the_cache(tmp_path):
    path = write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - 30 * HOUR)
    before = path.read_bytes()
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS[:2])})
    clock = Clock()
    store = make_store(tmp_path, net, clock=clock, min_records=5)
    assert store.ensure_loaded(allow_network=True) == "stale"
    assert "2 records, fewer than 5" in store.status()["last_error"]
    assert path.read_bytes() == before and leftovers(tmp_path) == []
    net.routes[SATCAT_URL] = b"<html>busy</html>"
    clock.t += 2 * HOUR
    assert store.ensure_loaded(allow_network=True) == "stale"
    assert "did not return a JSON array" in store.status()["last_error"]
    assert path.read_bytes() == before and leftovers(tmp_path) == []


def test_unreadable_cache_is_replaced_by_a_download(tmp_path):
    (tmp_path / "satcat.json").write_text("garbage", encoding="utf-8")
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    assert store.load_cache() is False
    assert "unreadable" in store.status()["last_error"]
    assert store.ensure_loaded(allow_network=True) == "downloaded"
    assert len(store) == SATCAT_COUNT


def test_ensure_cache_loaded_reads_disk_once_and_never_the_network(tmp_path):
    net = FakeNet({SATCAT_URL: as_bytes(SATCAT_ROWS)})
    store = make_store(tmp_path, net)
    store.ensure_cache_loaded()
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW)
    store.ensure_cache_loaded()  # the request path does not re-read the disk
    assert len(store) == 0 and net.calls == []
    fresh = make_store(tmp_path, net)
    fresh.ensure_cache_loaded()
    assert len(fresh) == SATCAT_COUNT and net.calls == []


# --- Space-Track fallback ------------------------------------------------------------------


def test_space_track_fallback_when_spacebook_fails_and_nothing_is_cached(tmp_path):
    calls = []

    def rows():
        calls.append(1)
        return SPACETRACK_ROWS

    store = make_store(tmp_path, FakeNet(), spacetrack=rows, spacetrack_configured=lambda: True)
    assert store.ensure_loaded(allow_network=True) == "space-track"
    assert calls == [1] and len(store) == 2
    assert store.provenance()["source"] == "space-track"
    # cached in Spacebook's shape, so the next start reads it without either service
    cached = json.loads((tmp_path / "satcat.json").read_text(encoding="utf-8"))
    assert cached[0]["noradId"] == 25544 and cached[0]["owner"] == "CIS"
    again = make_store(tmp_path)
    assert again.load_cache() is True
    assert again.provenance()["source"] == "space-track"
    card = again.card(25544)
    assert card is not None and card["owner"] == "Russia / former USSR"


def test_space_track_is_not_asked_while_a_stale_copy_exists(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - 30 * HOUR)
    calls = []
    store = make_store(tmp_path, FakeNet(), spacetrack=lambda: calls.append(1) or [],
                       spacetrack_configured=lambda: True)
    assert store.ensure_loaded(allow_network=True) == "stale"
    assert calls == []


def test_space_track_not_configured_leaves_an_empty_store_that_says_why(tmp_path):
    calls = []
    store = make_store(tmp_path, FakeNet(), spacetrack=lambda: calls.append(1) or [],
                       spacetrack_configured=lambda: False)
    assert store.ensure_loaded(allow_network=True) == "empty"
    assert calls == []
    assert "SDA_SPACETRACK_USER" in store.status()["last_error"]


def test_space_track_failure_or_short_reply_is_empty(tmp_path):
    def fails():
        raise SatcatFetchError("Space-Track: login failed")

    store = make_store(tmp_path, FakeNet(), spacetrack=fails, spacetrack_configured=lambda: True)
    assert store.ensure_loaded(allow_network=True) == "empty"
    assert "login failed" in store.status()["last_error"]
    short = make_store(tmp_path / "short", FakeNet(), spacetrack=lambda: SPACETRACK_ROWS,
                       spacetrack_configured=lambda: True, min_records=5)
    assert short.ensure_loaded(allow_network=True) == "empty"
    assert not (tmp_path / "short" / "satcat.json").exists()


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


class _Http:
    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] = []

    def post(self, url, data=None, json=None, **kwargs):
        return _Resp(None)

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Resp(self.payload)

    def close(self):
        pass


def test_spacetrack_client_satcat_query():
    http = _Http([*SPACETRACK_ROWS, "junk"])
    client = spacetrack.SpaceTrackClient(
        user="operator", password="secret", client=http,
        throttle=spacetrack.Throttle(clock=lambda: 0.0, sleep=lambda _s: None),
    )
    assert client.satcat() == SPACETRACK_ROWS
    url = http.urls[0]
    assert "class/satcat" in url and "CURRENT/Y" in url and "format/json" in url


def test_spacetrack_rows_needs_credentials(monkeypatch):
    monkeypatch.delenv(spacetrack.USER_ENV, raising=False)
    monkeypatch.delenv(spacetrack.PASS_ENV, raising=False)
    with pytest.raises(SatcatFetchError, match="credentials"):
        satcat.spacetrack_rows()
    assert satcat._spacetrack_configured() is False


# --- details -----------------------------------------------------------------------------


def test_details_load_lazily_from_cache(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    write_cache(tmp_path, "satcat-details.json", DETAILS_ROWS, NOW - HOUR)
    net = FakeNet()
    store = make_store(tmp_path, net)
    store.ensure_loaded(allow_network=False)
    first = store.card(25544)
    assert first is not None
    assert first["details"] is None and first["details_status"] == "not requested"
    assert store.request_details() == "loading"
    store.join_details(5)
    assert store.details_status() == "loaded" and net.calls == []
    card = store.card(25544)
    assert card is not None and card["details_status"] == "loaded"
    d = card["details"]
    assert (d["mass_kg"], d["span_m"], d["size_source"]) == (450000.0, 108.0, "COMSPOC")
    assert (d["parent"], d["parent_norad_id"], d["parent_name"]) == (
        "S25545", 25545, "SL-13 R/B")
    assert (d["manufacturer"], d["bus"], d["operator"]) == ("KHRR", "77KS", "JSC")
    assert d["fetched_at"] == satcat._iso(NOW - HOUR) and "GCAT" in d["source"]
    duplex = store.card(66906)
    assert duplex is not None
    assert duplex["details"]["parent_norad_id"] is None
    assert duplex["details"]["parent_name"] == ""
    no_details = store.card(42827)
    assert no_details is not None and no_details["details"] is None
    # loaded and current: asking again starts nothing
    assert store.request_details() == "loaded"
    assert store.status()["details"]["count"] == 5


def test_details_download_when_missing(tmp_path):
    net = FakeNet({DETAILS_URL: as_bytes(DETAILS_ROWS)})
    store = make_store(tmp_path, net, details_network=True)
    assert store.ensure_details() == "downloaded"
    assert net.calls == [DETAILS_URL]
    assert (tmp_path / "satcat-details.json").exists()
    meta = json.loads((tmp_path / "satcat-details.meta.json").read_text(encoding="utf-8"))
    assert meta["records"] == 5
    assert store.details_status() == "loaded" and leftovers(tmp_path) == []
    assert store.ensure_details() == "fresh" and len(net.calls) == 1


def test_details_unavailable_offline_and_no_retry_storm(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    net = FakeNet()
    store = make_store(tmp_path, net)
    store.ensure_loaded(allow_network=False)
    assert store.request_details() == "loading"
    store.join_details(5)
    assert store.details_status() == "unavailable"
    assert store.request_details() == "unavailable"  # no second thread
    card = store.card(25544)
    assert card is not None and card["details"] is None
    assert card["details_status"] == "unavailable"
    assert net.calls == []


def test_details_failure_backs_off(tmp_path):
    net = FakeNet()
    store = make_store(tmp_path, net, details_network=True)
    assert store.ensure_details() == "empty"
    assert store.status()["details"]["last_error"].startswith("Spacebook details:")
    assert store.request_details() == "unavailable"  # backing off, no thread
    assert store.ensure_details() == "backoff"
    assert len(net.calls) == 1


def test_stale_details_are_kept_offline(tmp_path):
    write_cache(tmp_path, "satcat-details.json", DETAILS_ROWS, NOW - 30 * HOUR)
    store = make_store(tmp_path)
    assert store.ensure_details() == "offline"
    assert store.details_status() == "stale"
    assert store.get_details(25544) is not None


def test_details_switch(monkeypatch, tmp_path):
    store = SatcatStore(tmp_path, download=FakeNet(), spacetrack=None)
    assert store.status()["details"]["network"] is True
    monkeypatch.setenv(satcat.DETAILS_ENV, "0")
    assert store.status()["details"]["network"] is False
    monkeypatch.delenv(satcat.DETAILS_ENV)
    monkeypatch.setenv(satcat.OFFLINE_ENV, "true")
    assert store.status()["details"]["network"] is False


# --- card, facets, filters -------------------------------------------------------------------


@pytest.fixture
def loaded(tmp_path) -> SatcatStore:
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    write_cache(tmp_path, "satcat-details.json", DETAILS_ROWS, NOW - HOUR)
    store = make_store(tmp_path)
    assert store.ensure_loaded(allow_network=False) == "fresh"
    assert store.ensure_details() == "fresh"
    return store


def test_card_answers_whose_what_how_big(loaded):
    card = loaded.card(25544)
    assert card is not None
    assert {k: card[k] for k in (
        "norad_id", "name", "intl_designator", "owner_code", "owner", "object_type",
        "object_type_label", "status", "on_orbit", "launch_date", "launch_site_code",
        "decay_date", "rcs_size", "rcs_label", "spacebook_id", "notes")} == {
        "norad_id": 25544, "name": "ISS (ZARYA)", "intl_designator": "1998-067A",
        "owner_code": "CIS", "owner": "Russia / former USSR", "object_type": "PAYLOAD",
        "object_type_label": "Payload", "status": "On orbit", "on_orbit": True,
        "launch_date": "1998-11-20", "launch_site_code": "TTMTR", "decay_date": "",
        "rcs_size": "LARGE", "rcs_label": "Large (over 1 m2)",
        "spacebook_id": "bf72c797-cee3-45b2-8de1-5bc16ac62ea8", "notes": []}
    assert card["launch_site"] and card["launch_site"] != "TTMTR"
    assert card["summary"] == "Payload, owned by Russia / former USSR, launched 1998, on orbit."
    assert card["attribution"] == ATTRIBUTION and card["source"] == "spacebook"
    assert card["details"]["mass_kg"] == 450000.0


def test_card_notes_and_summaries(loaded):
    duplex = loaded.card(66906)
    assert duplex is not None and len(duplex["notes"]) == 1
    assert duplex["notes"][0].startswith("Released from the ISS")
    tbd = loaded.card(42827)
    assert tbd is not None
    assert tbd["summary"] == "Unknown type, launched 2017, on orbit."
    assert any("not yet assigned" in n for n in tbd["notes"])
    rb = loaded.card(25545)
    assert rb is not None and rb["status"] == "Decayed"
    assert rb["notes"] == []  # the stage that launched Zarya was never on the station
    assert rb["summary"] == ("Rocket body, owned by Russia / former USSR, launched 1998, "
                             "decayed 1998-11-27.")
    analyst = loaded.card(81008)
    assert analyst is not None
    assert (analyst["owner"], analyst["launch_date"], analyst["summary"]) == (
        "Not published", "", "Unknown type, on orbit.")
    assert loaded.card(99999) is None


def test_facets_on_orbit(loaded):
    f = loaded.facets()
    assert f["total"] == len(ON_ORBIT) and f["on_orbit_only"] is True
    # most objects first, ties by code
    assert [(o["code"], o["count"]) for o in f["owners"]] == [
        ("PRC", 3), ("", 1), ("CIS", 1), ("POR", 1), ("TBD", 1), ("US", 1)]
    assert f["owners"][0]["name"] == "China" and f["owners"][1]["name"] == "Not published"
    assert [(t["code"], t["count"]) for t in f["types"]] == [
        ("PAYLOAD", 4), ("ROCKET BODY", 0), ("DEBRIS", 2), ("UNKNOWN", 2)]
    assert [(r["code"], r["count"]) for r in f["rcs"]] == [
        ("SMALL", 1), ("MEDIUM", 4), ("LARGE", 2), ("UNKNOWN", 1)]
    assert f["decades"] == [
        {"code": "1990s", "year_from": 1990, "year_to": 1999, "count": 4},
        {"code": "2010s", "year_from": 2010, "year_to": 2019, "count": 1},
        {"code": "2020s", "year_from": 2020, "year_to": 2029, "count": 2}]
    assert (f["year_min"], f["year_max"], f["no_launch_date"]) == (1998, 2026, 1)
    assert f["attribution"] == ATTRIBUTION
    assert loaded.facets() is f  # memoized until the catalog changes


def test_facets_all_objects(loaded):
    f = loaded.facets(on_orbit=False)
    assert f["total"] == SATCAT_COUNT
    assert {t["code"]: t["count"] for t in f["types"]}["ROCKET BODY"] == 1


def test_match_ids(loaded):
    assert loaded.match_ids() == ON_ORBIT
    assert loaded.match_ids(owners=["PRC"]) == [29733, 29734, 48274]
    assert loaded.match_ids(owners=["PRC", "US"], types=["PAYLOAD"]) == [48274, 66906]
    assert loaded.match_ids(types=["DEBRIS"], rcs=["SMALL"]) == [29734]
    assert loaded.match_ids(year_from=1998, year_to=1999) == [25544, 29733, 29734, 66906]
    assert loaded.match_ids(year_from=2017) == [42827, 48274, 100001]
    assert loaded.match_ids(year_to=1998) == [25544, 66906]  # no launch date never matches
    assert loaded.match_ids(types=["ROCKET BODY"]) == []
    assert loaded.match_ids(types=["ROCKET BODY"], on_orbit=False) == [25545]
    assert loaded.match_ids(rcs=["UNKNOWN"]) == [81008]


# --- process-wide store and startup task -----------------------------------------------------


def test_satcat_dir_follows_the_camera_convention(monkeypatch, tmp_path):
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(tmp_path / "x"))
    assert satcat.satcat_dir() == tmp_path / "x"
    monkeypatch.delenv(satcat.SATCAT_DIR_ENV)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert satcat.satcat_dir() == tmp_path / "local" / "sda" / "satcat"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert satcat.satcat_dir() == tmp_path / "xdg" / "sda" / "satcat"


def test_get_store_follows_the_variable_unless_pinned(monkeypatch, tmp_path):
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(tmp_path / "a"))
    first = satcat.get_store()
    assert first.directory == tmp_path / "a" and satcat.get_store() is first
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(tmp_path / "b"))
    assert satcat.get_store().directory == tmp_path / "b"
    pinned = make_store(tmp_path / "pinned")
    satcat.set_store(pinned)
    assert satcat.get_store() is pinned


def test_refresh_forever_loads_then_sleeps(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    net = FakeNet()
    store = make_store(tmp_path, net)
    satcat.set_store(store)

    async def run() -> None:
        task = asyncio.create_task(satcat.refresh_forever(allow_network=True, interval_s=3600))
        for _ in range(500):
            if len(store):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(store) == SATCAT_COUNT and net.calls == []  # fresh cache: no download


def test_cli_status(monkeypatch, tmp_path, capsys):
    cache = tmp_path / "cli"
    monkeypatch.setenv(satcat.SATCAT_DIR_ENV, str(cache))
    assert satcat.main(["status"]) == 1
    write_cache(cache, "satcat.json", SATCAT_ROWS, NOW)
    capsys.readouterr()
    assert satcat.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    body = json.loads(out.split("\n", 1)[1])
    assert body["available"] is True and body["count"] == SATCAT_COUNT


# --- routes ------------------------------------------------------------------------------------


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client(loaded) -> TestClient:
    satcat.set_store(loaded)
    return make_client()


@pytest.fixture
def empty_client(tmp_path) -> TestClient:
    satcat.set_store(make_store(tmp_path / "empty"))
    return make_client()


def test_route_status(client, tmp_path):
    r = client.get("/api/satcat/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["count"] == SATCAT_COUNT
    assert body["on_orbit"] == len(ON_ORBIT) and body["attribution"] == ATTRIBUTION
    assert body["details"]["status"] == "loaded" and body["details"]["count"] == 5
    assert str(tmp_path) not in r.text and tmp_path.name not in r.text


def test_route_card(client):
    r = client.get("/api/satcat/25544")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "ISS (ZARYA)" and card["owner"] == "Russia / former USSR"
    assert card["attribution"] == ATTRIBUTION
    assert card["details"]["mass_kg"] == 450000.0 and card["details"]["size_source"] == "COMSPOC"
    assert card["details_status"] == "loaded"
    assert client.get("/api/satcat/025544").json()["norad_id"] == 25544
    assert client.get("/api/satcat/A0001").json()["name"] == "SARAMAGO"


@pytest.mark.parametrize(("path", "code"), [
    ("/api/satcat/99999", 404), ("/api/satcat/339999", 404),
    ("/api/satcat/abc", 422), ("/api/satcat/0", 422), ("/api/satcat/I0001", 422),
    ("/api/satcat/340000", 422),
])
def test_route_card_errors(client, path, code):
    r = client.get(path)
    assert r.status_code == code
    if code == 404:
        assert "not in the SATCAT" in r.json()["detail"]


def test_route_card_starts_the_details_load(tmp_path):
    write_cache(tmp_path, "satcat.json", SATCAT_ROWS, NOW - HOUR)
    write_cache(tmp_path, "satcat-details.json", DETAILS_ROWS, NOW - HOUR)
    store = make_store(tmp_path)
    satcat.set_store(store)
    c = make_client()
    first = c.get("/api/satcat/25544").json()
    assert first["details_status"] in ("loading", "loaded")
    store.join_details(5)
    second = c.get("/api/satcat/25544").json()
    assert second["details_status"] == "loaded" and second["details"]["span_m"] == 108.0


def test_route_facets(client):
    r = client.get("/api/satcat/facets")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(ON_ORBIT) and body["owners"][0]["code"] == "PRC"
    assert body["attribution"] == ATTRIBUTION
    every = client.get("/api/satcat/facets", params={"on_orbit": "false"}).json()
    assert every["total"] == SATCAT_COUNT


@pytest.mark.parametrize(("params", "ids"), [
    ({}, ON_ORBIT),
    ({"owner": "PRC"}, [29733, 29734, 48274]),
    ({"owner": "prc, us", "type": "PAY"}, [48274, 66906]),
    ({"type": "DEB", "rcs": "small"}, [29734]),
    ({"year_from": 1998, "year_to": 1999}, [25544, 29733, 29734, 66906]),
    ({"year_from": 2017}, [42827, 48274, 100001]),
    ({"type": "R/B"}, []),
    ({"type": "rocket body", "on_orbit": "false"}, [25545]),
])
def test_route_ids(client, params, ids):
    r = client.get("/api/satcat/ids", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ids"] == ids and body["count"] == len(ids)
    assert body["attribution"] == ATTRIBUTION


def test_route_ids_echoes_normalized_filters(client):
    body = client.get("/api/satcat/ids", params={"owner": "us,PRC,us", "type": "deb,R/B",
                                                 "rcs": "large"}).json()
    assert body["filters"] == {"owner": ["PRC", "US"], "type": ["DEBRIS", "ROCKET BODY"],
                               "rcs": ["LARGE"], "year_from": None, "year_to": None,
                               "on_orbit": True}


@pytest.mark.parametrize("params", [
    {"type": "SATELLITE"}, {"rcs": "HUGE"}, {"owner": "<b>"}, {"owner": "A" * 17},
    {"year_from": 2020, "year_to": 2010}, {"year_from": 1900}, {"year_to": 3000},
    {"owner": ",".join(["US"] * 51)},
])
def test_route_ids_rejects_bad_filters(client, params):
    assert client.get("/api/satcat/ids", params=params).status_code == 422


def test_routes_when_empty(empty_client):
    status = empty_client.get("/api/satcat/status")
    assert status.status_code == 200
    assert status.json()["available"] is False and status.json()["hint"]
    for path in ("/api/satcat/facets", "/api/satcat/ids", "/api/satcat/25544"):
        r = empty_client.get(path)
        assert r.status_code == 503, path
        detail = r.json()["detail"]
        assert detail["message"] == "SATCAT not loaded" and detail["attribution"] == ATTRIBUTION
    # input errors still come first
    assert empty_client.get("/api/satcat/ids", params={"type": "x"}).status_code == 422
