"""Armed conflict on the globe: front lines, territorial control, verified events
and the historical record.

Four keyless sources and one optional keyed one, each read at runtime, cached on
disk under ``SDA_CONFLICT_DIR`` and credited wherever it is drawn. Nothing fetched
here is committed to the repository.

Front line, Russia-Ukraine
    DeepStateMap (https://deepstatemap.live), the public history API that the
    site's own map reads: ``/api/history/last`` for the current map,
    ``/api/history/public`` for the list of updates and
    ``/api/history/{id}/geojson`` for any earlier one. The project publishes no
    licence or terms for the API; it answers without a key and with
    ``Access-Control-Allow-Origin: *``. Used with the credit "(c) DeepStateMap",
    fetched at most once per ``DS_TTL_S`` and never redistributed in bulk.
    The map holds areas, not a line, so the line of contact is derived here: an
    edge of a held area is on the line when the land just outside it is inside
    Ukraine and not itself held. Ukraine's land outline comes from geoBoundaries
    (gbHumanitarian UKR ADM0, simplified: the OCHA COD-AB boundary from the
    State Scientific Production Enterprise "Kartographia", CC BY 3.0 IGO). It is
    clipped to the coast, so coastline edges of a held area do not count as
    front; the OpenStreetMap outline runs out to the territorial sea and does.

Control of settlements, other theatres
    The English Wikipedia "detailed map" modules (for example
    ``Module:2023 Sudanese Clashes detailed map``), read through the MediaWiki API.
    Each module lists towns with a marker whose file name carries the colour of
    the party holding it, and its caption carries the legend, so one parser
    serves every theatre. CC BY-SA 4.0; every mark links to the module revision
    it came from and carries that revision's date.

Verified events
    GeoConfirmed (https://geoconfirmed.org), the public read API documented at
    ``/openapi/v1.json``: ``/api/Map/export/csv?start=&end=`` returns every
    approved placemark in the window, each geolocated by volunteers against a
    photo or video, with its source posts. The API describes the data as freely
    available for research, journalism and analysis and asks clients for a
    descriptive User-Agent and polite polling; one request per ``GC_TTL_S``.

Historical record
    UCDP Georeferenced Event Dataset (https://ucdp.uu.se/downloads, CC BY 4.0),
    the yearly release plus the year-to-date candidate file, imported into
    SQLite by ``python -m sda.conflict import``. The UCDP API now needs a token,
    so the import reads the downloads. Drawn as a density grid with a year
    filter, single events once the view is close.

ACLED (optional)
    https://acleddata.com, OAuth since 2025 (access keys were retired). Set
    ``ACLED_EMAIL`` and ``ACLED_PASSWORD`` (``ACLED_KEY`` is read as the same
    secret). Without them the layer reports which variables it needs.

Coordinates are geodetic degrees as each source publishes them. Distances use a
spherical Earth of mean radius 6371 km; nothing here touches an inertial frame.
The wording about parties follows each source's own legend, shortened, and the
module never names or follows private individuals.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import html
import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote, urlencode

import numpy as np
import numpy.typing as npt

USER_AGENT = "vantage/0.3 (+https://github.com/jdardash/vantage; conflict layer)"

CONFLICT_DIR_ENV = "SDA_CONFLICT_DIR"
DB_NAME = "conflict.sqlite"
SCHEMA_VERSION = "2"
IMPORT_HINT = "No UCDP history imported. On the server run: python -m sda.conflict import"
OUTDATED_HINT = ("The UCDP history was imported by an older version. On the server run: "
                 "python -m sda.conflict import")

KM_PER_DEG = 111.195  # one degree of arc on a sphere of radius 6371 km
EARTH_MEAN_RADIUS_KM = 6371.0

_FloatArray = npt.NDArray[np.float64]
_BoolArray = npt.NDArray[np.bool_]


# ---------------------------------------------------------------------------
# Paths and small helpers
# ---------------------------------------------------------------------------


def conflict_dir() -> Path:
    """Directory holding caches and the UCDP store: SDA_CONFLICT_DIR, else the
    user cache dir (``%LOCALAPPDATA%/sda/conflict`` on Windows)."""
    configured = os.environ.get(CONFLICT_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "conflict"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "conflict"


def db_path() -> Path:
    return conflict_dir() / DB_NAME


def cache_dir() -> Path:
    return conflict_dir() / "cache"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://[^\s,;<>\"']+")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def clean_text(value: object, limit: int = 300) -> str:
    """Plain one-line text: tags dropped, entities decoded, spaces collapsed."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def find_urls(value: object, limit: int = 5) -> list[str]:
    """The distinct http(s) links in a free-text field, in order."""
    if not isinstance(value, str):
        return []
    out: list[str] = []
    for raw in _URL_RE.findall(value):
        url = raw.rstrip(").]")
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def safe_url(value: object) -> str:
    """The value when it is an absolute http(s) URL without spaces, else ''."""
    if isinstance(value, str) and re.fullmatch(r"https?://[^\s<>\"']+", value):
        return value
    return ""


def _float(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _int(value: object, default: int = 0) -> int:
    f = _float(value)
    return int(f) if f is not None else default


def valid_point(lat: float | None, lon: float | None) -> bool:
    return (lat is not None and lon is not None and -90.0 <= lat <= 90.0
            and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0))


# ---------------------------------------------------------------------------
# HTTP, pacing and caches
# ---------------------------------------------------------------------------


async def http_get(url: str, timeout_s: float = 30.0,
                   headers: dict[str, str] | None = None) -> bytes | None:
    """GET *url*; the body on HTTP 200, None on any failure at all.

    The one network entry point for reads; tests replace it with a fake that
    serves tests/data/conflict, so nothing here opens a socket under pytest.
    """
    import httpx

    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True,
                                     headers=hdrs) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.content
    except Exception:
        return None


async def http_post_form(url: str, data: dict[str, str], timeout_s: float = 30.0) -> Any:
    """POST a form and return parsed JSON, or None on any failure."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s,
                                     headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(url, data=data)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


async def get_json(url: str, timeout_s: float = 30.0,
                   headers: dict[str, str] | None = None) -> Any:
    body = await http_get(url, timeout_s, headers)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None


_host_last: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}
_key_locks: dict[str, asyncio.Lock] = {}


def _lock_for(table: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    lock = table.get(key)
    if lock is None:
        lock = asyncio.Lock()
        table[key] = lock
    return lock


async def paced(host: str, gap_s: float) -> None:
    """Wait until *gap_s* has passed since the last request to *host*."""
    async with _lock_for(_host_locks, host):
        wait = _host_last.get(host, 0.0) + gap_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last[host] = time.monotonic()


def cache_read(name: str, max_age_s: float | None) -> dict[str, Any] | None:
    """A cached JSON payload younger than *max_age_s* (None: any age)."""
    path = cache_dir() / name
    try:
        if max_age_s is not None and time.time() - path.stat().st_mtime > max_age_s:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def cache_write(name: str, payload: dict[str, Any]) -> None:
    path = cache_dir() / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def prune_cache(folder: str, keep: int) -> None:
    """Keep only the *keep* newest files in a cache sub-folder."""
    try:
        files = sorted((cache_dir() / folder).glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[keep:]:
        with contextlib.suppress(OSError):
            old.unlink()


_memo: dict[str, tuple[float, Any]] = {}


def memo_get(key: str, ttl_s: float) -> Any:
    hit = _memo.get(key)
    if hit is None or time.monotonic() - hit[0] >= ttl_s:
        return None
    return hit[1]


def memo_put(key: str, value: Any) -> Any:
    _memo[key] = (time.monotonic(), value)
    return value


def clear_caches() -> None:
    """Forget every in-memory payload and lock (tests use this between cases)."""
    _memo.clear()
    _gc_refresh.clear()
    _host_last.clear()
    _key_locks.clear()
    _host_locks.clear()
    _lines_memo.clear()


# ---------------------------------------------------------------------------
# Spherical geometry
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MEAN_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def line_length_km(points: list[list[float]]) -> float:
    return sum(haversine_km(a[1], a[0], b[1], b[0])
               for a, b in zip(points, points[1:], strict=False))


def as_ring(coords: Iterable[Any]) -> _FloatArray | None:
    """A closed (n, 2) lon/lat array from GeoJSON positions, or None."""
    pts: list[tuple[float, float]] = []
    for p in coords:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        lon, lat = _float(p[0]), _float(p[1])
        if lon is None or lat is None:
            continue
        pts.append((lon, lat))
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    if len(pts) < 4:
        return None
    return np.asarray(pts, dtype=np.float64)


def points_in_ring(lon: _FloatArray, lat: _FloatArray, ring: _FloatArray,
                   band_deg: float = 0.1, block: int = 512) -> _BoolArray:
    """Even-odd test of many points against one closed ring (planar in degrees,
    fine for rings far smaller than a hemisphere and away from the poles).

    Points are grouped into latitude bands of *band_deg* and tested only
    against the edges that reach their band, so a ring of thousands of
    vertices costs little more than a small one.
    """
    out = np.zeros(lon.shape[0], dtype=bool)
    if lon.size == 0:
        return out
    west, east = float(ring[:, 0].min()), float(ring[:, 0].max())
    south, north = float(ring[:, 1].min()), float(ring[:, 1].max())
    inside_box = (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)
    idx = np.nonzero(inside_box)[0]
    if idx.size == 0:
        return out
    x1, y1 = ring[:-1, 0], ring[:-1, 1]
    x2, y2 = ring[1:, 0], ring[1:, 1]
    dy = y2 - y1
    safe_dy = np.where(dy == 0.0, 1.0, dy)
    edge_lo = np.floor((np.minimum(y1, y2) - south) / band_deg).astype(np.int64)
    edge_hi = np.floor((np.maximum(y1, y2) - south) / band_deg).astype(np.int64)
    point_band = np.floor((lat[idx] - south) / band_deg).astype(np.int64)
    for band in np.unique(point_band):
        in_band = idx[point_band == band]
        emask = (edge_lo <= band) & (edge_hi >= band)
        if not emask.any():
            continue
        bx1, by1, bx2, by2 = x1[emask], y1[emask], x2[emask], y2[emask]
        bdy = safe_dy[emask]
        for start in range(0, in_band.size, block):
            sel = in_band[start:start + block]
            px = lon[sel][:, None]
            py = lat[sel][:, None]
            straddle = (by1 > py) != (by2 > py)
            x_cross = bx1 + (py - by1) * (bx2 - bx1) / bdy
            hits = straddle & (px < x_cross)
            out[sel] = (hits.sum(axis=1) % 2) == 1
    return out


@dataclass
class Polygon:
    outer: _FloatArray
    holes: list[_FloatArray] = field(default_factory=list)

    def contains(self, lon: _FloatArray, lat: _FloatArray) -> _BoolArray:
        inside = points_in_ring(lon, lat, self.outer)
        for hole in self.holes:
            if inside.any():
                inside &= ~points_in_ring(lon, lat, hole)
        return inside


def any_contains(polys: list[Polygon], lon: _FloatArray, lat: _FloatArray) -> _BoolArray:
    hit = np.zeros(lon.shape[0], dtype=bool)
    for poly in polys:
        hit |= poly.contains(lon, lat)
    return hit


def edge_side_points(ring: _FloatArray, offset_km: float) -> tuple[_FloatArray, ...]:
    """For each edge of *ring*, the points *offset_km* either side of its middle,
    on a local tangent plane: (lon_a, lat_a, lon_b, lat_b, length_km)."""
    lon1, lat1 = ring[:-1, 0], ring[:-1, 1]
    lon2, lat2 = ring[1:, 0], ring[1:, 1]
    mid_lat = (lat1 + lat2) / 2.0
    dlon = lon2 - lon1
    dlon = np.where(dlon > 180.0, dlon - 360.0, np.where(dlon < -180.0, dlon + 360.0, dlon))
    coslat = np.maximum(np.cos(np.radians(mid_lat)), 1e-6)
    east_km = dlon * KM_PER_DEG * coslat
    north_km = (lat2 - lat1) * KM_PER_DEG
    length = np.hypot(east_km, north_km)
    safe = np.where(length > 0.0, length, 1.0)
    n_east = -north_km / safe  # unit normal to the left of travel
    n_north = east_km / safe
    mid_lon = lon1 + dlon / 2.0
    d_lon = n_east * offset_km / (KM_PER_DEG * coslat)
    d_lat = n_north * offset_km / KM_PER_DEG
    return (mid_lon + d_lon, mid_lat + d_lat, mid_lon - d_lon, mid_lat - d_lat, length)


def runs_on_ring(flags: _BoolArray) -> list[tuple[int, int]]:
    """Maximal runs of True edges on a closed ring as (first edge, edge count),
    joining a run that wraps past the ring's start."""
    n = int(flags.size)
    if n == 0 or not flags.any():
        return []
    if flags.all():
        return [(0, n)]
    start = int(np.argmin(flags))  # a False edge, so no run wraps past it
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        k = (start + i) % n
        if flags[k]:
            j = i
            while j < n and flags[(start + j) % n]:
                j += 1
            runs.append((k, j - i))
            i = j
        else:
            i += 1
    return runs


