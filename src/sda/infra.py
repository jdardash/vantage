"""Critical infrastructure catalog behind the globe's infrastructure layer.

Power plants, airports, seaports, dams, data centres, submarine cables with
their landing points, and radiation monitoring stations, held in a local SQLite
file with an R*Tree index so the globe can ask what lies inside its view, plus a
separate disk cache for the cable map.

Where the data comes from
-------------------------
=============  ===============================================  =======================
kind           source                                           licence
=============  ===============================================  =======================
power/nuclear  WRI Global Power Plant Database v1.3 (CSV)       CC BY 4.0
airport        OurAirports airports.csv                          public domain
port           NGA World Port Index (Pub 150); when msi.nga.mil  public domain
               does not answer, the NGA file as republished on
               ArcGIS Online (layer ``UpdatedPub150``)
dam            Wikidata items that are a dam or barrage, with    CC0 1.0
               coordinates (SPARQL, one query)
datacenter     PeeringDB facilities API (anonymous, one call)    PeeringDB AUP
landing/cable  TeleGeography Submarine Cable Map JSON            CC BY-NC-SA 3.0
radiation      BfS ODL gamma dose rate network (Germany, WFS)    dl-de/by-2-0
               Safecast realtime sensors (tt.safecast.org)       CC0 1.0
=============  ===============================================  =======================

Global Dam Watch v1.0 and GRanD were the first choice for dams. GDW is published
on figshare, whose download host answers scripts with a browser challenge, and
GRanD sits behind an Earthdata login, so the importer uses Wikidata (about 80,000
dams and barrages worldwide; levees, causeways, sea walls and weirs are left out).

Ideas taken from reference/gods-eye-view (MIT, Copyright (c) 2026 Bilawal Sidhu):
the TeleGeography endpoint pair (``cable-geo.json`` plus ``landing-point-geo.json``),
the WRI CSV URL and the choice of capacity as the power plant ranking, and drawing
cable edges as rhumb lines. The SQLite layout, the R*Tree queries, the per-kind
quotas, the cable detail merge and the dependence measure are ours, in the shape
of sda.cameras and sda.peaks.

Nothing is downloaded by the server except the cable map: ``python -m sda.infra
import`` fetches every source once and writes ``infra.sqlite`` under
``SDA_INFRA_DIR`` (default ``%LOCALAPPDATA%/sda/infra`` on Windows,
``~/.cache/sda/infra`` elsewhere). A source that fails keeps the rows the previous
catalog held for it. The cable map is CC BY-NC-SA, so it is never bundled: the
import writes ``cables.json`` beside the catalog, and the server refreshes that
file in the background once it is a week old (never under ``SDA_OFFLINE=1``).

Ethics: no record names a private person. Safecast sensors often sit at a
volunteer's home, so only the place name, the reading and a position rounded to
0.01 degree are kept; the contact fields of the device list are never read.

Coordinates are geodetic degrees as the sources publish them. Nothing here
touches an inertial frame.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

INFRA_DIR_ENV = "SDA_INFRA_DIR"
OFFLINE_ENV = "SDA_OFFLINE"
DB_NAME = "infra.sqlite"
CABLES_NAME = "cables.json"
SCHEMA_VERSION = "1"
IMPORT_HINT = "Run: python -m sda.infra import"
USER_AGENT = "sda-infra-importer/1.0 (self-hosted catalog import)"
EARTH_MEAN_RADIUS_KM = 6371.0

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

WRI_URL = (
    "https://raw.githubusercontent.com/wri/global-power-plant-database/master/"
    "output_database/global_power_plant_database.csv"
)
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
NGA_WPI_URL = (
    "https://msi.nga.mil/api/publications/download?type=view"
    "&key=16920959/SFH00000/UpdatedPub150.csv"
)
WPI_ARCGIS_URL = (
    "https://services7.arcgis.com/xuAg12SDp2ow0Fb8/arcgis/rest/services/"
    "UpdatedPub150/FeatureServer/0/query"
)
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
PEERINGDB_URL = (
    "https://www.peeringdb.com/api/fac?status=ok&fields=id,name,org_name,city,state,"
    "country,latitude,longitude,net_count,ix_count,carrier_count,website,clli,"
    "region_continent,updated"
)
BFS_ODL_URL = (
    "https://www.imis.bfs.de/ogc/opendata/ows?service=WFS&version=1.1.0"
    "&request=GetFeature&typeName=opendata:odlinfo_odl_1h_latest"
    "&outputFormat=application/json"
)
SAFECAST_URL = "https://tt.safecast.org/devices"
CABLE_GEO_URL = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
LANDING_GEO_URL = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"
CABLE_DETAIL_URL = "https://www.submarinecablemap.com/api/v3/cable/{id}.json"

#: One SPARQL query for every dam and barrage with coordinates (about 30 s on the
#: public endpoint). English and multilingual labels come back as separate columns
#: because the CSV results drop the language tag.
DAMS_QUERY = """SELECT ?d ?c ?name ?mul ?cc ?h ?inc ?river ?tl WHERE {
  ?d wdt:P31 ?t . ?t wdt:P279* wd:Q12323 . ?d wdt:P625 ?c .
  OPTIONAL { ?t rdfs:label ?tl . FILTER(LANG(?tl) = "en") }
  OPTIONAL { ?d rdfs:label ?name . FILTER(LANG(?name) = "en") }
  OPTIONAL { ?d rdfs:label ?mul . FILTER(LANG(?mul) = "mul") }
  OPTIONAL { ?d wdt:P17 ?k . ?k wdt:P297 ?cc . }
  OPTIONAL { ?d wdt:P2048 ?h . }
  OPTIONAL { ?d wdt:P571 ?inc . }
  OPTIONAL { ?d wdt:P177 ?r . ?r rdfs:label ?river . FILTER(LANG(?river) = "en") }
}"""

#: Layer keys in display order. "nuclear" is the nuclear sub-kind of power plants,
#: kept as its own key so the globe can toggle it on its own.
KINDS: tuple[str, ...] = (
    "power", "nuclear", "airport", "port", "dam", "datacenter", "landing", "radiation",
)
KIND_GROUP = {"nuclear": "power"}

LABELS = {
    "power": "Power plants",
    "nuclear": "Nuclear power plants",
    "airport": "Airports",
    "port": "Seaports",
    "dam": "Dams",
    "datacenter": "Data centres",
    "landing": "Cable landing points",
    "radiation": "Radiation monitors",
}


@dataclass(frozen=True)
class SourceInfo:
    name: str
    licence: str
    url: str


SOURCES: dict[str, SourceInfo] = {
    "power": SourceInfo("WRI Global Power Plant Database v1.3", "CC BY 4.0",
                        "https://datasets.wri.org/dataset/globalpowerplantdatabase"),
    "airport": SourceInfo("OurAirports", "Public domain", "https://ourairports.com/data/"),
    "port": SourceInfo("NGA World Port Index (Pub 150)", "Public domain (US Government)",
                       "https://msi.nga.mil/Publications/WPI"),
    "dam": SourceInfo("Wikidata (dams and barrages)", "CC0 1.0",
                      "https://www.wikidata.org/wiki/Q12323"),
    "datacenter": SourceInfo("PeeringDB facilities", "PeeringDB Acceptable Use Policy",
                             "https://www.peeringdb.com/"),
    "landing": SourceInfo("TeleGeography Submarine Cable Map", "CC BY-NC-SA 3.0",
                          "https://www.submarinecablemap.com/"),
    "radiation": SourceInfo("BfS ODL network (dl-de/by-2-0); Safecast realtime (CC0 1.0)",
                            "dl-de/by-2-0; CC0 1.0", "https://odlinfo.bfs.de/"),
}
SOURCES["nuclear"] = SOURCES["power"]

ATTRIBUTION = (
    "Power plants: WRI Global Power Plant Database (CC BY 4.0). Airports: OurAirports "
    "(public domain). Ports: NGA World Port Index (public domain). Dams: Wikidata (CC0). "
    "Data centres: PeeringDB. Cables and landings: TeleGeography submarinecablemap.com "
    "(CC BY-NC-SA 3.0). Radiation: BfS ODL-Info (dl-de/by-2-0), Safecast (CC0)."
)

MAX_NAME = 160
MAX_TEXT = 200
#: Downloads younger than this are reused by a second import the same day.
DOWNLOAD_MAX_AGE_S = 24 * 3600
#: The cable map is refreshed in the background once its file is this old.
CABLE_TTL_S = 7 * 24 * 3600
#: Per-cable details older than this are fetched again on a refresh.
CABLE_DETAIL_TTL_S = 30 * 24 * 3600
#: After a failed cable refresh the server waits this long before trying again.
CABLE_RETRY_S = 15 * 60
#: Gap between two cable detail requests (one at a time, about 6 per second).
CABLE_PAUSE_S = 0.15
#: Safecast sensors silent for longer than this are left out.
SAFECAST_MAX_AGE = timedelta(days=30)
#: Counts per minute for one microsievert per hour, for the tubes Safecast documents.
CPM_PER_USVH = {"lnd_7318u": 334.0, "lnd_7318c": 334.0, "lnd_7128ec": 108.0}
SAFECAST_TUBES = ("lnd_7318u", "lnd_7318c", "lnd_7128ec", "lnd_712u", "lnd_78017w")
#: Boxes larger than this (square degrees) skip the R*Tree and walk the score index.
BIG_BOX_DEG2 = 900.0


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def infra_dir() -> Path:
    """Directory holding the catalog: SDA_INFRA_DIR, else the user cache dir."""
    configured = os.environ.get(INFRA_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "infra"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "infra"


def db_path() -> Path:
    return infra_dir() / DB_NAME


def cables_path() -> Path:
    return infra_dir() / CABLES_NAME


def offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_stamp(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Small value helpers
# --------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(value: Any, limit: int = MAX_TEXT) -> str:
    """One line of plain text, control characters dropped, at most *limit* chars."""
    if value is None:
        return ""
    text = _CONTROL.sub(" ", str(value)).strip()
    return text[:limit]


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(str(value).strip())
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def valid_point(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None and lon is not None and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0)
    )


def http_url(value: Any) -> str:
    """The value when it is an http(s) URL with a host, else ''."""
    text = clean(value, 500)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def with_commas(value: float) -> str:
    return f"{round(value):,}"


def norm_key(key: str) -> str:
    """'UN/LOCODE' and 'UN_LOCODE' -> 'un_locode'; 'Channel Depth (m)' -> 'channel_depth_m'."""
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


@dataclass
class InfraRow:
    """One catalog record. ``extra`` holds the kind's own fields for the card."""

    uid: str
    kind: str
    sub: str
    name: str
    country: str
    lat: float
    lon: float
    score: float
    brief: str
    codes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Parsers, one per source (pure: text or parsed JSON in, rows out)
