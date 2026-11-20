"""Alpha-5 catalog numbers (100,000 to 339,999) in TLE text.

The public five-digit catalog numbers ran out in July 2026. A TLE writes the larger
ones as a letter plus four digits ("A0005" is 100005). TLEStore.load_from_text used
int() on that field, which raised on the letter, and the record was dropped silently.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sgp4 import omm as sgp4_omm
from sgp4.api import WGS72, Satrec
from sgp4.exporter import export_tle

import sda.api as api
from sda.propagator import build_satrec, propagate_at
from sda.tle_store import TLEStore, decode_catalog_field


def _checksum(line: str) -> int:
    return sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line[:68]) % 10


def make_tle(field: str, name: str | None = None) -> str:
    """A LEO element set (ISS-like) with the given catalog field and valid checksums."""
    line1 = f"1 {field}U 26200A   26322.50000000  .00012516  00000-0  22596-3 0  999"
    line2 = f"2 {field}  51.6412 210.9280 0004885 231.2372 247.0342 15.5000000044001"
    lines = [f"{line1}{_checksum(line1)}", f"{line2}{_checksum(line2)}"]
    return "\n".join(([name] if name else []) + lines)


@pytest.fixture
def restore_store():
    saved = dict(api.store._catalog)
    yield
    api.store._catalog.clear()
    api.store._catalog.update(saved)


def test_a_two_line_alpha5_set_is_kept_with_an_integer_id():
    store = TLEStore()
    assert store.load_from_text(make_tle("A0005")) == 1
    record = store.get(100005)
    assert record is not None
    assert type(record.norad_id) is int
    # The fallback name used the raw field ("OBJECT A0005"); it is the number now.
    assert record.name == "OBJECT 100005"


def test_a_three_line_alpha5_set_keeps_its_name():
    store = TLEStore()
    assert store.load_from_text(make_tle("Z9999", name="NEW OBJECT")) == 1
    assert store.get(339999).name == "NEW OBJECT"


def test_five_digit_sets_load_as_before():
    store = TLEStore()
    text = "\n".join([make_tle("25544", name="ISS (ZARYA)"), make_tle("00005")])
    assert store.load_from_text(text) == 2
    assert store.get(25544).name == "ISS (ZARYA)"
    assert store.get(5).name == "OBJECT 5"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("25544", 25544),
        ("00005", 5),
        (" 5544", 5544),
        ("99999", 99999),
        ("A0000", 100000),
        ("A0005", 100005),
        ("H9999", 179999),
        ("J0000", 180000),  # I is skipped
        ("P0000", 230000),  # O is skipped
        ("Z9999", 339999),
    ],
)
def test_catalog_field_decodes(field, expected):
    assert decode_catalog_field(field) == expected


@pytest.mark.parametrize(
    "field", ["XXXXX", "I0001", "O0001", "a0005", "A005", "AB123", "", "1234X"]
)
def test_malformed_catalog_fields_are_refused(field):
    with pytest.raises(ValueError):
        decode_catalog_field(field)


def test_a_garbage_catalog_field_is_still_dropped():
    """python-sgp4 reads "XXXXX" as 310000; the int() it replaced refused it, and so
    must the store."""
    assert Satrec.twoline2rv(*make_tle("XXXXX").splitlines(), WGS72).satnum == 310000
    store = TLEStore()
    assert store.load_from_text(make_tle("XXXXX")) == 0
    assert store.count() == 0


@pytest.mark.parametrize("norad_id", [0, 5, 99999, 100000, 100005, 250000, 339999])
def test_decoding_agrees_with_what_python_sgp4_writes(norad_id):
    """Round trip through python-sgp4's own exporter, the path OMM ingest uses."""
    sat = Satrec()
    sgp4_omm.initialize(
        sat,
        {
            "OBJECT_ID": "2026-200A",
            "CLASSIFICATION_TYPE": "U",
            "EPHEMERIS_TYPE": 0,
            "ELEMENT_SET_NO": 999,
            "REV_AT_EPOCH": 1,
            "EPOCH": "2026-11-18T12:00:00.000000",
            "NORAD_CAT_ID": norad_id,
            "MEAN_MOTION": 15.5,
            "ECCENTRICITY": 0.0005,
            "INCLINATION": 51.64,
            "RA_OF_ASC_NODE": 210.9,
            "ARG_OF_PERICENTER": 231.2,
            "MEAN_ANOMALY": 247.0,
            "BSTAR": 0.0002,
            "MEAN_MOTION_DOT": 0.0001,
            "MEAN_MOTION_DDOT": 0.0,
        },
        WGS72,
    )
    line1, line2 = export_tle(sat)
    assert decode_catalog_field(line1[2:7]) == norad_id
    store = TLEStore()
    assert store.load_from_text(f"{line1}\n{line2}") == 1
    assert store.get(norad_id) is not None


def test_an_alpha5_object_propagates_in_leo():
    store = TLEStore()
    store.load_from_text(make_tle("A0005"))
    record = store.get(100005)
    state = propagate_at(build_satrec(record), record.epoch + timedelta(minutes=30))
    radius = math.dist(state.position_km, (0.0, 0.0, 0.0))
    speed = math.dist(state.velocity_km_s, (0.0, 0.0, 0.0))
    assert 6600.0 < radius < 7000.0  # ECI, km: about 400 km up
    assert 7.4 < speed < 7.9


def test_an_alpha5_object_is_served_by_the_api(restore_store):
    client = TestClient(api.app)
    body = client.post("/tle", json={"tle_text": make_tle("A0005", name="NEW OBJECT")}).json()
    assert body["ingested"] == 1

    detail = client.get("/satellites/100005")
    assert detail.status_code == 200
    assert detail.json()["norad_id"] == 100005

    rows = client.get("/api/tles").json()["satellites"]
    row = next(r for r in rows if r["norad_id"] == 100005)
    # The browser worker (satellite.js 5.0.0) reads the field as text, so the
    # alpha-5 line goes out unchanged.
    assert row["line1"].startswith("1 A0005U")