def _free_side(lon: _FloatArray, lat: _FloatArray, far_lon: _FloatArray,
               far_lat: _FloatArray, held: list[Polygon], land: list[Polygon]) -> _BoolArray:
    """Near points that are unheld land with land again at the far points."""
    free = ~any_contains(held, lon, lat)
    if free.any():
        idx = np.nonzero(free)[0]
        free[idx] = any_contains(land, lon[idx], lat[idx])
    if free.any():
        idx = np.nonzero(free)[0]
        free[idx] = any_contains(land, far_lon[idx], far_lat[idx])
    return free


def contact_lines(held: list[Polygon], land: list[Polygon], *, offset_km: float = 1.0,
                  reach_km: float = 3.0, min_km: float = 1.5) -> list[list[list[float]]]:
    """Line of contact: edges of the held areas with free land just outside them.

    An edge counts when, on one side of its middle, the point *offset_km* out
    is on *land* (the country's coast-clipped outline) and in no held area,
    and the point *reach_km* out is still on land. Edges on an international
    border or a coast fail the land tests, edges shared by two held areas fail
    the held test, and the far point drops thin slivers of land the source's
    polygons happen to leave uncovered along a coast or spit. Runs shorter
    than *min_km* are dropped.
    """
    if not land or not held:
        return []
    lines: list[list[list[float]]] = []
    for poly in held:
        for ring in [poly.outer, *poly.holes]:
            a_lon, a_lat, b_lon, b_lat, length = edge_side_points(ring, offset_km)
            fa_lon, fa_lat, fb_lon, fb_lat, _ = edge_side_points(ring, reach_km)
            free_a = _free_side(a_lon, a_lat, fa_lon, fa_lat, held, land)
            free_b = _free_side(b_lon, b_lat, fb_lon, fb_lat, held, land)
            flags = (free_a | free_b) & (length > 0.001)
            n = ring.shape[0] - 1
            for first, count in runs_on_ring(flags):
                pts = [ring[(first + i) % n] for i in range(count + 1)]
                line = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in pts]
                if line_length_km(line) >= min_km:
                    lines.append(line)
    return lines


def ring_list(ring: _FloatArray, decimals: int = 5) -> list[list[float]]:
    return [[round(float(x), decimals), round(float(y), decimals)] for x, y in ring]


# ---------------------------------------------------------------------------
# DeepStateMap: Russia-Ukraine front line
# ---------------------------------------------------------------------------

DS_BASE = "https://deepstatemap.live/api/history"
DS_LAST_URL = DS_BASE + "/last"
DS_LIST_URL = DS_BASE + "/public"
DS_SNAPSHOT_URL = DS_BASE + "/{id}/geojson"
DS_SITE = "https://deepstatemap.live/en"
DS_TTL_S = 1800.0
DS_LIST_TTL_S = 6 * 3600.0
DS_KEEP_SNAPSHOTS = 40
DS_ATTRIBUTION = ("Front line: (c) DeepStateMap (deepstatemap.live). Line of contact "
                  "derived from its areas and Ukraine's land outline (OCHA COD-AB via "
                  "geoBoundaries, Kartographia, CC BY 3.0 IGO).")

#: Area kinds, in drawing order, with neutral legend text.
DS_KINDS: dict[str, str] = {
    "held_2014": "Under Russian control since 2014 (Crimea, parts of Donetsk and Luhansk)",
    "held": "Under Russian control since 2022",
    "contested": "Contested or status unknown",
    "retaken": "Returned to Ukrainian control",
}
DS_HELD = ("held", "held_2014")

_DS_AT_RE = re.compile(r"\{\{\s*at\s*:\s*([^}]*)\}\}", re.I)

GB_UKR_META_URL = "https://www.geoboundaries.org/api/current/gbHumanitarian/UKR/ADM0/"
GB_TTL_S = 180 * 86400.0


# Maps before mid-2024 carry no geoJSON key, only free text ("Liberated 25.03",
# Ukrainian alone in 2022) and a fill colour, and the colour is the steadier of
# the two. The bright red layer is the site's wider "territories" (Transnistria,
# Abkhazia, Karelia and so on), which is not part of this front.
_DS_FILL_KIND: dict[str, str | None] = {
    "#a52714": "held", "#880e4f": "held_2014", "#000000": "held_2014",
    "#0f9d58": "retaken", "#0288d1": "retaken", "#01579b": "retaken",
    "#bcaaa4": "contested", "#bdbdbd": "contested", "#ff5252": None,
}
_DS_NOT_FRONT = ("transnistria", "придністров")
_DS_SINCE_2014 = ("crimea", "cadr", "calr", "ordlo", "tuzla", "крим",
                  "ордло", "тузла")
_DS_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("contested", ("contested", "unknown", "під питан"
                   "ням", "невідом")),
    ("retaken", ("liberated", "taken back", "recaptured",
                 "звільнен",
                 "відбит")),
    ("held", ("occupied", "окупован",
              "зайнят")),
)
_DS_DAY_RE = re.compile(r"\d{1,2}(?:\.\d{1,2})?(?:\s*-\s*\d{1,2}(?:\.\d{1,2})?)?")


def _legacy_kind(name: str, fill: object) -> str | None:
    text = " ".join(name.replace("\xa0", " ").lower().split())
    if any(w in text for w in _DS_NOT_FRONT):
        return None
    if any(w in text for w in _DS_SINCE_2014):
        return "held_2014"
    colour = fill.strip().lower() if isinstance(fill, str) else ""
    if colour in _DS_FILL_KIND:
        return _DS_FILL_KIND[colour]
    for kind, words in _DS_WORDS:
        if any(w in text for w in words):
            return kind
    return None


def deepstate_kind(name: object, fill: object = None) -> tuple[str | None, str]:
    """The area kind for a DeepStateMap feature name, and its date note.

    Names since mid-2024 read ``Ukrainian /// English /// geoJSON.status.occupied``
    and the last part is the stable key. Older maps have free text and a fill
    colour only; those are classed by colour, with Crimea, ORDLO and Tuzla
    named. Unit markers, attack arrows, capitals and the site's wider
    "territories" layer (Transnistria, Abkhazia and so on) return None.
    """
    if not isinstance(name, str):
        return None, ""
    key_full = name.split("///")[-1].strip()
    if not key_full.lower().startswith("geojson."):
        kind = _legacy_kind(name, fill)
        day = _DS_DAY_RE.search(name) if kind == "retaken" else None
        note = clean_text(day.group(0), 40) if day and "." in day.group(0) else ""
        return kind, note
    at = _DS_AT_RE.search(key_full)
    note = clean_text(at.group(1), 40) if at else ""
    key = key_full.split("{{")[0].strip().lower()
    if key == "geojson.status.occupied":
        return "held", note
    if key in ("geojson.territories.crimea", "geojson.territories.ordlo",
               "geojson.territories.tuzla"):
        return "held_2014", note
    if key == "geojson.status.unknown":
        return "contested", note
    if key.startswith("geojson.status.dismissed"):
        return "retaken", note
    return None, ""


def _polygons_of(geometry: object) -> list[Polygon]:
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    if gtype == "GeometryCollection":
        parts_of = geometry.get("geometries")
        if not isinstance(parts_of, list):
            return []
        return [p for g in parts_of for p in _polygons_of(g)]
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return []
    parts = [coords] if gtype == "Polygon" else coords if gtype == "MultiPolygon" else []
    out: list[Polygon] = []
    for part in parts:
        if not isinstance(part, list) or not part:
            continue
        outer = as_ring(part[0])
        if outer is None:
            continue
        holes = [h for h in (as_ring(r) for r in part[1:]) if h is not None]
        out.append(Polygon(outer, holes))
    return out


