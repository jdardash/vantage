"""The keyless catalog fallbacks for when CelesTrak does not answer.

From this machine CelesTrak's TCP connect times out for every group, the disk cache
was weeks old, and the globe fell back to the 38 bundled satellites. SatNOGS DB
serves one keyless JSON list of current TLEs, and COMSPOC Spacebook one keyless OMM
JSON catalog. They are asked in that order only when CelesTrak has delivered nothing
for a whole repeat window, each at most once per two hours (across a restart too when
the cache directory is on), never under SDA_OFFLINE, and every element set still goes
through TLEStore.load_from_text, labelled with where it came from. All offline: fake
clients only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import sda.api as api
from sda.tle_store import TLEStore

CACHE_ENV = "SDA_TLE_CACHE_DIR"


def _checksum(line: str) -> int:
    return sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line[:68]) % 10


def epoch_days_ago(days: float) -> str:
    """TLE epoch field (YYDDD.DDDDDDDD) for a moment *days* before now. The fallback
    leaves out sets older than 14 days, so fixed epochs would age out of these tests."""
    when = datetime.now(UTC) - timedelta(days=days)
    day_of_year = (when - datetime(when.year, 1, 1, tzinfo=UTC)).total_seconds() / 86400.0
    return f"{when.year % 100:02d}{day_of_year + 1.0:012.8f}"


def tle_lines(field: str, epoch: str | None = None) -> tuple[str, str]:
    epoch = epoch or epoch_days_ago(1.0)
    line1 = f"1 {field}U 26200A   {epoch}  .00012516  00000-0  22596-3 0  999"
    line2 = f"2 {field}  51.6412 210.9280 0004885 231.2372 247.0342 15.5000000044001"
    return f"{line1}{_checksum(line1)}", f"{line2}{_checksum(line2)}"


def satnogs_row(field: str, tle0: str | None, norad_id: int, days_old: float = 1.0) -> dict:
    line1, line2 = tle_lines(field, epoch_days_ago(days_old))
    return {
        "tle0": tle0,
        "tle1": line1,
        "tle2": line2,
        "tle_source": "Space-Track.org (catalog)",
        "norad_cat_id": norad_id,
        "updated": "2026-11-18T12:00:00.000000+0000",
    }


ROWS = [
    satnogs_row("25544", "0 ISS (ZARYA)", 25544),
    satnogs_row("43017", "AO-91", 43017),
    satnogs_row("A0005", "", 100005),
    # Refused by the store's validation (rule 4), not by the fallback.
    {"tle0": "0 BROKEN", "tle1": "1 XXXXXU garbage", "tle2": "2 XXXXX garbage", "norad_cat_id": 1},
    {"tle0": "0 NO LINES", "tle1": None, "tle2": None, "norad_cat_id": 2},
]
SATNOGS_BODY = json.dumps(ROWS)


def omm_epoch(days_old: float) -> str:
    """OMM EPOCH *days_old* before now (negative: ahead of now), as Spacebook writes it."""
    return (datetime.now(UTC) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%S.%f")


def spacebook_record(norad_id: int, name: str, days_old: float = 0.5, **changes) -> dict:
    """One record with the keys and value types Spacebook served on 2026-11-19."""
    record = {
        "CCSDS_OMM_VERS": "3.0",
        "COMMENT": "",
        "CREATION_DATE": "2026-11-19T22:23:45",
        "ORIGINATOR": "COMSPOC",
        "OBJECT_NAME": name,
        "OBJECT_ID": "1998-067A",
        "CENTER_NAME": "EARTH",
        "REF_FRAME": "TEME",
        "TIME_SYSTEM": "UTC",
        "MEAN_ELEMENT_THEORY": "SGP4",
        "EPOCH": omm_epoch(days_old),
        "MEAN_MOTION": 15.49584387,
        "ECCENTRICITY": 0.0004885,
        "INCLINATION": 51.6412,
        "RA_OF_ASC_NODE": 210.928,
        "ARG_OF_PERICENTER": 231.2372,
        "MEAN_ANOMALY": 247.0342,
        "EPHEMERIS_TYPE": "0",
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": norad_id,
        "ELEMENT_SET_NO": "999",
        "REV_AT_EPOCH": 44001,
        "BSTAR": 0.00022596,
        "MEAN_MOTION_DOT": 0.00012516,
        "MEAN_MOTION_DDOT": 0,
        "OBJECT_TYPE": "PAYLOAD",
    }
    return {**record, **changes}


SPACEBOOK_RECORDS = [
    spacebook_record(25544, "ISS (ZARYA)", days_old=0.5),  # newer than SatNOGS's (1 day)
    spacebook_record(270449, "SIX DIGITS", days_old=1.0, OBJECT_ID="2026-200A"),
    spacebook_record(836, "OPS 3662 (VELA 3)", days_old=-3.0),  # three days ahead
    spacebook_record(3956, "TITAN 3C TRANSTAGE R/B", days_old=-0.5),  # twelve hours ahead
    spacebook_record(7530, "OSCAR 7", days_old=20.0),  # past the EXPIRED line
    spacebook_record(400000, "TOO BIG FOR A TLE", days_old=1.0),
]
SPACEBOOK_BODY = json.dumps(SPACEBOOK_RECORDS)
SPACEBOOK_LEFT_OUT = {
    "EPOCH too far in the future": 1,
    "older than 14 days": 1,
    "catalog number above 339999": 1,
}
SAMPLE_TLE = "ISS (ZARYA)\n" + "\n".join(tle_lines("25544"))


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class Network:
    """Fake httpx.AsyncClient factory: CelesTrak, SatNOGS and Spacebook answer as told."""

    def __init__(self, celestrak=None, satnogs=None, spacebook=None, delay_s: float = 0.0):
        self.celestrak = celestrak  # None: connect timeout
        self.satnogs = satnogs if satnogs is not None else FakeResponse(200, SATNOGS_BODY)
        self.spacebook = spacebook  # None: connect timeout
        self.delay_s = delay_s
        self.seen: list[str] = []

    @property
    def satnogs_requests(self) -> int:
        return sum(url == api.SATNOGS_TLE_URL for url in self.seen)

    @property
    def spacebook_requests(self) -> int:
        return sum(url == api.SPACEBOOK_OMM_URL for url in self.seen)

    @property
    def celestrak_requests(self) -> int:
        return sum("celestrak.org" in url for url in self.seen)

    def client_class(self):
        network = self

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                network.seen.append(url)
                if network.delay_s:
                    await asyncio.sleep(network.delay_s)
                reply = {
                    api.SATNOGS_TLE_URL: network.satnogs,
                    api.SPACEBOOK_OMM_URL: network.spacebook,
                }.get(url, network.celestrak)
                if reply is None:
                    raise httpx.ConnectTimeout("timed out")
                if isinstance(reply, Exception):
                    raise reply
                return reply

        return _FakeAsyncClient


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    saved_catalog = dict(api.store._catalog)
    saved_sources = dict(api.store._sources)
    saved_refresh = api._last_tle_refresh
    monkeypatch.delenv("SDA_OFFLINE", raising=False)
    # The SatNOGS cases see SatNOGS alone; the Spacebook cases set their own list.
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "satnogs")
    api.store._catalog.clear()
    api.store._sources.clear()
    yield
    api.store._catalog.clear()
    api.store._catalog.update(saved_catalog)
    api.store._sources.clear()
    api.store._sources.update(saved_sources)
    api._last_tle_refresh = saved_refresh


def install(monkeypatch, network: Network) -> Network:
    monkeypatch.setattr(api.httpx, "AsyncClient", network.client_class())
    return network


def seed() -> int:
    return asyncio.run(api._seed_catalog())


def age(path, hours: float) -> None:
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))


# --- the fallback fills the catalog ---


def test_when_celestrak_does_not_answer_satnogs_fills_the_catalog(monkeypatch, caplog):
    network = install(monkeypatch, Network())
    api._last_tle_refresh = None

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        seed()

    assert network.celestrak_requests == len(api.CELESTRAK_GROUPS)
    assert network.satnogs_requests == 1
    assert api.store.get(25544).name == "ISS (ZARYA)"  # the "0 " is taken off
    assert api.store.get(43017).name == "AO-91"
    assert api.store.get(100005).name == "OBJECT 100005"  # alpha-5, no name line
    assert api.store.get(310000) is None  # garbage row refused by load_from_text
    assert api.store.count() > 3  # bundled objects SatNOGS lacks are still filled in
    assert (datetime.now(UTC) - api._last_tle_refresh).total_seconds() < 60

    source = api.catalog_source()
    assert source["source"] == "SatNOGS DB"
    assert source["objects"] == 3
    assert source["url"] == api.SATNOGS_TLE_URL
    assert source["element_sets"]["satnogs"] == 3
    assert api.store.source_of(100005) == "satnogs"
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("SatNOGS DB: 3 objects") for line in lines), lines


def test_satnogs_rows_are_read_as_validated_sets():
    read = api._read_satnogs(SATNOGS_BODY)
    assert read.left_out == {}
    names = {record.norad_id: record.name for record in read.sets}
    assert names == {25544: "ISS (ZARYA)", 43017: "AO-91", 100005: "OBJECT 100005"}
    assert next(r for r in read.sets if r.norad_id == 100005).line1.startswith("1 A0005U")
    for body in ("<html>error</html>", "{}", '{"detail": "throttled"}', "[]", ""):
        assert api._read_satnogs(body) == ([], {})


def test_the_satnogs_cache_file_is_three_line_tle_text(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    install(monkeypatch, Network())
    seed()
    text = (tmp_path / "satnogs-db.tle").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "ISS (ZARYA)"
    assert "AO-91\n1 43017U" in text
    assert "OBJECT 100005\n1 A0005U" in text and "\n\n" not in text
    assert "NO LINES" not in text and "BROKEN" not in text


@pytest.mark.parametrize("stale_first", [True, False])
def test_the_newest_of_rows_sharing_a_catalog_number_is_kept(monkeypatch, stale_first):
    """SatNOGS listed the ISS twice on 2026-11-19: one set 0.7 days old and one 1,413
    days old. A plain load keeps whichever row comes last."""
    current = satnogs_row("25544", "0 ISS (ZARYA)", 25544, days_old=0.7)
    ancient = satnogs_row("25544", "0 ISS (ZARYA)", 25544, days_old=1413.0)
    rows = [ancient, current] if stale_first else [current, ancient]
    install(monkeypatch, Network(satnogs=FakeResponse(200, json.dumps(rows))))

    seed()

    age_days = (datetime.now(UTC) - api.store.get(25544).epoch).total_seconds() / 86400.0
    assert 0.6 < age_days < 0.8


def test_element_sets_past_the_expired_line_are_left_out(monkeypatch, caplog):
    """Decayed objects keep their last set in SatNOGS, some of them decades old."""
    rows = [
        satnogs_row("43017", "0 AO-91", 43017, days_old=13.0),
        satnogs_row("07530", "0 OSCAR 7", 7530, days_old=20.0),
        satnogs_row("12345", "0 LONG GONE", 12345, days_old=18000.0),
    ]
    install(monkeypatch, Network(satnogs=FakeResponse(200, json.dumps(rows))))

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        seed()

    assert api.store.get(43017) is not None
    assert api.store.get(7530) is None
    assert api.store.get(12345) is None
    assert api.catalog_source()["left_out"] == {"older than 14 days": 2}
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        "SatNOGS DB: 1 objects" in line and "2 older than 14 days" in line for line in lines
    ), lines


def test_a_reply_of_only_expired_sets_caches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    rows = [satnogs_row("07530", "0 OSCAR 7", 7530, days_old=20.0)]
    install(monkeypatch, Network(satnogs=FakeResponse(200, json.dumps(rows))))
    seed()
    assert not (tmp_path / "satnogs-db.tle").exists()
    assert api.store.get(7530) is None


# --- the two-hour limit ---


def test_satnogs_is_asked_at_most_once_per_window(monkeypatch):
    network = install(monkeypatch, Network())
    seed()
    seed()
    assert network.satnogs_requests == 1
    assert api.SATNOGS_REPEAT_WINDOW_S == api.CELESTRAK_REPEAT_WINDOW_S == 7200.0


def test_a_request_that_got_no_reply_still_counts(monkeypatch):
    """At most one request per two hours, answered or not, so an outage at SatNOGS
    is not asked on every refresh."""
    network = install(monkeypatch, Network(satnogs=httpx.ConnectError("no route")))
    seed()
    seed()
    assert network.satnogs_requests == 1


def test_overlapping_refreshes_ask_satnogs_once(monkeypatch):
    network = install(monkeypatch, Network(delay_s=0.01))

    async def three_at_once():
        await asyncio.gather(*(api._seed_catalog() for _ in range(3)))

    asyncio.run(three_at_once())
    assert network.satnogs_requests == 1


def test_a_restart_inside_the_window_loads_the_cache_and_does_not_ask(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    network = install(monkeypatch, Network())
    seed()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["satnogs-db.asked", "satnogs-db.tle"]

    api._forget_celestrak()  # a restart: only the cache directory is left
    api.store._catalog.clear()
    api._last_tle_refresh = None
    assert api._load_tle_cache() == (1, 3)
    assert api.store.get(100005) is not None
    source = api.catalog_source()
    assert (source["source"], source["files"]) == ("cache", ["satnogs-db.tle"])

    seed()
    assert network.satnogs_requests == 1
    # Nothing new arrived, so the catalog is as old as the SatNOGS download.
    assert (datetime.now(UTC) - api._last_tle_refresh).total_seconds() < 60


@pytest.mark.parametrize("marker", ["satnogs-db.asked", "satnogs-db.tle"])
def test_satnogs_is_asked_again_once_the_window_has_passed(tmp_path, monkeypatch, marker):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    (tmp_path / marker).write_text("", encoding="utf-8")
    age(tmp_path / marker, hours=api.SATNOGS_REPEAT_WINDOW_S / 3600.0 + 0.05)
    network = install(monkeypatch, Network())
    seed()
    assert network.satnogs_requests == 1


def test_a_recent_marker_rests_satnogs_across_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    (tmp_path / "satnogs-db.asked").write_text("", encoding="utf-8")
    age(tmp_path / "satnogs-db.asked", hours=1.0)
    network = install(monkeypatch, Network())
    seed()
    assert network.satnogs_requests == 0


# --- when it is not asked ---


@pytest.mark.parametrize("value", ["1", "true"])
def test_offline_never_asks_satnogs(monkeypatch, value):
    monkeypatch.setenv("SDA_OFFLINE", value)
    network = install(monkeypatch, Network())
    seed()
    assert asyncio.run(api._fetch_fallback(api.SATNOGS)) == 0
    assert network.satnogs_requests == 0


def test_satnogs_is_not_asked_while_celestrak_serves(monkeypatch):
    network = install(monkeypatch, Network(celestrak=FakeResponse(200, SAMPLE_TLE)))
    seed()
    assert network.satnogs_requests == 0
    assert api.catalog_source()["source"] == "CelesTrak"
    assert api.store.source_of(25544) == "celestrak"


def test_satnogs_is_not_asked_when_celestrak_delivered_inside_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    for group in api.CELESTRAK_GROUPS:
        (tmp_path / f"{group}.tle").write_text(SAMPLE_TLE, encoding="utf-8")
        age(tmp_path / f"{group}.tle", hours=1.0)
    network = install(monkeypatch, Network())
    seed()
    assert network.seen == []


def test_satnogs_is_asked_when_celestrak_last_delivered_before_the_window(tmp_path, monkeypatch):
    """Groups downloaded three hours ago are due again; when every one of them now
    times out, CelesTrak has delivered nothing for a whole window."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    for group in api.CELESTRAK_GROUPS:
        (tmp_path / f"{group}.tle").write_text(SAMPLE_TLE, encoding="utf-8")
        age(tmp_path / f"{group}.tle", hours=3.0)
    network = install(monkeypatch, Network())
    seed()
    assert network.celestrak_requests == len(api.CELESTRAK_GROUPS)
    assert network.satnogs_requests == 1