# --------------------------------------------------------------------------


def _latest_generation(row: dict[str, str]) -> tuple[float | None, int | None, bool]:
    for year in range(2019, 2012, -1):
        v = to_float(row.get(f"generation_gwh_{year}"))
        if v is not None:
            return v, year, False
    for year in range(2017, 2012, -1):
        v = to_float(row.get(f"estimated_generation_gwh_{year}"))
        if v is not None:
            return v, year, True
    return None, None, False


def parse_wri(rows: Iterable[dict[str, str]]) -> Iterator[InfraRow]:
    """WRI Global Power Plant Database rows. Nuclear plants get kind 'nuclear'."""
    for r in rows:
        lat, lon = to_float(r.get("latitude")), to_float(r.get("longitude"))
        ident = clean(r.get("gppd_idnr"), 40)
        if not ident or not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        fuel = clean(r.get("primary_fuel"), 40) or "Other"
        cap = to_float(r.get("capacity_mw")) or 0.0
        kind = "nuclear" if fuel.lower() == "nuclear" else "power"
        others = [clean(r.get(f"other_fuel{i}"), 40) for i in (1, 2, 3)]
        gen, gen_year, estimated = _latest_generation(r)
        year = to_float(r.get("commissioning_year"))
        yield InfraRow(
            uid=f"{kind}-{ident}",
            kind=kind,
            sub=fuel.lower(),
            name=clean(r.get("name"), MAX_NAME) or "Unnamed plant",
            country=clean(r.get("country_long"), 80) or clean(r.get("country"), 8),
            lat=lat,
            lon=lon,
            score=cap,
            brief=f"{with_commas(cap)} MW {fuel.lower()}" if cap else fuel.lower(),
            codes=ident,
            extra={
                "capacity_mw": cap,
                "fuel": fuel,
                "other_fuels": [f for f in others if f],
                "commissioning_year": int(year) if year else None,
                "owner": clean(r.get("owner"), MAX_TEXT),
                "generation_gwh": gen,
                "generation_year": gen_year,
                "generation_estimated": estimated,
                "gppd_idnr": ident,
                "country_code": clean(r.get("country"), 8),
                "data_source": clean(r.get("source"), MAX_TEXT),
                "source_url": http_url(r.get("url")),
            },
        )


_AIRPORT_TYPES = {
    "large_airport": ("large", 4000.0, "Large airport"),
    "medium_airport": ("medium", 3000.0, "Medium airport"),
    "small_airport": ("small", 1000.0, "Small airport"),
    "seaplane_base": ("seaplane", 500.0, "Seaplane base"),
}
_ICAO = re.compile(r"^[A-Z]{4}$")


def icao_of(row: dict[str, str]) -> str:
    """The ICAO location indicator: icao_code, else a 4-letter GPS code or ident."""
    for key in ("icao_code", "gps_code", "ident"):
        v = clean(row.get(key), 8).upper()
        if _ICAO.match(v):
            return v
    return ""


def liveatc_url(icao: str) -> str:
    """LiveATC's feed search for one ICAO code ('' when the code is not 4 letters)."""
    code = (icao or "").strip().upper()
    if not _ICAO.match(code):
        return ""
    return "https://www.liveatc.net/search/?icao=" + code


def parse_ourairports(rows: Iterable[dict[str, str]]) -> Iterator[InfraRow]:
    """OurAirports rows: airports and seaplane bases; heliports, balloonports and
    closed fields are left out."""
    for r in rows:
        kind_info = _AIRPORT_TYPES.get(clean(r.get("type"), 40))
        if kind_info is None:
            continue
        lat, lon = to_float(r.get("latitude_deg")), to_float(r.get("longitude_deg"))
        ident = clean(r.get("ident"), 16)
        if not ident or not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        sub, base, label = kind_info
        scheduled = clean(r.get("scheduled_service"), 8).lower() == "yes"
        icao = icao_of(r)
        iata = clean(r.get("iata_code"), 4).upper()
        codes = " ".join(dict.fromkeys(c for c in (icao, iata, ident.upper()) if c))
        elev = to_float(r.get("elevation_ft"))
        yield InfraRow(
            uid=f"airport-{ident}",
            kind="airport",
            sub=sub,
            name=clean(r.get("name"), MAX_NAME) or ident,
            country=clean(r.get("iso_country"), 8),
            lat=lat,
            lon=lon,
            score=base + (500.0 if scheduled else 0.0),
            brief=label + (", " + " / ".join(c for c in (icao, iata) if c) if icao or iata
                           else ""),
            codes=codes,
            extra={
                "ident": ident,
                "icao": icao,
                "iata": iata,
                "type": label,
                "municipality": clean(r.get("municipality"), 80),
                "region": clean(r.get("iso_region"), 16),
                "elevation_ft": int(elev) if elev is not None else None,
                "scheduled_service": scheduled,
                "home_link": http_url(r.get("home_link")),
                "wikipedia_link": http_url(r.get("wikipedia_link")),
                "liveatc_url": liveatc_url(icao),
                "ourairports_url": "https://ourairports.com/airports/" + quote(ident) + "/",
            },
        )


_HARBOR_RANK = {"large": 4, "medium": 3, "small": 2, "very-small": 1}


def _positive(value: Any) -> float | None:
    v = to_float(value)
    return v if v is not None and v > 0 else None


def parse_wpi(rows: Iterable[dict[str, Any]]) -> Iterator[InfraRow]:
    """World Port Index rows, from the NGA CSV or the ArcGIS copy (keys normalized)."""
    for raw in rows:
        r = {norm_key(str(k)): v for k, v in raw.items()}
        lat, lon = to_float(r.get("latitude")), to_float(r.get("longitude"))
        number = to_float(r.get("world_port_index_number"))
        if number is None or not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        wpi = int(number)
        size = clean(r.get("harbor_size"), 20)
        sub = size.lower().replace(" ", "-") or "unknown"
        channel = _positive(r.get("channel_depth_m"))
        pier = _positive(r.get("cargo_pier_depth_m"))
        harbor_type = clean(r.get("harbor_type"), 40)
        locode = clean(r.get("un_locode"), 8).replace(" ", "").upper()
        name = clean(r.get("main_port_name"), MAX_NAME) or f"WPI {wpi}"
        brief = (size + " harbor" if size else "Harbor") + (
            ", " + harbor_type.lower() if harbor_type else "")
        yield InfraRow(
            uid=f"port-{wpi}",
            kind="port",
            sub=sub,
            name=name,
            country=clean(r.get("country_code"), 80),
            lat=lat,
            lon=lon,
            score=_HARBOR_RANK.get(sub, 0) * 1000.0 + max(channel or 0.0, pier or 0.0),
            brief=brief,
            codes=" ".join(c for c in (locode, str(wpi)) if c),
            extra={
                "wpi": wpi,
                "un_locode": locode,
                "alternate_name": clean(r.get("alternate_port_name"), MAX_NAME),
                "region": clean(r.get("region_name"), 80),
                "water_body": clean(r.get("world_water_body"), 80),
                "harbor_size": size,
                "harbor_type": harbor_type,
                "harbor_use": clean(r.get("harbor_use"), 40),
                "channel_depth_m": channel,
                "cargo_pier_depth_m": pier,
                "max_vessel_length_m": _positive(r.get("maximum_vessel_length_m")),
                "container": clean(r.get("facilities_container"), 12),
                "oil_terminal": clean(r.get("facilities_oil_terminal"), 12),
                "lng_terminal": clean(r.get("facilities_lng_terminal"), 12),
            },
        )


