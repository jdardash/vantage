"""Tests for the aviation overlay backend (sda.aviation, sda.routes.aviation).

Offline: httpx.AsyncClient inside sda.aviation is replaced by a fake that answers
from recorded adsb.lol samples in tests/data/aviation (ODbL, trimmed) and two VRS
standing-data routes (CC0). The router is mounted on its own FastAPI app, so these
tests do not depend on sda.api wiring.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sda.aviation as av
import sda.routes.aviation as routes
import sda.routes.worldview as wv

DATA = Path(__file__).resolve().parent / "data" / "aviation"


def load(name: str, now_ms: int | None = None) -> dict:
    data = json.loads((DATA / name).read_text(encoding="utf-8"))
    if now_ms is not None:
        data["now"] = now_ms
    return data


def now_ms() -> int:
    return int(time.time() * 1000)


# --- fake httpx -------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class Upstream:
    """Routes URLs to canned answers and records every request and header set."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.requests: list[str] = []
        self.client_headers: list[dict] = []

    def on(self, prefix: str, answer) -> None:
        self.routes.insert(0, (prefix, answer))

    def handle(self, url: str) -> FakeResponse:
        self.requests.append(url)
        for prefix, answer in self.routes:
            if url.startswith(prefix):
                if callable(answer):
                    return answer(url)
                if isinstance(answer, FakeResponse):
                    return answer
                return FakeResponse(200, answer, {"x-ratelimit-remaining": "400"})
        return FakeResponse(404, {})

    def client_class(self):
        upstream = self

        class _Client:
            def __init__(self, *args, **kwargs):
                upstream.client_headers.append(dict(kwargs.get("headers") or {}))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                return upstream.handle(url)

        return _Client


ADSB = "https://api.adsb.lol"
VRS = "https://vrs-standing-data.adsb.lol/routes/"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ADSBLOL_API_KEY", raising=False)
    monkeypatch.delenv("SDA_ADSBLOL_BUDGET", raising=False)
    av.reset_state()
    yield
    av.reset_state()


@pytest.fixture
def upstream(monkeypatch) -> Upstream:
    up = Upstream()
    monkeypatch.setattr(av.httpx, "AsyncClient", up.client_class())
    return up


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


# --- parsing -------------------------------------------------------------------


def test_parse_military_sample():
    data = load("adsblol_mil.json")
    now = data["now"] / 1000
    recs = {r["hex"]: r for r in (av.parse_aircraft(a, now) for a in data["ac"]) if r}
    assert len(recs) == 14
    c130 = recs["ae57d5"]
    assert c130["callsign"] == "C2011" and c130["registration"] == "2011"
    assert c130["type"] == "C30J" and c130["military"] is True
    assert c130["alt_m"] == pytest.approx(2850 * 0.3048, abs=0.1)
    assert c130["gps"] == "good" and c130["position_source"] == "adsb"
    # Ground: altitude 0, never counted for GPS.
    osprey = recs["ae5dcf"]
    assert osprey["on_ground"] is True and osprey["alt_m"] == 0.0 and osprey["gps"] is None
    # GPS lost (gpsOkBefore), position only from lastPosition with its age.
    pc21 = recs["896c13"]
    assert pc21["gps"] == "bad" and pc21["position_source"] == "last"
    assert pc21["lat"] == pytest.approx(24.817154, abs=1e-5)
    assert pc21["seen_at"] == pytest.approx(now - 233.27, abs=0.1)
    # No position at all, and the receiver-based rough fix is never used.
    assert recs["3520c6"]["lat"] is None
    assert av.parse_aircraft({"hex": "zzz"}, now) is None
    assert av.parse_aircraft("x", now) is None


def test_integrity_follows_the_gpsjam_rules():
    base = {"type": "adsb_icao", "alt_baro": 30000}
    assert av.integrity_state({**base, "nic": 8, "nac_p": 9}) == "good"
    assert av.integrity_state({**base, "nic": 7, "nac_p": 8}) == "good"
    assert av.integrity_state({**base, "nic": 6, "nac_p": 10}) == "bad"
    assert av.integrity_state({**base, "nic": 8, "nac_p": 7}) == "bad"
    assert av.integrity_state({**base, "nic": 0, "nac_p": 0}) == "bad"
    assert av.integrity_state({**base, "nic": 8}) is None
    # MLAT and TIS-B integrity describes the ground network: not counted.
    assert av.integrity_state({"type": "mlat", "nic": 0, "nac_p": 0, "alt_baro": 9000}) is None
    assert av.integrity_state({**base, "nic": 0, "nac_p": 0, "mlat": ["nic"]}) is None
    # readsb's GPS-lost marker wins, even on an MLAT track.
    assert av.integrity_state({"type": "mlat", "gpsOkBefore": 1.0, "alt_baro": 9000}) == "bad"
    assert av.integrity_state({**base, "alt_baro": "ground", "nic": 0, "nac_p": 0}) is None