def parse_deepstate(payload: object) -> dict[str, Any] | None:
    """Areas from a DeepStateMap payload: ``/last`` ({id, map, datetime}) or a
    bare FeatureCollection from ``/{id}/geojson``."""
    if not isinstance(payload, dict):
        return None
    snap_id = _int(payload.get("id"), 0)
    fc = payload.get("map") if "map" in payload else payload
    if not isinstance(fc, dict) or not isinstance(fc.get("features"), list):
        return None
    areas: list[dict[str, Any]] = []
    for feat in fc["features"]:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties")
        if not isinstance(props, dict):
            continue
        kind, note = deepstate_kind(props.get("name"), props.get("fill"))
        if kind is None:
            continue
        for poly in _polygons_of(feat.get("geometry")):
            areas.append({
                "kind": kind,
                "note": note,
                "rings": [ring_list(poly.outer)] + [ring_list(h) for h in poly.holes],
            })
    order = list(DS_KINDS)
    areas.sort(key=lambda a: order.index(a["kind"]))
    return {"id": snap_id, "areas": areas}


def areas_to_polygons(areas: list[dict[str, Any]], kinds: tuple[str, ...]) -> list[Polygon]:
    out: list[Polygon] = []
    for area in areas:
        if area.get("kind") not in kinds:
            continue
        rings = [r for r in (as_ring(x) for x in area.get("rings") or []) if r is not None]
        if rings:
            out.append(Polygon(rings[0], rings[1:]))
    return out


def parse_deepstate_history(payload: object) -> list[dict[str, Any]]:
    """The update list from ``/api/history/public``: one entry per map update,
    oldest first, as {id, at, note}. The id is the update's Unix time."""
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("status") is False:
            continue
        ident = _int(row.get("id"), 0)
        if ident <= 1_400_000_000:
            continue
        out.append({"id": ident, "at": iso_utc(ident),
                    "note": clean_text(row.get("descriptionEn") or "", 240)})
    out.sort(key=lambda r: r["id"])
    return out


