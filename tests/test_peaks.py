"""Tests for the worldwide peak catalog (sda.peaks) and its routes (sda.routes.peaks).

Every input is a small fixture written here in the formats of the real sources:
the Kirmse prominence list (comma-separated, metres), GeoNames allCountries and
admin1 (tab-separated), and the Kirmse isolation list (elevation in feet). Nothing
touches the network. The routes are mounted on a bare FastAPI app, since the
router is wired into sda.api separately.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import peaks
from sda.routes.peaks import router as peaks_router

# Kirmse rows: lat, lon, elevation m, key saddle lat, key saddle lon, prominence m.
# Ranked by prominence these become rid 1..9; the GeoNames-only hill becomes rid 10.
SUMMITS = """\
27.9892,86.9256,8737.79,27.0,86.0,8737.79
46.8523,-121.7603,4380.0,45.0,-120.0,4027.0
36.5785,-118.2923,4417.0,36.0,-117.0,3072.0
45.9763,7.6586,4470.0,45.9,7.6,1040.0
10.0,20.0,1500.0,10.5,20.5,500.0
51.0,179.9,500.0,51.0,179.0,300.0
51.0,-179.9,450.0,51.0,-179.0,250.0
36.5650,-118.2900,4200.0,36.5,-118.29,150.0
36.6000,-118.3000,3000.0,36.61,-118.31,40.0
bad,line
"""

EVEREST, RAINIER, WHITNEY, MATTERHORN, FAR, EAST, WEST, KNOB, TINY, LONE = range(1, 11)


def _gn(gid, name, alts, lat, lon, cls, code, cc, admin1, elev, dem):
    cols = [str(gid), name, name, alts, str(lat), str(lon), cls, code, cc, "", admin1,
            "", "", "", "0", str(elev), str(dem), "UTC", "2024-01-01"]
    return "\t".join(cols)


GEONAMES = "\n".join([
    _gn(1283416, "Mount Everest", "Chomolungma,Sagarmatha,Qomolangma Feng",
        27.98806, 86.92528, "T", "PK", "NP", "01", 8848, 8724),
    # The same mountain twice: the peak entry names it, the mountain entry is a duplicate.
    _gn(5370001, "Mount Whitney", "Whitney,Mt. Whitney", 36.5790, -118.2925, "T", "PK", "US",
        "CA", 4421, 4390),
    _gn(5370000, "Mount Whitney", "", 36.5785, -118.2920, "T", "MT", "US", "CA", 4418, 4390),
    _gn(2659000, "Matterhorn", "Cervino,Mont Cervin,C\u00e9rvino", 45.9764, 7.6585, "T", "PK",
        "CH", "VS", 4478, 4440),
    # Surveyed 200 m off the DEM summit: named, but the DEM height stays.
    _gn(9001, "Far Survey", "", 10.001, 20.001, "T", "PK", "TD", "", 1700, 1490),
    # No survey height: takes the highest free summit in range.
    _gn(9006, "Neighbour Knob", "", 36.5652, -118.2902, "T", "NTK", "US", "CA", "", 4150),
    # No Kirmse summit near it: kept as its own row, the second copy dropped.
    _gn(9002, "Lone Hill", "", 5.0, 5.0, "T", "HLL", "NG", "05", 0, 312),
    _gn(9003, "Lone Hill", "", 5.0, 5.0, "T", "HLL", "NG", "05", 0, 312),
    _gn(9004, "Some Range", "", 30.0, 30.0, "T", "MTS", "EG", "", 900, 880),
    _gn(9005, "Some Town", "", 36.5785, -118.2923, "P", "PPL", "US", "CA", 4000, 4000),
]) + "\n"

ADMIN1 = "US.CA\tCalifornia\tCalifornia\t5332921\nNP.01\tBagmati\tBagmati\t1\n" \
         "CH.VS\tValais\tValais\t2\nNG.05\tBauchi\tBauchi\t3\n"

# Kirmse isolation rows: lat, lon, elevation FEET, ILP lat, ILP lon, isolation km.
ISOLATION = """\
36.5786,-118.2924,14491,36.0,-117.0,2649.4
36.5785,-118.2923,14490,36.0,-117.0,100.0
27.9891,86.9257,28666,0,0,40000
45.9763,7.6586,10000,45.0,7.0,50.0
0.0,0.0,100,0,0,5
"""


def _write_sources(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = {
        "summits": folder / "all-peaks-sorted-p100.txt",
        "names": folder / "allCountries.txt",
        "admin1": folder / "admin1CodesASCII.txt",
        "isolation": folder / "alliso-sorted.txt",
    }
    out["summits"].write_text(SUMMITS, encoding="utf-8")
    out["names"].write_text(GEONAMES, encoding="utf-8")
    out["admin1"].write_text(ADMIN1, encoding="utf-8")
    out["isolation"].write_text(ISOLATION, encoding="utf-8")
    return out


def _build(folder: Path, dest: Path, **kw) -> peaks.BuildReport:
    src = _write_sources(folder)
    return peaks.build_catalog(
        src["summits"], dest, names=src["names"], admin1=src["admin1"],
        isolation=src["isolation"], **kw,
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Iterator[tuple[peaks.PeakCatalog, peaks.BuildReport, Path]]:
    work = tmp_path_factory.mktemp("peaks")
    dest = work / "db" / peaks.DB_NAME
    report = _build(work / "src", dest)
    cat = peaks.PeakCatalog(dest)
    yield cat, report, dest
    cat.close()


@pytest.fixture()
def cat(built) -> peaks.PeakCatalog:
    return built[0]


def ids(rows) -> list[int]:
    return [r["id"] for r in rows]


# --- text and parsing --------------------------------------------------------


def test_fold_and_search_keys():
    assert peaks.fold("C\u00e9rvino") == "cervino"
    assert peaks.fold("Pic d'Aneto") == "pic d aneto"
    assert peaks.fold("  Mt.   Whitney ") == "mt whitney"
    assert peaks.search_keys("Mount Whitney") == ["mount whitney", "whitney"]
    assert peaks.search_keys("K2", "k2") == ["k2"]
    assert peaks.search_keys("x") == []


def test_parse_geonames_line():
    codes = peaks.NAME_CODES
    line = GEONAMES.splitlines()[0].encode("utf-8")
    row = peaks.parse_geonames_line(line, codes)
    assert row is not None
    assert (row.gid, row.name, row.code, row.country, row.admin1) == (
        1283416, "Mount Everest", "PK", "NP", "01")
    assert row.survey_m == 8848 and row.dem_m == 8724
    assert row.alts == ("Chomolungma", "Sagarmatha", "Qomolangma Feng")
    lone = peaks.parse_geonames_line(GEONAMES.splitlines()[6].encode("utf-8"), codes)
    assert lone is not None and lone.survey_m is None and lone.height == 312
    # a range, a town and a short line are not summits
    for raw in (GEONAMES.splitlines()[8], GEONAMES.splitlines()[9], "1\tX\tT\tPK"):
        assert peaks.parse_geonames_line(raw.encode("utf-8"), codes) is None


def test_read_prominence_from_a_zip(tmp_path):
    inner = tmp_path / "all-peaks-sorted-p100.txt"
    inner.write_text(SUMMITS, encoding="utf-8")
    archive = tmp_path / "all-peaks.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "x")  # the smaller member is ignored
        zf.write(inner, inner.name)
    summits, read, dropped = peaks.read_prominence(archive, min_prom_m=100)
    assert read == 10 and dropped == 2  # the bad line and the 40 m summit
    assert summits.lat.size == 8
    assert summits.prom.min() >= 100


# --- build -------------------------------------------------------------------------


def test_build_report(built):
    _cat, report, _dest = built
    assert report.summits_read == 10 and report.summits_dropped == 1
    assert report.name_features == 8  # the range and the town are not read
    assert report.named == 5
    assert report.names_added == 1
    assert report.name_duplicates == 2
    assert report.isolation_read == 5 and report.isolation_matched == 3
    assert report.kept == 10 and report.with_prominence == 9
    assert report.countries == 5


def test_summary(cat):
    s = cat.summary()
    assert s["available"] is True
    assert s["count"] == 10
    assert s["named"] == 6
    assert s["with_prominence"] == 9
    assert s["with_isolation"] == 2
    assert s["geonames_only"] == 1
    assert s["country_count"] == 5
    assert "GeoNames" in s["attribution"] and "Kirmse" in s["attribution"]
    assert s["imported_at"].endswith("Z")


def test_rank_is_prominence_then_elevation(cat):
    rows, total, capped = cat.in_bbox(-180, -90, 180, 90, limit=300)
    assert ids(rows) == list(range(1, 11))
    assert total == 10 and capped is False
    proms = [r["prom_m"] for r in rows[:9]]
    assert proms == sorted(proms, reverse=True)
    assert rows[9]["prom_m"] is None


def test_names_and_elevation_sources(cat):
    whitney = cat.get(WHITNEY)
    assert whitney["name"] == "Mount Whitney"
    assert whitney["geonames_id"] == 5370001  # the peak entry, not its duplicate
    assert whitney["elev_m"] == 4421.0 and whitney["elev_source"] == "geonames"
    assert whitney["dem_m"] == 4417.0
    assert whitney["prom_m"] == 3072.0
    assert whitney["iso_km"] == 2649.4  # the larger of its two isolation rows
    assert (whitney["country"], whitney["region"]) == ("US", "California")
    assert whitney["source"] == "kirmse+geonames"

    assert cat.get(EVEREST)["elev_m"] == 8848.0
    far = cat.get(FAR)
    assert far["name"] == "Far Survey"
    assert far["elev_m"] == 1500.0 and far["elev_source"] == "glo30-dem"
    assert far["dem_m"] is None
    assert cat.get(KNOB)["name"] == "Neighbour Knob"  # highest free summit near it
    assert cat.get(TINY)["name"] == ""
    assert cat.get(MATTERHORN)["iso_km"] is None  # isolation row 400 m too low

    rainier = cat.get(RAINIER)
    assert rainier["name"] == "" and rainier["source"] == "kirmse"

    lone = cat.get(LONE)
    assert lone["name"] == "Lone Hill" and lone["source"] == "geonames"
    assert lone["prom_m"] is None
    assert lone["elev_m"] == 312.0 and lone["elev_source"] == "geonames-dem"
    assert lone["region"] == "Bauchi"
    assert cat.get(999) is None


# --- queries ------------------------------------------------------------------


def test_in_bbox_scan_and_index_agree(cat):
    # limit 2 over the whole Earth walks the ranked table; limit 300 uses the R*Tree
    few, total_few, _ = cat.in_bbox(-180, -90, 180, 90, limit=2)
    many, total_many, _ = cat.in_bbox(-180, -90, 180, 90, limit=300)
    assert ids(few) == ids(many)[:2] == [EVEREST, RAINIER]
    assert total_few == total_many == 10
    sierra, total, _ = cat.in_bbox(-119, 36, -118, 37)
    assert ids(sierra) == [WHITNEY, KNOB, TINY] and total == 3
    one, total, _ = cat.in_bbox(-119, 36, -118, 37, limit=1)
    assert ids(one) == [WHITNEY] and total == 3


def test_in_bbox_across_the_antimeridian(cat):
    rows, total, _ = cat.in_bbox(179, 50, -179, 52)
    assert ids(rows) == [EAST, WEST] and total == 2


def test_min_prominence(cat):
    rows, total, _ = cat.in_bbox(-180, -90, 180, 90, min_prom_m=1000)
    assert ids(rows) == [EVEREST, RAINIER, WHITNEY, MATTERHORN] and total == 4
    rows, total, _ = cat.in_bbox(-119, 36, -118, 37, min_prom_m=100)
    assert ids(rows) == [WHITNEY, KNOB] and total == 2
    assert cat.in_bbox(-180, -90, 180, 90, min_prom_m=1e5) == ([], 0, False)


def test_in_bbox_total_stops_at_the_cap(cat, monkeypatch):
    monkeypatch.setattr(peaks, "COUNT_CAP", 2)
    # the cells wholly inside the box already hold the cap: no count is run
    rows, total, capped = cat.in_bbox(-180, -90, 180, 90, limit=300)
    assert len(rows) == 10 and (total, capped) == (2, True)
    # a box inside one cell has no whole cell, so the index counts up to the cap
    rows, total, capped = cat.in_bbox(-118.35, 36.53, -118.25, 36.63)
    assert ids(rows) == [WHITNEY, KNOB, TINY] and (total, capped) == (2, True)
    # with a prominence floor the ranked rows are counted, also up to the cap
    rows, total, capped = cat.in_bbox(-180, -90, 180, 90, min_prom_m=1000)
    assert len(rows) == 4 and (total, capped) == (2, True)


def test_the_highest_name_claims_the_summit_first():
    """K2 (MT) beat Chogri Peak (PK) to K2's summit only once height came first."""
    summits = peaks.SummitArrays(
        lat=np.array([35.8800, 35.8790]), lon=np.array([76.5150, 76.5175]),
        elev=np.array([8611.0, 8570.0]), prom=np.array([4020.0, 50.0]),
    )

    def row(gid, name, code, lat, lon, survey):
        return peaks.NameRow(gid, name, name, (), lat, lon, code, "PK", "", survey, None)

    match = peaks.match_names(summits, [
        row(1, "Chogri Peak", "PK", 35.8805, 76.5160, 8600.0),
        row(2, "K2", "MT", 35.8808, 76.5155, 8611.0),
    ])
    assert match.named[0].name == "K2"
    assert match.named[1].name == "Chogri Peak"
    assert match.extra == [] and match.duplicates == 0


