"""Tests for the NASA EONET route (sda.routes.eonet, GET /api/eonet).

Offline and deterministic: the EONET fetch is replaced with a fake that hands
back canned Python objects (so NaN, infinities and booleans can be fed in), and
the tests that run the real fetch helper swap httpx.AsyncClient for a fake
client. tests/conftest.py refuses any connection off this machine, so nothing
here can reach eonet.gsfc.nasa.gov. The app under test holds only this router.
"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.routes import eonet

# Shapes follow a live EONET v3 answer of 2026-11-19 (37 open events, 30 days).
STORM = {
    "id": "EONET_26494",
    "title": "Tropical Cyclone Akvan",
    "description": None,
    "link": "https://eonet.gsfc.nasa.gov/api/v3/events/EONET_26494",
    "closed": None,
    "categories": [{"id": "severeStorms", "title": "Severe Storms"}],
    "sources": [{"id": "JTWC", "url": "https://www.metoc.navy.mil/jtwc/products/io0526.tcw"}],
    # deliberately out of order: the newest is in the middle
    "geometry": [
        {"magnitudeValue": 35.0, "magnitudeUnit": "kts", "date": "2026-11-16T18:00:00Z",
         "type": "Point", "coordinates": [86.8, 11.4]},
        {"magnitudeValue": 45.0, "magnitudeUnit": "kts", "date": "2026-11-19T12:00:00Z",
         "type": "Point", "coordinates": [85.3, 16]},
        {"magnitudeValue": 40.0, "magnitudeUnit": "kts", "date": "2026-11-17T06:00:00Z",
         "type": "Point", "coordinates": [87.3, 12]},
    ],
}
FIRE = {
    "id": "EONET_26500",
    "title": "Ridge Fire, Plumas, California",
    "closed": None,
    "categories": [{"id": "wildfires", "title": "Wildfires"}],
    "sources": [
        {"id": "IRWIN", "url": "https://irwin.doi.gov/observer/incidents/abc"},
        {"id": "BAD", "url": "javascript:alert(1)"},
        {"id": "FTP", "url": "ftp://example.org/x"},
        {"id": "REL", "url": "/relative/path"},
        {"id": "SPACE", "url": "https://exa mple.org/x"},
        {"id": "NOHOST", "url": "https://"},
        {"id": "NUM", "url": 42},
    ],
    "geometry": [
        {"magnitudeValue": 2378.77, "magnitudeUnit": "acres", "date": "2026-11-18T00:00:00Z",
         "type": "Point", "coordinates": [-120.9, 39.9]},
    ],
}
ICE = {
    "id": "EONET_26510",
    "title": "Iceberg D35",
    "categories": [{"id": "seaLakeIce", "title": "Sea and Lake Ice"}],
    "sources": [{"id": "NATICE", "url": "https://usicecenter.gov/pub/Iceberg_Tabular.csv"}],
    "geometry": [
        {"magnitudeValue": None, "magnitudeUnit": None, "date": "2026-11-15T00:00:00Z",
         "type": "Polygon",
         "coordinates": [[[-40, -60], [-38, -60], [-38, -58], [-40, -58], [-40, -60]]]},
    ],
}


def feed(*events: object) -> dict:
    return {"title": "EONET Events", "description": "", "link": eonet.EONET_URL,
            "events": list(events)}


class FakeFetch:
    """Stands in for eonet._fetch_events; returns (or raises) queued answers."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, days: int, status: str) -> object:
        self.calls.append((days, status))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _fresh_cache():
    eonet.clear_eonet_cache()
    yield
    eonet.clear_eonet_cache()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(eonet.router)
    return TestClient(app)


def use(monkeypatch, *answers: object) -> FakeFetch:
    fake = FakeFetch(*answers)
    monkeypatch.setattr(eonet, "_fetch_events", fake)
    return fake


def expire(days: int = 30, status: str = "open") -> None:
    """Age one pair's last attempt past the cache TTL, as if thirty minutes went by."""
    entry = eonet._state.entries[(days, status)]
    assert entry.attempted_at is not None
    entry.attempted_at -= eonet.EONET_CACHE_TTL_S + 1.0


def by_id(body: dict) -> dict:
    return {ev["id"]: ev for ev in body["events"]}


# --- shape, latest geometry, centroid, links --------------------------------------