def test_baltic_sample_counts_only_aircraft_reports():
    data = load("adsblol_point_baltic.json")
    recs = av.ingest(data)
    assert len(recs) == 65
    obs = list(av.state().observations)
    # 31 of the 65 are pure MLAT tracks; 22 carry their own integrity or a GPS-lost mark.
    assert len(obs) == 22
    assert sum(1 for o in obs if o[4]) == 12


def test_private_aircraft_are_blanked():
    data = load("adsblol_sqk7700.json")
    rec = av.parse_aircraft(data["ac"][1], data["now"] / 1000)
    assert rec["private"] is True
    assert rec["callsign"] == "" and rec["registration"] == "" and rec["operator"] == ""
    assert rec["squawk"] == "7700"


def test_operator_names_only_organisations():
    assert av.public_operator("DELTA AIR LINES INC", False) == "DELTA AIR LINES INC"
    assert av.public_operator("Bank of Utah Trustee", False) == "Bank of Utah Trustee"
    assert av.public_operator("JOHN Q PUBLIC", False) == ""
    assert av.public_operator("JOHN Q PUBLIC", True) == "JOHN Q PUBLIC"
    assert av.public_operator(None, True) == ""


# --- geometry -------------------------------------------------------------------


def test_dead_reckoning_on_the_sphere():
    lat, lon = av.dead_reckon(0.0, 0.0, 90.0, 360.0, 3600.0)
    assert lat == pytest.approx(0.0, abs=1e-9)
    assert lon == pytest.approx(math.degrees(360 * 1.852 / 6371.0088), abs=1e-9)
    lat, lon = av.dead_reckon(10.0, 20.0, 0.0, 60.0, 3600.0)
    assert lat == pytest.approx(10.0 + math.degrees(60 * 1.852 / 6371.0088), abs=1e-9)
    assert lon == pytest.approx(20.0, abs=1e-9)
    # Across the antimeridian the longitude wraps.
    _, lon = av.dead_reckon(0.0, 179.9, 90.0, 480.0, 600.0)
    assert lon == pytest.approx(179.9 + math.degrees(80 * 1.852 / 6371.0088) - 360, abs=1e-6)
    assert av.haversine_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)
    assert av.bearing_deg(0, 0, 1, 0) == pytest.approx(0.0, abs=1e-9)
    assert av.bearing_deg(0, 0, 0, 1) == pytest.approx(90.0, abs=1e-9)


def test_position_state_reported_estimated_lost():
    t0 = 1_800_000_000.0
    rec = {"lat": 50.0, "lon": 10.0, "track": 90.0, "gs_kt": 450.0, "seen_at": t0,
           "on_ground": False}
    assert av.position_state(rec, t0 + 30)["state"] == "reported"
    est = av.position_state(rec, t0 + 600)
    assert est["state"] == "estimated" and est["age_s"] == 600.0
    elat, elon = av.dead_reckon(50.0, 10.0, 90.0, 450.0, 600.0)
    assert est["lat"] == pytest.approx(elat, abs=1e-5)
    assert est["lon"] == pytest.approx(elon, abs=1e-5)
    assert est["from"] == {"lat": 50.0, "lon": 10.0}
    assert av.position_state(rec, t0 + av.DEAD_RECKON_MAX_S + 1)["state"] == "lost"
    assert av.position_state({**rec, "on_ground": True}, t0 + 600)["state"] == "lost"
    assert av.position_state({**rec, "gs_kt": 12.0}, t0 + 600)["state"] == "lost"
    assert av.position_state({**rec, "track": None}, t0 + 600)["state"] == "lost"
    assert av.position_state({"lat": None, "lon": 1, "seen_at": t0}, t0)["state"] == "lost"