def daily_snapshots(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last update of each UTC day: [{date, id, at}], oldest first."""
    by_day: dict[str, dict[str, Any]] = {}
    for row in history:
        by_day[row["at"][:10]] = {"date": row["at"][:10], "id": row["id"], "at": row["at"]}
    return [by_day[d] for d in sorted(by_day)]


def snapshot_for_date(history: list[dict[str, Any]], day: date) -> dict[str, Any] | None:
    """The newest update made before the end of *day* (UTC)."""
    end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    limit = end.timestamp()
    best: dict[str, Any] | None = None
    for row in history:
        if row["id"] < limit:
            best = row
        else:
            break
    return best


def parse_land(payload: object) -> list[Polygon]:
    """Every polygon (with its holes) of a GeoJSON object: a country's land
    parts in a coast-clipped geoBoundaries ADM0 file."""
    geoms: list[object] = []
    if isinstance(payload, dict):
        if payload.get("type") == "FeatureCollection":
            geoms = [f.get("geometry") for f in payload.get("features") or []
                     if isinstance(f, dict)]
        elif payload.get("type") == "Feature":
            geoms = [payload.get("geometry")]
        else:
            geoms = [payload]
    return [p for g in geoms for p in _polygons_of(g)]


async def ukraine_land() -> list[Polygon]:
    """Ukraine's land outline (geoBoundaries gbHumanitarian ADM0, simplified,
    coast-clipped), cached on disk for ``GB_TTL_S``; the stale copy is used
    when the refresh fails. Empty when no copy can be had."""
    cached = memo_get("ukr-land", 3600.0)
    if cached is not None:
        return list(cached)
    disk = cache_read("ukraine_land.json", GB_TTL_S)
    if disk is None:
        meta = await get_json(GB_UKR_META_URL)
        geo_url = safe_url(meta.get("simplifiedGeometryGeoJSON")) if isinstance(meta, dict) else ""
        polys = parse_land(await get_json(geo_url, timeout_s=60.0) if geo_url else None)
        if polys:
            disk = {"polygons": [[ring_list(p.outer, 4)] + [ring_list(h, 4) for h in p.holes]
                                 for p in polys], "source": geo_url}
            cache_write("ukraine_land.json", disk)
        else:
            disk = cache_read("ukraine_land.json", None)
    land: list[Polygon] = []
    for rings in (disk or {}).get("polygons") or []:
        parsed = [r for r in (as_ring(x) for x in rings) if r is not None]
        if parsed:
            land.append(Polygon(parsed[0], parsed[1:]))
    memo_put("ukr-land", land)
    return land


async def deepstate_history() -> list[dict[str, Any]]:
    """The DeepStateMap update list, cached ``DS_LIST_TTL_S``."""
    hit = memo_get("ds-history", DS_LIST_TTL_S)
    if hit is not None:
        return list(hit)
    async with _lock_for(_key_locks, "ds-history"):
        hit = memo_get("ds-history", DS_LIST_TTL_S)
        if hit is not None:
            return list(hit)
        disk = cache_read("deepstate/history.json", DS_LIST_TTL_S)
        rows: list[dict[str, Any]] = disk.get("rows", []) if disk else []
        if not rows:
            await paced("deepstatemap.live", 1.0)
            rows = parse_deepstate_history(await get_json(DS_LIST_URL, timeout_s=60.0))
            if rows:
                cache_write("deepstate/history.json", {"rows": rows})
            else:
                stale = cache_read("deepstate/history.json", None)
                rows = stale.get("rows", []) if stale else []
        if rows:
            memo_put("ds-history", rows)
        return list(rows)


async def deepstate_snapshot(snapshot_id: int | None) -> tuple[dict[str, Any] | None, bool]:
    """Parsed areas of the latest map (None) or of one update, and whether the
    copy is stale (served from disk after a failed refresh)."""
    if snapshot_id is None:
        hit = memo_get("ds-last", DS_TTL_S)
        if hit is not None:
            return hit, False
        async with _lock_for(_key_locks, "ds-last"):
            hit = memo_get("ds-last", DS_TTL_S)
            if hit is not None:
                return hit, False
            disk = cache_read("deepstate/last.json", DS_TTL_S)
            if disk is not None:
                return memo_put("ds-last", disk), False
            await paced("deepstatemap.live", 1.0)
            parsed = parse_deepstate(await get_json(DS_LAST_URL, timeout_s=60.0))
            if parsed is not None and parsed["areas"]:
                cache_write("deepstate/last.json", parsed)
                return memo_put("ds-last", parsed), False
            stale = cache_read("deepstate/last.json", None)
            return stale, stale is not None
    name = f"deepstate/{int(snapshot_id)}.json"
    disk = cache_read(name, None)
    if disk is not None:
        return disk, False
    async with _lock_for(_key_locks, name):
        disk = cache_read(name, None)
        if disk is not None:
            return disk, False
        await paced("deepstatemap.live", 1.0)
        parsed = parse_deepstate(
            await get_json(DS_SNAPSHOT_URL.format(id=int(snapshot_id)), timeout_s=60.0))
        if parsed is None or not parsed["areas"]:
            return None, False
        parsed["id"] = int(snapshot_id)
        cache_write(name, parsed)
        prune_cache("deepstate", DS_KEEP_SNAPSHOTS + 2)
        return parsed, False


_lines_memo: dict[tuple[int, bool], list[list[list[float]]]] = {}


async def ukraine_frontline(day: date | None = None) -> dict[str, Any]:
    """The Russia-Ukraine map for *day* (None: the latest), with the line of
    contact. Always a dict; ``error`` is set and ``areas`` empty on failure."""
    base: dict[str, Any] = {
        "theatre": "ukraine", "name": "Russia-Ukraine", "source": "DeepStateMap",
        "source_url": DS_SITE, "url": DS_SITE, "attribution": DS_ATTRIBUTION,
        "legend": DS_KINDS, "areas": [], "lines": [], "line_method": "none",
        "snapshot_id": None, "snapshot_at": "", "note": "", "counts": {},
        "requested": day.isoformat() if day else "", "stale": False, "error": "",
    }
    snap_id: int | None = None
    note = ""
    if day is not None:
        history = await deepstate_history()
        if not history:
            base["error"] = "DeepStateMap update list unavailable"
            return base
        row = snapshot_for_date(history, day)
        if row is None:
            base["error"] = f"DeepStateMap has no map on or before {day.isoformat()}"
            return base
        snap_id, note = int(row["id"]), str(row.get("note") or "")
    snap, stale = await deepstate_snapshot(snap_id)
    if snap is None:
        base["error"] = "DeepStateMap unavailable"
        return base
    ident = _int(snap.get("id"), 0)
    areas = list(snap.get("areas") or [])
    land = await ukraine_land()
    key = (ident, bool(land))
    lines = _lines_memo.get(key)
    if lines is None:
        held = areas_to_polygons(areas, DS_HELD)
        lines = await asyncio.to_thread(contact_lines, held, land)
        if len(_lines_memo) > 16:
            _lines_memo.clear()
        _lines_memo[key] = lines
    if not note:
        for row in reversed(memo_get("ds-history", DS_LIST_TTL_S) or []):
            if row["id"] == ident:
                note = str(row.get("note") or "")
                break
    counts: dict[str, int] = {}
    for area in areas:
        counts[area["kind"]] = counts.get(area["kind"], 0) + 1
    base.update({
        "areas": areas, "lines": lines,
        # "derived": the line of contact computed against the land outline;
        # "outline": no outline could be had, so the globe draws held-area edges.
        "line_method": "derived" if land else "outline",
        "snapshot_id": ident or None, "snapshot_at": iso_utc(ident) if ident else "",
        "note": note, "counts": counts, "stale": stale,
    })
    return base


# ---------------------------------------------------------------------------
# Wikipedia detailed maps: control of settlements in other theatres
# ---------------------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_TTL_S = 6 * 3600.0
WIKI_GAP_S = 1.0


@dataclass(frozen=True)
class Theatre:
    slug: str
    name: str
    module: str
    lat: float
    lon: float
    view_km: float


#: Theatres with a maintained Wikipedia module (Mozambique, last edited 2024-03,
#: is kept with its date shown). Russia-Ukraine is DeepStateMap's.
THEATRES: tuple[Theatre, ...] = (
    Theatre("sudan", "Sudan", "Module:2023 Sudanese Clashes detailed map", 14.5, 30.0, 2800),
    Theatre("myanmar", "Myanmar", "Module:Myanmar Civil War detailed map", 20.5, 96.0, 2200),
    Theatre("sahel", "Mali and the central Sahel", "Module:Mali War detailed map",
            16.0, -2.0, 2800),
    Theatre("yemen", "Yemen", "Module:Yemeni Civil War detailed map", 15.5, 46.0, 1600),
    Theatre("syria", "Syria", "Module:Syrian Civil War detailed map", 35.0, 38.5, 1200),
    Theatre("israel-palestine", "Israel, Gaza and the West Bank",
            "Module:Israeli-Palestinian conflict detailed map", 31.7, 35.0, 600),
    Theatre("lebanon", "Lebanon", "Module:Lebanese insurgency detailed map", 33.8, 35.8, 500),
    Theatre("somalia", "Somalia", "Module:Somali Civil War detailed map", 5.0, 46.0, 2200),
    Theatre("ethiopia", "Ethiopia", "Module:Ethiopian wars and insurgencies detailed map",
            9.5, 39.5, 2200),
    Theatre("car", "Central African Republic",
            "Module:Central African Republic Civil War detailed map", 6.6, 20.9, 1600),
    Theatre("libya", "Libya", "Module:Libyan Civil War detailed map", 27.0, 17.0, 2600),
    Theatre("nigeria", "Nigeria", "Module:Nigerian insurgency detailed map", 10.0, 8.0, 2000),
    Theatre("mozambique", "Northern Mozambique",
            "Module:Insurgency in Mozambique detailed map", -12.5, 39.5, 900),
)
THEATRE_BY_SLUG = {t.slug: t for t in THEATRES}

WIKI_ATTRIBUTION = ("Control of settlements: Wikipedia contributors, {module} "
                    "(en.wikipedia.org, revision {revid} of {date}), CC BY-SA 4.0.")

#: Display colours for the colour words Wikipedia marker files use.
COLOR_HEX: dict[str, str] = {
    "red": "#e0302b", "blue": "#2f6fe0", "yellow": "#f2d21b", "green": "#2e9e3e",
    "lime": "#7ddc1f", "orange": "#f08a1c", "purple": "#8e44ad", "black": "#222222",
    "grey": "#8a8a8a", "lightgrey": "#c8c8c8", "white": "#f4f4f4", "teal": "#178c8c",
    "cyan": "#22c7d6", "magenta": "#d42bc4", "pink": "#ef8fc0", "deeppink": "#ff1493",
    "brown": "#8b5a2b", "darkred": "#8b1a1a", "olive": "#808000", "navy": "#1f2f7a",
    "gold": "#d4a017", "coral": "#ff7f50", "ochre": "#c7881f", "bisque": "#f0d2a8",
    "darkslategray": "#2f4f4f", "maroon": "#800000", "violet": "#a05ad6", "tan": "#c9a46b",
    "salmon": "#f08070", "khaki": "#c3b66b", "darkgreen": "#1b5e20", "lightblue": "#7fb3ff",
    "darkblue": "#1a3a8b", "beige": "#e8dcb8", "aqua": "#22d6c8",
}

_FILE_RE = re.compile(r"\[\[\s*(?:File|Image)\s*:\s*([^|\]]+)(?:\|[^\]]*)?\]\]", re.I)
_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_LUA_BLOCK_COMMENT_RE = re.compile(r"--\[(=*)\[.*?\]\1\]", re.S)
_LUA_ENTRY_RE = re.compile(r"\{((?:[^{}\"']|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')*)\}")
_LUA_PAIR_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|"
    r"(-?\d+(?:\.\d+)?))")
_CAPTION_RE = re.compile(
    r"caption\s*=\s*(?:\[(=*)\[(.*?)\]\1\]|\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)')",
    re.S)
_PREFIX_RE = re.compile(
    r"^(?:under\s+(?:the\s+)?control\s+of|controlled\s+by|held\s+by)\s+(?:the\s+)?", re.I)


def norm_file(name: str) -> str:
    """A marker file name compared case-insensitively, with '_' as a space."""
    return _SPACE_RE.sub(" ", name.replace("_", " ")).strip().lower()


def wiki_text(value: str, limit: int = 140) -> str:
    """Plain text from a wikitext fragment: links to their labels, markup dropped."""
    text = _FILE_RE.sub(" ", value)
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = text.replace("'''", "").replace("''", "")
    text = clean_text(text, 1000).strip(" *:;,.-|")
    return text[:limit].rstrip()


def strip_lua_comments(text: str) -> str:
    """Lua source without comments; a ``--`` inside a quoted string is kept."""
    text = _LUA_BLOCK_COMMENT_RE.sub("", text)
    out_lines: list[str] = []
    for line in text.split("\n"):
        quote_ch = ""
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if quote_ch:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote_ch:
                    quote_ch = ""
            elif ch in ("'", '"'):
                quote_ch = ch
            elif ch == "-" and line.startswith("--", i):
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def parse_caption_legend(caption: str) -> dict[str, str]:
    """Marker file -> legend text from a module caption.

    Text after an icon belongs to it, and icons in a row with no text between
    them share the text that follows the last one ("[[File:a]] [[File:b]] Army
    base"). Leading "Under control of the" style wording is dropped.
    """
    def party(text: str) -> str:
        text = _PREFIX_RE.sub("", text).strip()
        return text[:1].upper() + text[1:]

    legend: dict[str, str] = {}
    for line in re.split(r"\n|<br\s*/?>", caption, flags=re.I):
        pending: list[str] = []
        pos = 0
        for m in _FILE_RE.finditer(line):
            between = wiki_text(line[pos:m.start()])
            if between and pending:
                for f in pending:
                    legend.setdefault(f, party(between))
                pending = []
            pending.append(norm_file(m.group(1)))
            pos = m.end()
        tail = wiki_text(line[pos:])
        if tail and pending:
            for f in pending:
                legend.setdefault(f, party(tail))
    return legend


_CONTROL_PATTERNS = (
    re.compile(r"^location dot ([a-z ]+?)\.(?:svg|png)$"),
    re.compile(r"^dot ([a-z]+) [0-9a-f]{3}\.(?:svg|png)$"),
    re.compile(r"^map-dot-([a-z]+)-[0-9a-f]{3}\.(?:svg|png)$"),
    re.compile(r"^([a-z]+) ?dot\.(?:svg|png)$"),
)
_PRESENCE_RE = re.compile(r"^[34]x[34]dot-([a-z ]+)\.(?:svg|png)$")
_HILL_RE = re.compile(r"^map-peak-([a-z ]+)\.(?:svg|png)$")
_CONTESTED_RE = re.compile(r"^\d+x\d+-([a-z]+(?:-[a-z]+)+)-anim\.gif$")
_SQUARE_ANIM_RE = re.compile(r"^([a-z]+(?:-[a-z]+)+)-square-anim\.gif$")
_MIXED_RE = re.compile(r"^map-ctl\d-([a-z+ ]+)\.(?:svg|png)$")


def control_color(fname: str) -> str | None:
    """The colour word of a settlement-control marker, or None."""
    for pat in _CONTROL_PATTERNS:
        m = pat.match(fname)
        if m:
            word = m.group(1).strip().replace(" ", "")
            return "grey" if word == "gray" else word
    return None


def color_words(compound: str) -> list[str]:
    """Colour words from an animated marker name: ``red-lime-green`` is red and
    lime (the ``lime-green`` file colour), ``dark-red-blue`` is darkred and blue."""
    words = compound.split("-")
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w == "lime" and nxt == "green":
            out.append("lime")
            i += 2
            continue
        if w in ("dark", "light") and nxt:
            out.append(w + nxt)
            i += 2
            continue
        out.append(w)
        i += 1
    return out


class Legend:
    """Party lookups for one module: by exact marker file, then by colour word."""

    def __init__(self, caption_legend: dict[str, str]) -> None:
        self.parties: list[dict[str, Any]] = []
        self._by_file: dict[str, int] = {}
        self._by_color: dict[str, int] = {}
        for fname, text in caption_legend.items():
            color = control_color(fname)
            if color is not None and text:
                self._add(fname, color, text)

    def _add(self, fname: str, color: str, text: str) -> int:
        idx = len(self.parties)
        self.parties.append({"party": text, "color": color,
                             "hex": COLOR_HEX.get(color, "#b0b0b0"), "count": 0})
        self._by_file[fname] = idx
        if fname.startswith("location dot ") or color not in self._by_color:
            self._by_color[color] = idx
        if color == "green" and fname.startswith("dot green"):
            # "Dot green 0d0" is drawn lime-green; hills and contested markers
            # name that colour "lime".
            self._by_color.setdefault("lime", idx)
        return idx

    def for_file(self, fname: str) -> int | None:
        idx = self._by_file.get(fname)
        if idx is not None:
            return idx
        color = control_color(fname)
        return None if color is None else self.for_color(color)

    def for_color(self, color: str) -> int:
        color = "grey" if color == "gray" else color
        idx = self._by_color.get(color)
        if idx is None and color == "lime":
            idx = self._by_color.get("green")
        if idx is None:
            idx = self._add(f"location dot {color}.svg", color,
                            f"Not in the map legend ({color} marker)")
        return idx


def parse_marks(text: str) -> list[dict[str, str]]:
    """Every ``{ lat = .., long = .., mark = .. }`` entry of a module, as strings."""
    body = strip_lua_comments(text)
    cut = body.find("containerArgs")
    if cut > 0:
        body = body[:cut]
    out: list[dict[str, str]] = []
    for m in _LUA_ENTRY_RE.finditer(body):
        pairs: dict[str, str] = {}
        for p in _LUA_PAIR_RE.finditer(m.group(1)):
            value = next((g for g in (p.group(2), p.group(3), p.group(4)) if g is not None), "")
            pairs[p.group(1).lower()] = value
        if "lat" in pairs and ("long" in pairs or "lon" in pairs) and "mark" in pairs:
            out.append(pairs)
    return out


def _classify(fname: str, legend: Legend) -> tuple[str, list[int]]:
    idx = legend.for_file(fname)
    if idx is not None:
        return "control", [idx]
    for regex, kind in ((_PRESENCE_RE, "presence"), (_HILL_RE, "hill")):
        m = regex.match(fname)
        if m:
            return kind, [legend.for_color(m.group(1).strip().replace(" ", ""))]
    m = _CONTESTED_RE.match(fname) or _SQUARE_ANIM_RE.match(fname)
    if m:
        return "contested", [legend.for_color(w) for w in color_words(m.group(1))]
    m = _MIXED_RE.match(fname)
    if m:
        return "mixed", [legend.for_color(w.strip()) for w in m.group(1).split("+") if w.strip()]
    return "", []


def parse_control_module(text: str) -> dict[str, Any]:
    """Legend and classified marks of one Wikipedia detailed-map module.

    Kinds: ``control`` (a settlement held by one party), ``presence`` (rural
    presence, the small square dots), ``hill`` (a held height), ``contested``
    (the animated two-colour marker) and ``mixed`` (stable shared control).
    Bases, airports, oil fields, dams, siege rings and pressure arcs are left out.
    """
    cap = _CAPTION_RE.search(text)
    caption = ""
    if cap:
        caption = next((g for g in (cap.group(2), cap.group(3), cap.group(4)) if g), "")
    legend = Legend(parse_caption_legend(caption))
    marks: list[dict[str, Any]] = []
    seen: set[tuple[float, float, str]] = set()
    for raw in parse_marks(text):
        lat = _float(raw.get("lat"))
        lon = _float(raw.get("long", raw.get("lon")))
        if lat is None or lon is None or not valid_point(lat, lon):
            continue
        kind, parties = _classify(norm_file(raw.get("mark", "")), legend)
        if not kind or not parties:
            continue
        key = (round(lat, 4), round(lon, 4), kind)
        if key in seen:
            continue
        seen.add(key)
        link_title = raw.get("link", "")
        if not link_title:
            lm = re.search(r"\[\[([^|\]]+)", raw.get("label", ""))
            link_title = lm.group(1) if lm else ""
        for p in parties:
            legend.parties[p]["count"] += 1
        marks.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "name": wiki_text(raw.get("label", ""), 80), "kind": kind, "p": parties,
            "wiki": clean_text(link_title, 120),
        })
    return {"legend": legend.parties, "marks": marks}


def wiki_page_url(title: str, revid: int | None = None) -> str:
    base = "https://en.wikipedia.org/w/index.php?title=" + quote(title.replace(" ", "_"))
    return base + (f"&oldid={revid}" if revid else "")


def wiki_article_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))


def parse_wiki_api(payload: object) -> tuple[str, int, str] | None:
    """(content, revision id, revision timestamp) from a MediaWiki
    ``prop=revisions`` answer (formatversion 2)."""
    try:
        rev = payload["query"]["pages"][0]["revisions"][0]  # type: ignore[index]
        return str(rev["slots"]["main"]["content"]), int(rev["revid"]), str(rev["timestamp"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def control_map(slug: str) -> dict[str, Any]:
    """Control of settlements for one theatre, cached ``WIKI_TTL_S``."""
    th = THEATRE_BY_SLUG.get(slug)
    base: dict[str, Any] = {"theatre": slug, "error": "", "marks": [], "legend": [],
                            "stale": False}
    if th is None:
        base["error"] = "Unknown theatre"
        return base
    base.update({"name": th.name, "module": th.module, "lat": th.lat, "lon": th.lon,
                 "view_km": th.view_km, "source": "Wikipedia"})
    key = f"wiki-{slug}"
    hit = memo_get(key, WIKI_TTL_S)
    if hit is not None:
        return dict(hit)
    async with _lock_for(_key_locks, key):
        hit = memo_get(key, WIKI_TTL_S)
        if hit is not None:
            return dict(hit)
        disk = cache_read(f"wiki/{slug}.json", WIKI_TTL_S)
        if disk is None:
            params = {"action": "query", "prop": "revisions", "rvprop": "content|timestamp|ids",
                      "rvslots": "main", "titles": th.module, "format": "json",
                      "formatversion": "2", "maxlag": "5"}
            await paced("en.wikipedia.org", WIKI_GAP_S)
            got = parse_wiki_api(await get_json(f"{WIKI_API}?{urlencode(params)}", 60.0))
            if got is not None:
                content, revid, stamp = got
                parsed = await asyncio.to_thread(parse_control_module, content)
                disk = {**base, **parsed, "revid": revid, "revised_at": stamp,
                        "url": wiki_page_url(th.module, revid),
                        "history_url": wiki_page_url(th.module) + "&action=history",
                        "attribution": WIKI_ATTRIBUTION.format(
                            module=th.module, revid=revid, date=stamp[:10])}
                cache_write(f"wiki/{slug}.json", disk)
            else:
                old = cache_read(f"wiki/{slug}.json", None)
                if old is None:
                    base["error"] = "Wikipedia unavailable"
                    return base
                disk = {**old, "stale": True}
        return dict(memo_put(key, disk))


def theatre_list() -> list[dict[str, Any]]:
    """Every theatre, with its data date when a copy is cached."""
    last = cache_read("deepstate/last.json", None) or {}
    ds_id = _int(last.get("id"), 0)
    out: list[dict[str, Any]] = [{
        "slug": "ukraine", "name": "Russia-Ukraine", "source": "DeepStateMap",
        "kind": "areas", "lat": 48.5, "lon": 36.5, "view_km": 1400,
        "updated": iso_utc(ds_id) if ds_id else "",
    }]
    for th in THEATRES:
        cached = cache_read(f"wiki/{th.slug}.json", None) or {}
        out.append({"slug": th.slug, "name": th.name, "source": "Wikipedia",
                    "kind": "settlements", "lat": th.lat, "lon": th.lon,
                    "view_km": th.view_km, "updated": str(cached.get("revised_at") or ""),
                    "module": th.module})
    return out


# ---------------------------------------------------------------------------
# GeoConfirmed: events verified against photo or video
# ---------------------------------------------------------------------------

GC_EXPORT_URL = "https://geoconfirmed.org/api/Map/export/csv?start={start}&end={end}"
GC_CONFLICTS_URL = "https://geoconfirmed.org/api/Conflict"
GC_SITE = "https://geoconfirmed.org"
GC_TTL_S = 1800.0
GC_WINDOW_DAYS = 90
GC_ATTRIBUTION = ("Verified events: GeoConfirmed (geoconfirmed.org), volunteer "
                  "geolocation of photo and video; each point links to its sources.")

#: Media type of the evidence, from the export's Origin column.
GC_MEDIA = {"VID": "video", "PIC": "photo", "UAV": "drone video", "SAT": "satellite image",
            "TXT": "text report", "MAP": "map", "AUD": "audio"}

#: Faction colours close to GeoConfirmed's own; others get a stable palette colour.
GC_FACTION_HEX = {
    "ukraine": "#0051ca", "russia": "#e00000", "ukraine civilian": "#3184ff",
    "russia civilian": "#ff6a6a", "neutral": "#666666", "unknown": "#999999",
    "idf": "#0051ca", "israel": "#0051ca", "hamas": "#0a5900", "hezbollah": "#e0c000",
    "iran": "#5e00bc", "belarus": "#7b3f00", "north korea": "#a000a0", "nato": "#00a0a0",
}
_PALETTE = ("#e07b39", "#2ca58d", "#b5179e", "#7209b7", "#3a86ff", "#8ac926", "#ff595e",
            "#ffca3a", "#6a4c93", "#1982c4")

#: Export ``Conflict`` value -> (page slug, display name); /api/Conflict refreshes it.
GC_CONFLICTS: dict[str, tuple[str, str]] = {
    "World": ("world", "Elsewhere"), "Ukraine": ("ukraine", "Ukraine"),
    "Iran": ("iran", "Iran"), "VEN": ("ven", "Venezuela"),
    "NagornoKarabakh": ("nagorno_karabakh", "Nagorno-Karabakh"),
    "Sahel": ("africa", "Africa"), "DRC": ("drc", "DR Congo"),
    "Israel": ("israel", "Israel, Gaza and Lebanon"), "Syria": ("syria", "Syria"),
    "Yemen": ("yemen", "Yemen"), "Afgh": ("afghanistan", "Afghanistan and Pakistan"),
    "INDPAK": ("indpak", "India-Pakistan"), "THAKHM": ("thailandcambodia",
                                                       "Thailand-Cambodia"),
    "Taiwan": ("pac", "Pacific"), "Myanmar": ("myanmar", "Myanmar"),
    "Cartel": ("cartel", "Cartels"),
}


#: The export's Name column is often just the day ("25 AUG 2026"); not a title then.
_GC_DATE_NAME_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}$")


def faction_hex(faction: str) -> str:
    key = faction.strip().lower()
    if key in GC_FACTION_HEX:
        return GC_FACTION_HEX[key]
    return _PALETTE[sum(map(ord, key)) % len(_PALETTE)]


def parse_geoconfirmed_conflicts(payload: object) -> dict[str, tuple[str, str]]:
    """``/api/Conflict`` rows as {shortName: (url slug, name)}."""
    if not isinstance(payload, list):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for row in payload:
        if not isinstance(row, dict) or row.get("isPrivate"):
            continue
        short, slug = row.get("shortName"), row.get("url")
        if isinstance(short, str) and isinstance(slug, str) and re.fullmatch(r"[\w-]+", slug):
            name = clean_text(row.get("name"), 60).replace(" - ", "-").replace("/", ", ")
            out[short] = (slug, name or short)
    return out


def parse_geoconfirmed_csv(text: str, conflicts: dict[str, tuple[str, str]] | None = None,
                           max_rows: int = 20000) -> list[dict[str, Any]]:
    """Dated placemarks from ``/api/Map/export/csv`` (semicolon separated, BOM),
    newest first, each id once (the export can repeat a placemark)."""
    table = {**GC_CONFLICTS, **(conflicts or {})}
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reader:
        if len(out) >= max_rows:
            break
        day = (row.get("Date") or "").strip()
        lat, lon = _float(row.get("Latitude")), _float(row.get("Longitude"))
        ident = (row.get("Id") or "").strip().lower()
        if (not _DATE_RE.match(day) or lat is None or lon is None or not valid_point(lat, lon)
                or not re.fullmatch(r"[0-9a-f-]{8,64}", ident) or ident in seen):
            continue
        seen.add(ident)
        conflict = clean_text(row.get("Conflict") or "", 40)
        slug, cname = table.get(conflict, (conflict.lower(), conflict))
        plus = clean_text(row.get("PlusCode") or "", 160)
        place = plus.split(" ", 1)[1] if re.match(r"^[0-9A-Z]{4,}\+\S*\s", plus) else plus
        faction = clean_text(row.get("Faction") or "", 40) or "Unknown"
        origin = (row.get("Origin") or "").strip().upper()
        name = clean_text(row.get("Name") or "", 160)
        out.append({
            "id": ident, "date": day[:10], "conflict": conflict, "conflict_name": cname,
            "faction": faction, "color": faction_hex(faction),
            "media": GC_MEDIA.get(origin, origin.lower()),
            "lat": round(lat, 5), "lon": round(lon, 5), "place": place,
            "title": "" if _GC_DATE_NAME_RE.match(name) else name,
            "text": clean_text(row.get("Description") or "", 600),
            "sources": find_urls(row.get("Source"), 6),
            "geolocation": find_urls(row.get("Geolocation"), 3),
            "equipment": clean_text(row.get("EquipmentItems") or row.get("Equipment") or "",
                                    200),
            "units": clean_text(row.get("OrbatUnits") or row.get("Units") or "", 200),
            "url": f"{GC_SITE}/{quote(slug)}/{ident}",
        })
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


#: Fields the globe needs to draw and filter a point; the rest come from /events/{id}.
GC_COMPACT = ("id", "date", "conflict", "faction", "color", "media", "lat", "lon")


_gc_refresh: set[asyncio.Task[None]] = set()


async def _fetch_geoconfirmed() -> dict[str, Any] | None:
    """One upstream pull of the window, written to disk; None when nothing came back."""
    now = utc_now().replace(minute=0, second=0, microsecond=0)
    start = (now - timedelta(days=GC_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conflicts = parse_geoconfirmed_conflicts(await get_json(GC_CONFLICTS_URL))
    await paced("geoconfirmed.org", 1.0)
    body = await http_get(GC_EXPORT_URL.format(start=start, end=end), 120.0)
    events = parse_geoconfirmed_csv(body.decode("utf-8-sig", "replace"), conflicts) \
        if body else []
    if not events:
        return None
    names = {k: v[1] for k, v in {**GC_CONFLICTS, **conflicts}.items()}
    fresh = {"events": events, "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "window_days": GC_WINDOW_DAYS, "conflict_names": names,
             "stale": False, "error": ""}
    cache_write("geoconfirmed.json", fresh)
    return fresh


async def _refresh_geoconfirmed() -> None:
    """Background refresh behind a stale copy; a failure keeps the stale copy served."""
    try:
        fresh = await _fetch_geoconfirmed()
    except Exception:  # noqa: BLE001 - a background refresh must never raise into the loop
        fresh = None
    if fresh is not None:
        memo_put("gc-events", fresh)


async def geoconfirmed_events() -> dict[str, Any]:
    """The last ``GC_WINDOW_DAYS`` of verified events, all conflicts, one upstream
    request per ``GC_TTL_S``. The 90-day export can take minutes, so once any copy is
    on disk an expired one is answered at once (marked stale) while a background task
    refreshes it; only a first run with nothing on disk waits for the upstream."""
    hit = memo_get("gc-events", GC_TTL_S)
    if hit is not None:
        return dict(hit)
    async with _lock_for(_key_locks, "gc-events"):
        hit = memo_get("gc-events", GC_TTL_S)
        if hit is not None:
            return dict(hit)
        disk = cache_read("geoconfirmed.json", GC_TTL_S)
        if disk is None:
            old = cache_read("geoconfirmed.json", None)
            if old is not None:
                if not _gc_refresh:
                    task = asyncio.get_running_loop().create_task(_refresh_geoconfirmed())
                    _gc_refresh.add(task)
                    task.add_done_callback(_gc_refresh.discard)
                # held for one TTL, so a refresh that fails is retried every 30 minutes,
                # not on every request
                return dict(memo_put("gc-events", {**old, "stale": True}))
            disk = await _fetch_geoconfirmed()
            if disk is None:
                return {"events": [], "error": "GeoConfirmed unavailable",
                        "window_days": GC_WINDOW_DAYS, "fetched_at": "",
                        "conflict_names": {}, "stale": False}
        return dict(memo_put("gc-events", disk))


# ---------------------------------------------------------------------------
# ACLED (optional, needs an account)
# ---------------------------------------------------------------------------

ACLED_TOKEN_URL = "https://acleddata.com/oauth/token"
ACLED_READ_URL = "https://acleddata.com/api/acled/read"
ACLED_TTL_S = 3600.0
ACLED_FIELDS = ("event_id_cnty|event_date|event_type|sub_event_type|actor1|actor2|country|"
                "admin1|location|latitude|longitude|fatalities|notes|source|geo_precision")
ACLED_NEEDS = ("ACLED needs an account: set ACLED_EMAIL and ACLED_PASSWORD on the server "
               "(ACLED_KEY is read as the password; ACLED retired access keys in 2025).")
ACLED_ATTRIBUTION = "Events: ACLED (acleddata.com), used under the account holder's terms."
ACLED_COMPACT = ("id", "date", "type", "lat", "lon", "fatalities")


def acled_credentials() -> tuple[str, str] | None:
    email = os.environ.get("ACLED_EMAIL", "").strip()
    secret = (os.environ.get("ACLED_PASSWORD", "") or os.environ.get("ACLED_KEY", "")).strip()
    return (email, secret) if email and secret else None


def parse_acled(payload: object) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
        day = str(row.get("event_date") or "")
        if lat is None or lon is None or not valid_point(lat, lon) or not _DATE_RE.match(day):
            continue
        out.append({
            "id": clean_text(row.get("event_id_cnty"), 40), "date": day[:10],
            "type": clean_text(row.get("event_type"), 60),
            "sub_type": clean_text(row.get("sub_event_type"), 80),
            "actor1": clean_text(row.get("actor1"), 120),
            "actor2": clean_text(row.get("actor2"), 120),
            "country": clean_text(row.get("country"), 60),
            "place": ", ".join(x for x in (clean_text(row.get("location"), 80),
                                           clean_text(row.get("admin1"), 80)) if x),
            "lat": round(lat, 5), "lon": round(lon, 5),
            "fatalities": _int(row.get("fatalities")),
            "text": clean_text(row.get("notes"), 600),
            "source": clean_text(row.get("source"), 200),
            "url": "https://acleddata.com/",
        })
    return out


async def acled_events(days: int = 30) -> dict[str, Any]:
    """Recent ACLED events when an account is configured, else the hint."""
    creds = acled_credentials()
    if creds is None:
        return {"events": [], "configured": False, "error": ACLED_NEEDS, "days": 0}
    days = max(1, min(90, int(days)))
    key = f"acled-{days}"
    hit = memo_get(key, ACLED_TTL_S)
    if hit is not None:
        return dict(hit)
    async with _lock_for(_key_locks, "acled"):
        hit = memo_get(key, ACLED_TTL_S)
        if hit is not None:
            return dict(hit)
        token = memo_get("acled-token", 20 * 3600.0)
        if not token:
            got = await http_post_form(ACLED_TOKEN_URL, {
                "username": creds[0], "password": creds[1], "grant_type": "password",
                "client_id": "acled", "scope": "authenticated"})
            token = got.get("access_token") if isinstance(got, dict) else None
            if not isinstance(token, str) or not token:
                return {"events": [], "configured": True, "days": days,
                        "error": "ACLED refused the credentials or is unavailable"}
            memo_put("acled-token", token)
        end = utc_now().date()
        start = end - timedelta(days=days)
        params = {"_format": "json", "event_date": f"{start}|{end}",
                  "event_date_where": "BETWEEN", "limit": "5000", "fields": ACLED_FIELDS}
        payload = await get_json(f"{ACLED_READ_URL}?{urlencode(params)}", 120.0,
                                 {"Authorization": f"Bearer {token}"})
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            _memo.pop("acled-token", None)
            return {"events": [], "configured": True, "days": days,
                    "error": "ACLED read failed"}
        result = {"events": parse_acled(payload), "configured": True, "error": "",
                  "days": days, "attribution": ACLED_ATTRIBUTION}
        return dict(memo_put(key, result))


def compact(events: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{k: e.get(k) for k in fields} for e in events]


# ---------------------------------------------------------------------------
# UCDP GED: the historical record, imported into SQLite
# ---------------------------------------------------------------------------

UCDP_DOWNLOADS = "https://ucdp.uu.se/downloads/index.html"
UCDP_GED_FALLBACK = "https://ucdp.uu.se/downloads/ged/ged261-csv.zip"
UCDP_CANDIDATE_FALLBACK = "https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_01_26_09.csv"
UCDP_ATTRIBUTION = ("History: UCDP Georeferenced Event Dataset (Sundberg and Melander 2013; "
                    "Davies et al. 2025), Uppsala Conflict Data Program, CC BY 4.0.")
VIOLENCE_TYPES = {1: "State-based conflict", 2: "Non-state conflict", 3: "One-sided violence"}
DENSITY_CELLS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)
MAX_DENSITY_CELLS = 20000
COUNT_CAP = 100_000

_UCDP_PAGES = {1: "conflict", 2: "nonstate", 3: "onesided"}


def ucdp_url(tv: int, conflict_id: int) -> str:
    """The UCDP Conflict Encyclopedia page for an event's conflict."""
    if conflict_id <= 0:
        return "https://ucdp.uu.se/"
    return f"https://ucdp.uu.se/{_UCDP_PAGES.get(tv, 'conflict')}/{conflict_id}"


_GED_COLS = ("id", "relid", "year", "date_start", "date_end", "tv", "conflict", "conflict_id",
             "dyad", "side_a", "side_b", "country", "region", "adm1", "place", "lat", "lon",
             "where_prec", "best", "low", "high", "civ", "source", "dataset", "status")


def parse_ged_row(row: dict[str, str], dataset: str) -> tuple[Any, ...] | None:
    """One GED CSV row as a tuple in ``_GED_COLS`` order, or None when unusable."""
    ident = _int(row.get("id"), -1)
    lat, lon = _float(row.get("latitude")), _float(row.get("longitude"))
    year = _int(row.get("year"), 0)
    if ident < 0 or year < 1946 or lat is None or lon is None or not valid_point(lat, lon):
        return None
    return (
        ident, clean_text(row.get("relid"), 40), year, (row.get("date_start") or "")[:10],
        (row.get("date_end") or "")[:10], _int(row.get("type_of_violence"), 0),
        clean_text(row.get("conflict_name"), 160), _int(row.get("conflict_new_id"), 0),
        clean_text(row.get("dyad_name"), 200), clean_text(row.get("side_a"), 160),
        clean_text(row.get("side_b"), 160), clean_text(row.get("country"), 80),
        clean_text(row.get("region"), 40), clean_text(row.get("adm_1"), 120),
        clean_text(row.get("where_coordinates"), 160), lat, lon,
        _int(row.get("where_prec"), 0), max(0, _int(row.get("best"))),
        max(0, _int(row.get("low"))), max(0, _int(row.get("high"))),
        max(0, _int(row.get("deaths_civilians"))), clean_text(row.get("source_article"), 300),
        dataset, clean_text(row.get("code_status"), 40),
    )


def open_ged(path: Path) -> Iterator[IO[str]]:
    """Text handles for every CSV inside *path* (a .zip or a .csv)."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    with zf.open(name) as raw:
                        yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            yield fh


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events (
  id INTEGER PRIMARY KEY, relid TEXT, year INTEGER NOT NULL, date_start TEXT, date_end TEXT,
  tv INTEGER NOT NULL, conflict TEXT, conflict_id INTEGER, dyad TEXT, side_a TEXT,
  side_b TEXT, country TEXT, region TEXT, adm1 TEXT, place TEXT, lat REAL NOT NULL,
  lon REAL NOT NULL, where_prec INTEGER, best INTEGER, low INTEGER, high INTEGER,
  civ INTEGER, source TEXT, dataset TEXT, status TEXT
);
CREATE VIRTUAL TABLE events_rt USING rtree(
  id, minlat, maxlat, minlon, maxlon, minyr, maxyr, +best, +tv
);
CREATE TABLE density (
  cell REAL NOT NULL, year INTEGER NOT NULL, tv INTEGER NOT NULL, ci INTEGER NOT NULL,
  cj INTEGER NOT NULL, n INTEGER NOT NULL, deaths INTEGER NOT NULL, slat REAL NOT NULL,
  slon REAL NOT NULL
);
"""


@dataclass
class ImportReport:
    events: int = 0
    skipped: int = 0
    datasets: list[str] = field(default_factory=list)
    years: tuple[int, int] = (0, 0)
    deaths: int = 0
    countries: int = 0
    seconds: float = 0.0


def build_history(inputs: list[tuple[Path, str]], dest: Path,
                  log: Callable[[str], None] = lambda _m: None) -> ImportReport:
    """Write the UCDP store to *dest* from (file, dataset label) pairs.

    A later input never adds events to a year an earlier one already covers,
    so the candidate file only adds the months the yearly release lacks. The
    store is built beside *dest* and swapped in; a server holding the old file
    open picks the new one up from ``<dest>.new`` on its next query.
    """
    started = time.monotonic()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    report = ImportReport()
    insert = f"INSERT OR IGNORE INTO events VALUES ({', '.join('?' * len(_GED_COLS))})"
    try:
        conn.executescript(_SCHEMA)
        covered = 0
        for path, label in inputs:
            batch: list[tuple[Any, ...]] = []
            kept = 0
            year_max = 0
            csv.field_size_limit(16 * 1024 * 1024)
            for fh in open_ged(path):
                for row in csv.DictReader(fh):
                    ev = parse_ged_row(row, label)
                    if ev is None:
                        continue
                    if ev[2] <= covered:
                        report.skipped += 1
                        continue
                    batch.append(ev)
                    year_max = max(year_max, ev[2])
                    if len(batch) >= 20000:
                        kept += _insert(conn, insert, batch)
                        batch.clear()
                        if kept % 100000 < 20000:
                            log(f"  {label}: {kept:,} events")
            kept += _insert(conn, insert, batch)
            covered = max(covered, year_max)
            report.datasets.append(f"{label} ({kept:,} events)")
            log(f"  {label}: {kept:,} events")
        conn.execute("INSERT INTO events_rt SELECT id, lat, lat, lon, lon, year, year, best, tv "
                     "FROM events")
        for cell in DENSITY_CELLS:
            conn.execute(
                "INSERT INTO density SELECT ?, year, tv, CAST((lat + 90.0) / ? AS INTEGER) AS ci,"
                " CAST((lon + 180.0) / ? AS INTEGER) AS cj, COUNT(*), SUM(best), SUM(lat),"
                " SUM(lon) FROM events GROUP BY year, tv, ci, cj", (cell, cell, cell))
        conn.execute("CREATE INDEX density_cell ON density(cell, ci, cj, year)")
        conn.execute("CREATE INDEX events_year ON events(year)")
        n, deaths, y0, y1, countries = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(best), 0), MIN(year), MAX(year),"
            " COUNT(DISTINCT country) FROM events").fetchone()
        years = conn.execute(
            "SELECT year, COUNT(*), SUM(best) FROM events GROUP BY year ORDER BY year"
        ).fetchall()
        meta = {
            "schema": SCHEMA_VERSION, "events": str(n), "deaths": str(deaths),
            "year_min": str(y0 or 0), "year_max": str(y1 or 0), "countries": str(countries),
            "datasets": json.dumps(report.datasets),
            "years": json.dumps([[int(y), int(c), int(d or 0)] for y, c, d in years]),
            "imported_at": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "complete": "1",
        }
        conn.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
        conn.commit()
        report.events, report.deaths, report.countries = int(n), int(deaths), int(countries)
        report.years = (int(y0 or 0), int(y1 or 0))
    finally:
        conn.close()
    pending = dest.with_name(dest.name + ".new")
    os.replace(tmp, pending)
    reset_store()
    try:
        os.replace(pending, dest)
    except OSError:
        log(f"  {dest.name} is open in a running server; the new store waits as "
            f"{pending.name} and is swapped in on the server's next query")
    report.seconds = time.monotonic() - started
    return report


