"""Earth routes: NASA EONET natural events behind the globe's events layer.

One endpoint:

* GET /api/eonet   natural events (wildfires, storms, volcanoes, floods, ice, dust...)

Source: NASA Earth Observatory Natural Event Tracker, EONET v3
(``https://eonet.gsfc.nasa.gov/api/v3/events``), public and keyless. Each event
carries a list of dated geometries (a storm track, a fire's daily perimeter
centre, an iceberg's drift). The browser only needs one place per event, so the
server keeps the latest geometry: a Point as given, a Polygon reduced to the
centre of its outer ring. Everything else is trimmed to what the globe's detail
card shows: the categories, the date, the magnitude when there is one, and the
source links (http and https only).

The answer for each (days, status) pair is cached for thirty minutes, whatever
the outcome, the same arrangement as /api/aurora in sda.routes.space: a failed
or malformed fetch keeps the last good copy in service, flagged ``stale``, and
the endpoint answers 502 only while no good copy of that pair has ever arrived.

Fetching reuses ``_get_json`` from sda.routes.worldview. Coordinates are
geodetic degrees, longitudes returned in -180..180.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda.routes.worldview import _get_json

router = APIRouter(tags=["Earth"])

EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
EONET_TIMEOUT_S = 20.0
EONET_CACHE_TTL_S = 1800.0
EONET_SOURCE = "NASA EONET v3"
DEFAULT_DAYS = 30
DAYS_MIN = 1
DAYS_MAX = 365

Status = Literal["open", "closed", "all"]

#: At most this many (days, status) answers are held; the oldest attempt goes first.
_CACHE_MAX_KEYS = 16
#: A feed this long is already far past anything the globe can show usefully.
_MAX_EVENTS = 5000
_MAX_CATEGORIES = 8
_MAX_SOURCES = 8
_ID_MAX_CHARS = 64
_TITLE_MAX_CHARS = 200
_UNIT_MAX_CHARS = 32
_DATE_MAX_CHARS = 40
_URL_MAX_CHARS = 2048
_LON_RANGE = (-180.0, 180.0)
_LAT_RANGE = (-90.0, 90.0)
_EPOCH = datetime.min.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class EonetCategory(BaseModel):
    id: str = Field(description="EONET category id, e.g. wildfires, severeStorms")
    title: str


class EonetSource(BaseModel):
    id: str = Field(description="Reporting source, e.g. IRWIN, JTWC, NATICE")
    url: str = Field(description="Source record, http or https only")


class EonetEvent(BaseModel):
    id: str
    title: str
    categories: list[EonetCategory]
    point: tuple[float, float] = Field(
        description="[lon -180..180, lat -90..90] of the latest geometry"
    )
    geometry_type: str = Field(
        description="Point, or Polygon when the point is the centre of its outer ring"
    )
    date: str = Field(description="Date of the latest geometry, as published")
    magnitude_value: float | None = None
    magnitude_unit: str | None = None
    observations: int = Field(description="How many dated geometries the event carries")
    sources: list[EonetSource]
    closed: str | None = Field(default=None, description="Closing date, null while open")


class EonetResponse(BaseModel):
    events: list[EonetEvent]
    count: int
    days: int
    status: str
    source: str
    stale: bool = Field(
        description="True when the last fetch failed and this is the last good copy"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _text(value: object, limit: int) -> str:
    """A string trimmed and cut to *limit*, control characters dropped; '' otherwise."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()[:limit]


def _number(value: object, within: tuple[float, float] | None = None) -> float | None:
    """A finite JSON number (never a bool or a string), optionally inside *within*."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if within is not None and not within[0] <= number <= within[1]:
        return None
    return number


def _http_url(value: object) -> str | None:
    """*value* when it is an absolute http or https URL with a host, else None."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _URL_MAX_CHARS or any(ch <= " " or ch == "\x7f" for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url


def _position(item: object) -> tuple[float, float] | None:
    """[lon, lat, ...] as a checked (lon, lat) pair."""
    if not isinstance(item, list | tuple) or len(item) < 2:
        return None
    lon = _number(item[0], _LON_RANGE)
    lat = _number(item[1], _LAT_RANGE)
    if lon is None or lat is None:
        return None
    return lon, lat