# --- what a bad or older reply does ---


@pytest.mark.parametrize(
    ("status", "body"),
    [(403, "Forbidden"), (200, "<html>maintenance</html>"), (200, "{}"), (200, "[]")],
)
def test_a_reply_without_element_sets_loads_and_caches_nothing(
    tmp_path, monkeypatch, caplog, status, body
):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    install(monkeypatch, Network(satnogs=FakeResponse(status, body)))
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    api._last_tle_refresh = earlier

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        seed()

    assert not (tmp_path / "satnogs-db.tle").exists()
    assert (tmp_path / "satnogs-db.asked").exists()
    assert api._last_tle_refresh == earlier
    assert api.catalog_source()["source"] != "SatNOGS DB"
    lines = [r.getMessage() for r in caplog.records]
    assert any(f"SatNOGS DB: gave no element sets (HTTP {status}" in line for line in lines)


def test_satnogs_never_replaces_newer_elements(monkeypatch):
    newer = "ISS (ZARYA)\n" + "\n".join(tle_lines("25544", epoch="30045.51782528"))
    api.store.load_from_text(newer)
    newer_epoch = api.store.get(25544).epoch
    install(monkeypatch, Network())

    seed()

    assert api.store.get(25544).epoch == newer_epoch
    source = api.catalog_source()
    assert (source["objects"], source["applied"]) == (3, 2)