_POINT = re.compile(r"Point\(\s*(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)")
_QID = re.compile(r"(Q\d+)$")
_DAM_CLASS = re.compile(r"\bdam\b|\bbarrage\b", re.IGNORECASE)
_NOT_DAM = ("splash dam",)


def is_dam_class(label: str) -> bool:
    low = (label or "").lower()
    return bool(_DAM_CLASS.search(low)) and not any(n in low for n in _NOT_DAM)


def parse_wikidata_dams(rows: Iterable[dict[str, str]]) -> Iterator[InfraRow]:
    """Wikidata SPARQL CSV rows (one per value combination), merged per item.

    An item is kept when at least one of its classes is a kind of dam or barrage
    by its English label, so items that are only a levee, causeway, sea wall or
    weir are dropped. Several heights keep the smallest.
    """
    items: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = _QID.search(clean(r.get("d"), 80))
        if not m:
            continue
        qid = m.group(1)
        it = items.get(qid)
        if it is None:
            pt = _POINT.search(r.get("c") or "")
            if not pt:
                continue
            it = items[qid] = {
                "lon": to_float(pt.group(1)), "lat": to_float(pt.group(2)), "name": "",
                "mul": "", "cc": "", "heights": set(), "year": None, "river": "",
                "types": [], "dam": False,
            }
        name = clean(r.get("name"), MAX_NAME)
        if name and not it["name"]:
            it["name"] = name
        mul = clean(r.get("mul"), MAX_NAME)
        if mul and not it["mul"]:
            it["mul"] = mul
        cc = clean(r.get("cc"), 4).upper()
        if cc and not it["cc"]:
            it["cc"] = cc
        h = _positive(r.get("h"))
        # The tallest dams stand about 335 m; larger values are feet or typing slips.
        if h is not None and h < 400.0:
            it["heights"].add(h)
        inc = re.match(r"^(-?\d{1,4})-", clean(r.get("inc"), 40))
        if inc and it["year"] is None:
            it["year"] = int(inc.group(1))
        river = clean(r.get("river"), 80)
        if river and not it["river"]:
            it["river"] = river
        tl = clean(r.get("tl"), 80)
        if tl and tl not in it["types"]:
            it["types"].append(tl)
            if is_dam_class(tl):
                it["dam"] = True
    for qid, it in items.items():
        if not it["dam"] or not valid_point(it["lat"], it["lon"]):
            continue
        height = min(it["heights"]) if it["heights"] else None
        name = it["name"] or it["mul"]
        barrage = any("barrage" in t.lower() for t in it["types"]) and not any(
            re.search(r"\bdam\b", t.lower()) for t in it["types"])
        label = "Barrage" if barrage else "Dam"
        yield InfraRow(
            uid=f"dam-{qid}",
            kind="dam",
            sub="barrage" if barrage else "dam",
            name=name or f"Unnamed {label.lower()} ({qid})",
            country=it["cc"],
            lat=float(it["lat"]),
            lon=float(it["lon"]),
            score=(height or 0.0) + (1.0 if name else 0.0),
            brief=label + (f", {height:g} m high" if height else ""),
            codes=qid,
            extra={
                "wikidata": qid,
                "wikidata_url": "https://www.wikidata.org/wiki/" + qid,
                "height_m": height,
                "year": it["year"],
                "river": it["river"],
                "types": [t for t in it["types"] if is_dam_class(t)][:4],
            },
        )


def parse_peeringdb(doc: Any) -> Iterator[InfraRow]:
    """PeeringDB ``/api/fac`` answer: facilities with a position."""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        return
    for f in data:
        if not isinstance(f, dict):
            continue
        if f.get("status") not in (None, "ok"):
            continue
        lat, lon = to_float(f.get("latitude")), to_float(f.get("longitude"))
        fid = f.get("id")
        if not isinstance(fid, int) or not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        nets = int(to_float(f.get("net_count")) or 0)
        ixs = int(to_float(f.get("ix_count")) or 0)
        carriers = int(to_float(f.get("carrier_count")) or 0)
        parts = [f"{nets} networks"]
        if ixs:
            parts.append(f"{ixs} exchanges")
        yield InfraRow(
            uid=f"datacenter-{fid}",
            kind="datacenter",
            sub="facility",
            name=clean(f.get("name"), MAX_NAME) or f"Facility {fid}",
            country=clean(f.get("country"), 8),
            lat=lat,
            lon=lon,
            score=float(nets + 5 * ixs + carriers),
            brief=", ".join(parts),
            codes=clean(f.get("clli"), 16),
            extra={
                "operator": clean(f.get("org_name"), MAX_NAME),
                "city": clean(f.get("city"), 80),
                "state": clean(f.get("state"), 80),
                "networks": nets,
                "exchanges": ixs,
                "carriers": carriers,
                "clli": clean(f.get("clli"), 16),
                "continent": clean(f.get("region_continent"), 40),
                "website": http_url(f.get("website")),
                "peeringdb_url": f"https://www.peeringdb.com/fac/{fid}",
                "updated": clean(f.get("updated"), 40),
            },
        )


def parse_bfs_odl(doc: Any) -> Iterator[InfraRow]:
    """BfS ODL-Info latest hourly gamma dose rates (GeoJSON from the WFS)."""
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        return
    for f in feats:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry") or {}
        props = f.get("properties") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if not isinstance(coords, list) or len(coords) < 2 or not isinstance(props, dict):
            continue
        lon, lat = to_float(coords[0]), to_float(coords[1])
        sid = clean(props.get("id"), 20)
        if not sid or not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        value = to_float(props.get("value"))
        operating = props.get("site_status") == 1
        yield InfraRow(
            uid=f"radiation-{sid}",
            kind="radiation",
            sub="bfs",
            name=clean(props.get("name"), MAX_NAME) or sid,
            country="DE",
            lat=lat,
            lon=lon,
            score=value or 0.0,
            brief=(f"{value:.3f} uSv/h" if value is not None else "no reading")
            + ("" if operating else ", out of service"),
            codes=sid,
            extra={
                "network": "BfS ODL (Germany)",
                "dose_rate_usvh": value,
                "cosmic_usvh": to_float(props.get("value_cosmic")),
                "terrestrial_usvh": to_float(props.get("value_terrestrial")),
                "measured_at": clean(props.get("end_measure"), 40),
                "station_height_m": to_float(props.get("height_above_sea")),
                "status": "operating" if operating else "out of service",
                "station_url": "https://odlinfo.bfs.de/",
            },
        )


def _parse_time(text: Any) -> datetime | None:
    raw = clean(text, 40)
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def parse_safecast(doc: Any, now: datetime | None = None) -> Iterator[InfraRow]:
    """Safecast realtime devices that carry a radiation tube and reported within
    SAFECAST_MAX_AGE. Only the reading, the place name and a position rounded to
    0.01 degree are copied; contact fields are never read."""
    if not isinstance(doc, list):
        return
    now = now or datetime.now(UTC)
    seen: set[str] = set()
    for d in doc:
        if not isinstance(d, dict):
            continue
        tube = next((t for t in SAFECAST_TUBES if to_float(d.get(t)) is not None), "")
        if not tube:
            continue
        lat, lon = to_float(d.get("loc_lat")), to_float(d.get("loc_lon"))
        if not valid_point(lat, lon):
            continue
        assert lat is not None and lon is not None
        when = _parse_time(d.get("when_captured"))
        if when is None or now - when > SAFECAST_MAX_AGE:
            continue
        device = d.get("device")
        if not isinstance(device, int) or str(device) in seen:
            continue
        seen.add(str(device))
        cpm = float(to_float(d.get(tube)) or 0.0)
        factor = CPM_PER_USVH.get(tube)
        usvh = round(cpm / factor, 3) if factor else None
        place = clean(d.get("loc_name"), 80)
        yield InfraRow(
            uid=f"radiation-sc{device}",
            kind="radiation",
            sub="safecast",
            name="Safecast sensor" + (", " + place if place else ""),
            country=clean(d.get("loc_country"), 8),
            lat=round(lat, 2),
            lon=round(lon, 2),
            score=usvh if usvh is not None else 0.0,
            brief=(f"about {usvh:.3f} uSv/h" if usvh is not None else f"{cpm:g} cpm"),
            extra={
                "network": "Safecast (volunteer)",
                "cpm": cpm,
                "tube": tube,
                "dose_rate_usvh_approx": usvh,
                "measured_at": _utc_stamp(when),
                "place": place,
                "position_note": "rounded to 0.01 degree",
                "station_url": "https://map.safecast.org/",
            },
        )


