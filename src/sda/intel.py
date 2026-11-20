"""Country intelligence for the globe: borders, facts, indicators, outbreaks,
cyber, internet outages, an instability heuristic and event convergence.

Everything here is keyless and read from public sources, each cached so the
upstream sees at most a handful of requests an hour:

* Natural Earth admin-0 countries at 1:50m (public domain), from the
  nvkelso/natural-earth-vector GitHub mirror. Downloaded once and kept on disk
  under ``SDA_INTEL_DIR`` (default ``%LOCALAPPDATA%/sda/intel`` on Windows,
  ``~/.cache/sda/intel`` elsewhere). Country polygons answer "which country is
  this point in" for every feed below.
* World Bank Indicators API v2 (CC BY 4.0): population, GDP, GDP per capita and
  inflation, the latest non-empty year of each, plus the country list (capital
  and its coordinates, region, income level). One request per indicator for all
  countries, cached a day.
* WHO Disease Outbreak News (the who.int public API). Cached an hour.
* abuse.ch Feodo Tracker botnet C2 blocklist (CC0). Cached an hour, aggregated
  per country; no address is passed on.
* IODA, Georgia Tech's Internet Outage Detection and Analysis, v2 alerts per
  country over the last 24 hours. Cached ten minutes.
* GDELT 2.0 event export: one 15-minute file at a time from
  data.gdeltproject.org, held for 24 hours. The GDELT GEO 2.0 API behind the
  older /api/gdelt route answers 404 since 2026, so this module reads the raw
  export instead. ``fetch_gdelt_events`` and ``parse_gdelt_export`` are public
  for other routes to reuse.
* This application's own feeds, by import from sda.routes.worldview and
  sda.routes.eonet: USGS quakes, NASA FIRMS fires (needs FIRMS_MAP_KEY), NWS
  alerts (United States only) and NASA EONET.

Local time uses the IANA database (tzdata's zone.tab), taking the zone nearest
the capital. Exchange hours are a static table of regular sessions; holidays,
half days and auctions are not modelled.

The instability score is this application's heuristic, not an index published
by anyone else: ``INSTABILITY_FORMULA`` spells it out and every response carries
it with the per-term breakdown. Convergence follows the idea of World Monitor's
geographic convergence (cells where several event domains meet within a day),
written here from scratch.

Positions are geodetic degrees; distances use a sphere of mean Earth radius. No
layer here names or follows a private individual: GDELT actor names are dropped
at parse time.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import time
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from sda.routes import eonet as eonet_routes
from sda.routes import worldview as wv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EARTH_RADIUS_KM = 6371.0088
INTEL_DIR_ENV = "SDA_INTEL_DIR"

NE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_admin_0_countries.geojson"
)
BORDERS_FILE = "ne_50m_admin_0_countries.json"
BORDERS_RETRY_S = 600.0
BORDERS_ATTRIBUTION = "Borders: Natural Earth admin-0 1:50m (public domain)"

WB_INDICATOR_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/{indicator}"
    "?format=json&mrnev=1&per_page=400"
)
WB_COUNTRIES_URL = "https://api.worldbank.org/v2/country?format=json&per_page=400"
WB_TTL_S = 86400.0
#: key -> (World Bank indicator id, label)
WB_INDICATORS: dict[str, tuple[str, str]] = {
    "population": ("SP.POP.TOTL", "Population"),
    "gdp_usd": ("NY.GDP.MKTP.CD", "GDP (current US$)"),
    "gdp_per_capita_usd": ("NY.GDP.PCAP.CD", "GDP per capita (current US$)"),
    "inflation_pct": ("FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)"),
}

WHO_URL = (
    "https://www.who.int/api/emergencies/diseaseoutbreaknews"
    "?sf_provider=dynamicProvider372&sf_culture=en"
    "&$orderby=PublicationDateAndTime%20desc"
    "&$select=Title,ItemDefaultUrl,PublicationDateAndTime&$top=50"
)
WHO_ITEM_URL = "https://www.who.int/emergencies/disease-outbreak-news/item"
WHO_LIST_URL = "https://www.who.int/emergencies/disease-outbreak-news"
WHO_TTL_S = 3600.0
OUTBREAK_ACTIVE_DAYS = 30

FEODO_URL = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
FEODO_TTL_S = 3600.0

IODA_ALERTS_URL = (
    "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"
    "?from={start}&until={end}&entityType=country&limit=5000"
)
IODA_DASHBOARD_URL = "https://ioda.inetintel.cc.gatech.edu/country/{code}"
IODA_TTL_S = 600.0

GDELT_LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_EXPORT_URL = "https://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
GDELT_SLOT_S = 900
GDELT_WINDOW_SLOTS = 96  # 24 hours of 15-minute files
GDELT_RAW_SLOTS = 8  # full rows kept for the newest two hours
GDELT_BACKFILL_PER_REFRESH = 4  # older files fetched per refresh, besides the newest
GDELT_CHECK_S = 300.0
GDELT_RETRY_MISSING_S = 120.0
GDELT_MAX_ROWS = 6000
GDELT_UNREST_POINTS_PER_SLOT = 300
#: CAMEO root codes counted as unrest: protest, assault, fight, mass violence.
UNREST_ROOT_CODES = frozenset({"14", "18", "19", "20"})
CAMEO_ROOTS = {
    "01": "public statement", "02": "appeal", "03": "intent to cooperate",
    "04": "consult", "05": "diplomatic cooperation", "06": "material cooperation",
    "07": "aid", "08": "yield", "09": "investigate", "10": "demand",
    "11": "disapprove", "12": "reject", "13": "threaten", "14": "protest",
    "15": "force posture", "16": "reduce relations", "17": "coerce",
    "18": "assault", "19": "fight", "20": "mass violence",
}

QUAKE_TTL_S = 600.0
SNAPSHOT_TTL_S = 300.0
WINDOW_H = 24

CONVERGENCE_KINDS = ("quake", "fire", "eonet", "weather", "gdelt", "outage", "outbreak")
COUNTRY_WIDE_KINDS = frozenset({"outage", "outbreak"})
CONVERGENCE_MIN_KINDS = 3
CONVERGENCE_MIN_LOCATED = 2
CONVERGENCE_MAX_CELLS = 200
KIND_LABELS = {
    "quake": "earthquakes", "fire": "fires", "eonet": "EONET natural events",
    "weather": "weather alerts", "gdelt": "news unrest (GDELT)",
    "outage": "internet outage (country-wide)", "outbreak": "disease outbreak (country-wide)",
}

INSTABILITY_FORMULA: tuple[str, ...] = (
    "score = unrest_volume + unrest_share + news_tone + internet_outages + earthquakes"
    " + fires + weather_alerts + outbreaks, from 0 to 100",
    "unrest_volume = 20 x min(1, log10(1 + U) / log10(101)); U = GDELT events with CAMEO"
    " root 14, 18, 19 or 20 located in the country",
    "unrest_share = 10 x min(1, (U / E) / 0.3) when E >= 20; E = all GDELT events located"
    " in the country",
    "news_tone = 15 x clamp((-T - 1.5) / 5, 0, 1) when E >= 20; T = article-weighted mean"
    " GDELT AvgTone",
    "internet_outages = 20 x min(1, (C + 0.5 W) / 3); C and W = IODA data sources with a"
    " critical or a warning alert in 24 h",
    "earthquakes = 10 x clamp((M - 4.5) / 2.5, 0, 1); M = largest USGS magnitude in the"
    " past day",
    "fires = 10 x min(1, log10(1 + F) / log10(2001)); F = FIRMS VIIRS detections in 24 h"
    " (needs FIRMS_MAP_KEY)",
    "weather_alerts = 5 x min(1, (2 X + S) / 6); X and S = active NWS Extreme and Severe"
    " alerts (United States only)",
    "outbreaks = 10 x min(1, O / 2); O = WHO Disease Outbreak News items naming the country"
    " in 30 days",
)
INSTABILITY_NOTE = (
    "This application's own heuristic from its live feeds, not an official or published"
    " index. News volume follows media attention, so heavily covered countries score"
    " higher on the GDELT terms."
)
INSTABILITY_LEVELS = ((80.0, "severe"), (60.0, "high"), (40.0, "elevated"),
                      (20.0, "guarded"), (0.0, "low"))

_DAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MON_FRI = (0, 1, 2, 3, 4)
SUN_THU = (6, 0, 1, 2, 3)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """The current time in UTC. Tests replace this to pin the clock."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: object) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return out if math.isfinite(out) else None


def _int(value: object, default: int = 0) -> int:
    n = _num(value)
    return int(n) if n is not None else default