def test_catalog_source_is_a_copy():
    api._set_catalog_source("cache", time.time(), files=["a.tle"], objects=1)
    source = api.catalog_source()
    source["files"].append("b.tle")
    source["source"] = "changed"
    assert api.catalog_source()["files"] == ["a.tle"]
    assert api.catalog_source()["source"] == "cache"


# --- COMSPOC Spacebook, the second fallback ---


def test_spacebook_is_asked_after_satnogs_and_fills_in_the_rest(monkeypatch, caplog):
    monkeypatch.delenv(api.CATALOG_FALLBACKS_ENV)  # the default: both, SatNOGS first
    network = install(monkeypatch, Network(spacebook=FakeResponse(200, SPACEBOOK_BODY)))
    api._last_tle_refresh = None

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        seed()

    asked = [url for url in network.seen if "celestrak.org" not in url]
    assert asked == [api.SATNOGS_TLE_URL, api.SPACEBOOK_OMM_URL]
    # Six-digit objects arrive through Spacebook, written as alpha-5 TLEs.
    assert api.store.get(270449).line1.startswith("1 T0449U 26200A ")
    assert api.store.source_of(270449) == "spacebook"
    assert api.store.source_of(43017) == "satnogs"
    # Both carry the ISS. Spacebook's set is newer (half a day against one), so it wins.
    assert api.store.source_of(25544) == "spacebook"
    assert (datetime.now(UTC) - api._last_tle_refresh).total_seconds() < 60

    source = api.catalog_source()
    assert source["source"] == "COMSPOC Spacebook"
    assert (source["objects"], source["applied"]) == (3, 3)
    assert source["url"] == api.SPACEBOOK_OMM_URL
    assert source["left_out"] == SPACEBOOK_LEFT_OUT
    counts = source["element_sets"]
    assert (counts["spacebook"], counts["satnogs"]) == (3, 2)
    assert counts["bundled"] == api.store.count() - 5

    lines = [r.getMessage() for r in caplog.records]
    spacebook_lines = [line for line in lines if line.startswith("COMSPOC Spacebook")]
    assert len(spacebook_lines) == 1, lines
    assert spacebook_lines[0].startswith("COMSPOC Spacebook: 3 objects (3 newer")
    assert "1 EPOCH too far in the future" in spacebook_lines[0]
    assert not any(line.startswith("OMM:") for line in lines)  # one line, not two