def _insert(conn: sqlite3.Connection, sql: str, batch: list[tuple[Any, ...]]) -> int:
    if not batch:
        return 0
    before = conn.total_changes
    conn.executemany(sql, batch)
    return conn.total_changes - before


def _lon_spans(west: float, east: float) -> list[tuple[float, float]]:
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def snap_cell(cell_deg: float) -> float:
    for c in DENSITY_CELLS:
        if c >= cell_deg - 1e-9:
            return c
    return DENSITY_CELLS[-1]


def _types_clause(types: tuple[int, ...], column: str = "tv") -> tuple[str, list[int]]:
    valid = sorted({t for t in types if t in VIOLENCE_TYPES})
    if not valid or len(valid) == len(VIOLENCE_TYPES):
        return "", []
    return f" AND {column} IN ({','.join('?' * len(valid))})", valid


def _event_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    d = dict(zip(_GED_COLS, row, strict=True))
    place = d["place"] or ""
    if d["adm1"] and d["adm1"] not in place:
        place = f"{place}, {d['adm1']}" if place else d["adm1"]
    return {
        "id": d["id"], "relid": d["relid"], "year": d["year"], "date": d["date_start"],
        "date_end": d["date_end"], "type": d["tv"], "type_name": VIOLENCE_TYPES.get(d["tv"], ""),
        "conflict": d["conflict"], "dyad": d["dyad"], "side_a": d["side_a"],
        "side_b": d["side_b"], "country": d["country"], "region": d["region"], "place": place,
        "lat": d["lat"], "lon": d["lon"], "where_prec": d["where_prec"], "best": d["best"],
        "low": d["low"], "high": d["high"], "civilian_deaths": d["civ"], "source": d["source"],
        "dataset": d["dataset"], "status": d["status"], "url": ucdp_url(d["tv"], d["conflict_id"]),
    }