def test_hex_grid_round_trips():
    for lat, lon in [(0.0, 0.0), (55.0, 20.0), (-33.9, 151.2), (70.0, -150.0), (12.3, 179.9)]:
        cell = av.hex_cell(lat, lon, 2.0)
        assert cell is not None
        clat, clon = av.hex_center(*cell, 2.0)
        ring = av.hex_boundary(*cell, 2.0)
        assert len(ring) == 6
        assert abs(clon - lon) <= 2.0 + 1e-9  # within the corner radius of the centre
        if abs(clon) <= 180.0:
            assert av.hex_cell(clat, clon, 2.0) == cell
        else:
            # The seam: a cell past 180 keeps its unwrapped centre, so the globe
            # draws it whole; points on the far side of 180 fall in other cells.
            assert av.hex_cell(clat, clon, 2.0) != cell
    assert av.hex_cell(85.0, 0.0) is None and av.hex_cell(float("nan"), 0.0) is None
    assert av.hex_cell(0.0, 0.0, 2.0) != av.hex_cell(0.0, 4.0, 2.0)


def test_gpsjam_share_and_levels():
    assert av.gpsjam_percent(1, 1) == 0.0
    assert av.gpsjam_percent(2, 2) == 50.0
    assert av.gpsjam_percent(0, 5) == 0.0
    assert av.gpsjam_percent(3, 100) == 2.0
    assert av.gpsjam_percent(0, 0) == 0.0
    assert [av.interference_level(p) for p in (0.0, 1.9, 2.0, 10.0, 10.1)] == [
        "low", "low", "medium", "medium", "high"]


def test_interference_hexes_window_and_dedupe():
    t = 1_800_000_000.0
    obs = [
        (t, 25.0, 55.0, "a", True), (t - 10, 25.0, 55.0, "a", False),  # one aircraft, bad once
        (t, 25.1, 55.1, "b", True), (t, 25.2, 55.2, "c", False),
        (t - 4000, 25.0, 55.0, "old", True),  # outside the hour
        (t, 40.0, -100.0, "solo", True),  # one aircraft: below the minimum
    ]
    hexes = av.interference_hexes(obs, t)
    assert len(hexes) == 1
    h = hexes[0]
    assert (h["aircraft"], h["degraded"], h["pct"], h["level"]) == (3, 2, 33.3, "high")
    assert len(h["boundary"]) == 6 and h["id"].count(",") == 1


# --- routes and links -------------------------------------------------------------


def test_route_picks_the_leg_and_checks_plausibility():
    dal = load("route_DAL1173.json")
    out = av.route_summary(dal, 36.0, -84.5, 0.0)
    assert (out["origin"]["icao"], out["destination"]["icao"]) == ("KATL", "KLEX")
    back = av.route_summary(dal, 36.0, -84.5, 180.0)
    assert (back["origin"]["icao"], back["destination"]["icao"]) == ("KLEX", "KATL")
    assert out["legs"] == 2 and out["plausible"] is True
    baw = load("route_BAW117.json")
    mid = av.route_summary(baw, 52.0, -20.0)
    assert mid["plausible"] is True and mid["destination"]["iata"] == "JFK"
    assert mid["origin"]["liveatc"] == "https://www.liveatc.net/search/?icao=EGLL"
    assert av.route_summary(baw, 10.0, 100.0)["plausible"] is False
    unknown = av.route_summary(baw, None, None)
    assert unknown["plausible"] is None and "not reported by the aircraft" in unknown["note"]
    assert av.route_summary({"_airports": []}, 0, 0) is None


def test_link_and_callsign_validation():
    assert av.liveatc_url("kjfk") == "https://www.liveatc.net/search/?icao=KJFK"
    assert av.liveatc_url("JFK") is None and av.liveatc_url("K JF") is None
    assert av.liveatc_url(None) is None
    assert av.airline_callsign(" baw117 ") == "BAW117"
    assert av.airline_callsign("RYR1AB") == "RYR1AB"
    assert av.airline_callsign("N123AB") is None
    assert av.airline_callsign("C2011") is None
    assert av.airline_callsign("") is None


def test_nearest_site_and_bearing():
    sites = [{"id": "way/1", "name": "Far", "lat": 10.0, "lon": 10.0},
             {"id": "way/2", "name": "Near", "kind": "airfield", "lat": 0.0, "lon": 0.5},
             {"id": "bad", "lat": None, "lon": 1.0}]
    near = av.nearest_site(sites, 0.0, 0.0)
    assert near["site"]["name"] == "Near" and near["site"]["kind"] == "airfield"
    assert near["distance_km"] == pytest.approx(55.6, abs=0.1)
    assert near["bearing_deg"] == 90.0
    assert av.nearest_site([], 0, 0) is None


