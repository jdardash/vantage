"""Tests for the public camera catalog (sda.cameras) and its routes (sda.routes.cameras).

Every test builds a small catalog from hand-written Argus-shaped GeoJSON in a temp
directory pointed to by SDA_CAMERA_DIR, so nothing here reads the real catalog or
touches the network. The TxDOT snapshot proxy gets a fake HTTP client.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sda import cameras
from sda.api import app
from sda.routes import cameras as camera_routes


def feature(
    cam_id: str,
    lat: float,
    lon: float,
    *,
    name: str | None = None,
    cam_type: str = "traffic",
    feed_type: str = "image",
    feed_url: str | None = None,
    stream_url: str = "",
    player_url: str = "",
    source: str = "test",
    direct: bool = True,
    rate_ms: int | None = 30000,
    country: str = "US",
    city: str = "Austin",
) -> dict:
    props = {
        "id": cam_id,
        "name": name if name is not None else f"Camera {cam_id}",
        "type": cam_type,
        "city": city,
        "country": country,
        "feedUrl": feed_url if feed_url is not None else f"https://cams.example.org/{cam_id}.jpg",
        "playerUrl": player_url,
        "streamUrl": stream_url,
        "feedType": feed_type,
        "source": source,
        "directEligible": direct,
    }
    if rate_ms is not None:
        props["updateRate"] = rate_ms
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


TXDOT_URL = (
    "https://its.txdot.gov/its/DistrictIts/GetCctvSnapshotByIcdId"
    "?icdId=ABL-%20IH20%20%40%20Arnold&districtCode=ABL"
)


def sample_features() -> list[dict]:
    return [
        # Austin: three traffic cameras, one not playable
        feature("a1", 30.2672, -97.7431),
        feature("a2", 30.2700, -97.7400, direct=False),
        feature("a3", 30.2800, -97.7500, feed_type="m3u8",
                stream_url="https://video.example.org/a3/playlist.m3u8"),
        # London landmark on YouTube, and the same webcam listed by two sources
        feature("l1", 51.5007, -0.1246, cam_type="landmark", feed_type="iframe",
                feed_url="https://www.youtube.com/embed/BMQifTcOgts", country="GB",
                city="London"),
        feature("windy_1049064406", 47.65, 9.48, cam_type="landmark", source="windy",
                feed_url="https://imgproxy.windy.com/_/preview/plain/current/1049064406/"
                         "original.jpg", country="DE", city="Friedrichshafen"),
        feature("opencctv_windy_windy-1049064406", 47.65, 9.48, cam_type="traffic",
                source="opencctv_windy", name="Friedrichshafen copy",
                feed_url="https://images-webcams.windy.com/06/1049064406/current/preview/"
                         "1049064406.jpg", country="DE", city="Friedrichshafen"),
        # Same feed URL under two ids: one copy kept
        feature("dup-a", 35.0, 139.0, feed_url="https://cams.example.org/shared.jpg",
                country="JP", city="Tokyo", direct=False),
        feature("dup-b", 35.0001, 139.0001, feed_url="https://cams.example.org/shared.jpg",
                country="JP", city="Tokyo"),
        # Either side of the antimeridian (Fiji)
        feature("fj-east", -17.8, 179.9, cam_type="beach", country="FJ", city="Suva"),
        feature("fj-west", -16.5, -179.9, cam_type="beach", country="FJ", city="Taveuni"),
        # TxDOT snapshot, MJPEG, a page-only feed, an mp4 clip
        feature("tx1", 32.45, -99.73, feed_type="txdot-json", feed_url=TXDOT_URL,
                city="Abilene"),
        feature("mj1", 23.47, 120.95, cam_type="ski", feed_type="mjpeg",
                feed_url="https://cam.example.tw/axis-cgi/mjpg/video.cgi", country="TW"),
        feature("pg1", -23.7, 133.9, cam_type="airport", feed_type="iframe",
                feed_url="https://www.alice.example.au/", country="AU",
                city="Alice Springs"),
        feature("mp1", 51.51, -0.12, feed_type="mp4",
                stream_url="https://s3.example.com/jamcams/1.mp4", country="GB",
                city="London", direct=False),
        # Unusable rows: placeholder scheme, zero island, out of range, bad iframe
        feature("bad-scheme", 40.0, -100.0, feed_url="faa-weathercam://10171"),
        feature("zero", 0.0, 0.0),
        feature("range", 95.0, 10.0),
        feature("bad-txdot", 31.0, -97.0, feed_type="txdot-json",
                feed_url="https://evil.example.com/its/DistrictIts/GetCctvSnapshotByIcdId"),
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": []},
         "properties": {}},
        "not a feature",
    ]


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(cameras.CAMERA_DIR_ENV, str(tmp_path))
    cameras.reset_catalog()
    report = cameras.build_database(
        sample_features(), tmp_path / cameras.DB_NAME, {"origin": "test", "generated": "x"}
    )
    yield tmp_path, report
    cameras.reset_catalog()


@pytest.fixture
def client(catalog_dir):
    camera_routes._snapshot_cache.clear()
    return TestClient(app)


# --- normalizing -----------------------------------------------------------


def test_classify_feed_kinds():
    assert cameras.classify_feed("image/jpeg", "https://a.org/x.jpg", "") == (
        "image", "https://a.org/x.jpg")
    assert cameras.classify_feed("m3u8", "", "https://a.org/p.m3u8") == (
        "hls", "https://a.org/p.m3u8")
    assert cameras.classify_feed("mp4", "https://a.org/c.mp4", "")[0] == "mp4"
    assert cameras.classify_feed("mjpeg", "http://a.org/v.cgi", "")[0] == "mjpeg"
    assert cameras.classify_feed("iframe", "https://www.youtube.com/embed/BMQifTcOgts", "") == (
        "youtube", "BMQifTcOgts")
    assert cameras.classify_feed("iframe", "https://a.org/page", "") == (
        "page", "https://a.org/page")
    assert cameras.classify_feed("txdot-json", TXDOT_URL, "") == ("txdot", TXDOT_URL)
    # Anything that is not plain http(s) is refused
    for url in ("javascript:alert(1)", "faa-weathercam://1", "ipcamlive://abc", "", "//x.org/a"):
        assert cameras.classify_feed("image", url, "") == ("", "")
    assert cameras.classify_feed("image", "https://a.org/x y.jpg", "") == ("", "")
    assert cameras.classify_feed("weird", "https://a.org/x", "") == ("", "")


def test_youtube_id_is_strict():
    assert cameras.youtube_id("https://www.youtube.com/embed/BMQifTcOgts") == "BMQifTcOgts"
    assert cameras.youtube_id("https://www.youtube-nocookie.com/embed/BMQifTcOgts") == (
        "BMQifTcOgts")
    assert cameras.youtube_id("http://www.youtube.com/embed/BMQifTcOgts") == ""
    assert cameras.youtube_id("https://evil.org/embed/BMQifTcOgts") == ""
    assert cameras.youtube_id("https://www.youtube.com/embed/short") == ""
    assert cameras.youtube_id("https://www.youtube.com/embed/BMQifTcOgt<") == ""
    assert cameras.youtube_id("https://www.youtube.com/watch?v=BMQifTcOgts") == ""


def test_normalize_feature_fields():
    row = cameras.normalize_feature(feature("a1", 30.2672, -97.7431, rate_ms=2000))
    assert row is not None
    assert row.group == "traffic" and row.feed_kind == "image" and row.playable
    assert row.refresh_s == cameras.MIN_REFRESH_S  # 2 s clamps up to the floor
    no_rate = cameras.normalize_feature(feature("a2", 30.0, -97.0, rate_ms=None))
    assert no_rate is not None and no_rate.refresh_s == cameras.DEFAULT_REFRESH_S
    landmark = cameras.normalize_feature(feature("l", 1.0, 1.0, cam_type="Landmark"))
    assert landmark is not None and landmark.group == "city"
    unknown = cameras.normalize_feature(feature("u", 1.0, 1.0, cam_type="zeppelin"))
    assert unknown is not None and unknown.group == cameras.OTHER_GROUP
    page = cameras.normalize_feature(feature("p", 1.0, 1.0, feed_type="iframe",
                                             feed_url="https://a.org/page"))
    assert page is not None and not page.playable and page.page_url == "https://a.org/page"
    control = cameras.normalize_feature(feature("c", 1.0, 1.0, name="bad\x00name\x07"))
    assert control is not None and control.name == "badname"


def test_excluded_sources_are_dropped(monkeypatch):
    monkeypatch.setattr(cameras, "EXCLUDED_SOURCES", frozenset({"scanner"}))
    assert cameras.normalize_feature(feature("s", 1.0, 1.0, source="scanner")) is None
    assert cameras.normalize_feature(feature("s", 1.0, 1.0, source="dot")) is not None


def test_scanned_and_private_devices_are_dropped():
    assert "opencctv_opentopia" in cameras.EXCLUDED_SOURCES
    private = [
        "http://admin:Admin12345@cam.ddns.bulsat.com/snap.jpg",
        "http://user@cams.example.org/snap.jpg",
        "http://home.myfritz.net/cam.jpg",
        "http://x.duckdns.org:8080/video.mjpg",
        "http://images.opentopia.com/cams/1/medium.jpg",
        "http://www.insecam.org/en/view/1/",
        "http://cams.example.org/cgi?usr=admin&amp;pwd=visitors",
        "http://cams.example.org/live?PASS=1234",
    ]
    for url in private:
        assert cameras.normalize_feature(feature("p", 1.0, 1.0, feed_url=url)) is None, url
    kept = "https://cams.example.org/snap.jpg?user=webcamstartseite&id=3"
    assert cameras.normalize_feature(feature("k", 1.0, 1.0, feed_url=kept)) is not None
    assert cameras.is_private_host("ddns.example.com")
    assert not cameras.is_private_host("cams.example.org")


def test_link_only_sources_are_never_played():
    row = cameras.normalize_feature(
        feature("h", 29.7, -95.4, feed_url="https://www.houstontranstar.org/snapshots/cctv/1.jpg",
                source="opencctv_houstontranstar"))
    assert row is not None
    assert row.feed_kind == "page" and row.playable is False
    assert row.page_url == "https://www.houstontranstar.org/snapshots/cctv/1.jpg"


def test_dedupe_key_matches_windy_copies():
    a = cameras.dedupe_key(
        "windy", "https://imgproxy.windy.com/_/preview/plain/current/1049064406/original.jpg")
    b = cameras.dedupe_key(
        "opencctv_windy",
        "https://images-webcams.windy.com/06/1049064406/current/preview/1049064406.jpg")
    assert a == b == "windy:1049064406"
    assert cameras.dedupe_key("dot", "https://cams.example.org/1.jpg") == ""


# --- build and query -------------------------------------------------------


def test_build_report_counts(catalog_dir):
    _, report = catalog_dir
    features = sample_features()
    assert report.read == len(features)
    assert report.dropped == 6
    assert report.copies == 2  # the windy pair and the shared feed URL
    assert report.kept == len(features) - 6 - 2


def test_dedupe_keeps_the_playable_copy(catalog_dir):
    cat = cameras.get_catalog()
    tokyo = cat.nearest(35.0, 139.0, n=10, radius_km=5, playable_only=False)
    assert len(tokyo) == 1 and tokyo[0]["playable"] is True


def test_summary(catalog_dir):
    s = cameras.get_catalog().summary()
    assert s["available"] is True
    assert s["count"] == 12
    assert s["groups"]["traffic"] >= 4
    assert s["feeds"]["youtube"] == 1 and s["feeds"]["txdot"] == 1
    assert {"code": "FJ", "count": 2} in s["countries"]
    assert s["origin"] == "test"


def test_bbox_playable_first_and_capped(catalog_dir):
    cat = cameras.get_catalog()
    rows, total = cat.in_bbox(-98.0, 30.0, -97.0, 31.0, limit=2)
    assert total == 3 and len(rows) == 2
    assert all(r["playable"] for r in rows)
    rows, total = cat.in_bbox(-98.0, 30.0, -97.0, 31.0, playable_only=True)
    assert total == 2


def test_bbox_across_antimeridian(catalog_dir):
    rows, total = cameras.get_catalog().in_bbox(179.0, -20.0, -179.0, -15.0)
    assert total == 2
    assert {r["city"] for r in rows} == {"Suva", "Taveuni"}


def test_bbox_group_filter(catalog_dir):
    cat = cameras.get_catalog()
    _, total = cat.in_bbox(-180, -90, 180, 90, groups=["beach"])
    assert total == 2
    _, none = cat.in_bbox(-180, -90, 180, 90, groups=["not-a-group"])
    assert none == 0


def test_density_sums_to_count(catalog_dir):
    cat = cameras.get_catalog()
    for cell in cameras.DENSITY_CELLS:
        cells = cat.density(-180, -90, 180, 90, cell)
        assert sum(c["count"] for c in cells) == 12
    beach = cat.density(-180, -90, 180, 90, 4.0, ["beach"])
    assert sum(c["count"] for c in beach) == 2
    fiji = cat.density(170.0, -30.0, -170.0, -10.0, 1.0)
    assert sum(c["count"] for c in fiji) == 2


def test_nearest_orders_by_distance(catalog_dir):
    rows = cameras.get_catalog().nearest(30.2672, -97.7431, n=5, radius_km=10)
    assert [r["rid"] for r in rows] == sorted(
        [r["rid"] for r in rows], key=lambda rid: next(x["distance_km"] for x in rows
                                                        if x["rid"] == rid))
    assert rows[0]["distance_km"] == 0.0
    assert all(r["playable"] for r in rows)
    everything = cameras.get_catalog().nearest(30.2672, -97.7431, n=5, radius_km=10,
                                               playable_only=False)
    assert len(everything) == 3


def test_nearest_across_antimeridian(catalog_dir):
    rows = cameras.get_catalog().nearest(-17.0, 180.0, n=5, radius_km=200)
    assert {r["city"] for r in rows} == {"Suva", "Taveuni"}


def test_search_and_random(catalog_dir):
    cat = cameras.get_catalog()
    assert {r["city"] for r in cat.search("abilene")} == {"Abilene"}
    assert cat.search("%") == []  # wildcard is escaped, and too short
    assert cat.search("1%") == []
    pick = cat.random(["beach"])
    assert pick is not None and pick["group"] == "beach"


def test_missing_catalog_reports_hint(tmp_path, monkeypatch):
    monkeypatch.setenv(cameras.CAMERA_DIR_ENV, str(tmp_path / "empty"))
    cameras.reset_catalog()
    try:
        s = cameras.get_catalog().summary()
        assert s["available"] is False and "import" in s["hint"]
        assert cameras.get_catalog().in_bbox(-180, -90, 180, 90) == ([], 0)
        assert cameras.get_catalog().density(-180, -90, 180, 90, 4) == []
    finally:
        cameras.reset_catalog()


def test_pending_catalog_is_swapped_in(catalog_dir):
    tmp, _ = catalog_dir
    cat = cameras.get_catalog()
    assert cat.summary()["count"] == 12
    # A re-import that could not replace the open file leaves <name>.new behind
    cameras.build_database([feature("only", 10.0, 10.0)], tmp / "staging.sqlite")
    (tmp / "staging.sqlite").replace(tmp / (cameras.DB_NAME + ".new"))
    cat._checked = 0.0  # skip the 10 s wait
    assert cat.summary()["count"] == 1
    assert not (tmp / (cameras.DB_NAME + ".new")).exists()


def test_import_cli_from_local_file(tmp_path, capsys):
    src = tmp_path / "cams.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "generated": "2026-07-25T22:41:20Z",
                               "features": sample_features()[:3]}), encoding="utf-8")
    db = tmp_path / "out.sqlite"
    assert cameras.main(["import", "--from", str(src), "--db", str(db)]) == 0
    assert "3 cameras kept" in capsys.readouterr().out
    assert src.exists()  # a --from file is never deleted
    assert cameras.main(["status", "--db", str(db)]) == 0
    assert "Cameras: 3" in capsys.readouterr().out
    assert cameras.read_generated(src) == "2026-07-25T22:41:20Z"


def test_camera_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv(cameras.CAMERA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cameras.camera_dir() == Path(tmp_path) / "sda" / "cameras"
    monkeypatch.setenv(cameras.CAMERA_DIR_ENV, str(tmp_path / "x"))
    assert cameras.camera_dir() == tmp_path / "x"


# --- TxDOT snapshot ---------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32


def test_decode_txdot_snapshot():
    good = {"icd_Id": "x", "snippet": base64.b64encode(JPEG).decode()}
    assert cameras.decode_txdot_snapshot(good) == JPEG
    prefixed = {"snippet": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}
    assert cameras.decode_txdot_snapshot(prefixed) == JPEG
    assert cameras.decode_txdot_snapshot({"snippet": base64.b64encode(b"<html>").decode()}) is None
    assert cameras.decode_txdot_snapshot({"snippet": ""}) is None
    assert cameras.decode_txdot_snapshot([]) is None


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.urls: list[str] = []

    async def get(self, url, headers=None):
        self.urls.append(url)
        return self.response

    async def aclose(self):
        return None


@pytest.mark.anyio
async def test_fetch_txdot_refuses_other_hosts():
    fake = FakeClient(FakeResponse(200, {"snippet": base64.b64encode(JPEG).decode()}))
    assert await cameras.fetch_txdot_snapshot("https://evil.example.com/x", fake) is None
    assert fake.urls == []
    assert await cameras.fetch_txdot_snapshot(TXDOT_URL, fake) == JPEG
    assert fake.urls == [TXDOT_URL]


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- routes -----------------------------------------------------------------


def test_route_summary_and_density(client):
    s = client.get("/api/cameras/summary").json()
    assert s["available"] is True and s["count"] == 12
    assert "traffic" in s["group_names"] and "other" in s["group_names"]
    d = client.get("/api/cameras/density?cell=3").json()
    assert d["cell_deg"] == 4.0 and d["total"] == 12


def test_route_list_and_detail(client):
    r = client.get("/api/cameras?west=-98&south=30&east=-97&north=31&limit=2").json()
    assert r["count"] == 2 and r["total"] == 3 and r["capped"] is True
    rid = r["cameras"][0]["rid"]
    d = client.get(f"/api/cameras/{rid}").json()
    assert d["rid"] == rid and d["feed_url"].startswith("https://")
    assert client.get("/api/cameras/987654").status_code == 404
    assert client.get("/api/cameras?west=-200").status_code == 422
    assert client.get("/api/cameras?limit=999999").status_code == 422


def test_route_nearest_random_search(client):
    n = client.get("/api/cameras/nearest?lat=51.5007&lon=-0.1246&n=2&radius_km=5").json()
    assert n["count"] == 1 and n["cameras"][0]["feed"] == "youtube"
    r = client.get("/api/cameras/random?groups=mountain").json()
    assert r["feed"] == "mjpeg"
    assert client.get("/api/cameras/random?groups=nothing").status_code == 404
    s = client.get("/api/cameras/search?q=Suva").json()
    assert s["count"] == 1
    assert client.get("/api/cameras/search?q=x").status_code == 422


def _rid_of(client, city: str) -> int:
    rows = client.get(f"/api/cameras/search?q={city}").json()["cameras"]
    return int(rows[0]["rid"])


def test_route_snapshot(client, monkeypatch):
    calls: list[str] = []

    async def fake_fetch(url, client=None):
        calls.append(url)
        return JPEG

    monkeypatch.setattr(camera_routes, "fetch_txdot_snapshot", fake_fetch)
    rid = _rid_of(client, "Abilene")
    r = client.get(f"/api/cameras/{rid}/snapshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == JPEG
    client.get(f"/api/cameras/{rid}/snapshot")
    assert calls == [TXDOT_URL]  # second call served from the 30 s cache
    other = _rid_of(client, "Suva")
    assert client.get(f"/api/cameras/{other}/snapshot").status_code == 400


def test_route_snapshot_upstream_failure(client, monkeypatch):
    async def fake_fetch(url, client=None):
        return None

    monkeypatch.setattr(camera_routes, "fetch_txdot_snapshot", fake_fetch)
    rid = _rid_of(client, "Abilene")
    assert client.get(f"/api/cameras/{rid}/snapshot").status_code == 502