def test_default_query_and_shape(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM, FIRE, ICE))
    resp = client.get("/api/eonet")
    assert resp.status_code == 200
    body = resp.json()
    assert fake.calls == [(30, "open")]
    assert set(body) == {"events", "count", "days", "status", "source", "stale"}
    assert body["count"] == 3 == len(body["events"])
    assert body["days"] == 30 and body["status"] == "open"
    assert body["source"] == "NASA EONET v3" and body["stale"] is False
    storm = by_id(body)["EONET_26494"]
    assert set(storm) == {"id", "title", "categories", "point", "geometry_type", "date",
                          "magnitude_value", "magnitude_unit", "observations", "sources",
                          "closed"}
    assert storm["title"] == "Tropical Cyclone Akvan"
    assert storm["categories"] == [{"id": "severeStorms", "title": "Severe Storms"}]
    assert storm["closed"] is None


def test_latest_geometry_is_by_date_not_list_order(client, monkeypatch):
    use(monkeypatch, feed(STORM))
    storm = client.get("/api/eonet").json()["events"][0]
    assert storm["point"] == [85.3, 16]
    assert storm["date"] == "2026-11-19T12:00:00Z"
    assert storm["magnitude_value"] == 45.0 and storm["magnitude_unit"] == "kts"
    assert storm["observations"] == 3
    assert storm["geometry_type"] == "Point"


def test_same_date_goes_to_the_later_entry(client, monkeypatch):
    twin = dict(STORM, geometry=[
        {"date": "2026-11-19T00:00:00Z", "type": "Point", "coordinates": [1, 1]},
        {"date": "2026-11-19T00:00:00Z", "type": "Point", "coordinates": [2, 2]},
        {"date": "not a date", "type": "Point", "coordinates": [3, 3]},
    ])
    use(monkeypatch, feed(twin))
    assert client.get("/api/eonet").json()["events"][0]["point"] == [2, 2]


def test_polygon_reduces_to_its_centre(client, monkeypatch):
    use(monkeypatch, feed(ICE))
    ice = client.get("/api/eonet").json()["events"][0]
    assert ice["point"] == [-39, -59]
    assert ice["geometry_type"] == "Polygon"
    assert ice["magnitude_value"] is None and ice["magnitude_unit"] is None


def test_polygon_across_the_antimeridian_centres_on_180(client, monkeypatch):
    ring = [[179, 10], [-179, 10], [-179, 12], [179, 12], [179, 10]]
    event = dict(ICE, id="EONET_X", geometry=[
        {"date": "2026-11-15T00:00:00Z", "type": "Polygon", "coordinates": [ring]}])
    use(monkeypatch, feed(event))
    lon, lat = client.get("/api/eonet").json()["events"][0]["point"]
    assert abs(abs(lon) - 180) < 1e-9 and lat == 11


def test_only_http_and_https_sources_survive(client, monkeypatch):
    use(monkeypatch, feed(FIRE))
    fire = client.get("/api/eonet").json()["events"][0]
    assert fire["sources"] == [
        {"id": "IRWIN", "url": "https://irwin.doi.gov/observer/incidents/abc"}]
    assert fire["magnitude_value"] == 2378.77 and fire["magnitude_unit"] == "acres"
    assert fire["observations"] == 1


@pytest.mark.parametrize(("days", "status"), [(1, "open"), (7, "closed"), (365, "all")])
def test_query_is_passed_through(client, monkeypatch, days, status):
    fake = use(monkeypatch, feed(STORM))
    body = client.get("/api/eonet", params={"days": days, "status": status}).json()
    assert fake.calls == [(days, status)]
    assert body["days"] == days and body["status"] == status


@pytest.mark.parametrize(
    "params",
    [{"days": 0}, {"days": 366}, {"days": "abc"}, {"days": "2.5"}, {"status": "bogus"},
     {"status": "OPEN"}, {"status": ""}],
)
def test_bad_query_is_422_and_fetches_nothing(client, monkeypatch, params):
    fake = use(monkeypatch, feed(STORM))
    assert client.get("/api/eonet", params=params).status_code == 422
    assert fake.calls == []


# --- hostile and malformed events ---------------------------------------------------