def read_csv_rows(text: str) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(text.lstrip("﻿")))


# --------------------------------------------------------------------------
# Submarine cables (TeleGeography), kept as a disk cache beside the catalog
# --------------------------------------------------------------------------

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_CABLE_COLOR = "#ff7043"


def _lines_of(geom: Any) -> list[list[tuple[float, float]]]:
    if not isinstance(geom, dict):
        return []
    coords = geom.get("coordinates")
    if geom.get("type") == "LineString":
        coords = [coords]
    elif geom.get("type") != "MultiLineString":
        return []
    out: list[list[tuple[float, float]]] = []
    for line in coords if isinstance(coords, list) else []:
        pts: list[tuple[float, float]] = []
        for p in line if isinstance(line, list) else []:
            if isinstance(p, list) and len(p) >= 2:
                lon, lat = to_float(p[0]), to_float(p[1])
                if lon is not None and lat is not None and abs(lat) <= 90 and abs(lon) <= 180:
                    pts.append((lon, lat))
        if len(pts) >= 2:
            out.append(pts)
    return out


def parse_cable_geo(doc: Any) -> dict[str, dict[str, Any]]:
    """``cable-geo.json``: cable id -> {id, name, color, lines}. A cable drawn as
    several features is merged into one entry."""
    out: dict[str, dict[str, Any]] = {}
    feats = doc.get("features") if isinstance(doc, dict) else None
    for f in feats if isinstance(feats, list) else []:
        props = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(props, dict):
            continue
        cid = clean(props.get("id"), 130)
        if not _SLUG.match(cid):
            continue
        lines = _lines_of(f.get("geometry"))
        if not lines:
            continue
        color = clean(props.get("color"), 7)
        entry = out.setdefault(cid, {
            "id": cid, "name": clean(props.get("name"), MAX_NAME) or cid,
            "color": color if _COLOR.match(color) else DEFAULT_CABLE_COLOR, "lines": [],
        })
        entry["lines"].extend(lines)
    return out


def parse_landing_geo(doc: Any) -> list[dict[str, Any]]:
    """``landing-point-geo.json``: [{id, name, lat, lon}]."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    feats = doc.get("features") if isinstance(doc, dict) else None
    for f in feats if isinstance(feats, list) else []:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lid = clean(props.get("id"), 130) if isinstance(props, dict) else ""
        if not _SLUG.match(lid) or lid in seen or not isinstance(coords, list) or len(coords) < 2:
            continue
        lon, lat = to_float(coords[0]), to_float(coords[1])
        if not valid_point(lat, lon):
            continue
        seen.add(lid)
        out.append({"id": lid, "name": clean(props.get("name"), MAX_NAME) or lid,
                    "lat": round(float(lat or 0.0), 5), "lon": round(float(lon or 0.0), 5)})
    return out


def parse_cable_detail(doc: Any) -> dict[str, Any] | None:
    """``cable/<id>.json``: length, ready-for-service year, owners and landing points."""
    if not isinstance(doc, dict):
        return None
    cid = clean(doc.get("id"), 130)
    if not _SLUG.match(cid):
        return None
    landings = []
    for lp in doc.get("landing_points") or []:
        if not isinstance(lp, dict):
            continue
        lid = clean(lp.get("id"), 130)
        if not _SLUG.match(lid):
            continue
        landings.append({"id": lid, "name": clean(lp.get("name"), MAX_NAME) or lid,
                         "country": clean(lp.get("country"), 80)})
    rfs = to_float(doc.get("rfs_year"))
    return {
        "id": cid,
        "length": clean(doc.get("length"), 40),
        "rfs_year": int(rfs) if rfs else None,
        "is_planned": bool(doc.get("is_planned")),
        "owners": clean(doc.get("owners"), 600),
        "suppliers": clean(doc.get("suppliers"), 200),
        "url": http_url(doc.get("url")),
        "landing_points": landings,
    }


def _mercator_y(lat: float) -> float:
    lat = max(-85.0, min(85.0, lat))
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))


def _mercator_lat(y: float) -> float:
    return math.degrees(2.0 * math.atan(math.exp(y)) - math.pi / 2.0)


def densify_rhumb(
    line: list[tuple[float, float]], step_deg: float = 1.0
) -> list[list[tuple[float, float]]]:
    """Points every *step_deg* or closer along rhumb lines between the vertices, so a
    long straight edge of the 2D map follows the sphere instead of cutting under it.
    A jump of more than 180 degrees of longitude starts a new line."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for i, (lon, lat) in enumerate(line):
        if i == 0:
            cur.append((round(lon, 4), round(lat, 4)))
            continue
        plon, plat = line[i - 1]
        dlon = lon - plon
        if abs(dlon) > 180.0:
            if len(cur) >= 2:
                out.append(cur)
            cur = [(round(lon, 4), round(lat, 4))]
            continue
        n = max(1, math.ceil(math.hypot(dlon, lat - plat) / step_deg))
        y0, y1 = _mercator_y(plat), _mercator_y(lat)
        for k in range(1, n + 1):
            t = k / n
            if k == n:
                pt = (lon, lat)
            elif abs(plat) > 85.0 or abs(lat) > 85.0:
                pt = (plon + dlon * t, plat + (lat - plat) * t)
            else:
                pt = (plon + dlon * t, _mercator_lat(y0 + (y1 - y0) * t))
            cur.append((round(pt[0], 4), round(pt[1], 4)))
    if len(cur) >= 2:
        out.append(cur)
    return out


def country_from_name(name: str) -> str:
    """'Cork, Ireland' -> 'Ireland'; 'Muanda, Congo, Dem. Rep.' -> 'Congo, Dem. Rep.'."""
    head, sep, tail = (name or "").partition(", ")
    return tail.strip() if sep else ""


def build_cable_dataset(
    geo: dict[str, dict[str, Any]],
    landings: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    fetched_at: str,
) -> dict[str, Any]:
    """Merge the three TeleGeography files into the cached dataset.

    ``country_cables`` counts, for each country, the distinct cables that land
    there; a cable card turns it into "other cables landing there", a plain
    dependence measure (0 means this cable is the country's only one).
    """
    lp_country: dict[str, str] = {}
    lp_cables: dict[str, list[str]] = defaultdict(list)
    country_sets: dict[str, set[str]] = defaultdict(set)
    for cid, det in details.items():
        if cid not in geo:
            continue
        for lp in det.get("landing_points", []):
            if lp.get("country"):
                lp_country.setdefault(lp["id"], lp["country"])
                country_sets[lp["country"]].add(cid)
            if cid not in lp_cables[lp["id"]]:
                lp_cables[lp["id"]].append(cid)
    cables = []
    for cid in sorted(geo):
        g = geo[cid]
        det = details.get(cid) or {}
        lps = det.get("landing_points", [])
        lines: list[list[tuple[float, float]]] = []
        for line in g["lines"]:
            lines.extend(densify_rhumb(line))
        cables.append({
            "id": cid,
            "name": g["name"],
            "color": g["color"],
            "lines": lines,
            "length": det.get("length", ""),
            "rfs_year": det.get("rfs_year"),
            "is_planned": bool(det.get("is_planned", False)),
            "owners": det.get("owners", ""),
            "suppliers": det.get("suppliers", ""),
            "url": det.get("url", ""),
            "landing_points": lps,
            "countries": sorted({lp["country"] for lp in lps if lp.get("country")}),
            "detail_fetched_at": det.get("detail_fetched_at", ""),
        })
    points = []
    for lp in landings:
        points.append({
            **lp,
            "country": lp_country.get(lp["id"]) or country_from_name(lp["name"]),
            "cables": sorted(lp_cables.get(lp["id"], [])),
        })
    return {
        "version": 1,
        "fetched_at": fetched_at,
        "source": CABLE_GEO_URL,
        "licence": "CC BY-NC-SA 3.0, TeleGeography submarinecablemap.com",
        "cables": cables,
        "landing_points": points,
        "country_cables": {c: len(s) for c, s in sorted(country_sets.items())},
    }


def dependence(cable: dict[str, Any], country_cables: dict[str, int]) -> list[dict[str, Any]]:
    """For each country the cable lands in: its landing count there, the number of
    cables landing in that country, and how many of those are other cables.
    Fewest alternatives first."""
    per: dict[str, int] = defaultdict(int)
    for lp in cable.get("landing_points", []):
        if lp.get("country"):
            per[lp["country"]] += 1
    rows = []
    for country, n in per.items():
        total = max(1, int(country_cables.get(country, 1)))
        rows.append({"country": country, "landings": n, "cables_total": total,
                     "other_cables": total - 1})
    rows.sort(key=lambda r: (r["other_cables"], r["country"]))
    return rows


Fetch = Callable[[str], Any]