# --- budget ----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_token_bucket_paces_and_reserves_for_emergencies():
    clock = Clock()
    b = av.TokenBucket(350, clock)
    assert b.capacity == 8.0
    took = [b.take("normal") for _ in range(10)]
    assert took.count(True) == 7  # one token held back
    assert b.take("emergency") is True and b.take("emergency") is False
    clock.t += 86400 / 350 * 2.01  # two tokens back
    assert b.take("normal") is True and b.take("normal") is False
    assert b.denied >= 4 and b.spent == 9


def test_token_bucket_obeys_upstream_headers():
    clock = Clock()
    b = av.TokenBucket(350, clock)
    b.note_headers({"x-ratelimit-remaining": "3", "x-ratelimit-limit": "500, 500;w=86400"})
    assert b.tokens == 3.0 and b.upstream_limit == "500, 500;w=86400"
    b.note_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "900"})
    assert b.take("emergency") is False
    clock.t += 901
    assert b.status()["blocked_s"] == 0
    b.note_headers({"x-ratelimit-remaining": "nope"})
    b.block_for(30)
    assert b.status()["blocked_s"] == 30


def test_feed_intervals_fit_the_budget(monkeypatch):
    slow = av.feed_intervals(350)
    per_day = sum(86400 / v for v in slow.values())
    assert per_day <= 350 * av.FEED_SHARE + 1e-6
    assert slow["mil"] == 960.0 and slow["sqk7700"] == 960.0 and slow["sqk7500"] == 1920.0
    assert av.feed_intervals(100_000) == av.FAST_INTERVALS_S
    monkeypatch.setenv("SDA_ADSBLOL_BUDGET", "7000")
    assert av.daily_budget() == 7000
    monkeypatch.setenv("SDA_ADSBLOL_BUDGET", "lots")
    assert av.daily_budget() == av.DEFAULT_DAILY_BUDGET


# --- endpoints ---------------------------------------------------------------------


def test_military_endpoint_caches_between_refreshes(client, upstream):
    upstream.on(ADSB + "/v2/mil", load("adsblol_mil.json", now_ms()))
    r = client.get("/api/aviation/military")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 13 and body["reported_total"] == 14
    assert body["source"] == "adsb.lol (ODbL 1.0)" and body["error"] == ""
    first = {a["hex"]: a for a in body["aircraft"]}
    assert first["ae57d5"]["military"] is True and first["ae57d5"]["age_s"] < 60
    assert body["feeds"]["mil"]["interval_s"] == 960.0
    again = client.get("/api/aviation/military").json()
    assert again["count"] == 13
    assert upstream.requests == [ADSB + "/v2/mil"]
    # Each request carried the polite User-Agent and no key.
    assert upstream.client_headers[0]["User-Agent"].startswith("vantage/")
    assert "x-api-key" not in upstream.client_headers[0]


def test_rate_limit_stops_all_calls(client, upstream):
    upstream.on(ADSB + "/v2/mil", FakeResponse(429, None, {"Retry-After": "600"}))
    body = client.get("/api/aviation/military").json()
    assert body["count"] == 0 and "rate limit" in body["error"]
    emerg = client.get("/api/aviation/emergencies").json()
    assert emerg["count"] == 0
    assert upstream.requests == [ADSB + "/v2/mil"]  # nothing else went out
    assert av.state().bucket.status()["blocked_s"] > 500


def test_upstream_failure_keeps_the_contract(client, upstream):
    upstream.on(ADSB + "/v2/mil", FakeResponse(200, ValueError("bad json")))
    body = client.get("/api/aviation/military").json()
    assert body["aircraft"] == [] and body["error"] == "adsb.lol request failed"
    upstream.on(ADSB + "/v2/mil", FakeResponse(503, {}))
    av.reset_state()
    body = client.get("/api/aviation/military").json()
    assert body["error"] == "adsb.lol answered 503"


def test_emergencies_endpoint(client, upstream):
    upstream.on(ADSB + "/v2/sqk/7700", load("adsblol_sqk7700.json", now_ms()))
    upstream.on(ADSB + "/v2/sqk/7600", {"ac": [], "now": now_ms()})
    upstream.on(ADSB + "/v2/sqk/7500", {"ac": [], "now": now_ms()})
    body = client.get("/api/aviation/emergencies").json()
    assert body["count"] == 2 and body["airborne"] == 1
    ac = {a["hex"]: a for a in body["aircraft"]}
    assert ac["fff001"]["meaning"] == "general emergency"
    assert ac["fff001"]["callsign"] == "TST7700"
    assert ac["fff002"]["private"] is True and ac["fff002"]["registration"] == ""
    assert sorted(u.rsplit("/", 1)[1] for u in upstream.requests) == ["7500", "7600", "7700"]
    assert body["codes"]["7500"] == "unlawful interference"