def test_spacebook_leaves_out_epochs_more_than_a_day_ahead():
    read = api._read_spacebook(SPACEBOOK_BODY)
    assert sorted(record.norad_id for record in read.sets) == [3956, 25544, 270449]
    assert read.left_out == SPACEBOOK_LEFT_OUT
    edge = [
        spacebook_record(836, "VELA 3", days_old=-1.05),
        spacebook_record(837, "VELA 4", days_old=-0.95),
    ]
    read = api._read_spacebook(json.dumps(edge))
    assert [record.norad_id for record in read.sets] == [837]
    assert api.SPACEBOOK_MAX_EPOCH_AHEAD_HOURS == 24.0


def test_spacebook_is_cached_as_tle_text_and_reloads_with_its_label(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    monkeypatch.delenv(api.CATALOG_FALLBACKS_ENV)
    network = install(monkeypatch, Network(spacebook=FakeResponse(200, SPACEBOOK_BODY)))
    seed()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "satnogs-db.asked", "satnogs-db.tle", "spacebook.asked", "spacebook.tle",
    ]
    text = (tmp_path / "spacebook.tle").read_text(encoding="utf-8")
    assert text.startswith("ISS (ZARYA)\n1 25544U")
    assert "SIX DIGITS\n1 T0449U" in text
    assert "VELA" not in text and "OSCAR 7" not in text

    api._forget_celestrak()  # a restart: only the cache directory is left
    api.store._catalog.clear()
    api.store._sources.clear()
    assert api._load_tle_cache() == (2, 6)  # SatNOGS 3, then Spacebook 3 (one newer ISS)
    assert api.store.source_of(270449) == "spacebook"
    assert api.store.source_of(25544) == "spacebook"
    assert api.store.source_of(100005) == "satnogs"
    source = api.catalog_source()
    assert source["files"] == ["satnogs-db.tle", "spacebook.tle"]

    seed()  # inside both windows: neither is asked again
    assert (network.satnogs_requests, network.spacebook_requests) == (1, 1)