def test_a_survey_never_names_a_summit_far_below_it():
    """A shoulder whose own top is not in the list stays its own row."""
    summits = peaks.SummitArrays(
        lat=np.array([45.9763, 45.9640]), lon=np.array([7.6586, 7.6450]),
        elev=np.array([4470.0, 3287.0]), prom=np.array([1040.0, 36.0]),
    )

    def row(gid, name, lat, lon, survey):
        return peaks.NameRow(gid, name, name, (), lat, lon, "PK", "IT", "", survey, None)

    match = peaks.match_names(summits, [
        row(1, "Matterhorn", 45.9764, 7.6585, 4478.0),
        row(2, "Pic Tyndall", 45.9750, 7.6560, 4241.0),  # 954 m above the free summit
        row(3, "Testa del Leone", 45.9645, 7.6455, 3500.0),  # 213 m: close enough
    ])
    assert match.named[0].name == "Matterhorn"
    assert match.named[1].name == "Testa del Leone"
    assert [r.name for r in match.extra] == ["Pic Tyndall"]
    assert match.height_misses == 1


def test_density(cat):
    cell, cells = cat.density(-180, -90, 180, 90, 16.0)
    assert cell == 16.0
    assert sum(c["count"] for c in cells) == 10
    sierra = next(c for c in cells if c["count"] == 3)
    assert sierra["top"]["id"] == WHITNEY  # highest summit in the cell
    assert 36.5 < sierra["lat"] < 36.6
    # a fine cell over the whole Earth is swapped for one that keeps under the cap
    cell, _ = cat.density(-180, -90, 180, 90, 0.25)
    assert cell == 2.0
    cell, cells = cat.density(-119, 36, -118, 37, 0.25)
    assert cell == 0.25 and sum(c["count"] for c in cells) == 3
    assert peaks.cell_for_zoom(0) == 16.0 and peaks.cell_for_zoom(6) == 0.25
    assert peaks.cell_for_zoom(3.3) == 2.0