def _cable_feature(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": c["id"],
        "properties": {
            "id": c["id"], "name": c["name"], "color": c["color"],
            "countries": c.get("countries", []), "rfs_year": c.get("rfs_year"),
            "is_planned": c.get("is_planned", False), "length": c.get("length", ""),
        },
        "geometry": {"type": "MultiLineString", "coordinates": c["lines"]},
    }


class CableStore:
    """The cable map cache file, read on demand and refreshed in the background."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._mtime = 0.0
        self._refreshing = False
        self._last_attempt = 0.0
        self._last_error = ""
        self._thread: threading.Thread | None = None
        self._index: dict[str, dict[str, Any]] = {}
        self._lp_index: dict[str, dict[str, Any]] = {}
        self._features: list[dict[str, Any]] = []

    # -- reading ------------------------------------------------------------

    def data(self) -> dict[str, Any] | None:
        with self._lock:
            return self._data_locked()

    def _data_locked(self) -> dict[str, Any] | None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._data = None
            self._mtime = 0.0
            return None
        if self._data is None or mtime != self._mtime:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self._data
            if isinstance(loaded, dict) and isinstance(loaded.get("cables"), list):
                self._data = loaded
                self._mtime = mtime
                self._index = {c["id"]: c for c in loaded["cables"]}
                self._lp_index = {p["id"]: p for p in loaded.get("landing_points", [])}
                self._features = [_cable_feature(c) for c in loaded["cables"]]
        return self._data

    def age_s(self) -> float | None:
        d = self.data()
        if not d:
            return None
        when = _parse_time(d.get("fetched_at"))
        if when is None:
            return None
        return (datetime.now(UTC) - when).total_seconds()

    def status(self) -> dict[str, Any]:
        d = self.data()
        age = self.age_s()
        with self._lock:
            loading, error = self._refreshing, self._last_error
        return {
            "available": d is not None,
            "count": len(d["cables"]) if d else 0,
            "landing_points": len(d.get("landing_points", [])) if d else 0,
            "fetched_at": d.get("fetched_at", "") if d else "",
            "stale": age is None or age > CABLE_TTL_S,
            "loading": loading,
            "error": error,
        }

    def geojson(self) -> dict[str, Any]:
        d = self.data()
        st = self.status()
        with self._lock:
            feats = list(self._features) if d else []
        return {
            "type": "FeatureCollection",
            "features": feats,
            "available": st["available"],
            "loading": st["loading"],
            "fetched_at": st["fetched_at"],
            "error": st["error"],
            "attribution": "TeleGeography submarinecablemap.com, CC BY-NC-SA 3.0",
        }

    def cable(self, cid: str) -> dict[str, Any] | None:
        d = self.data()
        if not d:
            return None
        c = self._index.get(cid)
        if c is None:
            return None
        out = {k: v for k, v in c.items() if k not in ("lines", "detail_fetched_at")}
        out["countries_detail"] = dependence(c, d.get("country_cables", {}))
        out["map_url"] = "https://www.submarinecablemap.com/submarine-cable/" + c["id"]
        return out

    def landing_rows(self) -> list[InfraRow]:
        d = self.data()
        if not d:
            return []
        rows = []
        for p in d.get("landing_points", []):
            n = len(p.get("cables", []))
            rows.append(InfraRow(
                uid="landing-" + p["id"], kind="landing", sub="landing", name=p["name"],
                country=p.get("country", ""), lat=p["lat"], lon=p["lon"], score=float(n),
                brief=f"{n} cable{'s' if n != 1 else ''}",
                extra={"landing_id": p["id"], "cables": p.get("cables", [])},
            ))
        return rows

    def landing(self, lid: str) -> dict[str, Any] | None:
        d = self.data()
        if not d:
            return None
        p = self._lp_index.get(lid)
        if p is None:
            return None
        country = p.get("country", "")
        names = [{"id": cid, "name": self._index[cid]["name"]}
                 for cid in p.get("cables", []) if cid in self._index]
        return {
            "landing_id": p["id"],
            "cables": names,
            "country_cables": int(d.get("country_cables", {}).get(country, 0)),
            "map_url": "https://www.submarinecablemap.com/landing-point/" + p["id"],
        }

    # -- refreshing ---------------------------------------------------------

    def ensure_fresh(self, fetch: Fetch | None = None) -> bool:
        """Start a background refresh when the file is missing or a week old.

        Never under SDA_OFFLINE, never twice at once, and not within
        CABLE_RETRY_S of a failed attempt. Returns True when a refresh started.
        """
        if offline():
            return False
        age = self.age_s()
        if age is not None and age <= CABLE_TTL_S:
            return False
        with self._lock:
            recent = (self._last_attempt > 0
                      and time.monotonic() - self._last_attempt < CABLE_RETRY_S)
            if self._refreshing or recent:
                return False
            self._refreshing = True
            self._last_attempt = time.monotonic()
        self._thread = threading.Thread(
            target=self._refresh_quietly, args=(fetch,), name="sda-infra-cables", daemon=True)
        self._thread.start()
        return True

    def _refresh_quietly(self, fetch: Fetch | None) -> None:
        try:
            self.refresh(fetch, already_marked=True)
        except Exception as exc:  # noqa: BLE001 - background task, reported in status
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"[:300]

    def refresh(
        self,
        fetch: Fetch | None = None,
        pause_s: float = CABLE_PAUSE_S,
        progress: Callable[[str], None] | None = None,
        already_marked: bool = False,
    ) -> dict[str, Any]:
        """Fetch the cable map now and rewrite the cache file; returns its status.

        Details fetched within CABLE_DETAIL_TTL_S are reused, so a weekly refresh
        asks only for new cables. Raises when the two map files cannot be read;
        the old file is then left as it was.
        """
        get = fetch or http_json
        if not already_marked:
            with self._lock:
                self._refreshing = True
        try:
            geo = parse_cable_geo(get(CABLE_GEO_URL))
            if not geo:
                raise ValueError("cable-geo.json held no cables")
            landings = parse_landing_geo(get(LANDING_GEO_URL))
            old = self.data() or {}
            reuse: dict[str, dict[str, Any]] = {}
            now = datetime.now(UTC)
            for c in old.get("cables", []):
                when = _parse_time(c.get("detail_fetched_at"))
                if when and (now - when).total_seconds() < CABLE_DETAIL_TTL_S:
                    reuse[c["id"]] = {k: c.get(k) for k in (
                        "id", "length", "rfs_year", "is_planned", "owners", "suppliers",
                        "url", "landing_points", "detail_fetched_at")}
            details: dict[str, dict[str, Any]] = {}
            failed = 0
            todo = [cid for cid in sorted(geo) if cid not in reuse]
            for i, cid in enumerate(todo):
                if i and pause_s > 0:
                    time.sleep(pause_s)
                try:
                    det = parse_cable_detail(get(CABLE_DETAIL_URL.format(id=cid)))
                except Exception:  # noqa: BLE001 - one cable, keep going
                    det = None
                if det is None:
                    failed += 1
                    continue
                det["detail_fetched_at"] = _utc_stamp()
                details[cid] = det
                if progress and (i + 1) % 100 == 0:
                    progress(f"  cable details {i + 1}/{len(todo)}")
            for cid, det in reuse.items():
                details.setdefault(cid, det)
            dataset = build_cable_dataset(geo, landings, details, _utc_stamp())
            dataset["details_failed"] = failed
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(dataset, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            with self._lock:
                self._last_error = ""
            return self.status()
        finally:
            with self._lock:
                self._refreshing = False


# --------------------------------------------------------------------------
# Building the catalog
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
    kind TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    licence TEXT NOT NULL,
    url TEXT NOT NULL,
    origin TEXT NOT NULL,
    count INTEGER NOT NULL,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE items (
    rid INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    sub TEXT NOT NULL,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    score REAL NOT NULL,
    brief TEXT NOT NULL,
    codes TEXT NOT NULL,
    extra TEXT NOT NULL
);
CREATE VIRTUAL TABLE item_index USING rtree(rid, min_lon, max_lon, min_lat, max_lat);
"""

_INDEXES = """
CREATE INDEX items_kind_score ON items(kind, score DESC);
"""


@dataclass
class SourceResult:
    """What one import step produced. ``rows`` is None when the step failed."""

    kinds: tuple[str, ...]
    rows: list[InfraRow] | None
    origin: str = ""
    error: str = ""
    skipped: bool = False


@dataclass
class BuildReport:
    counts: dict[str, int] = field(default_factory=dict)
    kept: dict[str, str] = field(default_factory=dict)
    duplicates: int = 0