class HistoryStore:
    """Read-only access to the UCDP store, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._meta: dict[str, str] = {}
        self._checked = 0.0
        self.outdated = False

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._meta = {}

    def _connection(self) -> sqlite3.Connection | None:
        now = time.monotonic()
        if self._conn is None or now - self._checked > 10.0:
            self._checked = now
            pending = self.path.with_name(self.path.name + ".new")
            if pending.exists():
                self._close_locked()
                with contextlib.suppress(OSError):
                    os.replace(pending, self.path)
        if self._conn is None:
            if not self.path.exists():
                return None
            uri = self.path.resolve().as_uri() + "?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            except sqlite3.Error:
                return None
            if meta.get("complete") != "1" or meta.get("schema") != SCHEMA_VERSION:
                self.outdated = meta.get("complete") == "1"
                conn.close()
                return None
            self.outdated = False
            self._conn, self._meta = conn, meta
        return self._conn

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connection() is not None

    def summary(self) -> dict[str, Any]:
        with self._lock:
            if self._connection() is None:
                return {"available": False, "events": 0,
                        "hint": OUTDATED_HINT if self.outdated else IMPORT_HINT,
                        "attribution": UCDP_ATTRIBUTION,
                        "types": {str(k): v for k, v in VIOLENCE_TYPES.items()}}
            m = self._meta
            return {
                "available": True, "events": int(m.get("events", "0")),
                "deaths": int(m.get("deaths", "0")), "countries": int(m.get("countries", "0")),
                "year_min": int(m.get("year_min", "0")), "year_max": int(m.get("year_max", "0")),
                "datasets": json.loads(m.get("datasets", "[]")),
                "imported_at": m.get("imported_at", ""), "attribution": UCDP_ATTRIBUTION,
                "types": {str(k): v for k, v in VIOLENCE_TYPES.items()}, "hint": "",
            }

    def years(self) -> list[dict[str, int]]:
        with self._lock:
            if self._connection() is None:
                return []
            rows = json.loads(self._meta.get("years", "[]"))
        return [{"year": y, "events": c, "deaths": d} for y, c, d in rows]

    def density(self, west: float, south: float, east: float, north: float, cell: float,
                year_from: int, year_to: int,
                types: tuple[int, ...] = ()) -> tuple[float, list[dict[str, Any]]]:
        """Grid cells over the box, each at the mean position of its events, with
        event and death totals. The cell grows until the answer fits the cap."""
        cell = snap_cell(cell)
        type_sql, type_args = _types_clause(types)
        with self._lock:
            conn = self._connection()
            if conn is None:
                return cell, []
            while True:
                ci0, ci1 = int((south + 90.0) / cell), int((north + 90.0) / cell)
                cells: list[dict[str, Any]] = []
                for w, e in _lon_spans(west, east):
                    cj0, cj1 = int((w + 180.0) / cell), int((e + 180.0) / cell)
                    rows = conn.execute(
                        "SELECT SUM(n), SUM(deaths), SUM(slat), SUM(slon) FROM density "
                        "WHERE cell = ? AND ci BETWEEN ? AND ? AND cj BETWEEN ? AND ? "
                        "AND year BETWEEN ? AND ?" + type_sql + " GROUP BY ci, cj LIMIT ?",
                        [cell, ci0, ci1, cj0, cj1, year_from, year_to, *type_args,
                         MAX_DENSITY_CELLS + 1]).fetchall()
                    cells.extend({"lat": round(slat / n, 4), "lon": round(slon / n, 4),
                                  "events": int(n), "deaths": int(d or 0)}
                                 for n, d, slat, slon in rows if n)
                if len(cells) <= MAX_DENSITY_CELLS or cell >= DENSITY_CELLS[-1]:
                    return cell, cells[:MAX_DENSITY_CELLS]
                cell = snap_cell(cell * 2)

    def events(self, west: float, south: float, east: float, north: float, year_from: int,
               year_to: int, types: tuple[int, ...] = (),
               limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """The deadliest *limit* events in the box, and how many match (capped).

        The R*Tree holds position and year as dimensions and the death toll and
        type alongside, so the filter, the count and the ranking never touch the
        events table; only the chosen rows are read from it.
        """
        type_sql, type_args = _types_clause(types, "tv")
        with self._lock:
            conn = self._connection()
            if conn is None:
                return [], 0
            top: list[tuple[int, int]] = []
            total = 0
            for w, e in _lon_spans(west, east):
                where = ("FROM events_rt WHERE minlat >= ? AND maxlat <= ? AND minlon >= ? "
                         "AND maxlon <= ? AND minyr >= ? AND maxyr <= ?" + type_sql)
                args = [south, north, w, e, year_from, year_to, *type_args]
                total += conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT 1 {where} LIMIT {COUNT_CAP})", args
                ).fetchone()[0]
                top.extend(conn.execute(
                    f"SELECT id, best {where} ORDER BY best DESC LIMIT ?", [*args, limit]
                ).fetchall())
            top.sort(key=lambda r: -int(r[1] or 0))
            ids = [int(r[0]) for r in top[:limit]]
            found: dict[int, tuple[Any, ...]] = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for row in conn.execute(
                        f"SELECT {', '.join(_GED_COLS)} FROM events WHERE id IN ({marks})", ids):
                    found[int(row[0])] = row
        return [_event_dict(found[i]) for i in ids if i in found], total

    def get(self, ident: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            row = conn.execute(f"SELECT {', '.join(_GED_COLS)} FROM events WHERE id = ?",
                               (ident,)).fetchone()
        return _event_dict(row) if row else None


_store: HistoryStore | None = None
_store_lock = threading.Lock()


def get_store() -> HistoryStore:
    """The process-wide UCDP store for the current SDA_CONFLICT_DIR."""
    global _store
    path = db_path()
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                _store.close()
            _store = HistoryStore(path)
        return _store


def reset_store() -> None:
    """Forget the open store (the import, and tests changing SDA_CONFLICT_DIR)."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


