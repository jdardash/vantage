"""OMM JSON ingest: GP records converted to TLE lines by sda.omm, then validated by
TLEStore.load_from_text (Satrec.twoline2rv) like every other element set.

Objects cataloged since July 2026 may be published only as OMM. Offline: the fetch
case below uses a fake client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sgp4 import omm as sgp4_omm
from sgp4.api import WGS72, Satrec

import sda.api as api
from sda import omm
from sda.propagator import build_satrec, datetime_to_jd
from sda.tle_store import TLEStore

OMM_ISS = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-11-18T12:00:00.123456",
    "MEAN_MOTION": 15.49584387,
    "ECCENTRICITY": 0.0004885,
    "INCLINATION": 51.6412,
    "RA_OF_ASC_NODE": 210.928,
    "ARG_OF_PERICENTER": 231.2372,
    "MEAN_ANOMALY": 247.0342,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 44001,
    "BSTAR": 0.00022596,
    "MEAN_MOTION_DOT": 0.00012516,
    "MEAN_MOTION_DDOT": 0,
}


def record(**changes) -> dict:
    return {**OMM_ISS, **changes}


def without(*keys: str) -> dict:
    return {k: v for k, v in OMM_ISS.items() if k not in keys}


@pytest.fixture
def restore_store():
    saved = dict(api.store._catalog)
    yield
    api.store._catalog.clear()
    api.store._catalog.update(saved)


# --- conversion ---


def test_a_record_becomes_a_validated_tle():
    store = TLEStore()
    assert store.load_from_omm(OMM_ISS) == 1
    tle = store.get(25544)
    assert tle.name == "ISS (ZARYA)"
    assert tle.line1.startswith("1 25544U 98067A   26322.50000143")
    assert len(tle.line1) == len(tle.line2) == 69
    expected = datetime(2026, 11, 18, 12, 0, 0, 123456, tzinfo=UTC)
    assert abs((tle.epoch - expected).total_seconds()) < 0.001


def test_converted_elements_propagate_like_the_omm_itself():
    """The TLE columns round the elements; the positions must still agree to well
    under a kilometre (WGS72, ECI) over a day."""
    direct = Satrec()
    sgp4_omm.initialize(direct, OMM_ISS, WGS72)
    store = TLEStore()
    store.load_from_omm(OMM_ISS)
    converted = build_satrec(store.get(25544))

    start = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
    for minutes in (0, 95, 1440):
        jd, fr = datetime_to_jd(start + timedelta(minutes=minutes))
        err_a, pos_a, _ = direct.sgp4(jd, fr)
        err_b, pos_b, _ = converted.sgp4(jd, fr)
        assert err_a == err_b == 0
        assert math.dist(pos_a, pos_b) < 0.1, minutes


def test_a_six_digit_record_becomes_an_alpha5_tle():
    store = TLEStore()
    assert store.load_from_omm(record(NORAD_CAT_ID=100005, OBJECT_NAME="NEW OBJECT")) == 1
    tle = store.get(100005)
    assert tle.line1[2:7] == "A0005"
    assert tle.name == "NEW OBJECT"


def test_ids_above_339999_are_skipped_counted_and_logged(caplog):
    batch = [record(NORAD_CAT_ID=339999), record(NORAD_CAT_ID=340000)]
    with caplog.at_level(logging.WARNING, logger=omm.logger.name):
        result = omm.omm_to_tle_text(batch)
    assert result.converted == 1
    assert result.skipped == {omm.SKIP_ID_TOO_LARGE: 1}
    assert "1 of 2 records converted" in caplog.text
    assert "1 catalog number above 339999" in caplog.text
    store = TLEStore()
    assert store.load_from_omm(batch) == 1
    assert store.get(339999) is not None


@pytest.mark.parametrize(
    "epoch",
    [
        "2026-11-18T12:00:00",  # no fractional seconds: sgp4.omm raises on this
        "2026-11-18T12:00:00Z",
        "2026-11-18T12:00:00.000000000",
        "2026-11-18T13:00:00+01:00",
        " 2026-11-18T12:00:00.0 ",
    ],
)
def test_epochs_are_normalised(epoch):
    assert omm.normalise_epoch(epoch) == "2026-11-18T12:00:00.000000"
    store = TLEStore()
    assert store.load_from_omm(record(EPOCH=epoch)) == 1
    assert store.get(25544).epoch == datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("epoch", ["yesterday", "", None, 26322.5, "2060-01-01T00:00:00"])
def test_an_unreadable_epoch_is_skipped(epoch):
    result = omm.omm_to_tle_text([record(EPOCH=epoch)])
    if epoch in ("", None):
        assert result.skipped == {omm.SKIP_MISSING_FIELD: 1}
    else:
        assert result.skipped == {omm.SKIP_BAD_EPOCH: 1}
    assert result.text == ""


def test_space_track_style_string_values_are_read():
    as_text = {key: str(value) for key, value in OMM_ISS.items()}
    as_text.update(
        CENTER_NAME="EARTH", REF_FRAME="TEME", TIME_SYSTEM="UTC", MEAN_ELEMENT_THEORY="SGP4"
    )
    store = TLEStore()
    assert store.load_from_omm(as_text) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"EPHEMERIS_TYPE": 4},  # SGP4-XP elements: SGP4 would propagate them wrongly
        {"REF_FRAME": "GCRF"},
        {"CENTER_NAME": "MOON"},
        {"MEAN_ELEMENT_THEORY": "DSST"},
    ],
)
def test_elements_that_are_not_sgp4_mean_elements_are_skipped(changes):
    result = omm.omm_to_tle_text([record(**changes)])
    assert result.skipped == {omm.SKIP_NOT_SGP4: 1}


def test_missing_drag_terms_default_to_zero():
    store = TLEStore()
    assert store.load_from_omm(without("BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT")) == 1
    sat = build_satrec(store.get(25544))
    assert sat.bstar == 0.0


@pytest.mark.parametrize("key", omm.REQUIRED_FIELDS)
def test_a_record_missing_a_required_field_is_skipped(key):
    assert omm.omm_to_tle_text([without(key)]).skipped == {omm.SKIP_MISSING_FIELD: 1}


@pytest.mark.parametrize(
    "changes",
    [
        {"ECCENTRICITY": 1.2},
        {"INCLINATION": 190.0},
        {"MEAN_MOTION": 0.0},
        {"RA_OF_ASC_NODE": 400.0},
        {"NORAD_CAT_ID": -1},
        {"NORAD_CAT_ID": 25544.5},
        {"NORAD_CAT_ID": True},
        {"MEAN_MOTION": "fast"},
        {"MEAN_MOTION": float("nan")},
        {"CLASSIFICATION_TYPE": "XX"},
        {"BSTAR": 1e12},  # too wide for its column: the line would be misread
    ],
)
def test_values_a_tle_cannot_carry_are_skipped(changes):
    assert omm.omm_to_tle_text([record(**changes)]).skipped == {omm.SKIP_BAD_VALUE: 1}


def test_element_set_and_revolution_numbers_wrap_like_published_tles():
    store = TLEStore()
    assert store.load_from_omm(record(ELEMENT_SET_NO=12345, REV_AT_EPOCH=123456)) == 1
    tle = store.get(25544)
    assert len(tle.line1) == len(tle.line2) == 69
    assert tle.line1[64:68] == "2345"
    assert tle.line2[63:68] == "23456"


def test_an_unusual_object_id_leaves_the_designator_blank():
    store = TLEStore()
    assert store.load_from_omm(record(OBJECT_ID="UNKNOWN-OBJECT-ID")) == 1
    assert store.get(25544).line1[9:17] == " " * 8


def test_a_record_with_no_name_is_named_from_its_number():
    store = TLEStore()
    store.load_from_omm(without("OBJECT_NAME"))
    assert store.get(25544).name == "OBJECT 25544"


def test_a_name_that_looks_like_json_does_not_confuse_the_store():
    """The converted text goes back through load_from_text, which sniffs for JSON."""
    store = TLEStore()
    assert store.load_from_omm(record(OBJECT_NAME='["TEST"] SAT')) == 1
    assert store.get(25544).name == '["TEST"] SAT'



def test_epochs_later_than_not_after_are_skipped_and_counted():
    """Spacebook dates some deep-space sets days ahead; sda.api passes a cutoff."""
    cutoff = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    records = [
        record(EPOCH="2026-11-19T11:59:59"),
        record(NORAD_CAT_ID=25545, EPOCH="2026-11-19T12:00:01.5"),
    ]
    conversion = omm.omm_to_tle_text(records, not_after=cutoff)
    assert conversion.converted == 1
    assert conversion.skipped == {omm.SKIP_FUTURE_EPOCH: 1}
    assert "\n1 25544U" in conversion.text
    # A naive cutoff is read as UTC; an aware one in another zone is converted.
    assert omm.omm_to_tle_text(records, not_after=cutoff.replace(tzinfo=None)).converted == 1
    pacific = cutoff.astimezone(timezone(timedelta(hours=-8)))
    assert omm.omm_to_tle_text(records, not_after=pacific).converted == 1
    assert omm.omm_to_tle_text(records).converted == 2


def test_skips_can_be_left_to_the_caller_to_log(caplog):
    with caplog.at_level(logging.WARNING, logger=omm.logger.name):
        conversion = omm.omm_to_tle_text([record(NORAD_CAT_ID=400000)], log_skips=False)
    assert conversion.skipped == {omm.SKIP_ID_TOO_LARGE: 1}
    assert not caplog.records

# --- entry points ---


def test_load_from_text_accepts_an_omm_json_body():
    store = TLEStore()
    body = json.dumps([record(), record(NORAD_CAT_ID=100005)])
    assert store.load_from_text(body) == 2
    assert store.load_from_text("﻿" + json.dumps(record(NORAD_CAT_ID=5))) == 1
    assert {25544, 100005, 5} <= {tle.norad_id for tle in store.get_all()}


@pytest.mark.parametrize(
    "body",
    [
        "ISS (ZARYA)\n1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
        "<html><body>No GP data found</body></html>",
        '"just a string"',
        "[not json",
        "No GP data found",
    ],
)
def test_bodies_that_are_not_omm_json_are_not_taken_for_it(body):
    assert omm.parse_omm_json(body) is None
    assert omm.omm_to_tle_text(body).records == 0


def test_an_html_error_page_ingests_nothing():
    assert TLEStore().load_from_text("<html><body>Error</body></html>") == 0
    assert TLEStore().load_from_text("[not json") == 0
    assert TLEStore().load_from_text("[]") == 0


def test_the_ingest_route_accepts_omm_json(restore_store):
    client = TestClient(api.app)
    body = client.post(
        "/tle", json={"tle_text": json.dumps([record(NORAD_CAT_ID=100007)])}
    ).json()
    assert body["ingested"] == 1
    assert client.get("/satellites/100007").json()["norad_id"] == 100007


# --- the CelesTrak fetch asks for OMM JSON and caches TLE text ---


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def fake_client(handler):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return handler(url)

    return _FakeAsyncClient


def test_celestrak_json_is_loaded_and_cached_as_tle_text(tmp_path, monkeypatch, restore_store):
    monkeypatch.setenv("SDA_TLE_CACHE_DIR", str(tmp_path))
    api.store._catalog.clear()
    seen: list[str] = []
    payload = json.dumps(
        [record(), record(NORAD_CAT_ID=100005, OBJECT_NAME="NEW"), record(NORAD_CAT_ID=400000)]
    )

    def handler(url):
        seen.append(url)
        return FakeResponse(200, payload)

    count = asyncio.run(api._fetch_celestrak_group(fake_client(handler)(), "stations"))

    assert count == 2
    assert seen and "FORMAT=json" in seen[0]
    cached = (tmp_path / "stations.tle").read_text(encoding="utf-8")
    assert not cached.lstrip().startswith("[")
    assert "\n1 A0005U " in cached
    taken_at, held = api.last_group_download("stations")
    assert held == cached

    # A restart reads the cache back through the store: same objects, same ids.
    api.store._catalog.clear()
    assert api._load_tle_cache() == (1, 2)
    assert api.store.get(100005).name == "NEW"
    assert api.catalog_source()["source"] == "cache"


def test_a_tle_reply_is_still_read_as_it_is(tmp_path, monkeypatch, restore_store):
    monkeypatch.setenv("SDA_TLE_CACHE_DIR", str(tmp_path))
    api.store._catalog.clear()
    tle = TLEStore()
    tle.load_from_omm(OMM_ISS)
    text = f"ISS (ZARYA)\n{tle.get(25544).line1}\n{tle.get(25544).line2}"

    client = fake_client(lambda url: FakeResponse(200, text))()
    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 1
    assert (tmp_path / "stations.tle").read_text(encoding="utf-8") == text