def _insert(conn: sqlite3.Connection, rows: Iterable[InfraRow]) -> tuple[int, int]:
    cur = conn.cursor()
    added = dup = 0
    for r in rows:
        cur.execute(
            "INSERT OR IGNORE INTO items(uid, kind, sub, name, country, lat, lon, score, brief,"
            " codes, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r.uid, r.kind, r.sub, r.name, r.country, r.lat, r.lon, r.score, r.brief,
             r.codes, json.dumps(r.extra, separators=(",", ":"))),
        )
        if cur.rowcount != 1:
            dup += 1
            continue
        added += 1
        cur.execute(
            "INSERT INTO item_index(rid, min_lon, max_lon, min_lat, max_lat)"
            " VALUES (?, ?, ?, ?, ?)",
            (cur.lastrowid, r.lon, r.lon, r.lat, r.lat),
        )
    return added, dup


def _previous_rows(path: Path, kinds: tuple[str, ...]) -> tuple[list[InfraRow], dict[str, Any]]:
    """Rows and source records of *kinds* from an earlier catalog, if one exists."""
    if not path.exists():
        return [], {}
    rows: list[InfraRow] = []
    sources: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return [], {}
    try:
        marks = ",".join("?" * len(kinds))
        for rec in conn.execute(
            "SELECT uid, kind, sub, name, country, lat, lon, score, brief, codes, extra"
            f" FROM items WHERE kind IN ({marks})", kinds,
        ):
            rows.append(InfraRow(
                uid=rec[0], kind=rec[1], sub=rec[2], name=rec[3], country=rec[4], lat=rec[5],
                lon=rec[6], score=rec[7], brief=rec[8], codes=rec[9], extra=json.loads(rec[10]),
            ))
        for rec in conn.execute(
            f"SELECT kind, origin, fetched_at, status FROM sources WHERE kind IN ({marks})",
            kinds,
        ):
            sources[rec[0]] = {"origin": rec[1], "fetched_at": rec[2], "status": rec[3]}
    except sqlite3.Error:
        return [], {}
    finally:
        conn.close()
    return rows, sources


def build_catalog(
    results: list[SourceResult], dest: Path, previous: Path | None = None
) -> BuildReport:
    """Write a fresh catalog to *dest*. A failed step keeps that kind's rows from
    *previous* (default: *dest* itself) and says so in the sources table.

    The file is written beside *dest* and renamed over it. On Windows a running
    server holds *dest* open; the new file is then left as ``<dest>.new`` and the
    server swaps it in on its next query.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    previous = previous if previous is not None else dest
    pending = dest.with_name(dest.name + ".new")
    if pending.exists():
        previous = pending
    report = BuildReport()
    stamp = _utc_stamp()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(_SCHEMA)
        for res in results:
            if res.rows is not None:
                rows, status, fetched, origin = res.rows, "ok", stamp, res.origin
            else:
                rows, old = _previous_rows(previous, res.kinds)
                first: dict[str, Any] = next(iter(old.values()), {})
                fetched = first.get("fetched_at", "")
                origin = first.get("origin", "")
                if res.skipped:
                    status = first.get("status", "") or "not imported"
                    note = "not asked this run, kept as it was"
                else:
                    status = (f"failed ({res.error}); kept {len(rows)} rows from {fetched}"
                              if rows else f"failed ({res.error})")
                    note = status
                report.kept.update({k: note for k in res.kinds})
            added, dup = _insert(conn, rows)
            report.duplicates += dup
            for kind in res.kinds:
                n = sum(1 for r in rows if r.kind == kind)
                report.counts[kind] = n
                src = SOURCES[kind]
                conn.execute(
                    "INSERT OR REPLACE INTO sources(kind, name, licence, url, origin, count,"
                    " status, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind, src.name, src.licence, src.url, origin, n, status[:400], fetched),
                )
        conn.executescript(_INDEXES)
        meta = {"schema": SCHEMA_VERSION, "imported_at": stamp}
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", list(meta.items()))
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    try:
        os.replace(tmp, dest)
        if pending.exists():
            pending.unlink()
    except PermissionError:
        os.replace(tmp, pending)
    return report


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

_LIST_COLUMNS = "c.uid, c.kind, c.sub, c.name, c.lat, c.lon, c.country, c.brief, c.score"


def _list_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0], "kind": row[1], "sub": row[2], "name": row[3],
        "lat": round(row[4], 5), "lon": round(row[5], 5), "country": row[6],
        "brief": row[7], "score": row[8],
    }


def _row_item(r: InfraRow) -> dict[str, Any]:
    return {"id": r.uid, "kind": r.kind, "sub": r.sub, "name": r.name,
            "lat": round(r.lat, 5), "lon": round(r.lon, 5), "country": r.country,
            "brief": r.brief, "score": r.score}


def lon_spans(west: float, east: float) -> list[tuple[float, float]]:
    """Longitude intervals for a box, split in two when it crosses the antimeridian."""
    west = max(-180.0, min(180.0, west))
    east = max(-180.0, min(180.0, east))
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def allocate(counts: dict[str, int], limit: int) -> dict[str, int]:
    """Split *limit* across kinds fairly: equal shares, with what a small kind
    cannot use passed on to the larger ones."""
    quotas = {k: 0 for k in counts}
    left = max(0, int(limit))
    open_kinds = [k for k, n in counts.items() if n > 0]
    while left > 0 and open_kinds:
        share = max(1, left // len(open_kinds))
        still: list[str] = []
        for k in open_kinds:
            take = min(share, counts[k] - quotas[k], left)
            quotas[k] += take
            left -= take
            if quotas[k] < counts[k]:
                still.append(k)
            if left == 0:
                break
        open_kinds = still
    return quotas


def parse_kinds(raw: str | None) -> list[str]:
    """'power,airport' -> ['power', 'airport'] in KINDS order; None or '' -> all.
    Unknown names are ignored."""
    if raw is None or not raw.strip():
        return list(KINDS)
    wanted = {k.strip().lower() for k in raw.split(",")}
    return [k for k in KINDS if k in wanted]


def links_for(kind: str, extra: dict[str, Any]) -> list[dict[str, str]]:
    """Outbound links for a card, http(s) only."""
    pairs: list[tuple[str, Any]] = []
    if kind == "airport":
        if extra.get("icao"):
            pairs.append(("LiveATC feeds for " + str(extra["icao"]), extra.get("liveatc_url")))
        pairs += [("OurAirports", extra.get("ourairports_url")),
                  ("Airport website", extra.get("home_link")),
                  ("Wikipedia", extra.get("wikipedia_link"))]
    elif kind in ("power", "nuclear"):
        pairs.append(("Data source", extra.get("source_url")))
    elif kind == "dam":
        pairs.append(("Wikidata", extra.get("wikidata_url")))
    elif kind == "datacenter":
        pairs += [("PeeringDB", extra.get("peeringdb_url")), ("Website", extra.get("website"))]
    elif kind == "radiation":
        pairs.append(("Network map", extra.get("station_url")))
    elif kind == "landing":
        pairs.append(("Submarine Cable Map", extra.get("map_url")))
    out = []
    for label, url in pairs:
        good = http_url(url)
        if good:
            out.append({"label": label, "url": good})
    return out


class InfraCatalog:
    """Read-only access to one catalog file, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._summary: dict[str, Any] | None = None
        self._checked = 0.0

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._summary = None

    def _swap_pending_locked(self) -> None:
        pending = self.path.with_name(self.path.name + ".new")
        if not pending.exists():
            return
        self._close_locked()
        try:
            os.replace(pending, self.path)
        except OSError:
            return

    def _connection(self) -> sqlite3.Connection | None:
        """Open the file on first use; pick up a re-import at most every 10 s."""
        now = time.monotonic()
        if now - self._checked > 10.0 or self._conn is None:
            self._checked = now
            self._swap_pending_locked()
        if self._conn is None:
            if not self.path.exists():
                return None
            uri = self.path.resolve().as_uri() + "?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.execute("SELECT 1 FROM items LIMIT 1")
            except sqlite3.Error:
                return None
            self._conn = conn
        return self._conn

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connection() is not None

    def summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return {"available": False, "count": 0, "kinds": {}, "imported_at": ""}
            if self._summary is not None:
                return self._summary
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            counts = dict(conn.execute(
                "SELECT kind, count(*) FROM items GROUP BY kind").fetchall())
            subs: dict[str, dict[str, int]] = defaultdict(dict)
            for kind, sub, n in conn.execute(
                "SELECT kind, sub, count(*) FROM items GROUP BY kind, sub ORDER BY 3 DESC"
            ):
                subs[kind][sub] = n
            kinds: dict[str, Any] = {}
            for kind, name, licence, url, origin, _n, status, fetched in conn.execute(
                "SELECT kind, name, licence, url, origin, count, status, fetched_at"
                " FROM sources"
            ):
                kinds[kind] = {
                    "count": int(counts.get(kind, 0)), "source": name, "licence": licence,
                    "url": url, "origin": origin, "status": status, "fetched_at": fetched,
                    "subkinds": subs.get(kind, {}),
                }
            self._summary = {
                "available": True,
                "count": int(sum(counts.values())),
                "kinds": kinds,
                "imported_at": meta.get("imported_at", ""),
            }
            return self._summary

    def counts_in(self, kinds: list[str], west: float, south: float, east: float,
                  north: float) -> dict[str, int]:
        out = {k: 0 for k in kinds}
        if not kinds:
            return out
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        marks = ",".join("?" * len(kinds))
        with self._lock:
            conn = self._connection()
            if conn is None:
                return out
            for w, e in lon_spans(west, east):
                if (e - w) * (north - south) >= BIG_BOX_DEG2:
                    sql = (f"SELECT c.kind, count(*) FROM items c WHERE c.kind IN ({marks})"
                           " AND c.lon BETWEEN ? AND ? AND c.lat BETWEEN ? AND ?"
                           " GROUP BY c.kind")
                    args: list[Any] = [*kinds, w, e, south, north]
                else:
                    sql = ("SELECT c.kind, count(*) FROM item_index r JOIN items c"
                           " ON c.rid = r.rid WHERE r.max_lon >= ? AND r.min_lon <= ?"
                           " AND r.max_lat >= ? AND r.min_lat <= ?"
                           f" AND c.kind IN ({marks}) GROUP BY c.kind")
                    args = [w, e, south, north, *kinds]
                for kind, n in conn.execute(sql, args):
                    out[kind] += int(n)
        return out

    def top_in(self, kind: str, n: int, west: float, south: float, east: float,
               north: float) -> list[dict[str, Any]]:
        """The *n* highest-scoring items of one kind inside the box."""
        if n <= 0:
            return []
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        rows: list[tuple[Any, ...]] = []
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            for w, e in lon_spans(west, east):
                if (e - w) * (north - south) >= BIG_BOX_DEG2:
                    sql = ("SELECT " + _LIST_COLUMNS + " FROM items c WHERE c.kind = ?"
                           " AND c.lon BETWEEN ? AND ? AND c.lat BETWEEN ? AND ?"
                           " ORDER BY c.score DESC, c.rid LIMIT ?")
                    args: list[Any] = [kind, w, e, south, north, n]
                else:
                    sql = ("SELECT " + _LIST_COLUMNS + " FROM item_index r JOIN items c"
                           " ON c.rid = r.rid WHERE r.max_lon >= ? AND r.min_lon <= ?"
                           " AND r.max_lat >= ? AND r.min_lat <= ? AND c.kind = ?"
                           " ORDER BY c.score DESC, c.rid LIMIT ?")
                    args = [w, e, south, north, kind, n]
                rows.extend(conn.execute(sql, args).fetchall())
        rows.sort(key=lambda r: -r[8])
        return [_list_row(r) for r in rows[:n]]

    def get(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT uid, kind, sub, name, lat, lon, country, brief, score, extra"
                " FROM items WHERE uid = ?", (uid,),
            ).fetchone()
        if row is None:
            return None
        out = _list_row(row[:9])
        out["fields"] = json.loads(row[9])
        return out

    def search(self, text: str, kinds: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """Items whose name contains *text*, or whose codes (ICAO, IATA, UN/LOCODE,
        CLLI, Wikidata id) start with it; highest score first."""
        needle = text.strip()
        if len(needle) < 2 or not kinds:
            return []
        esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        marks = ",".join("?" * len(kinds))
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT " + _LIST_COLUMNS + " FROM items c WHERE c.kind IN (" + marks + ")"
                " AND (c.name LIKE ? ESCAPE '\\' OR (' ' || c.codes) LIKE ? ESCAPE '\\')"
                " ORDER BY instr(' ' || upper(c.codes) || ' ', ?) > 0 DESC, c.score DESC"
                " LIMIT ?",
                (*kinds, "%" + esc + "%", "% " + esc + "%", " " + needle.upper() + " ",
                 max(1, min(100, limit))),
            ).fetchall()
        return [_list_row(r) for r in rows]


