"""Tests for the infrastructure catalog (sda.infra) and its routes (sda.routes.infra).

Offline throughout: parsers read the small samples under tests/data/infra (see the
NOTICE there; the cable and Safecast files are synthetic), the cable refresh gets a
fake fetch that answers from those files, and the routes run on a bare FastAPI app
with only this router, pointed at a temporary SDA_INFRA_DIR.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import infra
from sda.routes.infra import router

DATA = Path(__file__).parent / "data" / "infra"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _text(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def _json(name: str) -> Any:
    return json.loads(_text(name))


def _csv(name: str) -> list[dict[str, str]]:
    return list(infra.read_csv_rows(_text(name)))


def fake_fetch(fail_details: bool = False, fail_geo: bool = False):
    """A fetch that answers the TeleGeography URLs from the synthetic samples."""
    details = _json("cable_details_sample.json")
    asked: list[str] = []

    def fetch(url: str) -> Any:
        asked.append(url)
        if url == infra.CABLE_GEO_URL:
            if fail_geo:
                raise OSError("offline")
            return _json("cable_geo_sample.json")
        if url == infra.LANDING_GEO_URL:
            return _json("landing_geo_sample.json")
        for cid, doc in details.items():
            if url == infra.CABLE_DETAIL_URL.format(id=cid):
                if fail_details:
                    raise OSError("offline")
                return doc
        raise OSError("unexpected url " + url)

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


@pytest.fixture
def infra_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SDA_INFRA_DIR", str(tmp_path))
    monkeypatch.setenv("SDA_OFFLINE", "1")
    infra.reset_catalog()
    yield tmp_path
    infra.reset_catalog()


def sample_results() -> list[infra.SourceResult]:
    return [
        infra.SourceResult(("power", "nuclear"), list(infra.parse_wri(_csv("wri_sample.csv"))),
                           "wri"),
        infra.SourceResult(("airport",),
                           list(infra.parse_ourairports(_csv("airports_sample.csv"))), "oa"),
        infra.SourceResult(("port",), list(infra.parse_wpi(
            [f["attributes"] for f in _json("wpi_arcgis_sample.json")["features"]])), "wpi"),
        infra.SourceResult(("dam",),
                           list(infra.parse_wikidata_dams(_csv("wikidata_dams_sample.csv"))),
                           "wd"),
        infra.SourceResult(("datacenter",),
                           list(infra.parse_peeringdb(_json("peeringdb_fac_sample.json"))), "pdb"),
        infra.SourceResult(("radiation",), [
            *infra.parse_bfs_odl(_json("bfs_odl_sample.json")),
            *infra.parse_safecast(_json("safecast_sample.json"), NOW),
        ], "bfs+sc"),
    ]


@pytest.fixture
def built(infra_dir: Path) -> Path:
    infra.build_catalog(sample_results(), infra_dir / infra.DB_NAME)
    infra.CableStore(infra_dir / infra.CABLES_NAME).refresh(fake_fetch(), pause_s=0)
    infra.reset_catalog()
    return infra_dir


@pytest.fixture
def client(built: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def test_wri_rows_and_nuclear_subkind() -> None:
    rows = {r.uid: r for r in infra.parse_wri(_csv("wri_sample.csv"))}
    assert len(rows) == 7  # the copy without a latitude is dropped
    grav = rows["nuclear-WRI1002735"]
    assert (grav.kind, grav.sub, grav.score) == ("nuclear", "nuclear", 5460.0)
    assert grav.brief == "5,460 MW nuclear"
    assert grav.country == "France"
    assert grav.extra["country_code"] == "FRA"
    gorges = rows["power-WRI1000452"]
    assert (gorges.kind, gorges.sub) == ("power", "hydro")
    assert gorges.extra["generation_gwh"] is not None
    assert {r.kind for r in rows.values()} == {"power", "nuclear"}


def test_ourairports_types_codes_and_liveatc() -> None:
    rows = {r.uid: r for r in infra.parse_ourairports(_csv("airports_sample.csv"))}
    assert set(rows) == {"airport-KJFK", "airport-EGLL", "airport-KAUS", "airport-KSEA",
                         "airport-00AA", "airport-00NK", "airport-07FA"}
    jfk = rows["airport-KJFK"]
    assert (jfk.sub, jfk.score) == ("large", 4500.0)
    assert jfk.brief == "Large airport, KJFK / JFK"
    assert jfk.extra["liveatc_url"] == "https://www.liveatc.net/search/?icao=KJFK"
    assert jfk.codes.split() == ["KJFK", "JFK"]
    assert rows["airport-00NK"].sub == "seaplane"
    reef = rows["airport-07FA"]  # GPS code 07FA is not an ICAO indicator
    assert reef.extra["icao"] == "" and reef.extra["liveatc_url"] == ""
    assert reef.extra["iata"] == "OCA"
    assert infra.liveatc_url("kjfk") == "https://www.liveatc.net/search/?icao=KJFK"
    assert infra.liveatc_url("K1") == ""


def test_wpi_from_nga_csv_and_arcgis_agree() -> None:
    csv_rows = {r.uid: r for r in infra.parse_wpi(_csv("wpi_nga_sample.csv"))}
    arc_rows = {r.uid: r for r in infra.parse_wpi(
        [f["attributes"] for f in _json("wpi_arcgis_sample.json")["features"]])}
    assert set(csv_rows) == set(arc_rows) == {"port-31140", "port-40"}
    rot = csv_rows["port-31140"]
    assert (rot.name, rot.sub, rot.extra["un_locode"]) == ("Rotterdam", "large", "NLRTM")
    assert rot.score == arc_rows["port-31140"].score == 4011.0
    assert csv_rows["port-40"].sub == "unknown"
    assert csv_rows["port-40"].extra["cargo_pier_depth_m"] is None  # 0 means unknown


def test_wikidata_dams_merge_and_filter() -> None:
    rows = {r.uid: r for r in infra.parse_wikidata_dams(_csv("wikidata_dams_sample.csv"))}
    assert set(rows) == {"dam-Q7366120", "dam-Q2551168", "dam-Q224717", "dam-Q806225",
                         "dam-Q172822", "dam-Q12514"}
    hoover = rows["dam-Q172822"]
    assert hoover.name == "Hoover Dam"  # only a multilingual label
    assert hoover.extra["height_m"] == 221.4
    assert hoover.extra["year"] == 1931
    assert hoover.brief == "Dam, 221.4 m high"
    assert rows["dam-Q2551168"].name == "Unnamed dam (Q2551168)"
    assert rows["dam-Q2551168"].extra["river"] == "Isar"
    assert rows["dam-Q224717"].extra["types"] == ["dam"]  # levee and causeway dropped
    assert infra.is_dam_class("ancient Roman dam")
    assert not infra.is_dam_class("weir") and not infra.is_dam_class("splash dam")
    assert infra.is_dam_class("barrage")


def test_peeringdb_bfs_and_safecast() -> None:
    dcs = {r.uid: r for r in infra.parse_peeringdb(_json("peeringdb_fac_sample.json"))}
    assert set(dcs) == {"datacenter-1", "datacenter-2", "datacenter-18"}
    ash = dcs["datacenter-1"]
    assert ash.extra["peeringdb_url"] == "https://www.peeringdb.com/fac/1"
    assert ash.brief.startswith(f"{ash.extra['networks']} networks")

    bfs = {r.uid: r for r in infra.parse_bfs_odl(_json("bfs_odl_sample.json"))}
    assert len(bfs) == 4
    assert bfs["radiation-DEZ2524"].brief == "0.109 uSv/h"
    assert bfs["radiation-DEZ3051"].brief == "no reading, out of service"

    sc = list(infra.parse_safecast(_json("safecast_sample.json"), NOW))
    assert [r.uid for r in sc] == ["radiation-sc1001", "radiation-sc1004"]
    namie = sc[0]
    assert (namie.lat, namie.lon) == (37.47, 140.94)
    assert namie.extra["dose_rate_usvh_approx"] == pytest.approx(0.12, abs=1e-3)
    assert sc[1].brief == "200 cpm" and sc[1].extra["dose_rate_usvh_approx"] is None
    assert (sc[1].lat, sc[1].lon) == (34.12, -118.99)
    dumped = json.dumps([r.__dict__ for r in sc])
    for secret in ("Test Person", "person@example", "Example Org", "example.invalid"):
        assert secret not in dumped


def test_parsers_ignore_junk() -> None:
    assert list(infra.parse_peeringdb({"data": "x"})) == []
    assert list(infra.parse_peeringdb([1, 2])) == []
    assert list(infra.parse_bfs_odl({"features": [None, {"geometry": None}]})) == []
    assert list(infra.parse_safecast({"not": "a list"})) == []
    assert infra.http_url("javascript:alert(1)") == ""
    assert infra.http_url("https://example.com/x") == "https://example.com/x"
    assert infra.norm_key("UN/LOCODE") == infra.norm_key("UN_LOCODE") == "un_locode"
    assert infra.norm_key("Channel Depth (m)") == infra.norm_key("Channel_Depth__m_")


# --------------------------------------------------------------------------
# Cables
# --------------------------------------------------------------------------


def test_densify_follows_rhumb_and_splits_at_antimeridian() -> None:
    lines = infra.densify_rhumb([(-9.0, 52.0), (-39.0, 52.0)])
    assert len(lines) == 1 and len(lines[0]) == 31
    assert lines[0][0] == (-9.0, 52.0) and lines[0][-1] == (-39.0, 52.0)
    assert all(lat == pytest.approx(52.0) for _, lat in lines[0])
    split = infra.densify_rhumb([(170.0, -16.0), (179.5, -16.0), (-179.5, -16.5), (-170.0, -17.0)])
    assert len(split) == 2
    assert all(abs(lon) <= 180 for line in split for lon, _ in line)


def test_cable_refresh_builds_dependence(infra_dir: Path) -> None:
    store = infra.CableStore(infra_dir / infra.CABLES_NAME)
    fetch = fake_fetch()
    st = store.refresh(fetch, pause_s=0)
    assert (st["count"], st["landing_points"], st["available"]) == (3, 5, True)
    data = store.data()
    assert data is not None
    assert data["country_cables"] == {"Congo, Dem. Rep.": 1, "France": 2, "Ireland": 2}
    gamma = store.cable("gamma-express")
    assert gamma is not None
    assert gamma["color"] == infra.DEFAULT_CABLE_COLOR  # unsafe colour replaced
    assert gamma["countries"] == ["Congo, Dem. Rep.", "France"]
    assert gamma["countries_detail"][0] == {"country": "Congo, Dem. Rep.", "landings": 1,
                                            "cables_total": 1, "other_cables": 0}
    assert gamma["countries_detail"][1]["other_cables"] == 1
    assert gamma["is_planned"] is True and gamma["url"] == ""
    beta = store.cable("beta-cable")
    assert beta is not None and beta["url"] == ""  # javascript: link dropped
    assert beta["countries_detail"] == [{"country": "Ireland", "landings": 2, "cables_total": 2,
                                        "other_cables": 1}]
    alpha_lines = next(c for c in data["cables"] if c["id"] == "alpha-link")["lines"]
    assert len(alpha_lines) == 2 and len(alpha_lines[0]) > 20
    orphan = store.landing("orphan-point-iceland")
    assert orphan is not None and orphan["cables"] == []
    lands = {r.uid: r for r in store.landing_rows()}
    assert lands["landing-orphan-point-iceland"].country == "Iceland"  # from the name
    assert lands["landing-alpha-town-ireland"].score == 2.0
    geo = store.geojson()
    assert geo["available"] and len(geo["features"]) == 3
    assert store.cable("Bad Id!") is None and store.cable("missing") is None


def test_cable_refresh_reuses_details_and_keeps_old_file(infra_dir: Path) -> None:
    store = infra.CableStore(infra_dir / infra.CABLES_NAME)
    store.refresh(fake_fetch(), pause_s=0)
    before = store.path.read_bytes()
    again = fake_fetch(fail_details=True)
    store.refresh(again, pause_s=0)
    assert again.asked == [infra.CABLE_GEO_URL, infra.LANDING_GEO_URL]  # details reused
    gamma = store.cable("gamma-express")
    assert gamma is not None and gamma["countries"] == ["Congo, Dem. Rep.", "France"]
    with pytest.raises(OSError):
        store.refresh(fake_fetch(fail_geo=True), pause_s=0)
    assert store.status()["available"]
    assert json.loads(store.path.read_bytes())["cables"] == json.loads(before)["cables"]


def test_cable_background_refresh(infra_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = infra.CableStore(infra_dir / infra.CABLES_NAME)
    assert store.ensure_fresh(fake_fetch()) is False  # SDA_OFFLINE=1
    monkeypatch.delenv("SDA_OFFLINE")
    assert store.ensure_fresh(fake_fetch(fail_geo=True)) is True
    assert store._thread is not None
    store._thread.join(5)
    st = store.status()
    assert not st["available"] and "OSError" in st["error"] and not st["loading"]
    assert store.ensure_fresh(fake_fetch()) is False  # waits CABLE_RETRY_S after a failure
    store._last_attempt = 0.0
    assert store.ensure_fresh(fake_fetch()) is True
    store._thread.join(5)
    assert store.status()["available"] and store.status()["error"] == ""
    assert store.ensure_fresh(fake_fetch()) is False  # fresh now


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_allocate_shares_the_limit() -> None:
    assert infra.allocate({"a": 100, "b": 100}, 10) == {"a": 5, "b": 5}
    assert infra.allocate({"a": 2, "b": 100, "c": 0}, 10) == {"a": 2, "b": 8, "c": 0}
    assert infra.allocate({"a": 3, "b": 3}, 100) == {"a": 3, "b": 3}
    assert sum(infra.allocate({"a": 7, "b": 9, "c": 11}, 10).values()) == 10
    assert infra.allocate({}, 10) == {}
    assert infra.parse_kinds(None) == list(infra.KINDS)
    assert infra.parse_kinds("dam, power,bogus") == ["power", "dam"]


def test_build_and_query_catalog(built: Path) -> None:
    cat = infra.get_catalog()
    s = cat.summary()
    assert s["available"]
    counts = {k: v["count"] for k, v in s["kinds"].items()}
    assert counts == {"power": 6, "nuclear": 1, "airport": 7, "port": 2, "dam": 6,
                      "datacenter": 3, "radiation": 6}
    assert s["kinds"]["nuclear"]["licence"] == "CC BY 4.0"
    whole = cat.counts_in(["airport", "dam"], -180, -90, 180, 90)
    assert whole == {"airport": 7, "dam": 6}
    # New York area, small box (R*Tree path)
    ny = cat.counts_in(["airport"], -75, 40, -73, 41)
    assert ny == {"airport": 1}
    top = cat.top_in("airport", 3, -180, -90, 180, 90)
    assert [t["sub"] for t in top] == ["large", "large", "large"]
    # A box across the antimeridian holds nothing here but must not fail
    assert cat.counts_in(["airport"], 170, -50, -170, 50) == {"airport": 0}
    jfk = cat.get("airport-KJFK")
    assert jfk is not None and jfk["fields"]["icao"] == "KJFK"
    assert [r["id"] for r in cat.search("KJFK", ["airport"])][0] == "airport-KJFK"
    assert [r["id"] for r in cat.search("rotter", ["port"])] == ["port-31140"]
    assert cat.search("x", ["port"]) == []


def test_query_items_shares_limit_and_includes_landings(built: Path) -> None:
    got = infra.query_items(-180, -90, 180, 90, ["airport", "dam", "landing"], 6)
    assert got["counts"] == {"airport": 7, "dam": 6, "landing": 5}
    assert got["count"] == 6 and got["capped"]
    assert sorted(i["kind"] for i in got["items"]) == ["airport"] * 2 + ["dam"] * 2 + \
        ["landing"] * 2
    europe = infra.query_items(-25, 35, 30, 70, ["landing"], 100)
    assert {i["id"] for i in europe["items"]} == {
        "landing-alpha-town-ireland", "landing-beta-bay-france", "landing-delta-cove-ireland",
        "landing-orphan-point-iceland"}


def test_failed_step_keeps_previous_rows(built: Path) -> None:
    dest = built / infra.DB_NAME
    results = sample_results()
    results[1] = infra.SourceResult(("airport",), None, error="HTTPError")
    results[2] = infra.SourceResult(("port",), None, skipped=True)
    infra.reset_catalog()
    report = infra.build_catalog(results, dest)
    assert report.counts["airport"] == 7 and report.counts["port"] == 2
    infra.reset_catalog()
    s = infra.get_catalog().summary()
    assert s["kinds"]["airport"]["status"].startswith("failed (HTTPError); kept 7 rows")
    assert s["kinds"]["port"]["status"] == "ok"
    conn = sqlite3.connect(dest)
    try:
        n_index = conn.execute("SELECT count(*) FROM item_index").fetchone()[0]
        n_items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    assert n_index == n_items == 31


def test_cli_status_and_import(infra_dir: Path, monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    assert infra.main(["status"]) == 1
    results = {r.kinds[0]: r for r in sample_results()}

    def step_for(name: str):
        return lambda work, fresh: results[name]

    monkeypatch.setattr(infra, "STEPS", {n: step_for(n) for n in infra.STEPS})
    monkeypatch.setattr(infra, "http_json", fake_fetch())
    monkeypatch.setattr(infra, "CABLE_PAUSE_S", 0.0)
    assert infra.main(["import"]) == 0
    out = capsys.readouterr().out
    assert "[cables] 3 cables, 5 landing points" in out
    assert "airport           7" in out
    assert infra.main(["status"]) == 0
    assert infra.main(["import", "--only", "nonsense"]) == 2


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_routes_without_catalog(infra_dir: Path) -> None:
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    s = c.get("/api/infra/summary").json()
    assert s["available"] is False and "python -m sda.infra import" in s["hint"]
    assert s["kinds"]["airport"]["status"] == "not imported"
    r = c.get("/api/infra/items")
    assert r.status_code == 503 and "python -m sda.infra import" in r.json()["detail"]
    geo = c.get("/api/infra/cables")
    assert geo.status_code == 200 and geo.json()["features"] == []
    assert geo.headers["cache-control"] == "no-store"


def test_routes_summary_items_detail(client: TestClient) -> None:
    s = client.get("/api/infra/summary").json()
    assert s["available"] and s["kinds"]["landing"]["count"] == 5
    assert s["cables"]["count"] == 3 and "TeleGeography" in s["attribution"]
    r = client.get("/api/infra/items", params={"bbox": "-80,35,-70,45",
                                               "kinds": "airport,nuclear", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"nuclear": 0, "airport": 2}
    assert {i["id"] for i in body["items"]} == {"airport-KJFK", "airport-00NK"}
    assert client.get("/api/infra/items", params={"bbox": "1,2,3"}).status_code == 422
    assert client.get("/api/infra/items", params={"kinds": "bogus"}).status_code == 422
    assert client.get("/api/infra/items", params={"limit": 0}).status_code == 422

    d = client.get("/api/infra/item/airport-KJFK").json()
    assert d["label"] == "Airports" and d["source"]["licence"] == "Public domain"
    assert d["links"][0] == {"label": "LiveATC feeds for KJFK",
                             "url": "https://www.liveatc.net/search/?icao=KJFK"}
    dam = client.get("/api/infra/item/dam-Q172822").json()
    assert dam["links"] == [{"label": "Wikidata", "url": "https://www.wikidata.org/wiki/Q172822"}]
    land = client.get("/api/infra/item/landing-alpha-town-ireland").json()
    assert land["fields"]["country_cables"] == 2
    assert [c["name"] for c in land["fields"]["cables"]] == ["Alpha Link", "Beta Cable"]
    assert land["links"][0]["url"].endswith("/landing-point/alpha-town-ireland")
    assert client.get("/api/infra/item/airport-NOPE").status_code == 404
    assert client.get("/api/infra/item/..%2Fx").status_code == 404

    found = client.get("/api/infra/search", params={"q": "alpha", "kinds": "landing"}).json()
    assert [i["id"] for i in found["items"]] == ["landing-alpha-town-ireland"]
    assert client.get("/api/infra/search", params={"q": "jfk"}).json()["items"][0]["id"] == \
        "airport-KJFK"


def test_routes_cables(client: TestClient) -> None:
    r = client.get("/api/infra/cables")
    assert r.status_code == 200 and r.headers["cache-control"].startswith("public")
    geo = r.json()
    assert geo["type"] == "FeatureCollection" and len(geo["features"]) == 3
    props = {f["id"]: f["properties"] for f in geo["features"]}
    assert props["alpha-link"]["countries"] == ["France", "Ireland"]
    one = client.get("/api/infra/cables/gamma-express").json()
    assert one["countries_detail"][0]["other_cables"] == 0
    assert one["map_url"] == "https://www.submarinecablemap.com/submarine-cable/gamma-express"
    assert client.get("/api/infra/cables/missing").status_code == 404
    assert client.get("/api/infra/cables/Bad_Id").status_code == 404