def _text(value: object, limit: int = 200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a sphere of mean Earth radius."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def intel_dir() -> Path:
    """Where the borders file lives: SDA_INTEL_DIR, else the user cache dir."""
    configured = os.environ.get(INTEL_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "intel"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "intel"


# ---------------------------------------------------------------------------
# Fetching (tests replace these three)
# ---------------------------------------------------------------------------


async def _fetch_json(url: str, purpose: str, timeout_s: float = 20.0) -> object:
    return await wv._get_json(url, timeout_s=timeout_s, purpose=purpose)


async def _fetch_text(url: str, purpose: str, timeout_s: float = 20.0) -> str | None:
    return await wv._get_text(url, timeout_s=timeout_s, purpose=purpose)


async def _fetch_bytes(url: str, purpose: str, timeout_s: float = 30.0) -> bytes | None:
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True,
            headers={"User-Agent": f"vantage/0.2 (worldview {purpose})", "Accept": "*/*"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.content
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Borders and point-in-country
# ---------------------------------------------------------------------------

_BAND_DEG = 0.5


class Ring:
    """One closed ring of (lon, lat) points with its edges bucketed into
    latitude bands, so a crossing test only walks the edges near the point."""

    __slots__ = ("bands", "points")

    def __init__(self, points: list[tuple[float, float]]):
        self.points = tuple(points)
        bands: dict[int, list[tuple[float, float, float, float]]] = {}
        n = len(points)
        for i in range(n):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % n]
            if y1 == y2:
                continue
            lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
            for band in range(math.floor(lo / _BAND_DEG), math.floor(hi / _BAND_DEG) + 1):
                bands.setdefault(band, []).append((x1, y1, x2, y2))
        self.bands = bands

    def contains(self, lon: float, lat: float) -> bool:
        edges = self.bands.get(math.floor(lat / _BAND_DEG))
        if not edges:
            return False
        inside = False
        for x1, y1, x2, y2 in edges:
            if (y1 > lat) != (y2 > lat):
                x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < x_cross:
                    inside = not inside
        return inside


@dataclass(frozen=True)
class Polygon:
    outer: Ring
    holes: tuple[Ring, ...]
    bbox: tuple[float, float, float, float]  # west, south, east, north

    def contains(self, lon: float, lat: float) -> bool:
        w, s, e, n = self.bbox
        if not (w <= lon <= e and s <= lat <= n):
            return False
        if not self.outer.contains(lon, lat):
            return False
        return not any(h.contains(lon, lat) for h in self.holes)


@dataclass(frozen=True)
class Country:
    iso3: str
    iso2: str
    name: str
    name_long: str
    formal_name: str
    continent: str
    subregion: str
    region_wb: str
    economy: str
    income_group: str
    pop_est: float | None
    pop_year: int | None
    label_lat: float
    label_lon: float
    bbox: tuple[float, float, float, float]
    main_bbox: tuple[float, float, float, float]
    polygons: tuple[Polygon, ...] = field(repr=False)

    def brief(self) -> dict[str, Any]:
        return {"iso3": self.iso3, "iso2": self.iso2, "name": self.name,
                "lat": self.label_lat, "lon": self.label_lon}


def _code(props: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = props.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "-99":
            return value.strip().upper()
    return ""


def _ring_points(raw: object) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return pts
    for p in raw:
        if not isinstance(p, list) or len(p) < 2:
            continue
        lon, lat = _num(p[0]), _num(p[1])
        if lon is None or lat is None:
            continue
        pts.append((lon, lat))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _polygons(geometry: object) -> list[Polygon]:
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return []
    raw_polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    out: list[Polygon] = []
    for raw in raw_polys:
        if not isinstance(raw, list) or not raw:
            continue
        outer = _ring_points(raw[0])
        if len(outer) < 3:
            continue
        holes = [h for h in (_ring_points(r) for r in raw[1:]) if len(h) >= 3]
        lons = [p[0] for p in outer]
        lats = [p[1] for p in outer]
        out.append(Polygon(
            outer=Ring(outer), holes=tuple(Ring(h) for h in holes),
            bbox=(min(lons), min(lats), max(lons), max(lats)),
        ))
    return out


def _merge_bbox(boxes: Iterable[tuple[float, float, float, float]]
                ) -> tuple[float, float, float, float]:
    ws, ss, es, ns = zip(*boxes, strict=True)
    return min(ws), min(ss), max(es), max(ns)


def parse_countries(geojson: object) -> list[Country]:
    """Natural Earth admin-0 GeoJSON (or this module's disk copy) to countries.

    The code is ISO_A3_EH, else ISO_A3, else ADM0_A3, because Natural Earth
    writes -99 in ISO_A3 for France, Norway and a few disputed areas. Features
    that share a code are merged.
    """
    if not isinstance(geojson, dict) or not isinstance(geojson.get("features"), list):
        return []
    merged: dict[str, dict[str, Any]] = {}
    for feature in geojson["features"]:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        iso3 = _code(props, "ISO_A3_EH", "ISO_A3", "ADM0_A3")
        polys = _polygons(feature.get("geometry"))
        if not iso3 or not polys:
            continue
        if iso3 in merged:
            merged[iso3]["polygons"].extend(polys)
            continue
        merged[iso3] = {"props": props, "polygons": polys}

    countries: list[Country] = []
    for iso3, item in merged.items():
        p = item["props"]
        polys = item["polygons"]
        bbox = _merge_bbox(poly.bbox for poly in polys)
        main = max(polys, key=lambda q: (q.bbox[2] - q.bbox[0]) * (q.bbox[3] - q.bbox[1]))
        label_lon = _num(p.get("LABEL_X"))
        label_lat = _num(p.get("LABEL_Y"))
        if label_lon is None or label_lat is None:
            label_lon = (main.bbox[0] + main.bbox[2]) / 2
            label_lat = (main.bbox[1] + main.bbox[3]) / 2
        pop_year = _num(p.get("POP_YEAR"))
        countries.append(Country(
            iso3=iso3,
            iso2=_code(p, "ISO_A2_EH", "ISO_A2"),
            name=_text(p.get("NAME"), 80) or _text(p.get("ADMIN"), 80) or iso3,
            name_long=_text(p.get("NAME_LONG"), 120),
            formal_name=_text(p.get("FORMAL_EN"), 160),
            continent=_text(p.get("CONTINENT"), 40),
            subregion=_text(p.get("SUBREGION"), 60),
            region_wb=_text(p.get("REGION_WB"), 60),
            economy=_text(p.get("ECONOMY"), 60),
            income_group=_text(p.get("INCOME_GRP"), 60),
            pop_est=_num(p.get("POP_EST")),
            pop_year=int(pop_year) if pop_year is not None else None,
            label_lat=label_lat,
            label_lon=label_lon,
            bbox=bbox,
            main_bbox=main.bbox,
            polygons=tuple(polys),
        ))
    countries.sort(key=lambda c: c.name)
    return countries


_GRID_DEG = 5.0
_MEMO_MAX = 50000


class CountryIndex:
    """Countries with a coarse grid over their polygon boxes for fast lookup."""

    def __init__(self, countries: list[Country]):
        self.countries = countries
        self.by_iso3 = {c.iso3: c for c in countries}
        self.by_iso2 = {c.iso2: c for c in countries if c.iso2}
        grid: dict[tuple[int, int], list[tuple[Country, Polygon]]] = {}
        for c in countries:
            for poly in c.polygons:
                w, s, e, n = poly.bbox
                for gy in range(math.floor(s / _GRID_DEG), math.floor(n / _GRID_DEG) + 1):
                    for gx in range(math.floor(w / _GRID_DEG), math.floor(e / _GRID_DEG) + 1):
                        grid.setdefault((gy, gx), []).append((c, poly))
        self._grid = grid
        self._memo: dict[tuple[int, int], str] = {}
        self._names: list[tuple[str, str]] | None = None

    def lookup(self, code: str) -> Country | None:
        code = (code or "").strip().upper()
        if len(code) == 2:
            return self.by_iso2.get(code)
        return self.by_iso3.get(code)

    def locate(self, lat: float, lon: float, memo: bool = False) -> Country | None:
        """The country containing the point, or None at sea.

        With ``memo`` the answer is remembered on a 0.05 degree grid, which is
        what the bulk feeds use; a click asks without it.
        """
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        key = (round(lat * 20), round(lon * 20))
        if memo and key in self._memo:
            return self.by_iso3.get(self._memo[key])
        found: Country | None = None
        cell = (math.floor(lat / _GRID_DEG), math.floor(lon / _GRID_DEG))
        for c, poly in self._grid.get(cell, ()):
            if poly.contains(lon, lat):
                found = c
                break
        if memo:
            if len(self._memo) >= _MEMO_MAX:
                self._memo.clear()
            self._memo[key] = found.iso3 if found else ""
        return found

    def name_table(self) -> list[tuple[str, str]]:
        """(lower-case name, iso3), longest first, for matching names in text."""
        if self._names is None:
            seen: dict[str, str] = {}
            for alias, iso3 in WHO_ALIASES.items():
                if iso3 in self.by_iso3:
                    seen[alias] = iso3
            for c in self.countries:
                for name in (c.name, c.name_long, c.formal_name):
                    low = _fold(name).strip()
                    if len(low) >= 4 and low not in seen:
                        seen[low] = c.iso3
            self._names = sorted(seen.items(), key=lambda kv: -len(kv[0]))
        return self._names


def simplify_ring(ring: Ring, tol_deg: float, keep_small: bool = False) -> list[float]:
    """A ring as a flat [lon, lat, ...] list keeping points at least tol apart.

    Rounded to 0.01 degree (0.001 under a 0.05 degree tolerance). A ring with
    fewer than four surviving points is dropped, unless ``keep_small`` asks for
    it (a small country's main polygon), which keeps up to eight evenly spaced
    original points at 0.001 degree instead.
    """
    places = 3 if tol_deg < 0.05 else 2
    kept: list[tuple[float, float]] = []
    for lon, lat in ring.points:
        if kept:
            plon, plat = kept[-1]
            if max(abs(lon - plon), abs(lat - plat)) < tol_deg:
                continue
        kept.append((lon, lat))
    if len(kept) < 4:
        if not keep_small or len(ring.points) < 3:
            return []
        step = max(1, len(ring.points) // 8)
        kept = list(ring.points[::step])[:8]
        places = 3
    flat: list[float] = []
    for lon, lat in kept:
        flat.extend((round(lon, places), round(lat, places)))
    return flat


def borders_payload(index: CountryIndex, tol_deg: float) -> dict[str, Any]:
    """Simplified polygons for the browser's choropleth and click targets.

    Every country keeps at least its main polygon, however small.
    """
    out: list[dict[str, Any]] = []
    points = 0
    for c in index.countries:
        if c.iso3 == "ATA":
            continue  # Antarctica: no feed scores it and its polygon wraps a pole
        polys: list[list[list[float]]] = []
        for poly in c.polygons:
            outer = simplify_ring(poly.outer, tol_deg, keep_small=poly.bbox == c.main_bbox)
            if not outer:
                continue
            rings = [outer] + [h for h in (simplify_ring(r, tol_deg) for r in poly.holes) if h]
            points += sum(len(r) // 2 for r in rings)
            polys.append(rings)
        if polys:
            out.append({"iso3": c.iso3, "name": c.name, "polygons": polys})
    return {"tol_deg": tol_deg, "countries": out, "count": len(out), "points": points,
            "attribution": BORDERS_ATTRIBUTION}


def _compact_geojson(data: dict[str, Any]) -> dict[str, Any]:
    """The disk copy: only the properties used here, coordinates to 4 places."""
    keep = ("NAME", "NAME_LONG", "ADMIN", "FORMAL_EN", "ISO_A3", "ISO_A3_EH", "ADM0_A3",
            "ISO_A2", "ISO_A2_EH", "CONTINENT", "SUBREGION", "REGION_WB", "POP_EST",
            "POP_YEAR", "ECONOMY", "INCOME_GRP", "LABEL_X", "LABEL_Y")

    def rnd(c: Any) -> Any:
        if isinstance(c, list) and c and isinstance(c[0], (int, float)):
            return [round(float(v), 4) for v in c[:2]]
        if isinstance(c, list):
            return [rnd(x) for x in c]
        return c

    feats = []
    for f in data.get("features") or []:
        if not isinstance(f, dict) or not isinstance(f.get("properties"), dict):
            continue
        g = f.get("geometry")
        if not isinstance(g, dict):
            continue
        feats.append({
            "type": "Feature",
            "properties": {k: f["properties"].get(k) for k in keep},
            "geometry": {"type": g.get("type"), "coordinates": rnd(g.get("coordinates"))},
        })
    return {"type": "FeatureCollection", "source": NE_URL, "features": feats}


# ---------------------------------------------------------------------------
# Time zones and exchange hours
# ---------------------------------------------------------------------------

_ISO6709 = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?([+-])(\d{3})(\d{2})(\d{2})?$")


def parse_iso6709(text: str) -> tuple[float, float] | None:
    """zone.tab coordinates (+DDMM+DDDMM or with seconds) to (lat, lon)."""
    m = _ISO6709.match(text.strip())
    if not m:
        return None
    lat = int(m.group(2)) + int(m.group(3)) / 60 + int(m.group(4) or 0) / 3600
    lon = int(m.group(6)) + int(m.group(7)) / 60 + int(m.group(8) or 0) / 3600
    return (lat if m.group(1) == "+" else -lat, lon if m.group(5) == "+" else -lon)


def _read_zone_tab() -> str:
    try:
        from importlib.resources import files

        return files("tzdata").joinpath("zoneinfo").joinpath("zone.tab").read_text(
            encoding="utf-8")
    except Exception:
        pass
    try:
        return Path("/usr/share/zoneinfo/zone.tab").read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=1)
def zone_table() -> dict[str, tuple[tuple[str, float, float], ...]]:
    """Country code -> ((zone, lat, lon), ...) from the IANA zone.tab."""
    table: dict[str, list[tuple[str, float, float]]] = {}
    for line in _read_zone_tab().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pos = parse_iso6709(parts[1])
        if pos is None:
            continue
        table.setdefault(parts[0].strip().upper(), []).append((parts[2].strip(), pos[0], pos[1]))
    return {k: tuple(v) for k, v in table.items()}


def zone_for_country(iso2: str, lat: float, lon: float) -> str | None:
    """The IANA zone of the country whose reference city is nearest the point."""
    zones = zone_table().get((iso2 or "").upper())
    if not zones:
        return None
    return min(zones, key=lambda z: haversine_km(lat, lon, z[1], z[2]))[0]


def local_time(zone: str, now: datetime) -> dict[str, Any] | None:
    try:
        tz = ZoneInfo(zone)
    except Exception:
        return None
    local = now.astimezone(tz)
    offset = local.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return {
        "zone": zone,
        "time": local.strftime("%H:%M"),
        "date": local.strftime("%Y-%m-%d"),
        "weekday": _DAY[local.weekday()],
        "utc_offset": f"{sign}{minutes // 60:02d}:{minutes % 60:02d}",
    }


@dataclass(frozen=True)
class Exchange:
    mic: str
    name: str
    city: str
    iso2: str
    tz: str
    sessions: tuple[tuple[str, str], ...]
    days: tuple[int, ...] = MON_FRI  # Monday is 0


#: Regular continuous sessions from each exchange's published hours. Auctions,
#: holidays, half days and per-day exceptions are not modelled.
EXCHANGES: tuple[Exchange, ...] = (
    Exchange("XNYS", "New York Stock Exchange", "New York", "US", "America/New_York",
             (("09:30", "16:00"),)),
    Exchange("XNAS", "Nasdaq", "New York", "US", "America/New_York", (("09:30", "16:00"),)),
    Exchange("XTSE", "Toronto Stock Exchange", "Toronto", "CA", "America/Toronto",
             (("09:30", "16:00"),)),
    Exchange("XMEX", "Bolsa Mexicana de Valores", "Mexico City", "MX", "America/Mexico_City",
             (("08:30", "15:00"),)),
    Exchange("BVMF", "B3", "Sao Paulo", "BR", "America/Sao_Paulo", (("10:00", "17:00"),)),
    Exchange("XBUE", "Bolsas y Mercados Argentinos", "Buenos Aires", "AR",
             "America/Argentina/Buenos_Aires", (("11:00", "17:00"),)),
    Exchange("XSGO", "Bolsa de Santiago", "Santiago", "CL", "America/Santiago",
             (("09:30", "16:00"),)),
    Exchange("XBOG", "Bolsa de Valores de Colombia", "Bogota", "CO", "America/Bogota",
             (("09:30", "16:00"),)),
    Exchange("XLON", "London Stock Exchange", "London", "GB", "Europe/London",
             (("08:00", "16:30"),)),
    Exchange("XDUB", "Euronext Dublin", "Dublin", "IE", "Europe/Dublin", (("08:00", "16:30"),)),
    Exchange("XPAR", "Euronext Paris", "Paris", "FR", "Europe/Paris", (("09:00", "17:30"),)),
    Exchange("XAMS", "Euronext Amsterdam", "Amsterdam", "NL", "Europe/Amsterdam",
             (("09:00", "17:30"),)),
    Exchange("XBRU", "Euronext Brussels", "Brussels", "BE", "Europe/Brussels",
             (("09:00", "17:30"),)),
    Exchange("XLIS", "Euronext Lisbon", "Lisbon", "PT", "Europe/Lisbon", (("08:00", "16:30"),)),
    Exchange("XMIL", "Euronext Milan", "Milan", "IT", "Europe/Rome", (("09:00", "17:30"),)),
    Exchange("XOSL", "Euronext Oslo Bors", "Oslo", "NO", "Europe/Oslo", (("09:00", "16:20"),)),
    Exchange("XETR", "Xetra (Deutsche Boerse)", "Frankfurt", "DE", "Europe/Berlin",
             (("09:00", "17:30"),)),
    Exchange("XSWX", "SIX Swiss Exchange", "Zurich", "CH", "Europe/Zurich",
             (("09:00", "17:30"),)),
    Exchange("XMAD", "Bolsa de Madrid", "Madrid", "ES", "Europe/Madrid", (("09:00", "17:30"),)),
    Exchange("XWBO", "Wiener Boerse", "Vienna", "AT", "Europe/Vienna", (("09:00", "17:30"),)),
    Exchange("XSTO", "Nasdaq Stockholm", "Stockholm", "SE", "Europe/Stockholm",
             (("09:00", "17:30"),)),
    Exchange("XCSE", "Nasdaq Copenhagen", "Copenhagen", "DK", "Europe/Copenhagen",
             (("09:00", "17:00"),)),
    Exchange("XHEL", "Nasdaq Helsinki", "Helsinki", "FI", "Europe/Helsinki",
             (("10:00", "18:30"),)),
    Exchange("XWAR", "Warsaw Stock Exchange", "Warsaw", "PL", "Europe/Warsaw",
             (("09:00", "17:00"),)),
    Exchange("MISX", "Moscow Exchange", "Moscow", "RU", "Europe/Moscow", (("10:00", "18:50"),)),
    Exchange("XIST", "Borsa Istanbul", "Istanbul", "TR", "Europe/Istanbul",
             (("10:00", "18:00"),)),
    Exchange("XSAU", "Saudi Exchange (Tadawul)", "Riyadh", "SA", "Asia/Riyadh",
             (("10:00", "15:00"),), SUN_THU),
    Exchange("XDFM", "Dubai Financial Market", "Dubai", "AE", "Asia/Dubai",
             (("10:00", "15:00"),)),
    Exchange("XADS", "Abu Dhabi Securities Exchange", "Abu Dhabi", "AE", "Asia/Dubai",
             (("10:00", "15:00"),)),
    Exchange("DSMD", "Qatar Stock Exchange", "Doha", "QA", "Asia/Qatar",
             (("09:30", "13:15"),), SUN_THU),
    Exchange("XKUW", "Boursa Kuwait", "Kuwait City", "KW", "Asia/Kuwait",
             (("09:00", "12:40"),), SUN_THU),
    Exchange("XCAI", "Egyptian Exchange", "Cairo", "EG", "Africa/Cairo",
             (("10:00", "14:30"),), SUN_THU),
    Exchange("XJSE", "Johannesburg Stock Exchange", "Johannesburg", "ZA",
             "Africa/Johannesburg", (("09:00", "17:00"),)),
    Exchange("XNSA", "Nigerian Exchange", "Lagos", "NG", "Africa/Lagos", (("10:00", "14:30"),)),
    Exchange("XNAI", "Nairobi Securities Exchange", "Nairobi", "KE", "Africa/Nairobi",
             (("09:00", "15:00"),)),
    Exchange("XCAS", "Casablanca Stock Exchange", "Casablanca", "MA", "Africa/Casablanca",
             (("09:30", "15:30"),)),
    Exchange("XNSE", "National Stock Exchange of India", "Mumbai", "IN", "Asia/Kolkata",
             (("09:15", "15:30"),)),
    Exchange("XBOM", "BSE", "Mumbai", "IN", "Asia/Kolkata", (("09:15", "15:30"),)),
    Exchange("XKAR", "Pakistan Stock Exchange", "Karachi", "PK", "Asia/Karachi",
             (("09:30", "15:30"),)),
    Exchange("XSHG", "Shanghai Stock Exchange", "Shanghai", "CN", "Asia/Shanghai",
             (("09:30", "11:30"), ("13:00", "15:00"))),
    Exchange("XSHE", "Shenzhen Stock Exchange", "Shenzhen", "CN", "Asia/Shanghai",
             (("09:30", "11:30"), ("13:00", "15:00"))),
    Exchange("XHKG", "Hong Kong Exchanges", "Hong Kong", "HK", "Asia/Hong_Kong",
             (("09:30", "12:00"), ("13:00", "16:00"))),
    Exchange("XJPX", "Tokyo Stock Exchange", "Tokyo", "JP", "Asia/Tokyo",
             (("09:00", "11:30"), ("12:30", "15:30"))),
    Exchange("XKRX", "Korea Exchange", "Seoul", "KR", "Asia/Seoul", (("09:00", "15:30"),)),
    Exchange("XTAI", "Taiwan Stock Exchange", "Taipei", "TW", "Asia/Taipei",
             (("09:00", "13:30"),)),
    Exchange("XSES", "Singapore Exchange", "Singapore", "SG", "Asia/Singapore",
             (("09:00", "12:00"), ("13:00", "17:00"))),
    Exchange("XKLS", "Bursa Malaysia", "Kuala Lumpur", "MY", "Asia/Kuala_Lumpur",
             (("09:00", "12:30"), ("14:30", "17:00"))),
    Exchange("XBKK", "Stock Exchange of Thailand", "Bangkok", "TH", "Asia/Bangkok",
             (("10:00", "12:30"), ("14:30", "16:30"))),
    Exchange("XIDX", "Indonesia Stock Exchange", "Jakarta", "ID", "Asia/Jakarta",
             (("09:00", "12:00"), ("13:30", "15:50"))),
    Exchange("XSTC", "Ho Chi Minh Stock Exchange", "Ho Chi Minh City", "VN",
             "Asia/Ho_Chi_Minh", (("09:00", "11:30"), ("13:00", "14:45"))),
    Exchange("XASX", "ASX", "Sydney", "AU", "Australia/Sydney", (("10:00", "16:00"),)),
    Exchange("XNZE", "NZX", "Wellington", "NZ", "Pacific/Auckland", (("10:00", "16:45"),)),
)
EXCHANGE_NOTE = ("Regular session hours from a static table; public holidays, half days"
                 " and auctions are not modelled. No prices.")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def exchange_status(ex: Exchange, now: datetime) -> dict[str, Any]:
    """Whether the exchange's regular session is running at ``now``."""
    local = now.astimezone(ZoneInfo(ex.tz))
    minute = local.hour * 60 + local.minute
    spans = [(_minutes(a), _minutes(b)) for a, b in ex.sessions]
    trading_day = local.weekday() in ex.days
    is_open = False
    state = "closed" if trading_day else "non-trading day"
    upcoming = ""
    if trading_day:
        for start, end in spans:
            if start <= minute < end:
                is_open, state, upcoming = True, "open", f"closes {_hhmm(end)} local"
                break
        else:
            if spans[0][0] <= minute < spans[-1][1]:
                state = "midday break"
            later = [start for start, _ in spans if start > minute]
            if later:
                upcoming = f"opens {_hhmm(later[0])} local"
    if not upcoming:
        for step in range(1, 8):
            day = (local.weekday() + step) % 7
            if day in ex.days:
                upcoming = f"opens {_DAY[day]} {_hhmm(spans[0][0])} local"
                break
    return {
        "mic": ex.mic, "name": ex.name, "city": ex.city, "iso2": ex.iso2, "tz": ex.tz,
        "open": is_open, "state": state, "local_time": local.strftime("%H:%M"),
        "weekday": _DAY[local.weekday()],
        "sessions": ", ".join(f"{a}-{b}" for a, b in ex.sessions),
        "days": "Sun-Thu" if ex.days == SUN_THU else "Mon-Fri",
        "next": upcoming,
    }


def exchanges_for(iso2: str, now: datetime) -> list[dict[str, Any]]:
    code = (iso2 or "").upper()
    return [exchange_status(ex, now) for ex in EXCHANGES if ex.iso2 == code]


# ---------------------------------------------------------------------------
# World Bank
# ---------------------------------------------------------------------------


def parse_wb_indicator(data: object) -> dict[str, dict[str, Any]]:
    """Indicator rows (mrnev=1) to {iso3: {value, year}}; nulls dropped."""
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in data[1]:
        if not isinstance(row, dict):
            continue
        code = row.get("countryiso3code")
        value = _num(row.get("value"))
        year = _num(str(row.get("date") or "")[:4])
        if not isinstance(code, str) or len(code) != 3 or value is None or year is None:
            continue
        out[code.upper()] = {"value": value, "year": int(year)}
    return out


def parse_wb_countries(data: object) -> dict[str, dict[str, Any]]:
    """The World Bank country list to {iso3: capital, coordinates, region, income}.

    Aggregates (regions and income groups) are skipped.
    """
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in data[1]:
        if not isinstance(row, dict):
            continue
        code = row.get("id")
        region = row.get("region") if isinstance(row.get("region"), dict) else {}
        region_name = _text((region or {}).get("value"), 80)
        if not isinstance(code, str) or len(code) != 3 or region_name == "Aggregates":
            continue
        income = row.get("incomeLevel") if isinstance(row.get("incomeLevel"), dict) else {}
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
        out[code.upper()] = {
            "capital": _text(row.get("capitalCity"), 80),
            "lat": lat, "lon": lon,
            "region": region_name,
            "income": _text((income or {}).get("value"), 60),
        }
    return out


# ---------------------------------------------------------------------------
# WHO Disease Outbreak News
# ---------------------------------------------------------------------------

#: WHO spellings that differ from Natural Earth's names.
WHO_ALIASES: dict[str, str] = {
    "democratic republic of the congo": "COD", "dr congo": "COD",
    "republic of the congo": "COG", "the russian federation": "RUS",
    "russian federation": "RUS", "kingdom of saudi arabia": "SAU",
    "united republic of tanzania": "TZA", "turkiye": "TUR",
    "viet nam": "VNM", "cote d'ivoire": "CIV",
    "republic of korea": "KOR", "democratic people's republic of korea": "PRK",
    "lao people's democratic republic": "LAO", "iran (islamic republic of)": "IRN",
    "islamic republic of iran": "IRN", "bolivia (plurinational state of)": "BOL",
    "venezuela (bolivarian republic of)": "VEN", "syrian arab republic": "SYR",
    "united states of america": "USA", "united kingdom": "GBR",
    "united kingdom of great britain and northern ireland": "GBR", "cabo verde": "CPV",
    "czechia": "CZE", "eswatini": "SWZ", "republic of moldova": "MDA",
    "timor-leste": "TLS", "new mexico": "USA",
}
#: A hyphen, en dash or em dash between spaces separates disease and place.
_WHO_DASH = re.compile(r"\s+[-" + chr(0x2013) + chr(0x2014) + r"]\s+")
_WHO_SLUG = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")


def _fold(text: str) -> str:
    """Lower case without accents, so "Turkiye" matches its accented spelling."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def split_who_title(title: str) -> tuple[str, str]:
    """"Disease - Place" (any dash), "Disease, Place" or "Disease in Place"."""
    parts = _WHO_DASH.split(title, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    if ", " in title:
        disease, place = title.split(", ", 1)
        return disease.strip(), place.strip()
    m = re.search(r"\s+in\s+(?=[A-Z])", title)
    if m:
        return title[:m.start()].strip(), title[m.end():].strip()
    return title.strip(), ""


def match_countries(text: str, names: list[tuple[str, str]]) -> list[str]:
    """ISO3 codes of the country names in ``text``, in order of appearance.

    Longest names claim their span first, so "Papua New Guinea" never also
    counts as "Guinea" and "Democratic Republic of the Congo" never as "Congo".
    """
    low = _fold(text)
    taken = [False] * len(low)
    hits: list[tuple[int, str]] = []
    for name, iso3 in names:
        start = 0
        while True:
            at = low.find(name, start)
            if at < 0:
                break
            end = at + len(name)
            start = at + 1
            if (at > 0 and low[at - 1].isalpha()) or (end < len(low) and low[end].isalpha()):
                continue
            if any(taken[at:end]):
                continue
            for i in range(at, end):
                taken[i] = True
            hits.append((at, iso3))
    hits.sort()
    out: list[str] = []
    for _, iso3 in hits:
        if iso3 not in out:
            out.append(iso3)
    return out


def parse_who(data: object, index: CountryIndex | None) -> list[dict[str, Any]]:
    """WHO DON items, newest first, each with the countries it names."""
    rows = data.get("value") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    names = index.name_table() if index else []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("Title"), 300)
        if not title:
            continue
        slug = _text(item.get("ItemDefaultUrl"), 220).lstrip("/")
        url = f"{WHO_ITEM_URL}/{quote(slug)}" if _WHO_SLUG.match(slug) else WHO_LIST_URL
        published = _parse_iso(item.get("PublicationDateAndTime"))
        disease, place = split_who_title(title)
        codes = match_countries(place, names) if place else []
        if not codes:
            codes = match_countries(title, names)
        countries = []
        for code in codes:
            c = index.by_iso3.get(code) if index else None
            if c is not None:
                countries.append(c.brief())
        out.append({
            "id": slug or title,
            "title": title,
            "disease": disease,
            "place": place,
            "published": _iso(published) if published else "",
            "url": url,
            "countries": countries,
        })
    out.sort(key=lambda o: o["published"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Feodo Tracker (botnet C2 servers)
# ---------------------------------------------------------------------------


def parse_feodo(data: object) -> dict[str, dict[str, Any]]:
    """The Feodo IP blocklist aggregated to {iso2: servers, online, malware}.

    Addresses are counted, never passed on.
    """
    if not isinstance(data, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        cc = _text(row.get("country"), 2).upper()
        if len(cc) != 2 or not cc.isalpha():
            continue
        agg = out.setdefault(cc, {"servers": 0, "online": 0, "malware": {}, "last_online": ""})
        agg["servers"] += 1
        if _text(row.get("status"), 20).lower() == "online":
            agg["online"] += 1
        family = _text(row.get("malware"), 40) or "unknown"
        agg["malware"][family] = agg["malware"].get(family, 0) + 1
        last = _text(row.get("last_online"), 20)
        if last > agg["last_online"]:
            agg["last_online"] = last
    return out


# ---------------------------------------------------------------------------
# IODA internet outage alerts
# ---------------------------------------------------------------------------


def parse_ioda_alerts(data: object) -> list[dict[str, Any]]:
    """IODA v2 alerts to plain rows, every level kept (normal marks recovery)."""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        entity = a.get("entity") if isinstance(a.get("entity"), dict) else {}
        code = _text((entity or {}).get("code"), 2).upper()
        level = _text(a.get("level"), 20).lower()
        t = _num(a.get("time"))
        if len(code) != 2 or level not in ("critical", "warning", "normal") or t is None:
            continue
        out.append({
            "iso2": code,
            "name": _text((entity or {}).get("name"), 80),
            "datasource": _text(a.get("datasource"), 30),
            "level": level,
            "time": int(t),
            "value": _num(a.get("value")),
            "history": _num(a.get("historyValue")),
        })
    out.sort(key=lambda r: r["time"])
    return out


def aggregate_outages(alerts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per country: data sources with critical or warning alerts, and whether
    the latest alert of any source is still not normal (``ongoing``)."""
    out: dict[str, dict[str, Any]] = {}
    latest: dict[tuple[str, str], str] = {}
    for a in alerts:
        latest[(a["iso2"], a["datasource"])] = a["level"]
        if a["level"] == "normal":
            continue
        agg = out.setdefault(a["iso2"], {
            "name": a["name"], "critical_sources": set(), "warning_sources": set(),
            "alerts": 0, "last": 0,
        })
        agg["alerts"] += 1
        agg["last"] = max(agg["last"], a["time"])
        agg["critical_sources" if a["level"] == "critical" else "warning_sources"].add(
            a["datasource"])
    for code, agg in out.items():
        crit = sorted(agg.pop("critical_sources"))
        warn = sorted(set(agg.pop("warning_sources")) - set(crit))
        agg["critical"] = len(crit)
        agg["warning"] = len(warn)
        agg["datasources"] = crit + warn
        agg["ongoing"] = any(level != "normal" for (cc, _), level in latest.items() if cc == code)
        agg["last"] = _iso(datetime.fromtimestamp(agg["last"], tz=UTC))
        agg["url"] = IODA_DASHBOARD_URL.format(code=code)
    return out


# ---------------------------------------------------------------------------
# GDELT 2.0 event export
# ---------------------------------------------------------------------------

_STAMP = re.compile(r"/(\d{14})\.export\.CSV\.zip")


def parse_gdelt_lastupdate(text: str | None) -> str | None:
    """The 14-digit stamp of the newest export file in lastupdate.txt."""
    for line in (text or "").splitlines():
        m = _STAMP.search(line)
        if m:
            return m.group(1)
    return None


def slot_stamps(newest: str, count: int) -> list[str]:
    """``count`` 15-minute stamps from ``newest`` backwards, newest first."""
    try:
        t0 = datetime.strptime(newest, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return []
    return [(t0 - timedelta(seconds=GDELT_SLOT_S * i)).strftime("%Y%m%d%H%M%S")
            for i in range(max(0, count))]


def _stamp_iso(stamp: str) -> str:
    try:
        return _iso(datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC))
    except ValueError:
        return ""


def unzip_export(blob: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                return None
            return zf.read(names[0]).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        return None


def parse_gdelt_export(text: str, max_rows: int = GDELT_MAX_ROWS) -> list[dict[str, Any]]:
    """GDELT 2.0 event export (61 tab-separated columns) to located events.

    Rows without an action location are dropped, and so are actor names, which
    can name people. ``date`` is when GDELT added the row (UTC).
    """
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) < 61:
            continue
        lat, lon = _num(c[56]), _num(c[57])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        root = c[28].strip()
        source_url = c[60].strip()
        if not source_url.startswith(("http://", "https://")):
            source_url = ""
        out.append({
            "id": c[0].strip(),
            "date": _stamp_iso(c[59].strip()),
            "lat": lat,
            "lon": lon,
            "place": c[52].strip()[:120],
            "country_fips": c[53].strip()[:2],
            "event_code": c[26].strip(),
            "root_code": root,
            "event": CAMEO_ROOTS.get(root, ""),
            "quad_class": _int(c[29]),
            "goldstein": _num(c[30]) or 0.0,
            "mentions": _int(c[31]),
            "sources": _int(c[32]),
            "articles": _int(c[33]),
            "tone": _num(c[34]) or 0.0,
            "source_url": source_url[:500],
        })
        if len(out) >= max_rows:
            break
    return out


@dataclass
class _GdeltSlot:
    stamp: str
    rows: list[dict[str, Any]] | None
    # iso3 -> [events, articles, tone x articles, unrest events]
    by_country: dict[str, list[float]] | None = None
    unrest: list[tuple[float, float, str, int, str]] = field(default_factory=list)


def _locate_slot(slot: _GdeltSlot, index: CountryIndex) -> None:
    if slot.by_country is not None or slot.rows is None:
        return
    agg: dict[str, list[float]] = {}
    unrest: list[tuple[float, float, str, int, str]] = []
    for r in slot.rows:
        c = index.locate(r["lat"], r["lon"], memo=True)
        is_unrest = r["root_code"] in UNREST_ROOT_CODES
        if is_unrest and len(unrest) < GDELT_UNREST_POINTS_PER_SLOT:
            unrest.append((r["lat"], r["lon"], r["root_code"], r["articles"], r["place"]))
        if c is None:
            continue
        a = agg.setdefault(c.iso3, [0.0, 0.0, 0.0, 0.0])
        weight = max(1, r["articles"])
        a[0] += 1
        a[1] += weight
        a[2] += r["tone"] * weight
        if is_unrest:
            a[3] += 1
    slot.by_country = agg
    slot.unrest = unrest


@dataclass
class _GdeltWindow:
    slots: dict[str, _GdeltSlot] = field(default_factory=dict)
    newest: str = ""
    checked_at: float = -math.inf
    failed: dict[str, float] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------


@dataclass
class _Cached:
    value: Any = None
    attempted_at: float = -math.inf
    error: str = ""


@dataclass
class _State:
    index: CountryIndex | None = None
    borders_attempted_at: float = -math.inf
    borders_error: str = ""
    borders_cache: dict[float, dict[str, Any]] = field(default_factory=dict)
    cached: dict[str, _Cached] = field(default_factory=dict)
    gdelt: _GdeltWindow = field(default_factory=_GdeltWindow)
    snapshot: dict[str, Any] | None = None
    snapshot_at: float = -math.inf
    locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = field(
        default_factory=dict)


_state = _State()


def reset_state() -> None:
    """Forget everything held in memory (tests, and /doctor style resets)."""
    global _state
    _state = _State()
    zone_table.cache_clear()


def _lock(name: str) -> asyncio.Lock:
    """One lock per name per event loop (TestClient runs a loop per client)."""
    loop = asyncio.get_running_loop()
    held = _state.locks.get(name)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _state.locks[name] = held
    return held[1]


async def _cached(name: str, ttl_s: float,
                  loader: Callable[[], Awaitable[Any]]) -> tuple[Any, str]:
    """The last good value of ``loader`` and an error note.

    A failed load (None or an exception) keeps the previous value in service
    and waits out the same ``ttl_s`` before trying again.
    """
    entry = _state.cached.setdefault(name, _Cached())
    if time.monotonic() - entry.attempted_at >= ttl_s:
        async with _lock(name):
            if time.monotonic() - entry.attempted_at >= ttl_s:
                try:
                    value = await loader()
                except Exception:
                    value = None
                entry.attempted_at = time.monotonic()
                if value is not None:
                    entry.value, entry.error = value, ""
                else:
                    entry.error = f"{name} unavailable"
    return entry.value, entry.error


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_borders_file(path: Path) -> CountryIndex | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    countries = parse_countries(data)
    return CountryIndex(countries) if countries else None


def _save_borders_file(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_compact_geojson(data), separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


async def get_index() -> CountryIndex | None:
    """Country borders: memory, else the disk copy, else Natural Earth.

    A failed download is retried after ten minutes; until then this is None and
    the routes answer 503 with the reason.
    """
    if _state.index is not None:
        return _state.index
    async with _lock("borders"):
        if _state.index is not None:
            return _state.index
        path = intel_dir() / BORDERS_FILE
        index = await asyncio.to_thread(_load_borders_file, path)
        if index is not None:
            _state.index = index
            return index
        if time.monotonic() - _state.borders_attempted_at < BORDERS_RETRY_S:
            return None
        _state.borders_attempted_at = time.monotonic()
        data = await _fetch_json(NE_URL, "borders", timeout_s=90.0)
        countries = await asyncio.to_thread(parse_countries, data)
        if not countries or not isinstance(data, dict):
            _state.borders_error = ("Natural Earth country borders did not download;"
                                    " retrying in 10 minutes")
            return None
        _state.index = CountryIndex(countries)
        _state.borders_error = ""
        await asyncio.to_thread(_save_borders_file, path, data)
        return _state.index


def borders_status() -> dict[str, Any]:
    idx = _state.index
    return {"loaded": idx is not None, "countries": len(idx.countries) if idx else 0,
            "error": _state.borders_error, "dir": str(intel_dir())}


async def get_borders(tol_deg: float) -> dict[str, Any] | None:
    index = await get_index()
    if index is None:
        return None
    key = round(tol_deg, 3)
    cached = _state.borders_cache.get(key)
    if cached is None:
        cached = await asyncio.to_thread(borders_payload, index, key)
        _state.borders_cache[key] = cached
    return cached


async def _load_world_bank() -> dict[str, Any] | None:
    keys = list(WB_INDICATORS)
    urls = [WB_INDICATOR_URL.format(indicator=WB_INDICATORS[k][0]) for k in keys]
    results = await asyncio.gather(
        *(_fetch_json(u, "world bank", 30.0) for u in urls),
        _fetch_json(WB_COUNTRIES_URL, "world bank", 30.0),
    )
    indicators = {k: parse_wb_indicator(r) for k, r in zip(keys, results[:-1], strict=True)}
    countries = parse_wb_countries(results[-1])
    if not countries and not any(indicators.values()):
        return None
    return {"indicators": indicators, "countries": countries}


async def world_bank() -> tuple[dict[str, Any] | None, str]:
    value, error = await _cached("world_bank", WB_TTL_S, _load_world_bank)
    return value, error


async def _load_who() -> list[dict[str, Any]] | None:
    data = await _fetch_json(WHO_URL, "who outbreaks", 25.0)
    if not isinstance(data, dict):
        return None
    return parse_who(data, await get_index())


async def outbreaks() -> tuple[list[dict[str, Any]], str]:
    value, error = await _cached("who", WHO_TTL_S, _load_who)
    return value or [], error


async def _load_feodo() -> dict[str, dict[str, Any]] | None:
    data = await _fetch_json(FEODO_URL, "feodo", 25.0)
    return parse_feodo(data) if isinstance(data, list) else None


async def cyber() -> tuple[dict[str, dict[str, Any]], str]:
    value, error = await _cached("feodo", FEODO_TTL_S, _load_feodo)
    return value or {}, error


async def _load_ioda() -> list[dict[str, Any]] | None:
    end = int(_utcnow().timestamp())
    url = IODA_ALERTS_URL.format(start=end - WINDOW_H * 3600, end=end)
    data = await _fetch_json(url, "ioda", 30.0)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return parse_ioda_alerts(data)


async def outage_alerts() -> tuple[list[dict[str, Any]], str]:
    value, error = await _cached("ioda", IODA_TTL_S, _load_ioda)
    return value or [], error


async def _load_quakes() -> list[dict[str, Any]] | None:
    res = await wv.proxy_earthquakes()
    if not isinstance(res, dict) or res.get("error"):
        return None
    rows = res.get("earthquakes")
    return rows if isinstance(rows, list) else None


async def _quakes() -> tuple[list[dict[str, Any]], str]:
    value, error = await _cached("quakes", QUAKE_TTL_S, _load_quakes)
    return value or [], error


# ---------------------------------------------------------------------------
# GDELT window
# ---------------------------------------------------------------------------


async def _fetch_slot(stamp: str) -> list[dict[str, Any]] | None:
    blob = await _fetch_bytes(GDELT_EXPORT_URL.format(stamp=stamp), "gdelt", 30.0)
    if blob is None:
        return None
    text = await asyncio.to_thread(unzip_export, blob)
    if text is None:
        return None
    return await asyncio.to_thread(parse_gdelt_export, text)


async def refresh_gdelt(want_slots: int = GDELT_WINDOW_SLOTS) -> None:
    """Bring the 24-hour GDELT window up to date.

    lastupdate.txt is read at most every five minutes. The newest file is
    fetched when missing, plus up to GDELT_BACKFILL_PER_REFRESH older ones
    until ``want_slots`` slots are held, so a cold start fills the day over a
    few calls instead of 96 downloads at once. A file that 404s (GDELT lists
    the newest a moment before it is served) is retried after two minutes.
    """
    w = _state.gdelt
    want = max(1, min(GDELT_WINDOW_SLOTS, want_slots))
    async with _lock("gdelt"):
        now_m = time.monotonic()
        if not w.newest or now_m - w.checked_at >= GDELT_CHECK_S:
            w.checked_at = now_m
            stamp = parse_gdelt_lastupdate(await _fetch_text(GDELT_LASTUPDATE_URL, "gdelt"))
            if stamp:
                w.newest, w.error = stamp, ""
            elif not w.newest:
                w.error = "GDELT lastupdate.txt unavailable"
        if not w.newest:
            return
        window = slot_stamps(w.newest, GDELT_WINDOW_SLOTS)
        keep = set(window)
        for stamp in list(w.slots):
            if stamp not in keep:
                del w.slots[stamp]
        w.failed = {s: t for s, t in w.failed.items() if s in keep}
        missing = [s for s in window[:want] if s not in w.slots
                   and now_m - w.failed.get(s, -math.inf) >= GDELT_RETRY_MISSING_S]
        batch = missing[:1 + GDELT_BACKFILL_PER_REFRESH]
        if batch:
            results = await asyncio.gather(*(_fetch_slot(s) for s in batch))
            for stamp, rows in zip(batch, results, strict=True):
                if rows is None:
                    w.failed[stamp] = now_m
                else:
                    w.slots[stamp] = _GdeltSlot(stamp=stamp, rows=rows)
        index = _state.index
        ordered = sorted(w.slots, reverse=True)
        for i, stamp in enumerate(ordered):
            slot = w.slots[stamp]
            if index is not None:
                await asyncio.to_thread(_locate_slot, slot, index)
            if i >= GDELT_RAW_SLOTS and slot.by_country is not None:
                slot.rows = None


async def fetch_gdelt_events(max_age_slots: int = 2) -> list[dict[str, Any]]:
    """Located GDELT events from the newest ``max_age_slots`` 15-minute files.

    Public for other routes (the News mentions layer, conflict views). Each row
    carries lat, lon, tone, quad_class, root_code, event_code, event (CAMEO
    root label), goldstein, mentions, sources, articles, place, country_fips,
    source_url and date (ISO UTC). At most GDELT_RAW_SLOTS (two hours) of rows
    are held; an empty list means GDELT could not be reached.
    """
    n = max(1, min(GDELT_RAW_SLOTS, int(max_age_slots)))
    await refresh_gdelt(n)
    w = _state.gdelt
    out: list[dict[str, Any]] = []
    for stamp in slot_stamps(w.newest, n):
        slot = w.slots.get(stamp)
        if slot is not None and slot.rows:
            out.extend(slot.rows)
    return out


def gdelt_status() -> dict[str, Any]:
    w = _state.gdelt
    located = [s for s in w.slots.values() if s.by_country is not None]
    return {"newest": _stamp_iso(w.newest) if w.newest else "", "files": len(w.slots),
            "hours": round(len(located) * GDELT_SLOT_S / 3600, 2), "error": w.error}


def _gdelt_by_country() -> dict[str, list[float]]:
    total: dict[str, list[float]] = {}
    for slot in _state.gdelt.slots.values():
        for iso3, a in (slot.by_country or {}).items():
            t = total.setdefault(iso3, [0.0, 0.0, 0.0, 0.0])
            for i in range(4):
                t[i] += a[i]
    return total


def _gdelt_points(now: datetime) -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    for stamp, slot in _state.gdelt.slots.items():
        when = _stamp_iso(stamp)
        for lat, lon, root, articles, place in slot.unrest:
            pts.append({"kind": "gdelt", "lat": lat, "lon": lon, "time": when,
                        "label": f"{CAMEO_ROOTS.get(root, 'event')}: {place}"[:160],
                        "weight": articles})
    return pts


# ---------------------------------------------------------------------------
# Instability
# ---------------------------------------------------------------------------


def instability_level(score: float) -> str:
    for floor, name in INSTABILITY_LEVELS:
        if score >= floor:
            return name
    return "low"


def instability_score(inputs: dict[str, Any]) -> dict[str, Any]:
    """The heuristic in INSTABILITY_FORMULA, with each term shown."""
    events = float(inputs.get("gdelt_events") or 0)
    unrest = float(inputs.get("gdelt_unrest") or 0)
    tone = inputs.get("gdelt_tone")
    crit = float(inputs.get("outage_critical") or 0)
    warn = float(inputs.get("outage_warning") or 0)
    mag = inputs.get("quake_max_mag")
    fires = float(inputs.get("fires") or 0)
    extreme = float(inputs.get("weather_extreme") or 0)
    severe = float(inputs.get("weather_severe") or 0)
    outbreaks_n = float(inputs.get("outbreaks") or 0)
    terms = {
        "unrest_volume": 20 * min(1.0, math.log10(1 + unrest) / math.log10(101)),
        "unrest_share": 10 * min(1.0, (unrest / events) / 0.3) if events >= 20 else 0.0,
        "news_tone": (15 * _clamp((-float(tone) - 1.5) / 5, 0.0, 1.0)
                      if events >= 20 and isinstance(tone, (int, float)) else 0.0),
        "internet_outages": 20 * min(1.0, (crit + 0.5 * warn) / 3),
        "earthquakes": (10 * _clamp((float(mag) - 4.5) / 2.5, 0.0, 1.0)
                        if isinstance(mag, (int, float)) else 0.0),
        "fires": 10 * min(1.0, math.log10(1 + fires) / math.log10(2001)),
        "weather_alerts": 5 * min(1.0, (2 * extreme + severe) / 6),
        "outbreaks": 10 * min(1.0, outbreaks_n / 2),
    }
    terms = {k: round(v, 2) for k, v in terms.items()}
    score = round(_clamp(sum(terms.values()), 0.0, 100.0), 1)
    return {"score": score, "level": instability_level(score), "terms": terms}


# ---------------------------------------------------------------------------
# Snapshot: every feed, located once
# ---------------------------------------------------------------------------


def _quake_points(rows: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    out = []
    for q in rows:
        lat, lon, mag = _num(q.get("lat")), _num(q.get("lon")), _num(q.get("magnitude"))
        if lat is None or lon is None:
            continue
        when = None
        with_time = q.get("time")
        if isinstance(with_time, str):
            try:
                when = datetime.strptime(with_time, "%Y-%m-%d %H:%M UTC").replace(tzinfo=UTC)
            except ValueError:
                when = None
        if when is not None and when < since:
            continue
        out.append({"kind": "quake", "lat": lat, "lon": lon, "mag": mag,
                    "time": _iso(when) if when else "",
                    "label": _text(q.get("title"), 160) or f"M{mag}"})
    return out


def _fire_points(rows: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    out = []
    for f in rows:
        lat, lon = _num(f.get("lat")), _num(f.get("lon"))
        if lat is None or lon is None:
            continue
        when = None
        try:
            when = datetime.strptime(
                f"{f.get('acq_date', '')} {str(f.get('acq_time', '')).zfill(4)}",
                "%Y-%m-%d %H%M").replace(tzinfo=UTC)
        except ValueError:
            when = None
        if when is not None and when < since:
            continue
        out.append({"kind": "fire", "lat": lat, "lon": lon,
                    "time": _iso(when) if when else "",
                    "label": f"fire {f.get('frp_mw', 0)} MW"})
    return out


def _weather_points(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for a in rows:
        rings = a.get("rings")
        if not isinstance(rings, list) or not rings or not isinstance(rings[0], list):
            continue
        flat = [v for v in (_num(x) for x in rings[0]) if v is not None]
        if len(flat) < 6:
            continue
        lons, lats = flat[0::2], flat[1::2]
        out.append({"kind": "weather", "lat": sum(lats) / len(lats),
                    "lon": sum(lons) / len(lons), "time": _iso(now),
                    "severity": _text(a.get("severity"), 20),
                    "label": _text(a.get("headline"), 160) or _text(a.get("event"), 80)})
    return out


def _eonet_points(events: Iterable[Any], since: datetime) -> list[dict[str, Any]]:
    out = []
    for e in events:
        point = getattr(e, "point", None)
        when = _parse_iso(getattr(e, "date", ""))
        if not point or len(point) < 2 or when is None or when < since:
            continue
        cats = getattr(e, "categories", None) or []
        cat = _text(getattr(cats[0], "title", ""), 40) if cats else ""
        title = _text(getattr(e, "title", ""), 140)
        out.append({"kind": "eonet", "lat": float(point[1]), "lon": float(point[0]),
                    "time": _iso(when), "label": f"{cat}: {title}" if cat else title})
    return out


async def _eonet_events() -> tuple[list[Any], str]:
    try:
        events, stale = await eonet_routes._current(eonet_routes.DEFAULT_DAYS, "open")
    except Exception:
        return [], "EONET unavailable"
    if events is None:
        return [], "EONET unavailable"
    return list(events), "EONET copy is stale" if stale else ""


async def _fires() -> tuple[list[dict[str, Any]], str, bool]:
    try:
        res = await wv.proxy_fires(days=1, source="VIIRS_SNPP_NRT", south=-90.0,
                                   west=-180.0, north=90.0, east=180.0)
    except Exception:
        return [], "FIRMS request failed", True
    configured = bool(res.get("key_configured"))
    rows = res.get("fires") if isinstance(res.get("fires"), list) else []
    error = _text(res.get("error"), 160)
    if not configured:
        error = "FIRMS_MAP_KEY is not set on the server; fires are left out"
    return rows or [], error, configured


async def _weather() -> tuple[list[dict[str, Any]], str]:
    try:
        res = await wv.proxy_weather_alerts()
    except Exception:
        return [], "NWS alerts unavailable"
    rows = res.get("alerts") if isinstance(res.get("alerts"), list) else []
    return rows or [], _text(res.get("error"), 160)


def _locate_points(index: CountryIndex, points: list[dict[str, Any]]) -> None:
    for p in points:
        c = index.locate(p["lat"], p["lon"], memo=True)
        p["iso3"] = c.iso3 if c else ""


def _build_inputs(index: CountryIndex, points: list[dict[str, Any]],
                  outages_by_iso3: dict[str, dict[str, Any]],
                  outbreak_counts: dict[str, int]) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}

    def slot(iso3: str) -> dict[str, Any]:
        return inputs.setdefault(iso3, {
            "gdelt_events": 0, "gdelt_articles": 0, "gdelt_tone": None, "gdelt_unrest": 0,
            "outage_critical": 0, "outage_warning": 0, "quake_max_mag": None, "quakes": 0,
            "fires": 0, "weather_alerts": 0, "weather_extreme": 0, "weather_severe": 0,
            "eonet": 0, "outbreaks": 0,
        })

    for iso3, (events, articles, tone_w, unrest) in _gdelt_by_country().items():
        s = slot(iso3)
        s["gdelt_events"] = int(events)
        s["gdelt_articles"] = int(articles)
        s["gdelt_tone"] = round(tone_w / articles, 2) if articles else None
        s["gdelt_unrest"] = int(unrest)
    for p in points:
        if not p.get("iso3") or p["kind"] == "gdelt":
            continue
        s = slot(p["iso3"])
        if p["kind"] == "quake":
            s["quakes"] += 1
            mag = p.get("mag")
            if isinstance(mag, (int, float)) and (s["quake_max_mag"] is None
                                                   or mag > s["quake_max_mag"]):
                s["quake_max_mag"] = mag
        elif p["kind"] == "fire":
            s["fires"] += 1
        elif p["kind"] == "weather":
            s["weather_alerts"] += 1
            if p.get("severity") == "Extreme":
                s["weather_extreme"] += 1
            elif p.get("severity") == "Severe":
                s["weather_severe"] += 1
        elif p["kind"] == "eonet":
            s["eonet"] += 1
    for iso3, agg in outages_by_iso3.items():
        s = slot(iso3)
        s["outage_critical"] = agg["critical"]
        s["outage_warning"] = agg["warning"]
    for iso3, n in outbreak_counts.items():
        slot(iso3)["outbreaks"] = n
    return inputs


async def snapshot(force: bool = False) -> dict[str, Any] | None:
    """Every feed, fetched or taken from its cache, located once, scored.

    Rebuilt at most every SNAPSHOT_TTL_S. None only when the borders are not
    loaded; a missing feed leaves its terms at zero and says so in ``sources``.
    """
    index = await get_index()
    if index is None:
        return None
    fresh = time.monotonic() - _state.snapshot_at < SNAPSHOT_TTL_S
    if _state.snapshot is not None and fresh and not force:
        return _state.snapshot
    async with _lock("snapshot"):
        if (_state.snapshot is not None and not force
                and time.monotonic() - _state.snapshot_at < SNAPSHOT_TTL_S):
            return _state.snapshot
        now = _utcnow()
        since = now - timedelta(hours=WINDOW_H)
        local_feeds, remote_feeds = await asyncio.gather(
            asyncio.gather(_quakes(), _fires(), _weather(), _eonet_events()),
            asyncio.gather(outage_alerts(), outbreaks(), cyber(), refresh_gdelt()),
        )
        (quakes, q_err), (fires, f_err, f_key), (alerts, w_err), (eonet, e_err) = local_feeds
        (ioda, o_err), (who, who_err), (c2, c2_err), _ = remote_feeds

        points = (_quake_points(quakes, since) + _fire_points(fires, since)
                  + _weather_points(alerts, now) + _eonet_points(eonet, since)
                  + _gdelt_points(now))
        await asyncio.to_thread(_locate_points, index, points)

        recent = [a for a in ioda if a["time"] >= int(since.timestamp())]
        outages_iso2 = aggregate_outages(recent)
        outages_iso3: dict[str, dict[str, Any]] = {}
        for iso2, agg in outages_iso2.items():
            c = index.by_iso2.get(iso2)
            if c is not None:
                outages_iso3[c.iso3] = {**agg, **c.brief()}

        cutoff = _iso(now - timedelta(days=OUTBREAK_ACTIVE_DAYS))
        active = [o for o in who if o["published"] >= cutoff]
        outbreak_counts: dict[str, int] = {}
        for o in active:
            for c_brief in o["countries"]:
                outbreak_counts[c_brief["iso3"]] = outbreak_counts.get(c_brief["iso3"], 0) + 1

        cyber_iso3: dict[str, dict[str, Any]] = {}
        for iso2, agg in c2.items():
            c = index.by_iso2.get(iso2)
            if c is not None:
                cyber_iso3[c.iso3] = {**agg, **c.brief()}

        inputs = _build_inputs(index, points, outages_iso3, outbreak_counts)
        scores = {iso3: instability_score(inp) for iso3, inp in inputs.items()}
        gd = gdelt_status()
        sources = {
            "gdelt": {"ok": gd["files"] > 0, "count": sum(
                int(i["gdelt_events"]) for i in inputs.values()),
                "note": gd["error"] or f"{gd['hours']} h of GDELT export files held"},
            "ioda": {"ok": not o_err, "count": len(recent), "note": o_err},
            "quakes": {"ok": not q_err, "count": len(quakes), "note": q_err},
            "fires": {"ok": f_key and not f_err, "count": len(fires), "note": f_err},
            "weather": {"ok": not w_err, "count": len(alerts),
                        "note": w_err or "NWS alerts cover the United States only"},
            "eonet": {"ok": not e_err, "count": len(eonet), "note": e_err},
            "who": {"ok": not who_err, "count": len(who), "note": who_err},
            "feodo": {"ok": not c2_err, "count": sum(a["servers"] for a in c2.values()),
                      "note": c2_err},
        }
        _state.snapshot = {
            "generated": _iso(now),
            "since": _iso(since),
            "points": points,
            "inputs": inputs,
            "scores": scores,
            "outages": outages_iso3,
            "outbreaks": who,
            "outbreaks_active": active,
            "cyber": cyber_iso3,
            "sources": sources,
            "gdelt": gd,
        }
        _state.snapshot_at = time.monotonic()
        return _state.snapshot


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def convergence_cells(points: list[dict[str, Any]],
                      country_kinds: dict[str, set[str]],
                      cell_deg: float = 2.0,
                      min_kinds: int = CONVERGENCE_MIN_KINDS) -> list[dict[str, Any]]:
    """Grid cells where at least ``min_kinds`` event kinds meet.

    ``points`` are located events (kind, lat, lon, label, iso3) already inside
    the time window. ``country_kinds`` maps iso3 to the country-wide kinds
    (internet outage, outbreak) active there; those count for every cell that
    holds a located event in that country. A cell also needs at least
    CONVERGENCE_MIN_LOCATED kinds of its own located events, so one news point
    in a country with an outage and an outbreak does not make a ring.
    Score: 25 per kind plus 2 per event up to 25, at most 100.
    """
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    for p in points:
        key = (math.floor(p["lat"] / cell_deg), math.floor(p["lon"] / cell_deg))
        c = cells.setdefault(key, {"counts": {}, "lat": 0.0, "lon": 0.0, "n": 0,
                                   "countries": set(), "samples": {}})
        c["counts"][p["kind"]] = c["counts"].get(p["kind"], 0) + 1
        c["lat"] += p["lat"]
        c["lon"] += p["lon"]
        c["n"] += 1
        if p.get("iso3"):
            c["countries"].add(p["iso3"])
        samples = c["samples"].setdefault(p["kind"], [])
        if len(samples) < 3 and p.get("label"):
            samples.append(p["label"])
    out: list[dict[str, Any]] = []
    for (cy, cx), c in cells.items():
        located = set(c["counts"])
        if len(located) < CONVERGENCE_MIN_LOCATED:
            continue
        wide: set[str] = set()
        for iso3 in c["countries"]:
            wide |= country_kinds.get(iso3, set())
        kinds = located | wide
        if len(kinds) < min_kinds:
            continue
        events = c["n"] + len(wide - located)
        score = min(100, 25 * len(kinds) + min(25, 2 * events))
        ordered = [k for k in CONVERGENCE_KINDS if k in kinds]
        out.append({
            "id": f"{cy}:{cx}",
            "lat": round(c["lat"] / c["n"], 4),
            "lon": round(c["lon"] / c["n"], 4),
            "cell": [cx * cell_deg, cy * cell_deg, (cx + 1) * cell_deg, (cy + 1) * cell_deg],
            "kinds": ordered,
            "labels": [KIND_LABELS[k] for k in ordered],
            "counts": {k: c["counts"].get(k, 0) for k in ordered},
            "events": events,
            "score": score,
            "countries": sorted(c["countries"]),
            "samples": {k: c["samples"].get(k, []) for k in ordered if k in located},
        })
    out.sort(key=lambda r: (-r["score"], -r["events"]))
    return out[:CONVERGENCE_MAX_CELLS]


def country_wide_kinds(snap: dict[str, Any]) -> dict[str, set[str]]:
    kinds: dict[str, set[str]] = {}
    for iso3, agg in snap["outages"].items():
        if agg.get("critical") or agg.get("warning"):
            kinds.setdefault(iso3, set()).add("outage")
    for o in snap["outbreaks_active"]:
        for c in o["countries"]:
            kinds.setdefault(c["iso3"], set()).add("outbreak")
    return kinds


CONVERGENCE_METHOD = (
    "Events from the last 24 h are binned on a grid of cell_deg degrees. A cell is drawn"
    " when at least three kinds meet there, two of them located events in the cell"
    " (quakes, fires, EONET, NWS alerts, GDELT unrest). Internet outages (IODA, 24 h) and"
    " WHO outbreaks (30 days) are country-wide and count for every cell that holds an"
    " event in that country. Score = 25 per kind + 2 per event up to 25, at most 100."
)


# ---------------------------------------------------------------------------
# The country card
# ---------------------------------------------------------------------------


def _view(c: Country) -> dict[str, Any]:
    w, s, e, n = c.main_bbox
    diag = haversine_km(s, w, n, e)
    return {"lat": c.label_lat, "lon": c.label_lon,
            "height_m": round(_clamp(diag * 1500.0, 400000.0, 12000000.0)),
            "bbox": list(c.main_bbox)}


async def country_card(code: str) -> dict[str, Any] | None:
    """Everything the country card shows. None when the code is unknown or
    the borders are not loaded (the route tells the two apart)."""
    index = await get_index()
    if index is None:
        return None
    c = index.lookup(code)
    if c is None:
        return None
    (wb, wb_err), snap = await asyncio.gather(world_bank(), snapshot())
    now = _utcnow()
    meta = ((wb or {}).get("countries") or {}).get(c.iso3) or {}
    cap_lat, cap_lon = meta.get("lat"), meta.get("lon")
    capital = None
    if meta.get("capital") and cap_lat is not None and cap_lon is not None:
        capital = {"name": meta["capital"], "lat": cap_lat, "lon": cap_lon}
    ref_lat = cap_lat if cap_lat is not None else c.label_lat
    ref_lon = cap_lon if cap_lon is not None else c.label_lon
    zone = zone_for_country(c.iso2, ref_lat, ref_lon) if c.iso2 else None

    indicators = []
    for key, (wb_id, label) in WB_INDICATORS.items():
        hit = ((wb or {}).get("indicators") or {}).get(key, {}).get(c.iso3)
        indicators.append({"key": key, "id": wb_id, "label": label,
                           "value": hit["value"] if hit else None,
                           "year": hit["year"] if hit else None})

    inputs = (snap or {}).get("inputs", {}).get(c.iso3) or {}
    score = (snap or {}).get("scores", {}).get(c.iso3) or instability_score({})
    who = [o for o in (snap or {}).get("outbreaks", [])
           if any(x["iso3"] == c.iso3 for x in o["countries"])]
    span_km = haversine_km(c.main_bbox[1], c.main_bbox[0], c.main_bbox[3], c.main_bbox[2])
    cam_radius = round(_clamp(span_km / 2, 100.0, 2000.0))
    return {
        "iso3": c.iso3, "iso2": c.iso2, "name": c.name, "name_long": c.name_long,
        "formal_name": c.formal_name, "continent": c.continent, "subregion": c.subregion,
        "region": meta.get("region") or c.region_wb,
        "income_group": meta.get("income") or c.income_group,
        "capital": capital,
        "pop_est": {"value": c.pop_est, "year": c.pop_year, "source": "Natural Earth"},
        "indicators": indicators,
        "indicators_source": "World Bank Open Data (CC BY 4.0), latest non-empty year",
        "indicators_error": wb_err,
        "local_time": local_time(zone, now) if zone else None,
        "exchanges": exchanges_for(c.iso2, now),
        "exchanges_note": EXCHANGE_NOTE,
        "events_24h": {
            "quakes": inputs.get("quakes", 0),
            "fires": inputs.get("fires", 0),
            "weather_alerts": inputs.get("weather_alerts", 0),
            "eonet": inputs.get("eonet", 0),
            "gdelt_events": inputs.get("gdelt_events", 0),
            "gdelt_unrest": inputs.get("gdelt_unrest", 0),
            "outage_sources": inputs.get("outage_critical", 0)
            + inputs.get("outage_warning", 0),
            "outbreaks_30d": inputs.get("outbreaks", 0),
        },
        "instability": {
            **score, "inputs": inputs, "formula": list(INSTABILITY_FORMULA),
            "note": INSTABILITY_NOTE,
        },
        "outbreaks": who[:10],
        "cyber": (snap or {}).get("cyber", {}).get(c.iso3),
        "outages": (snap or {}).get("outages", {}).get(c.iso3),
        "sources": (snap or {}).get("sources", {}),
        "generated": (snap or {}).get("generated", _iso(now)),
        "view": _view(c),
        "cameras": {"lat": ref_lat, "lon": ref_lon, "radius_km": cam_radius},
        "attribution": [BORDERS_ATTRIBUTION, "Indicators: World Bank (CC BY 4.0)",
                        "Time zones: IANA tz database"],
    }