# ---------------------------------------------------------------------------
# Import command
# ---------------------------------------------------------------------------


def discover_ucdp_urls(page: str) -> tuple[str, str]:
    """The newest yearly GED csv zip and the newest year-to-date candidate CSV
    linked from the UCDP downloads page, or the known fallbacks."""
    ged, cand = UCDP_GED_FALLBACK, UCDP_CANDIDATE_FALLBACK
    geds = re.findall(r"https://ucdp\.uu\.se/downloads/ged/ged(\d+)-csv\.zip", page)
    if geds:
        ged = f"https://ucdp.uu.se/downloads/ged/ged{max(geds, key=int)}-csv.zip"
    cands = re.findall(
        r"https://ucdp\.uu\.se/downloads/candidateged/GEDEvent_v(\d+)_01_(\d+)_(\d+)\.csv", page)
    if cands:
        v, y, m = max(cands, key=lambda c: (int(c[0]), int(c[1]), int(c[2])))
        cand = f"https://ucdp.uu.se/downloads/candidateged/GEDEvent_v{v}_01_{y}_{m}.csv"
    return ged, cand


def dataset_label(path: Path) -> str:
    """A readable dataset name from a UCDP file name."""
    name = path.name
    m = re.match(r"ged(\d\d)(\d+)-csv\.zip$", name, re.I)
    if m:
        return f"UCDP GED {m.group(1)}.{m.group(2)}"
    m = re.match(r"GEDEvent_v(\d+)_01_(\d+)_(\d+)\.csv$", name, re.I)
    if m:
        return f"UCDP Candidate {m.group(1)}.01-{m.group(2)}.{m.group(3)}"
    m = re.match(r"GEDEvent_v(\d+)_(\d+)\.csv$", name, re.I)
    if m:
        return f"UCDP GED {m.group(1)}.{m.group(2)}"
    m = re.match(r"GEDEvent_v(\d+)_(\d+)_(\d+)\.csv$", name, re.I)
    if m:
        return f"UCDP Candidate {m.group(1)}.{m.group(2)}.{m.group(3)}"
    return path.stem