def test_malformed_events_are_skipped_not_raised(client, monkeypatch):
    bad = [
        None, "x", 42, [],
        {"title": "no id", "geometry": STORM["geometry"]},
        {"id": "", "geometry": STORM["geometry"]},
        {"id": 7, "geometry": STORM["geometry"]},
        {"id": "E1", "title": "no geometry"},
        {"id": "E2", "geometry": "nope"},
        {"id": "E3", "geometry": [{"type": "Point", "coordinates": [200, 10]}]},
        {"id": "E4", "geometry": [{"type": "Point", "coordinates": [10, 95]}]},
        {"id": "E5", "geometry": [{"type": "Point", "coordinates": [True, 10]}]},
        {"id": "E6", "geometry": [{"type": "Point", "coordinates": ["10", "20"]}]},
        {"id": "E7", "geometry": [{"type": "Point", "coordinates": [math.nan, 10]}]},
        {"id": "E8", "geometry": [{"type": "Point", "coordinates": [math.inf, 10]}]},
        {"id": "E9", "geometry": [{"type": "Point", "coordinates": [10]}]},
        {"id": "E10", "geometry": [{"type": "LineString", "coordinates": [[1, 2], [3, 4]]}]},
        {"id": "E11", "geometry": [{"type": "Polygon", "coordinates": []}]},
        {"id": "E12", "geometry": [{"type": "Polygon", "coordinates": [[["a", "b"]]]}]},
        {"id": "E13", "geometry": [None, "x", {"type": "Point"}]},
    ]
    use(monkeypatch, feed(*bad, STORM))
    body = client.get("/api/eonet").json()
    assert [ev["id"] for ev in body["events"]] == ["EONET_26494"]
    assert body["count"] == 1


def test_odd_fields_are_cleaned(client, monkeypatch):
    odd = {
        "id": "EONET_ODD",
        "title": "  A\x00 fire\x1b[31m <b>here</b> " + "x" * 400,
        "closed": "2026-11-10T00:00:00Z",
        "categories": [{"id": "wildfires", "title": None}, {"title": "no id"}, "x", None,
                       {"id": True}],
        "sources": "not a list",
        "geometry": [{"type": "Point", "coordinates": [10, 20, 500],
                      "date": "2026-11-19T00:00:00Z",
                      "magnitudeValue": True, "magnitudeUnit": 12}],
    }
    nan_mag = {"id": "EONET_NAN", "geometry": [
        {"type": "Point", "coordinates": [1, 2], "magnitudeValue": math.nan,
         "magnitudeUnit": "acres"}]}
    use(monkeypatch, feed(odd, nan_mag))
    body = client.get("/api/eonet").json()
    ev = by_id(body)["EONET_ODD"]
    assert ev["title"].startswith("A fire[31m <b>here</b> ")
    assert len(ev["title"]) == 200
    assert all(ord(ch) >= 32 for ch in ev["title"])
    assert ev["categories"] == [{"id": "wildfires", "title": "wildfires"}]
    assert ev["sources"] == []
    assert ev["point"] == [10, 20]
    assert ev["magnitude_value"] is None and ev["magnitude_unit"] is None
    assert ev["closed"] == "2026-11-10T00:00:00Z"
    nan = by_id(body)["EONET_NAN"]
    assert nan["title"] == "EONET_NAN"  # no title: the id stands in
    assert nan["magnitude_value"] is None and nan["magnitude_unit"] == "acres"
    assert nan["date"] == ""


def test_duplicate_ids_keep_the_first(client, monkeypatch):
    again = dict(STORM, title="Second copy")
    use(monkeypatch, feed(STORM, again))
    body = client.get("/api/eonet").json()
    assert body["count"] == 1 and body["events"][0]["title"] == "Tropical Cyclone Akvan"


def test_empty_feed_is_a_real_answer(client, monkeypatch):
    use(monkeypatch, feed())
    resp = client.get("/api/eonet")
    assert resp.status_code == 200
    assert resp.json()["events"] == [] and resp.json()["count"] == 0


@pytest.mark.parametrize(
    "payload",
    [None, "x", [], 42, {}, {"events": "nope"}, {"events": None},
     {"events": [None, {"id": "E1"}]}],
)
def test_malformed_feed_is_502_not_500(client, monkeypatch, payload):
    use(monkeypatch, payload)
    resp = client.get("/api/eonet")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "NASA EONET events feed unavailable"


def test_fetch_exception_is_502(client, monkeypatch):
    use(monkeypatch, RuntimeError("boom"))
    assert client.get("/api/eonet").status_code == 502


# --- cache, stale, keys --------------------------------------------------------------


def test_cache_reused_within_ttl(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM))
    for _ in range(3):
        assert client.get("/api/eonet").status_code == 200
    assert fake.calls == [(30, "open")]