def test_search(cat):
    assert ids(cat.search("whit")) == [WHITNEY]
    assert ids(cat.search("MOUNT")) == [EVEREST, WHITNEY]
    assert ids(cat.search("sagarmatha")) == [EVEREST]
    assert ids(cat.search("c\u00e9rv")) == [MATTERHORN]
    assert ids(cat.search("mont cervin")) == [MATTERHORN]
    assert ids(cat.search("knob")) == [KNOB]
    assert cat.search("x") == []
    assert cat.search("zzzz") == []


def test_nearest(cat):
    rows = cat.nearest(36.57, -118.29, n=3)
    assert ids(rows) == [KNOB, WHITNEY, TINY]
    assert rows[0]["distance_km"] < rows[1]["distance_km"] < rows[2]["distance_km"]
    assert ids(cat.nearest(36.57, -118.29, n=5, radius_km=0.8)) == [KNOB]
    assert ids(cat.nearest(36.57, -118.29, n=5, min_prom_m=1000)) == [WHITNEY]
    assert ids(cat.nearest(51.0, 179.95, n=5, radius_km=20)) == [EAST, WEST]


# --- the file swap, the command and the default directory -----------------------------


def test_reimport_swaps_in_when_the_file_is_held(tmp_path, monkeypatch):
    dest = tmp_path / "db" / peaks.DB_NAME
    _build(tmp_path / "a", dest)
    cat = peaks.PeakCatalog(dest)
    try:
        assert cat.summary()["count"] == 10
        real = peaks.os.replace

        def held(src, dst):
            if str(src).endswith(".tmp") and Path(dst) == dest:
                raise PermissionError("held open")
            return real(src, dst)

        monkeypatch.setattr(peaks.os, "replace", held)
        src = _write_sources(tmp_path / "b")
        peaks.build_catalog(src["summits"], dest, min_prom_m=1000)
        pending = dest.with_name(dest.name + ".new")
        assert pending.exists()
        assert cat.summary()["count"] == 10  # still the old file until the next check
        cat._checked = 0.0
        assert cat.summary()["count"] == 4
        assert not pending.exists()
    finally:
        cat.close()