def _ring_centre(ring: object) -> tuple[float, float] | None:
    """Mean of a ring's vertices, the closing repeat dropped.

    Longitudes are unwrapped against the first vertex before averaging, so a box
    across the antimeridian (179 E to 179 W) centres on 180 and not on 0.
    """
    if not isinstance(ring, list | tuple):
        return None
    points = [p for p in (_position(item) for item in ring) if p is not None]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if not points:
        return None
    base = points[0][0]
    lons = []
    for lon, _ in points:
        while lon - base > 180.0:
            lon -= 360.0
        while lon - base < -180.0:
            lon += 360.0
        lons.append(lon)
    lon = sum(lons) / len(lons)
    lon = ((lon + 180.0) % 360.0) - 180.0
    lat = sum(p[1] for p in points) / len(points)
    return round(lon, 4), round(lat, 4)


def _geometry_point(geom: dict) -> tuple[float, float] | None:
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if kind == "Point":
        return _position(coords)
    if kind == "Polygon" and isinstance(coords, list) and coords:
        return _ring_centre(coords[0])
    return None


def _parse_date(text: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Latest:
    point: tuple[float, float]
    geometry_type: str
    date: str
    magnitude_value: float | None
    magnitude_unit: str | None


def _latest_geometry(geometries: object) -> tuple[_Latest | None, int]:
    """The newest usable geometry and how many usable ones there were.

    Newest is by parsed date; an unreadable date sorts first, and ties go to the
    later entry in the list, which is the order EONET publishes them in.
    """
    if not isinstance(geometries, list):
        return None, 0
    best: tuple[datetime, int] | None = None
    latest: _Latest | None = None
    usable = 0
    for index, geom in enumerate(geometries):
        if not isinstance(geom, dict):
            continue
        point = _geometry_point(geom)
        if point is None:
            continue
        usable += 1
        date = _text(geom.get("date"), _DATE_MAX_CHARS)
        key = (_parse_date(date) or _EPOCH, index)
        if best is not None and key < best:
            continue
        best = key
        unit = _text(geom.get("magnitudeUnit"), _UNIT_MAX_CHARS)
        latest = _Latest(
            point=point,
            geometry_type=str(geom.get("type")),
            date=date,
            magnitude_value=_number(geom.get("magnitudeValue")),
            magnitude_unit=unit or None,
        )
    return latest, usable


def _categories(value: object) -> list[EonetCategory]:
    out: list[EonetCategory] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        cat_id = _text(item.get("id"), _ID_MAX_CHARS)
        if not cat_id:
            continue
        out.append(EonetCategory(id=cat_id, title=_text(item.get("title"), _TITLE_MAX_CHARS)
                                 or cat_id))
        if len(out) >= _MAX_CATEGORIES:
            break
    return out


def _sources(value: object) -> list[EonetSource]:
    out: list[EonetSource] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _http_url(item.get("url"))
        if url is None:
            continue
        out.append(EonetSource(id=_text(item.get("id"), _ID_MAX_CHARS) or "source", url=url))
        if len(out) >= _MAX_SOURCES:
            break
    return out


def _parse_event(item: object) -> EonetEvent | None:
    """One compact event, or None when it has no id or no usable geometry."""
    if not isinstance(item, dict):
        return None
    event_id = _text(item.get("id"), _ID_MAX_CHARS)
    if not event_id:
        return None
    latest, usable = _latest_geometry(item.get("geometry"))
    if latest is None:
        return None
    closed = _text(item.get("closed"), _DATE_MAX_CHARS)
    return EonetEvent(
        id=event_id,
        title=_text(item.get("title"), _TITLE_MAX_CHARS) or event_id,
        categories=_categories(item.get("categories")),
        point=latest.point,
        geometry_type=latest.geometry_type,
        date=latest.date,
        magnitude_value=latest.magnitude_value,
        magnitude_unit=latest.magnitude_unit,
        observations=usable,
        sources=_sources(item.get("sources")),
        closed=closed or None,
    )


def _parse_events(data: object) -> tuple[EonetEvent, ...] | None:
    """The feed as compact events, or None when it is not a usable EONET answer.

    An empty ``events`` list is a real answer (nothing open) and parses to ().
    A non-empty list in which not one event survives the checks is treated as
    malformed, so the last good copy stays in service instead.
    """
    if not isinstance(data, dict):
        return None
    items = data.get("events")
    if not isinstance(items, list):
        return None
    events: list[EonetEvent] = []
    seen: set[str] = set()
    for item in items[:_MAX_EVENTS]:
        event = _parse_event(item)
        if event is None or event.id in seen:
            continue
        seen.add(event.id)
        events.append(event)
    if items and not events:
        return None
    return tuple(events)


# --------------------------------------------------------------------------
# Fetch and cache
# --------------------------------------------------------------------------


@dataclass
class _Entry:
    attempted_at: float | None = None
    good: tuple[EonetEvent, ...] | None = None
    stale: bool = False
    lock: asyncio.Lock | None = None
    lock_loop: asyncio.AbstractEventLoop | None = None


@dataclass
class _EonetState:
    entries: dict[tuple[int, str], _Entry] = field(default_factory=dict)


_state = _EonetState()


def clear_eonet_cache() -> None:
    """Forget every cached answer and attempt. Used by the tests."""
    global _state
    _state = _EonetState()


def _entry(key: tuple[int, str]) -> _Entry:
    entry = _state.entries.get(key)
    if entry is None:
        if len(_state.entries) >= _CACHE_MAX_KEYS:
            oldest = min(
                _state.entries,
                key=lambda k: _state.entries[k].attempted_at or 0.0,
            )
            del _state.entries[oldest]
        entry = _state.entries[key] = _Entry()
    return entry


def _lock(entry: _Entry) -> asyncio.Lock:
    """One lock per entry and event loop, so a test client's fresh loop never inherits one."""
    loop = asyncio.get_running_loop()
    if entry.lock is None or entry.lock_loop is not loop:
        entry.lock = asyncio.Lock()
        entry.lock_loop = loop
    return entry.lock


def _due(entry: _Entry) -> bool:
    at = entry.attempted_at
    return at is None or time.monotonic() - at >= EONET_CACHE_TTL_S


def _events_url(days: int, status: str) -> str:
    return EONET_URL + "?" + urlencode({"status": status, "days": days})


async def _fetch_events(days: int, status: str) -> object:
    """The raw EONET JSON, or None on any failure. The only network call here."""
    return await _get_json(_events_url(days, status), timeout_s=EONET_TIMEOUT_S, purpose="eonet")


async def _current(days: int, status: str) -> tuple[tuple[EonetEvent, ...] | None, bool]:
    """The last good events for (days, status) and whether they are stale.

    The attempt time is stamped when the fetch ends, success or failure, so a
    failure waits out the same thirty minutes as a success. Requests for the
    same pair that arrive while its fetch is in flight wait for that one fetch.
    """
    entry = _entry((days, status))
    if _due(entry):
        async with _lock(entry):
            if _due(entry):
                try:
                    parsed = await asyncio.to_thread(
                        _parse_events, await _fetch_events(days, status)
                    )
                except Exception:
                    parsed = None
                finally:
                    entry.attempted_at = time.monotonic()
                if parsed is not None:
                    entry.good, entry.stale = parsed, False
                else:
                    entry.stale = True
    return entry.good, entry.stale


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------


@router.get(
    "/api/eonet",
    response_model=EonetResponse,
    summary="NASA EONET natural events, one point each (latest geometry)",
)
async def eonet(
    days: int = Query(
        DEFAULT_DAYS,
        ge=DAYS_MIN,
        le=DAYS_MAX,
        description="Events with a geometry dated within this many days",
    ),
    status: Annotated[Status, Query(description="open, closed or all")] = "open",
) -> EonetResponse:
    events, stale = await _current(days, status)
    if events is None:
        raise HTTPException(status_code=502, detail="NASA EONET events feed unavailable")
    return EonetResponse(
        events=list(events),
        count=len(events),
        days=days,
        status=status,
        source=EONET_SOURCE,
        stale=stale,
    )