def test_each_query_pair_has_its_own_cache(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM), feed(STORM, FIRE), feed(ICE))
    assert client.get("/api/eonet").json()["count"] == 1
    assert client.get("/api/eonet", params={"days": 7}).json()["count"] == 2
    assert client.get("/api/eonet", params={"status": "all"}).json()["count"] == 1
    assert client.get("/api/eonet").json()["count"] == 1  # still the first answer
    assert fake.calls == [(30, "open"), (7, "open"), (30, "all")]


def test_refetch_after_ttl(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM), feed(STORM, FIRE))
    assert client.get("/api/eonet").json()["count"] == 1
    expire()
    assert client.get("/api/eonet").json()["count"] == 2
    assert len(fake.calls) == 2


def test_stale_on_error_keeps_last_good_copy(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM, FIRE), RuntimeError("down"), None, feed(ICE))
    assert client.get("/api/eonet").json()["stale"] is False
    expire()
    body = client.get("/api/eonet").json()
    assert body["stale"] is True and body["count"] == 2
    expire()
    body = client.get("/api/eonet").json()
    assert body["stale"] is True and body["count"] == 2
    expire()
    body = client.get("/api/eonet").json()
    assert body["stale"] is False and [ev["id"] for ev in body["events"]] == ["EONET_26510"]
    assert len(fake.calls) == 4


def test_502_on_first_failure_and_no_retry_within_ttl(client, monkeypatch):
    fake = use(monkeypatch, None, feed(STORM))
    assert client.get("/api/eonet").status_code == 502
    assert client.get("/api/eonet").status_code == 502
    assert len(fake.calls) == 1
    expire()
    assert client.get("/api/eonet").status_code == 200


def test_cache_holds_a_bounded_number_of_pairs(client, monkeypatch):
    use(monkeypatch, feed(STORM))
    for days in range(1, eonet._CACHE_MAX_KEYS + 6):
        assert client.get("/api/eonet", params={"days": days}).status_code == 200
    assert len(eonet._state.entries) == eonet._CACHE_MAX_KEYS
    assert (1, "open") not in eonet._state.entries  # the oldest attempt went first


def test_clear_cache_forgets_everything(client, monkeypatch):
    fake = use(monkeypatch, feed(STORM))
    client.get("/api/eonet")
    eonet.clear_eonet_cache()
    assert eonet._state.entries == {}
    client.get("/api/eonet")
    assert len(fake.calls) == 2


def test_concurrent_requests_share_one_fetch(monkeypatch):
    calls = 0

    async def slow_fetch(days: int, status: str) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return feed(STORM)

    monkeypatch.setattr(eonet, "_fetch_events", slow_fetch)

    async def many() -> list:
        return await asyncio.gather(*(eonet._current(30, "open") for _ in range(5)))

    results = asyncio.run(many())
    assert calls == 1
    assert all(events is not None and not stale for events, stale in results)


# --- the real fetch helper, against a fake httpx client --------------------------------


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


def test_events_url():
    assert eonet._events_url(30, "open") == (
        "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=30")


def test_upstream_error_status_is_502(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(503, {}, seen))
    assert client.get("/api/eonet").status_code == 502
    assert ("get", eonet._events_url(30, "open")) in seen
    assert ("init", eonet.EONET_TIMEOUT_S) in seen


def test_upstream_ok_goes_through_the_parser(client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(200, feed(STORM, ICE), seen))
    body = client.get("/api/eonet", params={"days": 5, "status": "closed"}).json()
    assert body["count"] == 2
    assert seen.count(("get", eonet._events_url(5, "closed"))) == 1


# --- the parser on its own ---------------------------------------------------------------


def test_ring_centre_drops_the_closing_vertex():
    square = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    assert eonet._ring_centre(square) == (2, 2)
    assert eonet._ring_centre([[5, 6]]) == (5, 6)
    assert eonet._ring_centre([]) is None
    assert eonet._ring_centre("x") is None


@pytest.mark.parametrize(
    ("url", "ok"),
    [("https://a.example/x", True), ("http://a.example", True), ("HTTPS://A.EXAMPLE/", True),
     ("javascript:alert(1)", False), ("data:text/html,x", False), ("ftp://a.example", False),
     ("//a.example/x", False), ("https://", False), ("https://a .example", False),
     ("https://a.exa\nmple/", False), (" https://a.example/ ", True), ("", False),
     (None, False), (5, False),
     ("https://a.example/" + "x" * 3000, False)],
)
def test_http_url(url, ok):
    assert (eonet._http_url(url) is not None) is ok