def download(url: str, dest: Path) -> None:
    """Stream *url* to *dest*, refusing an HTML page in place of a data file."""
    import httpx

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    last = 0.0
    with httpx.stream("GET", url, timeout=300.0, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise RuntimeError(f"{url} answered with a web page, not a data file")
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 5.0:
                    last = time.monotonic()
                    print(f"  downloaded {done / 1e6:.1f} MB", flush=True)
    os.replace(part, dest)
    print(f"  saved {done / 1e6:.1f} MB as {dest.name}", flush=True)


def resolve_inputs(source: str | None, no_candidate: bool, refresh: bool,
                   work: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """(file, label) pairs to import and the files this run downloaded."""
    if source:
        src = Path(source)
        if src.is_dir():
            yearly = sorted(src.glob("ged*-csv.zip"))[-1:]
            cands = [] if no_candidate else sorted(src.glob("GEDEvent_v*_01_*_*.csv"))[-1:]
            chosen = yearly + cands
            if not chosen:
                raise FileNotFoundError(f"no ged*-csv.zip or GEDEvent_v*.csv in {src}")
        elif src.is_file():
            chosen = [src]
        else:
            raise FileNotFoundError(f"no such file or folder: {src}")
        return [(p, dataset_label(p)) for p in chosen], []
    import httpx

    try:
        page = httpx.get(UCDP_DOWNLOADS, timeout=60.0, headers={"User-Agent": USER_AGENT},
                         follow_redirects=True).text
    except httpx.HTTPError:
        page = ""
    ged_url, cand_url = discover_ucdp_urls(page)
    out: list[tuple[Path, str]] = []
    fetched: list[Path] = []
    for url in [ged_url] + ([] if no_candidate else [cand_url]):
        target = work / url.rsplit("/", 1)[-1]
        if target.exists() and target.stat().st_size > 0 and not refresh:
            print(f"Using {target.name}, already downloaded", flush=True)
        else:
            print(f"Fetching {url} ...", flush=True)
            download(url, target)
            fetched.append(target)
        out.append((target, dataset_label(target)))
    return out, fetched


def _cmd_import(args: argparse.Namespace) -> int:
    dest = Path(args.db) if args.db else db_path()
    inputs, fetched = resolve_inputs(args.source, args.no_candidate, args.refresh,
                                     dest.parent / "download")
    print("Importing " + ", ".join(f"{label} ({p.name})" for p, label in inputs) + " ...",
          flush=True)
    report = build_history(inputs, dest, log=lambda m: print(m, flush=True))
    if args.delete_downloads:
        for path in fetched:
            path.unlink(missing_ok=True)
    print(f"Done in {report.seconds:.0f} s: {report.events:,} events in "
          f"{report.countries} countries, {report.deaths:,} deaths (best estimate), "
          f"{report.years[0]}-{report.years[1]}. {report.skipped:,} rows skipped as already "
          f"covered. Store: {dest}", flush=True)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else db_path()
    store = HistoryStore(path)
    try:
        s = store.summary()
    finally:
        store.close()
    if not s.get("available"):
        print(f"No usable UCDP store at {path}. {s.get('hint') or IMPORT_HINT}")
        return 1
    print(f"Store: {path} ({path.stat().st_size / 1e6:,.0f} MB)")
    print(f"Events: {s['events']:,}, deaths {s['deaths']:,}, {s['countries']} countries, "
          f"{s['year_min']}-{s['year_max']}")
    print(f"Imported {s['imported_at']} from {'; '.join(s['datasets'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sda.conflict",
        description="Import the UCDP Georeferenced Event Dataset for the globe's conflict layer.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="build the UCDP history store")
    imp.add_argument("--from", dest="source",
                     help="a ged*-csv.zip or GEDEvent CSV, or a folder holding them "
                          "(default: download the newest from ucdp.uu.se)")
    imp.add_argument("--no-candidate", action="store_true",
                     help="skip the year-to-date candidate events")
    imp.add_argument("--refresh", action="store_true",
                     help="download again even when the files are already present")
    imp.add_argument("--delete-downloads", action="store_true",
                     help="delete the files this run downloaded once imported")
    imp.add_argument("--db", help=f"store file (default: {db_path()})")
    imp.set_defaults(func=_cmd_import)
    st = sub.add_parser("status", help="show what the store holds")
    st.add_argument("--db", help=f"store file (default: {db_path()})")
    st.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