_catalog: InfraCatalog | None = None
_cables: CableStore | None = None
_singleton_lock = threading.Lock()


def get_catalog() -> InfraCatalog:
    """The process-wide catalog for the current SDA_INFRA_DIR."""
    global _catalog
    path = db_path()
    with _singleton_lock:
        if _catalog is None or _catalog.path != path:
            if _catalog is not None:
                _catalog.close()
            _catalog = InfraCatalog(path)
        return _catalog


def get_cable_store() -> CableStore:
    """The process-wide cable cache for the current SDA_INFRA_DIR."""
    global _cables
    path = cables_path()
    with _singleton_lock:
        if _cables is None or _cables.path != path:
            _cables = CableStore(path)
        return _cables


def reset_catalog() -> None:
    """Forget the cached catalog and cable store (tests change SDA_INFRA_DIR)."""
    global _catalog, _cables
    with _singleton_lock:
        if _catalog is not None:
            _catalog.close()
        _catalog = None
        _cables = None


def summary() -> dict[str, Any]:
    """Counts per kind with sources and licences, the cable cache state, attribution."""
    cat = get_catalog().summary()
    store = get_cable_store()
    cab = store.status()
    kinds = {}
    for kind in KINDS:
        src = SOURCES[kind]
        if kind == "landing":
            kinds[kind] = {
                "label": LABELS[kind], "count": cab["landing_points"], "source": src.name,
                "licence": src.licence, "url": src.url, "origin": LANDING_GEO_URL,
                "status": "ok" if cab["available"] else "not fetched yet",
                "fetched_at": cab["fetched_at"], "subkinds": {},
            }
            continue
        got = cat.get("kinds", {}).get(kind)
        kinds[kind] = {"label": LABELS[kind], **(got or {
            "count": 0, "source": src.name, "licence": src.licence, "url": src.url,
            "origin": "", "status": "not imported", "fetched_at": "", "subkinds": {}})}
    return {
        "available": bool(cat.get("available")),
        "count": int(cat.get("count", 0)) + cab["landing_points"],
        "imported_at": cat.get("imported_at", ""),
        "kinds": kinds,
        "cables": cab,
        "attribution": ATTRIBUTION,
        "hint": "" if cat.get("available") else
        "No infrastructure catalog imported. On the server run: python -m sda.infra import",
    }


def query_items(west: float, south: float, east: float, north: float,
                kinds: list[str], limit: int) -> dict[str, Any]:
    """Items inside a box for the requested kinds, the limit shared fairly across
    kinds, highest score first within each. Also the full count per kind."""
    cat = get_catalog()
    store = get_cable_store()
    db_kinds = [k for k in kinds if k != "landing"]
    counts = cat.counts_in(db_kinds, west, south, east, north)
    landing_in: list[InfraRow] = []
    if "landing" in kinds:
        lo, hi = sorted((south, north))
        spans = lon_spans(west, east)
        landing_in = [r for r in store.landing_rows() if lo <= r.lat <= hi
                      and any(w <= r.lon <= e for w, e in spans)]
        counts["landing"] = len(landing_in)
    ordered = {k: counts.get(k, 0) for k in kinds}
    quotas = allocate(ordered, limit)
    items: list[dict[str, Any]] = []
    for kind in kinds:
        q = quotas.get(kind, 0)
        if not q:
            continue
        if kind == "landing":
            landing_in.sort(key=lambda r: -r.score)
            items.extend(_row_item(r) for r in landing_in[:q])
        else:
            items.extend(cat.top_in(kind, q, west, south, east, north))
    total = sum(ordered.values())
    return {"items": items, "count": len(items), "counts": ordered, "total": total,
            "capped": total > len(items)}


def item_detail(uid: str) -> dict[str, Any] | None:
    """One item with its fields, links and source; landing points come from the
    cable cache."""
    if uid.startswith("landing-"):
        store = get_cable_store()
        lid = uid[len("landing-"):]
        extra = store.landing(lid)
        row = next((r for r in store.landing_rows() if r.uid == uid), None)
        if extra is None or row is None:
            return None
        out = _row_item(row)
        out["fields"] = extra
    else:
        got = get_catalog().get(uid)
        if got is None:
            return None
        out = got
    src = SOURCES[out["kind"]]
    out["links"] = links_for(out["kind"], out["fields"])
    out["source"] = {"name": src.name, "licence": src.licence, "url": src.url}
    out["label"] = LABELS[out["kind"]]
    return out


# --------------------------------------------------------------------------
# Network (import only, plus the cable refresh)
# --------------------------------------------------------------------------