def test_emergency_squawks_seen_in_other_feeds_are_included(client, upstream):
    mil = load("adsblol_mil.json", now_ms())
    mil["ac"][5]["squawk"] = "7600"  # the C-130
    upstream.on(ADSB + "/v2/mil", mil)
    client.get("/api/aviation/military")
    upstream.on(ADSB + "/v2/sqk/", {"ac": [], "now": now_ms()})
    body = client.get("/api/aviation/emergencies").json()
    assert [a["hex"] for a in body["aircraft"]] == ["ae57d5"]
    assert body["aircraft"][0]["meaning"] == "radio failure"


def test_gps_interference_endpoint_samples_the_view_once(client, upstream):
    upstream.on(ADSB + "/v2/point/", load("adsblol_point_baltic.json", now_ms()))
    body = client.get("/api/aviation/gps-interference?lat=55&lon=20&size=2").json()
    assert body["sample"] == "sampled"
    assert upstream.requests == [ADSB + "/v2/point/56/20/250"]
    assert body["aircraft_counted"] == 22 and body["count"] >= 1
    assert body["method"].startswith("Inferred from aircraft reports, not measured")
    assert {h["level"] for h in body["hexes"]} <= {"low", "medium", "high"}
    assert body["sampled_areas"][0]["lat"] == 56.0
    again = client.get("/api/aviation/gps-interference?lat=54&lon=21").json()
    assert again["sample"] == "recent" and len(upstream.requests) == 1
    elsewhere = client.get("/api/aviation/gps-interference?lat=0&lon=0").json()
    assert elsewhere["sample"] == "waiting" and len(upstream.requests) == 1
    plain = client.get("/api/aviation/gps-interference").json()
    assert plain["sample"] == "none"
    assert client.get("/api/aviation/gps-interference?lat=95&lon=0").status_code == 422


def test_card_for_a_known_military_aircraft(client, upstream):
    upstream.on(ADSB + "/v2/mil", load("adsblol_mil.json", now_ms()))
    client.get("/api/aviation/military")
    card = client.get("/api/aviation/aircraft/AE57D5").json()
    assert card["found"] is True and card["lookup"] == "store"
    assert card["registration"] == "2011" and card["type"] == "C30J"
    assert card["position"]["state"] == "reported"
    assert card["route"] is None  # C2011 is not an airline callsign: no route request
    assert not any(u.startswith(VRS) for u in upstream.requests)


def test_card_looks_up_the_hex_and_the_route(client, upstream):
    hit = {"ac": [{"hex": "406a93", "type": "adsb_icao", "flight": "BAW117  ", "r": "G-XWBA",
                   "t": "A35K", "alt_baro": 38000, "gs": 480.0, "track": 255.0,
                   "lat": 52.0, "lon": -20.0, "nic": 8, "nac_p": 10, "seen_pos": 1.0}],
           "now": now_ms()}
    upstream.on(ADSB + "/v2/hex/406a93", hit)
    upstream.on(VRS + "BA/BAW117.json", load("route_BAW117.json"))
    card = client.get("/api/aviation/aircraft/406a93").json()
    assert card["lookup"] == "adsb.lol" and card["callsign"] == "BAW117"
    assert card["route"]["origin"]["icao"] == "EGLL"
    assert card["route"]["destination"]["liveatc"] == "https://www.liveatc.net/search/?icao=KJFK"
    assert card["route"]["plausible"] is True
    assert card["operator"] == "BAW"
    assert card["operator_source"] == "airline code from the callsign"
    assert "VRS standing data via adsb.lol (CC0)" in card["sources"]
    # The route is cached; a second card makes no new route request.
    client.get("/api/aviation/aircraft/406a93")
    assert sum(1 for u in upstream.requests if u.startswith(VRS)) == 1


