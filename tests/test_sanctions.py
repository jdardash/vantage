"""Tests for the sanctions lists (sda.sanctions) and their routes.

Parsers run on rows recorded from the real files on 2026-11-19 (tests/data/sanctions):
the OFAC SDN CSV pair and the UK Sanctions List CSV, cut to a dozen rows. Downloads
are a fake fetcher that copies those files, so nothing here touches the network.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import sanctions
from sda.routes.sanctions import router

DATA = Path(__file__).resolve().parent / "data" / "sanctions"
SDN = DATA / "sdn_sample.csv"
COMMENTS = DATA / "sdn_comments_sample.csv"
UK = DATA / "uk_sanctions_sample.csv"


def _sdn_text() -> str:
    return SDN.read_text(encoding="utf-8")


def _comments_text() -> str:
    return COMMENTS.read_text(encoding="utf-8")


def _by(records: list[dict], key: str, value: str) -> dict:
    hits = [r for r in records if r[key] == value]
    assert len(hits) == 1, (key, value, [r[key] for r in records])
    return hits[0]


class FakeFetch:
    """Copies the recorded files in place of a download; records every URL asked."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url: str, dest: Path) -> None:
        self.urls.append(url)
        if url in self.fail:
            raise RuntimeError("HTTP 503")
        src = {sanctions.OFAC_SDN_URL: SDN, sanctions.OFAC_COMMENTS_URL: COMMENTS,
               sanctions.UK_LIST_URL: UK}[url]
        shutil.copyfile(src, dest)


@pytest.fixture
def store(tmp_path: Path):
    fetch = FakeFetch()
    s = sanctions.SanctionsStore(tmp_path / "cache", fetch=fetch, allow_network=True,
                                 include_uk=True)
    assert s.refresh() == "downloaded"
    s.fake_fetch = fetch  # type: ignore[attr-defined]
    sanctions.set_store(s)
    yield s
    sanctions.set_store(None)


@pytest.fixture
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --- normalisation ---------------------------------------------------------------


def test_identifier_normalisation():
    assert sanctions.norm_imo("IMO 9187629") == "9187629"
    assert sanctions.norm_imo("IMO9562233") == "9562233"
    assert sanctions.norm_imo("918762") == ""
    assert sanctions.norm_mmsi("572469210") == "572469210"
    assert sanctions.norm_mmsi("57246921") == ""
    assert sanctions.norm_reg("ep-gol") == "EPGOL"
    assert sanctions.norm_hex("140ae7") == "140AE7"
    assert sanctions.norm_hex("140ae") == ""
    assert sanctions.norm_name("  Jal_Vahini ") == "JALVAHINI"
    assert sanctions.clean_text("a\u202fb\xa0 c\n d") == "a b c d"


def test_imo_check_digit():
    assert sanctions.imo_checksum_ok("9187629")
    assert sanctions.imo_checksum_ok("9826902")
    assert not sanctions.imo_checksum_ok("9187628")
    assert not sanctions.imo_checksum_ok("abc")