def _client() -> Any:
    import httpx

    return httpx.Client(timeout=120.0, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def http_json(url: str) -> Any:
    """GET *url* and parse JSON; raises on any failure."""
    with _client() as client:
        resp = client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


def download(url: str, dest: Path, fresh: bool = False, method: str = "GET",
             data: dict[str, str] | None = None, accept: str = "*/*",
             attempts: int = 2) -> Path:
    """Stream *url* to *dest*, reusing a copy younger than DOWNLOAD_MAX_AGE_S."""
    if not fresh and dest.exists() and time.time() - dest.stat().st_mtime < DOWNLOAD_MAX_AGE_S:
        print(f"  reusing {dest.name} (under a day old)", flush=True)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(5.0)
        try:
            with _client() as client, client.stream(
                method, url, data=data, headers={"Accept": accept}
            ) as resp:
                resp.raise_for_status()
                size = 0
                with part.open("wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
                        size += len(chunk)
            if size == 0:
                raise ValueError("empty response")
            os.replace(part, dest)
            print(f"  downloaded {size / 1e6:.1f} MB from {urlparse(url).netloc}", flush=True)
            return dest
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
    part.unlink(missing_ok=True)
    assert last is not None
    raise last


def fetch_wpi_arcgis(fetch: Fetch | None = None, page: int = 1000) -> list[dict[str, Any]]:
    """Every WPI row from the ArcGIS copy, a page at a time (the layer's maximum is
    1000 records per request)."""
    get = fetch or http_json
    rows: list[dict[str, Any]] = []
    for i in range(20):
        url = (WPI_ARCGIS_URL + "?where=1%3D1&outFields=*&returnGeometry=false"
               f"&orderByFields=ObjectId&resultOffset={i * page}&resultRecordCount={page}&f=json")
        doc = get(url)
        feats = doc.get("features") if isinstance(doc, dict) else None
        if not isinstance(feats, list):
            raise ValueError("ArcGIS answer without features")
        rows.extend(f["attributes"] for f in feats
                    if isinstance(f, dict) and isinstance(f.get("attributes"), dict))
        if len(feats) < page and not doc.get("exceededTransferLimit"):
            break
    return rows


# --------------------------------------------------------------------------
# Import steps
# --------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def step_power(work: Path, fresh: bool) -> SourceResult:
    path = download(WRI_URL, work / "wri-gppd.csv", fresh)
    return SourceResult(("power", "nuclear"), list(parse_wri(read_csv_rows(_read_text(path)))),
                        WRI_URL)


def step_airports(work: Path, fresh: bool) -> SourceResult:
    path = download(OURAIRPORTS_URL, work / "ourairports.csv", fresh)
    return SourceResult(("airport",), list(parse_ourairports(read_csv_rows(_read_text(path)))),
                        OURAIRPORTS_URL)


def step_ports(work: Path, fresh: bool) -> SourceResult:
    try:
        path = download(NGA_WPI_URL, work / "wpi.csv", fresh, attempts=1)
        rows = list(parse_wpi(read_csv_rows(_read_text(path))))
        if rows:
            return SourceResult(("port",), rows, NGA_WPI_URL)
    except Exception as exc:  # noqa: BLE001 - fall back to the ArcGIS copy
        print(f"  NGA did not answer ({type(exc).__name__}); using the ArcGIS copy", flush=True)
    cache = work / "wpi-arcgis.json"
    if not fresh and cache.exists() and time.time() - cache.stat().st_mtime < DOWNLOAD_MAX_AGE_S:
        attrs = json.loads(_read_text(cache))
    else:
        attrs = fetch_wpi_arcgis()
        cache.write_text(json.dumps(attrs), encoding="utf-8")
    return SourceResult(("port",), list(parse_wpi(attrs)), WPI_ARCGIS_URL)


def step_dams(work: Path, fresh: bool) -> SourceResult:
    path = download(WIKIDATA_SPARQL_URL, work / "wikidata-dams.csv", fresh, method="POST",
                    data={"query": DAMS_QUERY}, accept="text/csv")
    return SourceResult(("dam",), list(parse_wikidata_dams(read_csv_rows(_read_text(path)))),
                        WIKIDATA_SPARQL_URL)


def step_datacenters(work: Path, fresh: bool) -> SourceResult:
    path = download(PEERINGDB_URL, work / "peeringdb-fac.json", fresh, accept="application/json")
    return SourceResult(("datacenter",), list(parse_peeringdb(json.loads(_read_text(path)))),
                        "https://www.peeringdb.com/api/fac")


def step_radiation(work: Path, fresh: bool) -> SourceResult:
    rows: list[InfraRow] = []
    origins: list[str] = []
    errors: list[str] = []
    try:
        path = download(BFS_ODL_URL, work / "bfs-odl.json", fresh, accept="application/json")
        rows.extend(parse_bfs_odl(json.loads(_read_text(path))))
        origins.append("https://www.imis.bfs.de/ogc/opendata/ows")
    except Exception as exc:  # noqa: BLE001 - the other network may still answer
        errors.append(f"BfS: {type(exc).__name__}")
    try:
        path = download(SAFECAST_URL, work / "safecast-devices.json", fresh,
                        accept="application/json")
        rows.extend(parse_safecast(json.loads(_read_text(path))))
        origins.append(SAFECAST_URL)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Safecast: {type(exc).__name__}")
    if not rows:
        return SourceResult(("radiation",), None, error="; ".join(errors) or "no stations")
    return SourceResult(("radiation",), rows, " + ".join(origins))


STEPS: dict[str, Callable[[Path, bool], SourceResult]] = {
    "power": step_power,
    "airport": step_airports,
    "port": step_ports,
    "dam": step_dams,
    "datacenter": step_datacenters,
    "radiation": step_radiation,
}
STEP_KINDS = {"power": ("power", "nuclear"), "airport": ("airport",), "port": ("port",),
              "dam": ("dam",), "datacenter": ("datacenter",), "radiation": ("radiation",)}


def _cmd_import(args: argparse.Namespace) -> int:
    dest = Path(args.db) if args.db else db_path()
    work = dest.parent / "downloads"
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    unknown = only - set(STEPS) - {"cables"}
    if unknown:
        print(f"Unknown step(s): {', '.join(sorted(unknown))}. "
              f"Steps: {', '.join([*STEPS, 'cables'])}")
        return 2
    started = time.monotonic()
    results: list[SourceResult] = []
    for name, step in STEPS.items():
        if only and name not in only:
            results.append(SourceResult(STEP_KINDS[name], None, skipped=True))
            continue
        print(f"[{name}] fetching ...", flush=True)
        try:
            res = step(work, args.fresh)
        except Exception as exc:  # noqa: BLE001 - the step keeps its previous rows
            res = SourceResult(STEP_KINDS[name], None, error=f"{type(exc).__name__}: {exc}"[:200])
        if res.rows is None:
            print(f"[{name}] FAILED: {res.error}", flush=True)
        else:
            print(f"[{name}] {len(res.rows)} rows", flush=True)
        results.append(res)
    report = build_catalog(results, dest)
    if not args.skip_cables and (not only or "cables" in only):
        print("[cables] fetching the TeleGeography map and per-cable details ...", flush=True)
        store = CableStore(dest.parent / CABLES_NAME)
        try:
            st = store.refresh(progress=lambda msg: print(msg, flush=True))
            print(f"[cables] {st['count']} cables, {st['landing_points']} landing points",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 - the old cache stays
            print(f"[cables] FAILED: {type(exc).__name__}: {exc}", flush=True)
    print(f"Done in {time.monotonic() - started:.0f} s. Catalog: {dest}")
    for kind in KINDS:
        if kind in report.counts:
            note = f"  ({report.kept[kind]})" if kind in report.kept else ""
            print(f"  {kind:<11} {report.counts[kind]:>7}{note}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else db_path()
    cat = InfraCatalog(path)
    try:
        s = cat.summary()
    finally:
        cat.close()
    store = CableStore(path.parent / CABLES_NAME)
    cab = store.status()
    if not s.get("available"):
        print(f"No infrastructure catalog at {path}. {IMPORT_HINT}")
        return 1
    print(f"Catalog: {path}  (imported {s['imported_at']})")
    for kind, k in s["kinds"].items():
        print(f"  {kind:<11} {k['count']:>7}  {k['source']} [{k['status']}]")
    print(f"  cables      {cab['count']:>7}  landing points {cab['landing_points']}, "
          f"fetched {cab['fetched_at'] or 'never'}")
    return 0


def _cmd_cables(args: argparse.Namespace) -> int:
    store = CableStore(infra_dir() / CABLES_NAME)
    st = store.refresh(progress=lambda msg: print(msg, flush=True))
    print(f"{st['count']} cables, {st['landing_points']} landing points -> {store.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sda.infra", description=__doc__.split(
        "\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="download every source and build the catalog")
    imp.add_argument("--only", help="comma list of steps: " + ",".join([*STEPS, "cables"]))
    imp.add_argument("--skip-cables", action="store_true", help="leave the cable cache as is")
    imp.add_argument("--fresh", action="store_true", help="ignore downloads under a day old")
    imp.add_argument("--db", help=f"catalog file (default: {db_path()})")
    imp.set_defaults(func=_cmd_import)
    st = sub.add_parser("status", help="show what the catalog holds")
    st.add_argument("--db", help=f"catalog file (default: {db_path()})")
    st.set_defaults(func=_cmd_status)
    cb = sub.add_parser("cables", help="refresh the submarine cable cache only")
    cb.set_defaults(func=_cmd_cables)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
