"""/api/gdelt over the GDELT 2.0 event export, grouped by place.

The GEO 2.0 API the layer used to call answers 404 since 2026-11; the route now
groups sda.intel's event-export rows. The sample is 60 real rows from one
15-minute export (tests/data/intel/gdelt_export.CSV.zip).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sda import intel
from sda.api import app
from sda.routes import worldview as wv

SAMPLE = Path(__file__).parent / "data" / "intel" / "gdelt_export.CSV.zip"


@pytest.fixture
def sample_rows() -> list[dict]:
    text = intel.unzip_export(SAMPLE.read_bytes())
    assert text
    return intel.parse_gdelt_export(text)


@pytest.fixture
def fresh_cache():
    wv.clear_proxy_caches()
    yield
    wv.clear_proxy_caches()


def row(place="Paris, France", lat=48.85, lon=2.35, articles=1, tone=0.0, event="Consult",
        quad=1, url="", date="2026-11-19T05:00:00Z"):
    return {"place": place, "lat": lat, "lon": lon, "articles": articles, "tone": tone,
            "event": event, "quad_class": quad, "source_url": url, "date": date}


def test_groups_by_place_and_weights_by_articles():
    rows = [
        row(articles=4, tone=-4.0, event="Protest", quad=3, url="https://a.example/1",
            date="2026-11-19T05:00:00Z"),
        row(articles=1, tone=2.0, event="Protest", quad=4, url="https://a.example/2",
            date="2026-11-19T05:15:00Z"),
        row(articles=0, tone=0.0, event="Consult", quad=1),
        row(place="Lagos, Nigeria", lat=6.45, lon=3.39, articles=2, tone=1.0),
    ]
    places = wv.gdelt_places(rows)
    assert [p["name"] for p in places] == ["Paris, France", "Lagos, Nigeria"]
    paris = places[0]
    # zero articles weighs 1: 4 + 1 + 1
    assert paris["count"] == 6 and paris["events"] == 3 and paris["conflict"] == 2
    assert paris["tone"] == pytest.approx(round((-16.0 + 2.0 + 0.0) / 6, 2))
    assert paris["event"] == "Protest"
    assert paris["source_url"] == "https://a.example/1"  # the most-cited event's article
    assert paris["date"] == "2026-11-19T05:15:00Z"  # the latest
    assert not any(k.startswith("_") for k in paris)


def test_query_filters_on_place_or_event_label():
    rows = [row(place="Kyiv, Ukraine", event="Fight"), row(place="Lima, Peru", event="Consult")]
    assert [p["name"] for p in wv.gdelt_places(rows, "ukraine")] == ["Kyiv, Ukraine"]
    assert [p["name"] for p in wv.gdelt_places(rows, "CONSULT")] == ["Lima, Peru"]
    assert wv.gdelt_places(rows, "nowhere") == []


def test_bad_coordinates_are_skipped_and_limit_holds():
    rows = [row(lat=None), *(row(place=f"P{i}", lat=float(i), lon=float(i)) for i in range(5))]
    assert len(wv.gdelt_places(rows)) == 5
    assert len(wv.gdelt_places(rows, limit=2)) == 2


def test_real_sample_groups_and_never_carries_actor_names(sample_rows):
    assert sample_rows, "the 60-row sample should parse"
    places = wv.gdelt_places(sample_rows)
    assert places and len(places) <= len(sample_rows)
    assert sum(p["events"] for p in places) == len(sample_rows)
    counts = [p["count"] for p in places]
    assert counts == sorted(counts, reverse=True)
    for p in places:
        assert -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180
        assert p["source_url"] == "" or p["source_url"].startswith(("http://", "https://"))
        assert not {"actor1", "actor2", "Actor1Name", "Actor2Name"} & set(p)


def test_route_serves_grouped_places(monkeypatch, fresh_cache, sample_rows):
    seen = []

    async def fake_fetch(max_age_slots=2):
        seen.append(max_age_slots)
        return sample_rows

    monkeypatch.setattr(intel, "fetch_gdelt_events", fake_fetch)
    monkeypatch.setattr(intel, "gdelt_status",
                        lambda: {"newest": "2026-11-19T05:15:00Z", "files": 1, "hours": 0.25,
                                 "error": ""})
    client = TestClient(app)
    body = client.get("/api/gdelt").json()
    assert seen == [4]  # 60 minutes of 15-minute files
    assert body["count"] == len(body["events"]) > 0
    assert body["raw_events"] == len(sample_rows)
    assert body["minutes"] == 60 and body["newest"] == "2026-11-19T05:15:00Z"
    assert body["source"] == "GDELT Project 2.0 event export"
    assert "error" not in body

    client.get("/api/gdelt")  # cached: no second fetch
    assert seen == [4]
    assert client.get("/api/gdelt?minutes=15").json()["minutes"] == 15
    assert seen == [4, 1]
    assert client.get("/api/gdelt?minutes=5").status_code == 422


def test_route_reports_gdelt_down(monkeypatch, fresh_cache):
    async def nothing(max_age_slots=2):
        return []

    monkeypatch.setattr(intel, "fetch_gdelt_events", nothing)
    body = TestClient(app).get("/api/gdelt?q=port").json()
    assert body == {"events": [], "count": 0, "query": "port", "error": "GDELT unavailable"}