def test_cache_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SDA_SANCTIONS_DIR", str(tmp_path / "x"))
    assert sanctions.sanctions_dir() == tmp_path / "x"
    monkeypatch.delenv("SDA_SANCTIONS_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert sanctions.sanctions_dir() == tmp_path / "local" / "sda" / "sanctions"


# --- OFAC parsing ------------------------------------------------------------------


def test_ofac_keeps_only_vessels_and_aircraft():
    recs = sanctions.parse_ofac(_sdn_text(), _comments_text())
    kinds = sorted({r["kind"] for r in recs})
    assert kinds == ["aircraft", "vessel"]
    uids = {r["uid"] for r in recs}
    assert not uids & {"36", "28263", "17016"}  # an airline, a person, a bank
    assert len([r for r in recs if r["kind"] == "vessel"]) == 4
    assert len([r for r in recs if r["kind"] == "aircraft"]) == 6


def test_ofac_vessel_identifiers():
    recs = sanctions.parse_ofac(_sdn_text())
    artavil = _by(recs, "name", "ARTAVIL")
    assert artavil["imo"] == "9187629"
    assert artavil["mmsi"] == "572469210"
    assert artavil["programmes"] == ["IRAN"]
    assert "NATIONAL IRANIAN TANKER COMPANY" in artavil["remarks"]
    assert artavil["aliases"] == ["ALPHA"]
    assert artavil["details"]["vessel_type"] == "Crude/Oil Products Tanker"
    assert artavil["details"]["call_sign"] == "T2EU4"

    ebano = _by(recs, "name", "EBANO")
    assert ebano["imo"] == "7406784" and ebano["mmsi"] == ""
    assert ebano["aliases"] == ["ANA I", "SAND SWAN"]

    tug = _by(recs, "name", "MAR AZUL")
    assert tug["imo"] == "" and tug["remarks"] == ""
    assert tug["details"] == {"call_sign": "CL2192", "vessel_type": "Tug", "grt": "212",
                              "flag": "Cuba", "owner": "Samir de Navegacion S.A."}

    galaxy = _by(recs, "name", "GALAXY")
    assert galaxy["programmes"] == ["UKRAINE-EO13662", "RUSSIA-EO14024"]
    assert (galaxy["imo"], galaxy["mmsi"]) == ("9826902", "314905000")


def test_ofac_aircraft_identifiers():
    recs = sanctions.parse_ofac(_sdn_text())
    gol = _by(recs, "name", "EP-GOL")
    assert gol["regs"] == ["EP-GOL"]
    assert gol["serials"] == ["1013409297", "8305"]
    assert gol["details"] == {"model": "IL-76TD", "operator": "YAS AIR", "manufactured": "1991"}

    mahan = _by(recs, "name", "EX-35011")
    assert mahan["programmes"] == ["SDGT", "NPWMD", "IFSR"]
    assert mahan["serials"] == ["838"]

    jet = _by(recs, "name", "RA-02791")
    assert jet["mode_s"] == "140AE7"
    assert "258512" in jet["serials"]

    # The listing name is a serial; the tail number is in the remarks.
    a340 = _by(recs, "name", "MSN 391")
    assert a340["regs"] == ["EP-MMH"] and a340["serials"] == ["391"]

    # Only a previous tail number: kept apart, never a current registration.
    old = _by(recs, "name", "MSN 449")
    assert old["regs"] == [] and old["previous_regs"] == ["YI-NAE"]
    assert old["serials"] == ["449"]

    yv = _by(recs, "name", "YV3360")
    assert yv["regs"] == ["YV3360"]  # name and remark agree; listed once
    assert yv["serials"] == ["17"]


def test_ofac_remark_overflow_is_joined():
    sdn = ('9001,"TEST SHIP","vessel","IRAN",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,'
           '"Vessel Registration Identification IMO 91876"\r\n\x1a')
    comments = '9001,"29; MMSI 572469210; Linked To: SOMEONE."\r\n\x1a'
    rec = sanctions.parse_ofac(sdn, comments)[0]
    assert rec["imo"] == "9187629"
    assert rec["mmsi"] == "572469210"
    assert rec["remarks"].endswith("Linked To: SOMEONE.")


# --- UK parsing --------------------------------------------------------------------


def _uk_records() -> list[dict]:
    with UK.open(encoding="utf-8-sig", newline="") as fh:
        return sanctions.parse_uk(fh)


def test_uk_ships_one_record_per_listing():
    recs = _uk_records()
    assert sorted(r["uid"] for r in recs) == ["DPR0075", "DPR0076", "RUS2176"]
    petrel = _by(recs, "uid", "DPR0075")
    assert petrel["name"] == "Petrel 8"
    assert "Jal Vahini" in petrel["aliases"] and "Tian Tai 36" in petrel["aliases"]
    assert petrel["imo"] == "9562233"
    assert petrel["programmes"] == ["UK DEMOCRATIC PEOPLE'S REPUBLIC OF KOREA"]
    assert petrel["details"]["sanctions_imposed"] == "Prohibition of port entry"

    korolev = _by(recs, "uid", "RUS2176")
    assert korolev["imo"] == "9826902"
    assert korolev["programmes"] == ["UK RUSSIA"]
    assert korolev["remarks"].startswith("KOROLEV PROSPECT (IMO 9826902)")
    assert "\u202f" not in korolev["remarks"]
    imposed = korolev["details"]["sanctions_imposed"]
    assert imposed == "Shipping sanctions: (see 'Other information')"  # the duplicate dropped
    assert korolev["details"]["flag"] == "Gabon"


def test_uk_programme_names():
    assert sanctions.uk_programme("The Russia (Sanctions) (EU Exit) Regulations 2019") == \
        "UK RUSSIA"
    assert sanctions.uk_programme("") == "UK"


# --- store: refresh, cache, lookups -------------------------------------------------------


def test_refresh_writes_a_cache_the_next_process_reads(store, tmp_path):
    assert store.cache_path.exists()
    assert not list(store.directory.glob("*.csv"))  # raw downloads removed
    assert store.fake_fetch.urls == [sanctions.OFAC_SDN_URL, sanctions.OFAC_COMMENTS_URL,
                                     sanctions.UK_LIST_URL]
    again = sanctions.SanctionsStore(store.directory, fetch=FakeFetch(), allow_network=True)
    assert again.load_cache()
    assert again.count("vessel") == 7 and again.count("aircraft") == 6
    assert not again.needs_refresh()
    assert again.refresh() == "fresh"


def test_refresh_is_daily_and_retries_hourly(tmp_path):
    s = sanctions.SanctionsStore(tmp_path, fetch=FakeFetch(), allow_network=True)
    now = time.time()
    assert s.needs_refresh(now)  # nothing held, never asked
    s.fetched_at = now - 25 * 3600
    s.asked_path.write_text(f"{now - 600:.0f}", encoding="ascii")
    assert not s.needs_refresh(now)  # asked ten minutes ago
    s.asked_path.write_text(f"{now - 2 * 3600:.0f}", encoding="ascii")
    assert s.needs_refresh(now)
    s.fetched_at = now - 3600
    assert not s.needs_refresh(now)


def test_failed_source_keeps_its_previous_records(store):
    store.fake_fetch.fail = (sanctions.UK_LIST_URL,)
    assert store.refresh(force=True) == "partial"
    assert "UK Sanctions List" in (store.error or "")
    assert store.count("vessel", "uk") == 3
    store.fake_fetch.fail = (sanctions.UK_LIST_URL, sanctions.OFAC_SDN_URL)
    assert store.refresh(force=True) == "failed"
    assert store.count() == 13


def test_offline_store_never_downloads(tmp_path):
    fetch = FakeFetch()
    s = sanctions.SanctionsStore(tmp_path, fetch=fetch, allow_network=False)
    assert s.refresh() == "offline"
    s.ensure_loaded()
    assert fetch.urls == [] and s.records == {}


def test_uk_list_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SDA_SANCTIONS_UK", "0")
    fetch = FakeFetch()
    s = sanctions.SanctionsStore(tmp_path, fetch=fetch, allow_network=True)
    assert s.refresh() == "downloaded"
    assert sanctions.UK_LIST_URL not in fetch.urls
    assert [x["id"] for x in s.summary()["sources"]] == ["ofac"]


def test_background_load_on_first_use(tmp_path):
    fetch = FakeFetch()
    s = sanctions.SanctionsStore(tmp_path, fetch=fetch, allow_network=True)
    s.ensure_loaded()
    s.wait(10)
    assert s.count("vessel") == 7
    s.ensure_loaded()  # fresh now: no second download
    s.wait(10)
    assert len(fetch.urls) == 3


def test_vessel_lookup_by_imo_spans_both_lists(store):
    hits = store.vessel(imo="IMO 9826902")
    assert {(h["source"], h["name"]) for h in hits} == {
        ("OFAC SDN", "GALAXY"), ("UK Sanctions List", "KOROLEV PROSPECT")}
    assert all(h["matched_on"] == "imo" for h in hits)
    by_mmsi = store.vessel(mmsi="314905000")
    assert [h["name"] for h in by_mmsi] == ["GALAXY"]
    assert by_mmsi[0]["url"].endswith("id=51771")


def test_vessel_name_is_only_a_lead(store):
    assert store.vessel(imo="", mmsi="") == []
    names = store.vessel_name_match("galaxy")
    assert [n["matched_on"] for n in names] == ["name"]
    assert store.vessel_name_match("MAR") == []  # too short to mean anything


def test_aircraft_lookups(store):
    assert [h["name"] for h in store.aircraft(reg="ep-gol")] == ["EP-GOL"]
    assert [h["name"] for h in store.aircraft(reg="EPMMH")] == ["MSN 391"]
    assert [h["name"] for h in store.aircraft(hex_code="140ae7")] == ["RA-02791"]
    assert [h["name"] for h in store.aircraft(serial="1013409297")] == ["EP-GOL"]
    assert store.aircraft(reg="YI-NAE") == []
    assert [h["name"] for h in store.aircraft_previous_reg("YI-NAE")] == ["MSN 449"]


def test_in_process_aircraft_answer(store):
    hit = sanctions.lookup_aircraft(reg="EP-GOL")
    assert hit["listed"] and hit["programme"] == "SDGT" and hit["name"] == "EP-GOL"
    assert "YAS AIR" in hit["remarks"]


# --- routes --------------------------------------------------------------------------


def test_vessel_route_hit_and_miss(client):
    hit = client.get("/api/sanctions/vessel", params={"imo": "IMO 9187629"}).json()
    assert hit["listed"] is True
    assert hit["programme"] == "IRAN" and hit["name"] == "ARTAVIL"
    assert "Vessel Registration Identification IMO 9187629" in hit["remarks"]
    assert hit["source"] == "OFAC SDN" and hit["list_loaded"] is True

    miss = client.get("/api/sanctions/vessel", params={"imo": "9074729"})
    assert miss.status_code == 200
    assert miss.json()["listed"] is False and miss.json()["matches"] == []

    assert client.get("/api/sanctions/vessel", params={"imo": "12"}).status_code == 422
    assert client.get("/api/sanctions/vessel").status_code == 422
    assert client.get("/api/sanctions/vessel", params={"mmsi": "572469210"}).json()["listed"]


def test_aircraft_route(client):
    hit = client.get("/api/sanctions/aircraft", params={"reg": "EX-35011"}).json()
    assert hit["listed"] and hit["programme"] == "SDGT; NPWMD; IFSR"
    assert "MAHAN AIR" in hit["remarks"]
    lead = client.get("/api/sanctions/aircraft", params={"reg": "YI-NAE"}).json()
    assert lead["listed"] is False
    assert [p["name"] for p in lead["possible_matches"]] == ["MSN 449"]
    assert client.get("/api/sanctions/aircraft", params={"hex": "140AE7"}).json()["listed"]
    assert client.get("/api/sanctions/aircraft", params={"hex": "xyz"}).status_code == 422
    assert client.get("/api/sanctions/aircraft").status_code == 422


def test_summary_and_index_routes(client):
    summary = client.get("/api/sanctions/summary").json()
    assert summary["list_loaded"] is True
    assert (summary["vessels"], summary["aircraft"]) == (7, 6)
    assert [s["id"] for s in summary["sources"]] == ["ofac", "uk"]
    assert summary["updated"].endswith("Z")
    air = client.get("/api/sanctions/index", params={"kind": "aircraft"}).json()
    assert "EPGOL" in air["regs"] and air["hex"] == ["140AE7"]
    assert "YINAE" not in air["regs"]
    ves = client.get("/api/sanctions/index", params={"kind": "vessel"}).json()
    assert "9826902" in ves["imo"] and "572469210" in ves["mmsi"]
    assert client.get("/api/sanctions/index", params={"kind": "bus"}).status_code == 422


def test_routes_before_the_list_has_loaded(tmp_path):
    sanctions.set_store(sanctions.SanctionsStore(tmp_path, fetch=FakeFetch(),
                                                 allow_network=False))
    try:
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        body = c.get("/api/sanctions/vessel", params={"imo": "9187629"}).json()
        assert body["listed"] is False and body["list_loaded"] is False
        assert "downloads are off" in body["note"]
        assert c.get("/api/sanctions/summary").json()["list_loaded"] is False
    finally:
        sanctions.set_store(None)


def test_cache_file_is_ascii_json(store):
    raw = store.cache_path.read_bytes()
    assert all(b < 128 for b in raw)
    data = json.loads(raw)
    assert data["version"] == sanctions.CACHE_VERSION
    assert set(data["records"]) == {"ofac", "uk"}