def test_spacebook_is_asked_at_most_once_per_window(monkeypatch):
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "spacebook")
    network = install(monkeypatch, Network(spacebook=httpx.ReadTimeout("30 MB, slowly")))
    seed()
    seed()
    assert network.spacebook_requests == 1
    assert api.SPACEBOOK_REPEAT_WINDOW_S == 7200.0


@pytest.mark.parametrize(("hours", "requests"), [(1.0, 0), (2.05, 1)])
def test_the_spacebook_marker_carries_its_window_across_a_restart(
    tmp_path, monkeypatch, hours, requests
):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "spacebook")
    (tmp_path / "spacebook.asked").write_text("", encoding="utf-8")
    age(tmp_path / "spacebook.asked", hours=hours)
    network = install(monkeypatch, Network(spacebook=FakeResponse(200, SPACEBOOK_BODY)))
    seed()
    assert network.spacebook_requests == requests


@pytest.mark.parametrize("value", ["1", "true"])
def test_offline_never_asks_spacebook(monkeypatch, value):
    monkeypatch.setenv("SDA_OFFLINE", value)
    monkeypatch.delenv(api.CATALOG_FALLBACKS_ENV)
    network = install(monkeypatch, Network(spacebook=FakeResponse(200, SPACEBOOK_BODY)))
    seed()
    assert asyncio.run(api._fetch_fallback(api.SPACEBOOK)) == 0
    assert network.spacebook_requests == 0