def test_card_with_hints_when_adsblol_knows_nothing(client, upstream):
    upstream.on(ADSB + "/v2/hex/", {"ac": [], "now": now_ms()})
    upstream.on(VRS + "DA/DAL1173.json", load("route_DAL1173.json"))
    card = client.get("/api/aviation/aircraft/a1b2c3?callsign=dal1173&lat=36&lon=-84.5").json()
    assert card["found"] is False and card["position"]["state"] == "lost"
    assert card["route"]["origin"]["icao"] == "KATL"


def test_card_for_a_private_aircraft_withholds_identity(client, upstream):
    upstream.on(ADSB + "/v2/sqk/", {"ac": [], "now": now_ms()})
    upstream.on(ADSB + "/v2/sqk/7700", load("adsblol_sqk7700.json", now_ms()))
    client.get("/api/aviation/emergencies")
    card = client.get("/api/aviation/aircraft/fff002?callsign=XTEST2").json()
    assert card["private"] is True and card["route"] is None
    assert card["registration"] == "" and card["callsign"] == ""
    assert "privacy" in card["note"]


def test_card_rejects_a_bad_hex(client):
    assert client.get("/api/aviation/aircraft/xyz123").status_code == 422
    assert client.get("/api/aviation/aircraft/12345").status_code == 422


def test_nearest_site_endpoint_caches_per_cell(client, monkeypatch):
    calls = []

    async def fake_sites(south, west, north, east):
        calls.append((south, west, north, east))
        return {"sites": [{"id": "way/9", "name": "RAF Lakenheath", "kind": "airfield",
                           "operator": "", "lat": 52.409, "lon": 0.556}], "count": 1}

    monkeypatch.setattr(wv, "proxy_military_sites", fake_sites)
    body = client.get("/api/aviation/nearest-site?lat=52.2&lon=0.9").json()
    assert body["nearest"]["site"]["name"] == "RAF Lakenheath"
    assert 20 < body["nearest"]["distance_km"] < 40
    south, west, north, east = calls[0]
    assert north - south <= 6 and east - west <= 6
    client.get("/api/aviation/nearest-site?lat=51.8&lon=1.2")  # same whole-degree cell
    assert len(calls) == 1
    edge = client.get("/api/aviation/nearest-site?lat=89.9&lon=179.9").json()
    assert edge["box"]["north"] == 90.0 and edge["box"]["east"] == 180.0


def test_nearest_site_failure_is_reported(client, monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(wv, "proxy_military_sites", broken)
    body = client.get("/api/aviation/nearest-site?lat=10&lon=10").json()
    assert body["nearest"] is None and body["error"] == "Overpass request failed"


def test_status_and_key_stay_server_side(client, upstream, monkeypatch):
    body = client.get("/api/aviation/status").json()
    assert body["keyed"] is False and "500 requests a day" in body["note"]
    assert body["intervals_s"]["mil"] == 960.0
    monkeypatch.setenv("ADSBLOL_API_KEY", "secret-key-123")
    upstream.on(ADSB + "/v2/mil", load("adsblol_mil.json", now_ms()))
    mil = client.get("/api/aviation/military")
    assert upstream.client_headers[-1]["x-api-key"] == "secret-key-123"
    status = client.get("/api/aviation/status")
    assert status.json()["keyed"] is True
    for resp in (mil, status):
        assert "secret-key-123" not in resp.text


def test_fetch_v2_guards_paths_and_feeds_the_snapshot(upstream):
    import asyncio

    upstream.on(ADSB + "/v2/callsign/C2011", load("adsblol_mil.json", now_ms()))
    for bad in ("/v1/mil", "/v2/../secret", "https://evil/v2/x", "/v2/a b", 5):
        assert asyncio.run(av.fetch_v2(bad)) is None
    assert upstream.requests == []
    got = asyncio.run(av.fetch_v2("/v2/callsign/C2011"))
    assert got is not None and len(got["ac"]) == 14
    asyncio.run(av.fetch_v2("/v2/callsign/C2011"))
    assert len(upstream.requests) == 1  # cached
    snap = {a["icao24"]: a for a in av.aircraft_snapshot()}
    assert snap["ae57d5"]["callsign"] == "C2011" and snap["ae57d5"]["military"] is True
    assert set(snap["ae57d5"]) == {"icao24", "callsign", "registration", "type", "lat", "lon",
                                   "alt_m", "gs_kt", "track", "squawk", "military", "seen_at"}
    assert "3520c6" not in snap  # no position


def test_module_text_is_ascii():
    for path in (Path(av.__file__), Path(routes.__file__)):
        text = path.read_text(encoding="utf-8")
        assert all(ord(ch) < 128 for ch in text), path.name
