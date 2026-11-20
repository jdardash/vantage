"""OpenStreetMap buildings for the globe's 3D city view.

Footprints come from the public Overpass API (OpenStreetMap contributors, ODbL)
one small box at a time, and heights from the OSM tags: ``height`` in metres
when it parses, else ``building:levels`` times 3.2 m, else 8 m. The globe
extrudes them when no Google Photorealistic 3D Tiles are available, which is
the case whenever neither a Google key nor a free Cesium ion token is set.

Boxes are rounded outward to 0.001 degrees and each rounded box is cached for
24 hours, so a camera moving about one district asks Overpass once. A failed
answer is never cached and comes back as a sentence, not an empty layer.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from sda import hostthrottle

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_HOST = "overpass-api.de"
USER_AGENT = "vantage/0.2 (worldview 3D buildings)"

MAX_SPAN_DEG = 0.02
GRID_DEG = 0.001
CACHE_TTL_S = 24 * 3600.0
CACHE_MAX = 256
MAX_BUILDINGS = 3000

METRES_PER_LEVEL = 3.2
DEFAULT_HEIGHT_M = 8.0
_FOOT_M = 0.3048

ATTRIBUTION = "Buildings: OpenStreetMap contributors (ODbL), via Overpass"
UNAVAILABLE = (
    "OpenStreetMap buildings did not load (the free Overpass server is busy or "
    "offline); move the map to try again."
)

_HEIGHT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|metres|meters|ft|feet)?\s*$", re.IGNORECASE)
_GENERIC = {"yes", "true", "1"}

# rounded box -> (monotonic time, rows, truncated)
_cache: dict[tuple[float, float, float, float], tuple[float, list[dict[str, Any]], bool]] = {}


def clear_cache() -> None:
    """Forget every cached box (tests)."""
    _cache.clear()


# ---------------------------------------------------------------- heights


def _height_tag(raw: Any) -> float | None:
    match = _HEIGHT_RE.match(str(raw)) if raw is not None else None
    if not match:
        return None
    value = float(match.group(1))
    if (match.group(2) or "m").lower() in ("ft", "feet"):
        value *= _FOOT_M
    return value if 0 < value < 1000 else None


def _levels(raw: Any) -> float | None:
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and 0 < value < 300 else None


def _height(tags: Mapping[str, str]) -> tuple[float, str]:
    tagged = _height_tag(tags.get("height"))
    if tagged is not None:
        return tagged, "height"
    levels = _levels(tags.get("building:levels"))
    if levels is not None:
        return levels * METRES_PER_LEVEL, "levels"
    return DEFAULT_HEIGHT_M, "default"


def building_height_m(tags: Mapping[str, str]) -> float:
    """``height`` in metres when it parses, else levels times 3.2, else 8.0."""
    return _height(tags)[0]


# ---------------------------------------------------------------- parsing


def _point(node: Any) -> list[float] | None:
    if not isinstance(node, Mapping):
        return None
    try:
        lat = float(node["lat"])
        lon = float(node["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return [round(lon, 7), round(lat, 7)]


def _ring(geometry: Any, close: bool = True) -> list[list[float]] | None:
    """A closed [lon, lat] ring of at least three corners, or None.

    A way Overpass sent unclosed is closed here; with ``close`` False (the pieces
    of a multipolygon, which may be one segment of a longer ring) it is refused.
    """
    if not isinstance(geometry, list):
        return None
    ring = [p for p in (_point(n) for n in geometry) if p is not None]
    if len(ring) != len(geometry) or len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        if not close:
            return None
        ring.append(list(ring[0]))
    return ring if len(ring) >= 4 else None


def _area(ring: list[list[float]]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:], strict=False))) / 2


def _outer_ring(members: Any) -> list[list[float]] | None:
    """The largest outer ring of a multipolygon that closes on its own."""
    if not isinstance(members, list):
        return None
    rings = [
        _ring(m.get("geometry"), close=False)
        for m in members
        if isinstance(m, Mapping) and m.get("type") == "way" and m.get("role") == "outer"
    ]
    closed = [r for r in rings if r is not None]
    return max(closed, key=_area) if closed else None


def _kind(tags: Mapping[str, str]) -> str:
    building = str(tags.get("building", "")).strip().lower()
    if building and building not in _GENERIC:
        return building[:40]
    if tags.get("amenity"):
        return str(tags["amenity"]).strip().lower()[:40]
    for key in ("shop", "office"):
        if tags.get(key):
            return key
    return "building"


def _name(tags: Mapping[str, str]) -> str:
    return str(tags.get("name") or tags.get("name:en") or "").strip()[:120]


def parse_buildings(overpass_json: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Building ways and multipolygon relations as extrudable footprints.

    Each row is ``{"id": "way/123", "name", "kind", "height_m", "height_from",
    "levels", "ring": [[lon, lat], ...]}`` with the ring closed. ``height_from``
    says where the height came from: "height", "levels" or "default".
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for element in overpass_json.get("elements") or []:
        if not isinstance(element, Mapping):
            continue
        tags = element.get("tags")
        if not isinstance(tags, Mapping) or not tags.get("building"):
            continue
        kind = element.get("type")
        if kind == "way":
            ring = _ring(element.get("geometry"))
        elif kind == "relation":
            ring = _outer_ring(element.get("members"))
        else:
            continue
        osm_id = f"{kind}/{element.get('id')}"
        if ring is None or osm_id in seen:
            continue
        seen.add(osm_id)
        height, source = _height(tags)
        levels = _levels(tags.get("building:levels"))
        rows.append({
            "id": osm_id,
            "name": _name(tags),
            "kind": _kind(tags),
            "height_m": round(height, 2),
            "height_from": source,
            "levels": round(levels) if levels is not None else None,
            "ring": ring,
        })
    return rows


# ---------------------------------------------------------------- boxes


def lon_span(west: float, east: float) -> float:
    """Degrees of longitude from west to east, across the antimeridian if east < west."""
    return east - west if east >= west else east + 360.0 - west


def split_box(
    south: float, west: float, north: float, east: float
) -> list[tuple[float, float, float, float]]:
    """One box, or two when it crosses the antimeridian (east < west)."""
    if east >= west:
        return [(south, west, north, east)]
    return [(south, west, north, 180.0), (south, -180.0, north, east)]


def rounded_box(
    south: float, west: float, north: float, east: float
) -> tuple[float, float, float, float]:
    """The box grown outward to the 0.001 degree grid: the cache key."""

    def down(x: float) -> float:
        return round(math.floor(x / GRID_DEG + 1e-6) * GRID_DEG, 3)

    def up(x: float) -> float:
        return round(math.ceil(x / GRID_DEG - 1e-6) * GRID_DEG, 3)

    return (max(-90.0, down(south)), max(-180.0, down(west)),
            min(90.0, up(north)), min(180.0, up(east)))


def overpass_query(box: tuple[float, float, float, float]) -> str:
    bbox = "{:.3f},{:.3f},{:.3f},{:.3f}".format(*box)
    return (
        "[out:json][timeout:25];"
        f'(way["building"]({bbox});'
        f'relation["building"]["type"="multipolygon"]({bbox}););'
        f"out geom {MAX_BUILDINGS};"
    )


# ---------------------------------------------------------------- fetching


async def fetch_overpass(query: str) -> dict[str, Any] | None:
    """Overpass's JSON answer, or None on any failure (busy, 429, timeout, bad JSON)."""
    await hostthrottle.wait_turn(OVERPASS_HOST)
    try:
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # A query that ran out of time answers 200 with a partial list and a remark.
    if "error" in str(data.get("remark") or "").lower():
        return None
    return data


async def _box_buildings(
    box: tuple[float, float, float, float],
) -> tuple[list[dict[str, Any]], bool, bool]:
    """(rows, truncated, ok) for one rounded box, from the cache when fresh."""
    now = time.monotonic()
    hit = _cache.get(box)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1], hit[2], True
    data = await fetch_overpass(overpass_query(box))
    if data is None:
        return [], False, False
    rows = parse_buildings(data)
    truncated = len(data.get("elements") or []) >= MAX_BUILDINGS
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[box] = (now, rows, truncated)
    return rows, truncated, True


async def lookup(south: float, west: float, north: float, east: float) -> dict[str, Any]:
    """Buildings in a box already checked by the route (small, non-empty)."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    truncated = False
    failed = False
    for part in split_box(south, west, north, east):
        got, cut, ok = await _box_buildings(rounded_box(*part))
        failed = failed or not ok
        truncated = truncated or cut
        for row in got:
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
    out: dict[str, Any] = {
        "buildings": rows,
        "count": len(rows),
        "truncated": truncated,
        "attribution": ATTRIBUTION,
    }
    if failed:
        out["error"] = UNAVAILABLE
    return out