def test_spacebook_is_not_asked_while_celestrak_serves(monkeypatch):
    monkeypatch.delenv(api.CATALOG_FALLBACKS_ENV)
    network = install(
        monkeypatch,
        Network(
            celestrak=FakeResponse(200, SAMPLE_TLE),
            spacebook=FakeResponse(200, SPACEBOOK_BODY),
        ),
    )
    seed()
    assert (network.satnogs_requests, network.spacebook_requests) == (0, 0)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, "Forbidden"),
        (200, "<!doctype html><html><body>app shell</body></html>"),
        (200, '{"error": "busy"}'),
        (200, "[]"),
    ],
)
def test_a_spacebook_reply_without_element_sets_loads_and_caches_nothing(
    tmp_path, monkeypatch, caplog, status, body
):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "spacebook")
    install(monkeypatch, Network(spacebook=FakeResponse(status, body)))

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        seed()

    assert not (tmp_path / "spacebook.tle").exists()
    assert (tmp_path / "spacebook.asked").exists()
    assert api.catalog_source()["source"] != "COMSPOC Spacebook"
    lines = [r.getMessage() for r in caplog.records]
    assert any(
        f"COMSPOC Spacebook: gave no element sets (HTTP {status}" in line for line in lines
    ), lines


# --- which fallbacks, in which order ---


