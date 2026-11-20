"""OpenStreetMap buildings for the 3D city view (sda.buildings, GET /api/buildings).

The Kano fixture is 40 real footprints captured once from Overpass for a 0.01
degree box at 12.0095 N, 8.5872 E (tests/data/overpass_buildings_kano.json).
Overpass itself is replaced by a fake that records the queries it was sent, and
the per-host throttle is checked on its own with a short gap.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import buildings, hostthrottle
from sda.buildings import building_height_m, parse_buildings
from sda.routes.buildings import router

DATA = Path(__file__).resolve().parent / "data"


def kano() -> dict:
    return json.loads((DATA / "overpass_buildings_kano.json").read_text(encoding="utf-8"))


class FakeOverpass:
    """Stands in for sda.buildings.fetch_overpass and keeps every query."""

    def __init__(self, answer: dict | None) -> None:
        self.answer = answer
        self.queries: list[str] = []

    async def __call__(self, query: str) -> dict | None:
        self.queries.append(query)
        return self.answer


@pytest.fixture
def fake_overpass(monkeypatch: pytest.MonkeyPatch) -> FakeOverpass:
    fake = FakeOverpass(kano())
    monkeypatch.setattr(buildings, "fetch_overpass", fake)
    buildings.clear_cache()
    return fake


@pytest.fixture
def client(fake_overpass: FakeOverpass) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_building_height_rules():
    assert building_height_m({"height": "25 m"}) == 25.0
    assert building_height_m({"building:levels": "4"}) == pytest.approx(12.8)
    assert building_height_m({"height": "tall", "building:levels": "2"}) == pytest.approx(6.4)
    assert building_height_m({}) == 8.0


def test_height_tag_forms():
    assert building_height_m({"height": "12.5"}) == 12.5
    assert building_height_m({"height": "100 ft"}) == pytest.approx(30.48)
    assert building_height_m({"height": "-3", "building:levels": "1"}) == pytest.approx(3.2)
    assert building_height_m({"building:levels": "many"}) == 8.0


def test_parse_buildings_fixture():
    rows = parse_buildings(kano())
    assert rows and all(r["ring"][0] == r["ring"][-1] for r in rows)
    assert len(rows) == 40
    first = rows[0]
    assert first["id"] == "way/295258554"
    assert set(first) == {"id", "name", "kind", "height_m", "height_from", "levels", "ring"}
    # Kano's footprints carry building=yes and nothing else: default height, no levels.
    assert first["kind"] == "building" and first["height_m"] == 8.0
    assert first["height_from"] == "default" and first["levels"] is None
    lon, lat = first["ring"][0]
    assert 8.58 < lon < 8.60 and 12.00 < lat < 12.02


def test_parse_buildings_tags_relations_and_bad_rings():
    square = [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.001},
              {"lat": 0.001, "lon": 0.001}, {"lat": 0.001, "lon": 0.0}]
    data = {"elements": [
        # Not closed by Overpass: the parser closes it.
        {"type": "way", "id": 1, "geometry": square,
         "tags": {"building": "mosque", "name": "Kano Central Mosque", "building:levels": "3"}},
        {"type": "way", "id": 2, "geometry": square[:2], "tags": {"building": "yes"}},
        {"type": "way", "id": 3, "geometry": square, "tags": {"highway": "residential"}},
        {"type": "way", "id": 4, "geometry": square,
         "tags": {"building": "yes", "amenity": "school", "height": "14 m"}},
        {"type": "relation", "id": 5, "tags": {"building": "commercial", "type": "multipolygon"},
         "members": [
             {"type": "way", "role": "inner", "geometry": square + square[:1]},
             {"type": "way", "role": "outer", "geometry": [
                 {"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.01},
                 {"lat": 0.01, "lon": 0.01}, {"lat": 0.0, "lon": 0.0}]},
         ]},
        {"type": "way", "id": 1, "geometry": square, "tags": {"building": "yes"}},
        {"type": "node", "id": 6, "lat": 0.0, "lon": 0.0, "tags": {"building": "yes"}},
    ]}
    rows = {r["id"]: r for r in parse_buildings(data)}
    assert list(rows) == ["way/1", "way/4", "relation/5"]
    mosque = rows["way/1"]
    assert mosque["name"] == "Kano Central Mosque" and mosque["kind"] == "mosque"
    assert mosque["levels"] == 3 and mosque["height_m"] == pytest.approx(9.6)
    assert mosque["height_from"] == "levels"
    assert mosque["ring"][0] == mosque["ring"][-1] and len(mosque["ring"]) == 5
    assert rows["way/4"]["kind"] == "school" and rows["way/4"]["height_m"] == 14.0
    assert rows["way/4"]["height_from"] == "height"
    assert rows["relation/5"]["kind"] == "commercial"
    assert len(rows["relation/5"]["ring"]) == 4  # the outer ring, not the inner one


TOO_BIG = {"south": 12, "west": 8.5, "north": 12.1, "east": 8.6}
KANO_BOX = {"south": 12.0045, "west": 8.5822, "north": 12.0145, "east": 8.5922}


def test_bbox_too_large(client):
    r = client.get("/api/buildings", params=TOO_BIG)
    assert r.status_code == 400 and r.json()["detail"] == "Zoom in to see buildings"


def test_empty_bbox_is_refused(client, fake_overpass):
    upside_down = {"south": 12.01, "west": 8.5, "north": 12.0, "east": 8.51}
    r = client.get("/api/buildings", params=upside_down)
    assert r.status_code == 400
    assert fake_overpass.queries == []


def test_bbox_across_antimeridian(client, fake_overpass):
    fiji = {"south": -17.80, "west": 179.99, "north": -17.79, "east": -179.995}
    client.get("/api/buildings", params=fiji)
    assert len(fake_overpass.queries) == 2


def test_buildings_answer_and_query(client, fake_overpass):
    r = client.get("/api/buildings", params=KANO_BOX)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 40 == len(body["buildings"])
    assert body["truncated"] is False and "error" not in body
    assert "OpenStreetMap contributors" in body["attribution"]
    (query,) = fake_overpass.queries
    # The box is rounded outward to 0.001 degrees: that rounded box is the cache key.
    assert '(12.004,8.582,12.015,8.593)' in query
    assert 'way["building"]' in query and "out geom" in query


def test_answers_are_cached_per_rounded_box(client, fake_overpass):
    client.get("/api/buildings", params=KANO_BOX)
    nudged = {k: v + 0.0002 for k, v in KANO_BOX.items()}
    nudged["north"] = 12.0148
    nudged["east"] = 8.5928
    client.get("/api/buildings", params=nudged)
    assert len(fake_overpass.queries) == 1


def test_overpass_failure_is_a_sentence(client, fake_overpass):
    fake_overpass.answer = None
    r = client.get("/api/buildings", params=KANO_BOX)
    assert r.status_code == 200
    body = r.json()
    assert body["buildings"] == [] and body["count"] == 0
    assert body["error"].startswith("OpenStreetMap buildings did not load")
    # A failure is not cached: the next request asks again.
    fake_overpass.answer = kano()
    r = client.get("/api/buildings", params=KANO_BOX)
    assert r.json()["count"] == 40 and len(fake_overpass.queries) == 2


def test_route_is_registered_on_the_app():
    from sda.api import app

    r = TestClient(app).get("/api/buildings", params=TOO_BIG)
    assert r.status_code == 400 and r.json()["detail"] == "Zoom in to see buildings"


def test_wait_turn_spaces_calls_to_one_host():
    hostthrottle.reset()

    async def go() -> list[float]:
        stamps = []
        for host in ("a.example", "a.example", "b.example"):
            await hostthrottle.wait_turn(host, gap_s=0.03)
            stamps.append(time.monotonic())
        return stamps

    a1, a2, b1 = asyncio.run(go())
    hostthrottle.reset()
    assert a2 - a1 >= 0.029
    assert b1 - a2 < 0.02  # another host does not wait behind the first