def test_import_command_from_a_folder(tmp_path, capsys):
    folder = tmp_path / "src"
    src = _write_sources(folder)
    with zipfile.ZipFile(folder / "all-peaks-p100-2023.zip", "w") as zf:
        zf.write(src["summits"], src["summits"].name)
    src["summits"].unlink()
    dest = tmp_path / "out" / peaks.DB_NAME
    assert peaks.main(["import", "--from", str(folder), "--db", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "Done in" in out and "10 peaks" in out
    assert out.isascii()
    assert peaks.main(["status", "--db", str(dest)]) == 0
    status = capsys.readouterr().out
    assert "Peaks: 10" in status and "all-peaks-p100-2023.zip" in status
    assert peaks.main(["import", "--from", str(folder), "--db", str(dest), "--no-names",
                       "--no-isolation"]) == 0
    assert "9 peaks" in capsys.readouterr().out
    assert peaks.main(["status", "--db", str(tmp_path / "none.sqlite")]) == 1


def test_import_command_from_a_url(tmp_path, monkeypatch, capsys):
    src = _write_sources(tmp_path / "src")
    fetched: list[str] = []

    def fake_download(url: str, target: Path) -> None:
        fetched.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src["summits"].read_bytes())

    monkeypatch.setattr(peaks, "download", fake_download)
    dest = tmp_path / "out" / peaks.DB_NAME
    url = "https://example.org/all-peaks.zip"
    assert peaks.main(["import", "--from", url, "--db", str(dest)]) == 0
    assert fetched == [url]
    assert "9 peaks" in capsys.readouterr().out
    assert not (dest.parent / "download" / "all-peaks-p100.zip").exists()  # not kept


def test_peak_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(peaks.PEAK_DIR_ENV, str(tmp_path / "x"))
    assert peaks.peak_dir() == tmp_path / "x"
    assert peaks.db_path() == tmp_path / "x" / peaks.DB_NAME
    monkeypatch.delenv(peaks.PEAK_DIR_ENV)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert peaks.peak_dir() == tmp_path / "local" / "sda" / "peaks"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert peaks.peak_dir() == tmp_path / "cache" / "sda" / "peaks"


def test_missing_catalog(tmp_path):
    cat = peaks.PeakCatalog(tmp_path / "absent.sqlite")
    assert cat.available is False
    assert cat.summary() == {"available": False, "count": 0, "hint": peaks.IMPORT_HINT}
    assert cat.in_bbox(-180, -90, 180, 90) == ([], 0, False)
    assert cat.density(-180, -90, 180, 90, 16) == (16.0, [])
    assert cat.search("whitney") == [] and cat.nearest(0, 0) == [] and cat.get(1) is None


# --- routes ---------------------------------------------------------------------


@pytest.fixture()
def client(built, monkeypatch) -> Iterator[TestClient]:
    _cat, _report, dest = built
    monkeypatch.setenv(peaks.PEAK_DIR_ENV, str(dest.parent))
    peaks.reset_catalog()
    app = FastAPI()
    app.include_router(peaks_router)
    with TestClient(app) as c:
        yield c
    peaks.reset_catalog()


@pytest.fixture()
def empty_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv(peaks.PEAK_DIR_ENV, str(tmp_path / "empty"))
    peaks.reset_catalog()
    app = FastAPI()
    app.include_router(peaks_router)
    with TestClient(app) as c:
        yield c
    peaks.reset_catalog()


def test_route_summary(client):
    body = client.get("/api/peaks/summary").json()
    assert body["available"] is True and body["count"] == 10
    assert body["named"] == 6 and body["country_count"] == 5


def test_route_list(client):
    body = client.get("/api/peaks", params={"bbox": "-119,36,-118,37"}).json()
    assert ids(body["peaks"]) == [WHITNEY, KNOB, TINY]
    assert body["count"] == 3 and body["total"] == 3 and body["capped"] is False
    assert body["peaks"][0]["name"] == "Mount Whitney"
    body = client.get("/api/peaks", params={"limit": 2}).json()
    assert ids(body["peaks"]) == [EVEREST, RAINIER]
    assert body["capped"] is True and body["total"] == 10 and body["total_capped"] is False
    body = client.get("/api/peaks", params={"min_prom": 1000}).json()
    assert body["count"] == 4
    body = client.get("/api/peaks", params={"bbox": "179,50,-179,52"}).json()
    assert ids(body["peaks"]) == [EAST, WEST]


@pytest.mark.parametrize("bbox", ["1,2,3", "a,b,c,d", "-200,0,0,10", "0,-95,10,10", "nan,0,1,1"])
def test_route_bad_bbox(client, bbox):
    assert client.get("/api/peaks", params={"bbox": bbox}).status_code == 422


def test_route_density(client):
    body = client.get("/api/peaks/density", params={"zoom": 0}).json()
    assert body["cell_deg"] == 16.0 and body["total"] == 10
    assert all("top" in c and c["top"]["id"] >= 1 for c in body["cells"])
    body = client.get("/api/peaks/density", params={"cell": 0.25}).json()
    assert body["cell_deg"] == 2.0
    body = client.get("/api/peaks/density",
                      params={"bbox": "-119,36,-118,37", "zoom": 8}).json()
    assert body["cell_deg"] == 0.25 and body["total"] == 3


def test_route_search_nearest_detail(client):
    body = client.get("/api/peaks/search", params={"q": "whit"}).json()
    assert ids(body["peaks"]) == [WHITNEY] and body["count"] == 1
    assert client.get("/api/peaks/search", params={"q": "w"}).status_code == 422
    body = client.get("/api/peaks/nearest",
                      params={"lat": 36.57, "lon": -118.29, "n": 2}).json()
    assert ids(body["peaks"]) == [KNOB, WHITNEY]
    assert body["peaks"][0]["distance_km"] > 0
    detail = client.get(f"/api/peaks/{WHITNEY}").json()
    assert detail["elev_source"] == "geonames" and detail["dem_m"] == 4417.0
    assert detail["geonames_id"] == 5370001 and detail["source"] == "kirmse+geonames"
    assert client.get("/api/peaks/999").status_code == 404


def test_routes_without_a_catalog(empty_client):
    body = empty_client.get("/api/peaks/summary").json()
    assert body["available"] is False and "python -m sda.peaks import" in body["hint"]
    for path, params in (
        ("/api/peaks", {}),
        ("/api/peaks/density", {}),
        ("/api/peaks/search", {"q": "whitney"}),
        ("/api/peaks/nearest", {"lat": 0, "lon": 0}),
        ("/api/peaks/1", {}),
    ):
        resp = empty_client.get(path, params=params)
        assert resp.status_code == 503, path
        assert "python -m sda.peaks import" in resp.json()["detail"]