def test_by_default_both_fallbacks_are_asked_satnogs_first(monkeypatch):
    monkeypatch.delenv(api.CATALOG_FALLBACKS_ENV)
    assert api._catalog_fallbacks() == [api.SATNOGS, api.SPACEBOOK]


@pytest.mark.parametrize(
    ("setting", "asked"),
    [
        ("spacebook", ["spacebook"]),
        ("spacebook,satnogs", ["spacebook", "satnogs"]),
        (" SatNOGS , spacebook ", ["satnogs", "spacebook"]),
        ("satnogs,satnogs", ["satnogs"]),
        ("none", []),
        ("", []),
    ],
)
def test_sda_catalog_fallbacks_picks_the_fallbacks_and_their_order(monkeypatch, setting, asked):
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, setting)
    network = install(monkeypatch, Network())
    seed()
    names = {api.SATNOGS_TLE_URL: "satnogs", api.SPACEBOOK_OMM_URL: "spacebook"}
    assert [names[url] for url in network.seen if url in names] == asked


def test_an_unknown_fallback_name_is_logged_and_ignored(monkeypatch, caplog):
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "spacetrack,satnogs")
    with caplog.at_level(logging.WARNING, logger=api.logger.name):
        chosen = api._catalog_fallbacks()
    assert chosen == [api.SATNOGS]
    assert any("'spacetrack' is not a catalog fallback" in r.getMessage() for r in caplog.records)


# --- provenance per element set ---


def test_each_element_set_carries_where_it_came_from():
    store = TLEStore()
    iss = "ISS (ZARYA)\n" + "\n".join(tle_lines("25544"))
    ao91 = "AO-91\n" + "\n".join(tle_lines("43017"))
    assert store.load_from_text(iss, source="celestrak") == 1
    assert store.load_from_text(ao91, source="satnogs") == 1
    assert store.source_of(25544) == "celestrak"
    assert store.source_counts() == {"celestrak": 1, "satnogs": 1}
    store.load_from_text(iss)  # a set with no source (POST /tle) drops the old label
    assert store.source_of(25544) is None
    assert store.source_counts() == {"satnogs": 1}
    assert store.remove(43017)
    assert store.source_of(43017) is None
    assert store.source_counts() == {}
    assert store.source_of(99999) is None


def test_omm_ingest_carries_its_source_too():
    store = TLEStore()
    six_digits = spacebook_record(270449, "SIX DIGITS", OBJECT_ID="2026-200A")
    assert store.load_from_omm([six_digits], source="spacebook") == 1
    assert store.load_from_text(json.dumps(SPACEBOOK_RECORDS[0]), source="spacebook") == 1
    assert store.source_counts() == {"spacebook": 2}


def test_a_label_never_outlives_its_set():
    store = TLEStore()
    store.load_from_text("ISS (ZARYA)\n" + "\n".join(tle_lines("25544")), source="spacebook")
    store._catalog.clear()  # as some tests elsewhere empty the catalog
    assert store.source_of(25544) is None
    assert store.source_counts() == {}


def test_bundled_sets_are_labelled_when_nothing_else_answers(monkeypatch):
    monkeypatch.setenv(api.CATALOG_FALLBACKS_ENV, "none")
    install(monkeypatch, Network())
    seed()
    assert api.store.count() > 30
    assert api.store.source_counts() == {"bundled": api.store.count()}
    assert api.catalog_source()["element_sets"] == {"bundled": api.store.count()}
