"""Weather routes behind the globe's weather layer: a global wind grid and tropical cyclones.

Endpoints:

* GET /api/weather/wind       10 m wind u/v on a 5-degree grid, for the current hour
* GET /api/weather/cyclones   active tropical cyclones: NOAA NHC / CPHC (Atlantic,
  East and Central Pacific, with forecast track, points and cone) and GDACS for
  the other basins

Precipitation radar has no route here. RainViewer's tile index and tiles send
``Access-Control-Allow-Origin: *``, so the browser reads them directly
(static/js/weather.js), which keeps radar working in the static demo build too.

Wind
----

Source: the Open-Meteo forecast API (``api.open-meteo.com/v1/forecast``), keyless,
free for non-commercial use under CC BY 4.0. One request takes comma-separated
latitude and longitude lists, up to 1000 places. The free tier allows 600 calls a
minute, 5000 an hour and 10,000 a day, and a request counts once per place (more
for over ten variables or over two weeks), so a grid of N points costs N calls
however it is batched.

The grid is 5 degrees, 72 longitudes from 180 W by 31 latitudes from 75 N to
75 S: 2232 points. Two hourly variables (speed and direction at 10 m) for the
past hour and the next 36 are fetched per point, which still counts once, so the
server can serve the current hour, interpolated in time, for twelve hours before
it asks again. A full build therefore costs 2232 calls, at most twice a day:
under half of the daily allowance even if the layer runs around the clock, and
only while someone has the wind layer on (builds start on request, never on a
timer).

To stay under 600 calls a minute a build runs in passes of at most 576 points,
at least 65 seconds apart, each pass split into HTTP requests of 144 points so
no URL grows long. The first pass is the 10-degree subgrid (every other row and
column), so the first request already gets a whole-globe field: the other points
are filled from their coarse neighbours and the answer says ``complete: false``.
Each later request that finds a pass due runs the next one; the browser polls
about once a minute until ``complete`` is true, which takes four passes. A point
that comes back unusable twice is dropped and filled from its neighbours.

While a rebuild runs, the last complete grid stays in service. A pass that gets
nothing back sets ``stale`` and the next attempt waits five minutes. The
endpoint answers 502 only while no usable grid has ever been built.

Cyclones
--------

NHC: ``www.nhc.noaa.gov/CurrentStorms.json`` lists the active storms with the
latest advisory (intensity in knots, pressure in millibars, motion speed in
miles per hour, which is converted to knots here). When there is at least one
storm, the forecast points, track and cone come from NOAA's tropical weather
summary MapServer (layers 5, 6 and 7, queried as GeoJSON) and are matched to
their storm by the ``idp_source`` id.

GDACS: ``www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC``
covers every basin (JTWC, RSMC). Only events GDACS marks current are kept, and
one that duplicates an NHC storm (same name, or within 500 km) is dropped. GDACS
publishes the event's peak wind, not the current one, and says so in
``wind_basis``.

Both are cached for fifteen minutes; after a failure the next attempt comes
five minutes later. Each source keeps its own last good copy, and the endpoint
answers 502 only while neither has ever answered.

Parsing rules for NHC (the id pattern, the mph to knot conversion, the three
MapServer layers and the ``idp_source`` match) follow gods-eye-view
``server/providers/cyclones.js`` (https://github.com/bilawalsidhu/gods-eye-view,
MIT, Copyright (c) 2026 Bilawal Sidhu, commit 9423a26487de), rewritten in
Python. Coordinates are geodetic degrees, longitudes returned in -180..180.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sda.routes.worldview import _get_json

router = APIRouter(tags=["WorldView"])

# --------------------------------------------------------------------------
# Wind grid constants
# --------------------------------------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WIND_SOURCE = "Open-Meteo forecast API (best match models), 10 m wind, CC BY 4.0"
WIND_STEP_DEG = 5
WIND_LAT_MAX = 75
WIND_NX = 360 // WIND_STEP_DEG  # 72 columns, 180 W first
WIND_NY = 2 * WIND_LAT_MAX // WIND_STEP_DEG + 1  # 31 rows, 75 N first
WIND_POINTS = WIND_NX * WIND_NY  # 2232
WIND_TTL_S = 12 * 3600.0
WIND_PASS_POINTS = 576  # Open-Meteo calls per pass; the free tier allows 600 a minute
WIND_PASS_INTERVAL_S = 65.0
WIND_RETRY_S = 300.0
WIND_REQUEST_POINTS = 144  # places per HTTP request, which keeps each URL near 1.5 kB
WIND_TIMEOUT_S = 30.0
WIND_PAST_HOURS = 1
WIND_FORECAST_HOURS = 36
WIND_MAX_MISSES = 2

#: Open-Meteo snaps a place to its model cell; a block further than this from
#: the point asked for is not that point's answer.
_SNAP_TOL_DEG = 2.0
_MAX_HOURS = 240
_HOUR_S = 3600
_SPEED_RANGE = (0.0, 150.0)  # m/s; the strongest 10 m gusts on record are about 113
_DIR_RANGE = (0.0, 360.0)
_REAL = (int, float)

# --------------------------------------------------------------------------
# Cyclone constants
# --------------------------------------------------------------------------

NHC_STATUS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
NHC_GIS_URL = (
    "https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/"
    "NHC_tropical_weather_summary/MapServer"
)
#: MapServer layer ids: 5 Forecast Points, 6 Forecast Track, 7 Forecast Cone.
NHC_LAYERS: dict[str, int] = {"pts": 5, "lin": 6, "pgn": 7}
GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC"
CYCLONE_TTL_S = 900.0
CYCLONE_RETRY_S = 300.0
CYCLONE_TIMEOUT_S = 20.0
CYCLONE_SOURCE = "NOAA NHC / CPHC advisories; GDACS (UN, European Commission)"
CYCLONE_COVERAGE = (
    "NHC: Atlantic, East and Central Pacific with forecast track and cone. "
    "GDACS: every basin, position and event peak wind only."
)
#: A GDACS event this close to an NHC storm is the same storm.
DEDUPE_KM = 500.0
#: GDACS sometimes leaves an event marked current; one not updated for this long is not.
GDACS_MAX_AGE = timedelta(days=10)

_MAX_STORMS = 64
_MAX_PARTS = 16
_MAX_PART_POINTS = 2000
_MAX_FORECAST_POINTS = 32
_NAME_MAX_CHARS = 40
_TEXT_MAX_CHARS = 80
_URL_MAX_CHARS = 512
_KMH_PER_KT = 1.852
_KT_PER_MPH = 0.868976
_EARTH_RADIUS_KM = 6371.0
_NHC_ID = re.compile(r"^(?:al|ep|cp)\d{6}$")
_ADVISORY = re.compile(r"^\d{1,3}[A-Za-z]?$")
_IDP_SOURCE = re.compile(r"^((?:al|ep|cp)\d{6})-(\d{1,3}[a-z]?)_5day_(pts|lin|pgn)$", re.I)
_ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?Z?$")
_GDACS_SUFFIX = re.compile(r"-\d{2}$")

#: NHC classification codes and how the panel names them.
_NHC_CLASSES: dict[str, str] = {
    "HU": "Hurricane",
    "TY": "Typhoon",
    "TS": "Tropical storm",
    "TD": "Tropical depression",
    "STS": "Subtropical storm",
    "STD": "Subtropical depression",
    "PTC": "Potential tropical cyclone",
    "PC": "Post-tropical cyclone",
    "TC": "Tropical cyclone",
    "LO": "Remnant low",
    "DB": "Disturbance",
    "EX": "Extratropical cyclone",
}

Intensity = Literal["TD", "TS", "C1", "C2", "C3", "C4", "C5", "UNK"]


# --------------------------------------------------------------------------
# Shared value checks
# --------------------------------------------------------------------------


def _number(value: object, within: tuple[float, float] | None = None) -> float | None:
    """A finite number, or a numeric string, optionally inside *within*; never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if within is not None and not within[0] <= number <= within[1]:
        return None
    return number


def _text(value: object, limit: int = _TEXT_MAX_CHARS) -> str:
    """A string with control characters and angle brackets dropped, trimmed and cut."""
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if ch >= " " and ch not in "\x7f<>")
    return kept.strip()[:limit]


def _https_url(value: object, hosts: tuple[str, ...]) -> str | None:
    """*value* when it is an https URL on one of *hosts* with no credentials."""
    if not isinstance(value, str) or len(value) > _URL_MAX_CHARS:
        return None
    url = value.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname not in hosts:
        return None
    if parts.username or parts.password:
        return None
    return url


def _iso(value: object) -> str | None:
    """An ISO 8601 UTC time as published by NHC or GDACS, normalised; None otherwise."""
    if not isinstance(value, str) or not _ISO_Z.match(value.strip()):
        return None
    text = value.strip().rstrip("Z")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _wrap_lon(lon: float) -> float:
    wrapped = ((lon + 180.0) % 360.0) - 180.0
    return 180.0 if wrapped == -180.0 and lon > 0 else wrapped


def _lon_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def _utc_iso(unix_s: float) -> str:
    return datetime.fromtimestamp(unix_s, tz=UTC).isoformat().replace("+00:00", "Z")


def _wall_time() -> float:
    """Unix seconds now. Tests replace this to pin the hour the field is served for."""
    return time.time()


# --------------------------------------------------------------------------
# Wind: response model
# --------------------------------------------------------------------------


class WindResponse(BaseModel):
    nx: int = Field(description="Longitudes in the grid")
    ny: int = Field(description="Latitudes in the grid")
    lo1: float = Field(description="Longitude of the first column, degrees east")
    la1: float = Field(description="Latitude of the first row (the northernmost), degrees")
    dx: float = Field(description="Column spacing, degrees; columns wrap at 180")
    dy: float = Field(description="Row spacing, degrees, running south")
    u: list[float] = Field(description="Eastward wind, m/s, row by row from la1, west to east")
    v: list[float] = Field(description="Northward wind, m/s, same order as u")
    units: str
    valid_time: str = Field(description="UTC time the field is interpolated to")
    fetched_at: str | None = Field(description="When the grid served here finished building")
    complete: bool = Field(
        description="False while only the 10-degree first pass is in: other points are "
        "filled from their neighbours until the build finishes"
    )
    resolution_deg: int = Field(description="Spacing of the points actually fetched")
    filled: int = Field(description="Grid points with fetched data")
    total: int
    refreshing: bool = Field(description="A newer grid is being built in passes")
    max_speed: float = Field(description="Strongest wind in the field, m/s")
    source: str
    stale: bool = Field(
        description="True when the last fetch failed, or the forecast no longer reaches now"
    )


# --------------------------------------------------------------------------
# Wind: grid layout and parsing
# --------------------------------------------------------------------------


def _node_point(index: int) -> tuple[float, float]:
    """(lat, lon) of grid point *index*, row-major from 75 N and 180 W."""
    row, col = divmod(index, WIND_NX)
    return float(WIND_LAT_MAX - row * WIND_STEP_DEG), float(-180 + col * WIND_STEP_DEG)


def _coarse(index: int) -> bool:
    row, col = divmod(index, WIND_NX)
    return row % 2 == 0 and col % 2 == 0


#: Fetch order: the 10-degree subgrid first, then the rest row by row.
_ORDER: tuple[int, ...] = tuple(i for i in range(WIND_POINTS) if _coarse(i)) + tuple(
    i for i in range(WIND_POINTS) if not _coarse(i)
)
_COARSE: frozenset[int] = frozenset(i for i in range(WIND_POINTS) if _coarse(i))


@dataclass(frozen=True)
class _Node:
    """One point's hourly series: u and v in m/s from unix time t0, one per hour."""

    t0: int
    u: tuple[float, ...]
    v: tuple[float, ...]


def _uv(speed: float, direction_deg: float) -> tuple[float, float]:
    """Speed and the direction the wind blows from (meteorological) as eastward, northward."""
    rad = math.radians(direction_deg)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def _parse_block(block: object, lat: float, lon: float) -> _Node | None:
    """One place's Open-Meteo answer as a series, or None when it is not usable.

    The block must sit within two degrees of the point asked for, its hourly
    times must be whole unix seconds one hour apart, and the two value lists
    must match the times in length. A missing or out-of-range value becomes
    NaN for that hour; a series with no usable hour at all is refused.
    """
    if not isinstance(block, dict):
        return None
    blat = _number(block.get("latitude"), (-90.0, 90.0))
    blon = _number(block.get("longitude"), (-180.0, 180.0))
    if blat is None or blon is None:
        return None
    if abs(blat - lat) > _SNAP_TOL_DEG or _lon_gap(blon, lon) > _SNAP_TOL_DEG:
        return None
    hourly = block.get("hourly")
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time")
    speeds = hourly.get("wind_speed_10m")
    dirs = hourly.get("wind_direction_10m")
    if not isinstance(times, list) or not isinstance(speeds, list) or not isinstance(dirs, list):
        return None
    count = len(times)
    if count == 0 or count > _MAX_HOURS or len(speeds) != count or len(dirs) != count:
        return None
    # Exact type checks (a bool is not a number here, nor is a numeric string),
    # and chained comparisons, which are False for NaN and out-of-range values.
    # This loop runs for every hour of every place, so it avoids _number().
    first = times[0]
    if type(first) not in _REAL or not math.isfinite(first) or first != int(first):
        return None
    for k, stamp in enumerate(times):
        if type(stamp) not in _REAL or stamp != first + k * _HOUR_S:
            return None
    lo_s, hi_s = _SPEED_RANGE
    lo_d, hi_d = _DIR_RANGE
    us: list[float] = []
    vs: list[float] = []
    usable = False
    for speed, direction in zip(speeds, dirs, strict=True):
        if (
            type(speed) not in _REAL
            or type(direction) not in _REAL
            or not lo_s <= speed <= hi_s
            or not lo_d <= direction <= hi_d
        ):
            us.append(math.nan)
            vs.append(math.nan)
            continue
        east, north = _uv(speed, direction)
        us.append(east)
        vs.append(north)
        usable = True
    if not usable:
        return None
    return _Node(t0=int(first), u=tuple(us), v=tuple(vs))


def _coord(value: float) -> str:
    return f"{value:g}"


async def _fetch_open_meteo(points: list[tuple[float, float]]) -> object:
    """Open-Meteo's answer for *points* [(lat, lon)], or None. The only wind network call."""
    query = urlencode(
        {
            "latitude": ",".join(_coord(lat) for lat, _ in points),
            "longitude": ",".join(_coord(lon) for _, lon in points),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms",
            "timeformat": "unixtime",
            "timezone": "GMT",
            "past_hours": WIND_PAST_HOURS,
            "forecast_hours": WIND_FORECAST_HOURS,
            "cell_selection": "nearest",
        },
        safe=",",
    )
    return await _get_json(f"{OPEN_METEO_URL}?{query}", timeout_s=WIND_TIMEOUT_S, purpose="wind")


async def _fetch_pass(indices: list[int]) -> tuple[dict[int, _Node], set[int]]:
    """Fetch *indices* in requests of WIND_REQUEST_POINTS.

    Returns the points that parsed, and every point whose request was answered
    with one block per place (so a point in there but not in the first map came
    back unusable, as opposed to its request failing outright).
    """
    chunks = [
        indices[i : i + WIND_REQUEST_POINTS] for i in range(0, len(indices), WIND_REQUEST_POINTS)
    ]
    answers = await asyncio.gather(
        *(_fetch_open_meteo([_node_point(i) for i in chunk]) for chunk in chunks),
        return_exceptions=True,
    )
    got: dict[int, _Node] = {}
    answered: set[int] = set()
    for chunk, answer in zip(chunks, answers, strict=True):
        if isinstance(answer, BaseException) or answer is None:
            continue
        blocks = answer if isinstance(answer, list) else [answer]
        if len(blocks) != len(chunk):
            continue
        answered.update(chunk)
        for index, block in zip(chunk, blocks, strict=True):
            lat, lon = _node_point(index)
            node = _parse_block(block, lat, lon)
            if node is not None:
                got[index] = node
    return got, answered


# --------------------------------------------------------------------------
# Wind: the field at one time
# --------------------------------------------------------------------------


def _mix(a: float, b: float, frac: float) -> float:
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a + (b - a) * frac


def _node_at(node: _Node | None, t: float) -> tuple[float, float]:
    """(u, v) of *node* at unix time *t*, linear in time, clamped to the series."""
    if node is None:
        return math.nan, math.nan
    last = len(node.u) - 1
    k = min(max((t - node.t0) / _HOUR_S, 0.0), float(last))
    i0 = int(math.floor(k))
    i1 = min(i0 + 1, last)
    frac = k - i0
    return _mix(node.u[i0], node.u[i1], frac), _mix(node.v[i0], node.v[i1], frac)


def _neighbours(index: int, diagonal: bool) -> list[int]:
    row, col = divmod(index, WIND_NX)
    steps = (
        ((-1, -1), (-1, 1), (1, -1), (1, 1)) if diagonal else ((-1, 0), (1, 0), (0, -1), (0, 1))
    )
    out = []
    for dr, dc in steps:
        r = row + dr
        if 0 <= r < WIND_NY:
            out.append(r * WIND_NX + (col + dc) % WIND_NX)
    return out


def _fill(us: list[float], vs: list[float]) -> None:
    """Fill NaN points in place from their neighbours, sweep by sweep.

    A point takes the mean of its valid side neighbours, or when it has none,
    of its valid diagonal ones. Each sweep reads the previous sweep's values,
    so a point between two coarse points gets their midpoint and a point at the
    centre of four gets their mean: bilinear on the 10-degree subgrid. Anything
    still empty after a few sweeps is calm.
    """
    for _ in range(4):
        holes = [i for i in range(WIND_POINTS) if math.isnan(us[i])]
        if not holes:
            return
        fresh: dict[int, tuple[float, float]] = {}
        for i in holes:
            for diagonal in (False, True):
                near = [n for n in _neighbours(i, diagonal) if not math.isnan(us[n])]
                if near:
                    fresh[i] = (
                        sum(us[n] for n in near) / len(near),
                        sum(vs[n] for n in near) / len(near),
                    )
                    break
        if not fresh:
            break
        for i, (u, v) in fresh.items():
            us[i], vs[i] = u, v
    for i in range(WIND_POINTS):
        if math.isnan(us[i]) or math.isnan(vs[i]):
            us[i], vs[i] = 0.0, 0.0


def _field(nodes: list[_Node | None], t: float) -> tuple[list[float], list[float], bool]:
    """u, v for every grid point at *t*, holes filled, and whether *t* is past the forecast."""
    us: list[float] = []
    vs: list[float] = []
    ends: list[int] = []
    for node in nodes:
        u, v = _node_at(node, t)
        if math.isnan(u) or math.isnan(v):
            u = v = math.nan
        us.append(u)
        vs.append(v)
        if node is not None:
            ends.append(node.t0 + (len(node.u) - 1) * _HOUR_S)
    _fill(us, vs)
    beyond = bool(ends) and t > min(ends) + _HOUR_S
    return us, vs, beyond


# --------------------------------------------------------------------------
# Wind: build state
# --------------------------------------------------------------------------


@dataclass
class _WindGrid:
    nodes: tuple[_Node | None, ...]
    built_at: float  # monotonic
    fetched_at: str


@dataclass
class _WindBuild:
    nodes: dict[int, _Node] = field(default_factory=dict)
    pending: list[int] = field(default_factory=lambda: list(_ORDER))
    misses: dict[int, int] = field(default_factory=dict)


@dataclass
class _WindState:
    good: _WindGrid | None = None
    build: _WindBuild | None = None
    pass_at: float | None = None
    pass_failed: bool = False
    stale: bool = False
    passes: int = 0
    lock: asyncio.Lock | None = None
    lock_loop: asyncio.AbstractEventLoop | None = None


_wind = _WindState()


def clear_wind_cache() -> None:
    """Forget the grid, any build in progress and the pacing. Used by the tests."""
    global _wind
    _wind = _WindState()


def _wind_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _wind.lock is None or _wind.lock_loop is not loop:
        _wind.lock = asyncio.Lock()
        _wind.lock_loop = loop
    return _wind.lock


def _wind_pass_due(now: float) -> bool:
    if _wind.pass_at is None:
        return True
    wait = WIND_RETRY_S if _wind.pass_failed else WIND_PASS_INTERVAL_S
    return now - _wind.pass_at >= wait


async def _advance_wind() -> None:
    """Run the next pass of the build when one is wanted and due. Call under the lock."""
    now = time.monotonic()
    if _wind.build is None:
        if _wind.good is not None and now - _wind.good.built_at < WIND_TTL_S:
            return
        _wind.build = _WindBuild()
    if not _wind_pass_due(now):
        return
    build = _wind.build
    take = build.pending[:WIND_PASS_POINTS]
    try:
        got, answered = await _fetch_pass(take)
    except Exception:
        got, answered = {}, set()
    finally:
        _wind.pass_at = time.monotonic()
        _wind.passes += 1
    _wind.pass_failed = not got
    _wind.stale = not got
    if not got:
        return
    build.nodes.update(got)
    dropped: set[int] = set()
    for index in answered - got.keys():
        build.misses[index] = build.misses.get(index, 0) + 1
        if build.misses[index] >= WIND_MAX_MISSES:
            dropped.add(index)
    build.pending = [i for i in build.pending if i not in got and i not in dropped]
    if not build.pending:
        _wind.good = _WindGrid(
            nodes=tuple(build.nodes.get(i) for i in range(WIND_POINTS)),
            built_at=time.monotonic(),
            fetched_at=_utc_iso(_wall_time()),
        )
        _wind.build = None


def _coarse_ready(build: _WindBuild) -> bool:
    """Every 10-degree point has been answered, usable or not (unusable ones are filled)."""
    return all(i in build.nodes or i in build.misses for i in _COARSE)


def _wind_snapshot() -> WindResponse | None:
    """The field to serve now: the last complete grid, else a provisional one, else None."""
    t = _wall_time()
    if _wind.good is not None:
        nodes = list(_wind.good.nodes)
        complete, resolution, fetched_at = True, WIND_STEP_DEG, _wind.good.fetched_at
    elif _wind.build is not None and _coarse_ready(_wind.build):
        nodes = [_wind.build.nodes.get(i) for i in range(WIND_POINTS)]
        complete, resolution, fetched_at = False, 2 * WIND_STEP_DEG, None
    else:
        return None
    us, vs, beyond = _field(nodes, t)
    top = max((math.hypot(u, v) for u, v in zip(us, vs, strict=True)), default=0.0)
    return WindResponse(
        nx=WIND_NX,
        ny=WIND_NY,
        lo1=-180.0,
        la1=float(WIND_LAT_MAX),
        dx=float(WIND_STEP_DEG),
        dy=float(WIND_STEP_DEG),
        u=[round(u, 2) for u in us],
        v=[round(v, 2) for v in vs],
        units="m/s",
        valid_time=_utc_iso(t),
        fetched_at=fetched_at,
        complete=complete,
        resolution_deg=resolution,
        filled=sum(1 for node in nodes if node is not None),
        total=WIND_POINTS,
        refreshing=_wind.good is not None and _wind.build is not None,
        max_speed=round(top, 2),
        source=WIND_SOURCE,
        stale=_wind.stale or beyond,
    )


@router.get(
    "/api/weather/wind",
    response_model=WindResponse,
    summary="Global 10 m wind u/v on a 5-degree grid for the current hour (Open-Meteo)",
)
async def weather_wind() -> WindResponse:
    async with _wind_lock():
        await _advance_wind()
        snapshot = _wind_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=502, detail="Open-Meteo wind grid unavailable")
    return snapshot


# --------------------------------------------------------------------------
# Cyclones: response models
# --------------------------------------------------------------------------

LonLat = tuple[float, float]


class ForecastPoint(BaseModel):
    lon: float
    lat: float
    tau_h: float | None = Field(description="Hours after the advisory")
    wind_kt: float | None


class Storm(BaseModel):
    id: str
    name: str
    source: str = Field(description="NHC or GDACS")
    agency: str = Field(description="Issuing centre: NHC, CPHC, JTWC, an RSMC...")
    basin: str | None
    classification: str | None = Field(description="NHC code (HU, TS, TD, PTC...) when known")
    category: str = Field(description="Readable class, e.g. 'Hurricane (category 3)'")
    intensity: Intensity = Field(
        description="Saffir-Simpson class from the wind: TD, TS, C1..C5, or UNK"
    )
    wind_kt: float | None
    wind_basis: str = Field(description="'current advisory' (NHC) or 'event peak' (GDACS)")
    pressure_hpa: float | None
    lon: float
    lat: float
    movement_dir_deg: float | None
    movement_speed_kt: float | None
    advisory: str | None
    issued_at: str | None
    updated_at: str | None
    url: str | None = Field(description="Official advisory or report page, https only")
    alert_level: str | None = Field(description="GDACS Green, Orange or Red")
    country: str | None
    track: list[list[LonLat]] = Field(description="Forecast track lines, [lon, lat] points")
    cone: list[list[LonLat]] = Field(description="Forecast cone outer rings, [lon, lat] points")
    forecast_points: list[ForecastPoint]
    geometry_advisory: str | None = Field(description="Advisory the track and cone come from")
    geometry_status: str = Field(description="current, older advisory, unavailable or none")


class CycloneResponse(BaseModel):
    storms: list[Storm]
    count: int
    sources: dict[str, str] = Field(description="Per source: ok, stale or unavailable")
    coverage: str
    source: str
    stale: bool = Field(
        description="True when a source is serving its last good copy or has not answered yet"
    )


# --------------------------------------------------------------------------
# Cyclones: parsing
# --------------------------------------------------------------------------


def intensity_for(wind_kt: float | None) -> Intensity:
    """Saffir-Simpson class for one-minute sustained wind in knots."""
    if wind_kt is None:
        return "UNK"
    if wind_kt < 34:
        return "TD"
    if wind_kt < 64:
        return "TS"
    if wind_kt < 83:
        return "C1"
    if wind_kt < 96:
        return "C2"
    if wind_kt < 113:
        return "C3"
    if wind_kt < 137:
        return "C4"
    return "C5"


def _category_label(code: str | None, intensity: Intensity) -> str:
    if code in ("HU", "TY") and intensity.startswith("C"):
        return f"{_NHC_CLASSES[code]} (category {intensity[1]})"
    if code in _NHC_CLASSES:
        return _NHC_CLASSES[code]
    if intensity.startswith("C"):
        return f"Hurricane/typhoon strength (category {intensity[1]})"
    if intensity == "TS":
        return "Tropical storm"
    if intensity == "TD":
        return "Tropical depression"
    return code or "Tropical cyclone"


def _advisory(value: object) -> str | None:
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not _ADVISORY.match(text):
        return None
    return text.lstrip("0").upper() or "0"


def _position(lon: object, lat: object) -> LonLat | None:
    x = _number(lon, (-180.0, 180.0))
    y = _number(lat, (-90.0, 90.0))
    if x is None or y is None:
        return None
    return (x, y)


def _parse_nhc_storm(raw: object) -> Storm | None:
    if not isinstance(raw, dict):
        return None
    storm_id = raw.get("id")
    if not isinstance(storm_id, str) or not _NHC_ID.match(storm_id):
        return None
    name = _text(raw.get("name"), _NAME_MAX_CHARS)
    where = _position(raw.get("longitudeNumeric"), raw.get("latitudeNumeric"))
    if not name or where is None:
        return None
    code = _text(raw.get("classification"), 4).upper() or None
    wind = _number(raw.get("intensity"), (0.0, 250.0))
    level = intensity_for(wind)
    mph = _number(raw.get("movementSpeed"), (0.0, 200.0))
    forecast = raw.get("forecastAdvisory")
    forecast = forecast if isinstance(forecast, dict) else {}
    public = raw.get("publicAdvisory")
    public = public if isinstance(public, dict) else {}
    url = _https_url(public.get("url"), ("www.nhc.noaa.gov",)) or _https_url(
        forecast.get("url"), ("www.nhc.noaa.gov",)
    )
    return Storm(
        id=storm_id,
        name=name,
        source="NHC",
        agency="CPHC" if storm_id.startswith("cp") else "NHC",
        basin=storm_id[:2].upper(),
        classification=code,
        category=_category_label(code, level),
        intensity=level,
        wind_kt=wind,
        wind_basis="current advisory",
        pressure_hpa=_number(raw.get("pressure"), (850.0, 1100.0)),
        lon=where[0],
        lat=where[1],
        movement_dir_deg=_number(raw.get("movementDir"), (0.0, 360.0)),
        movement_speed_kt=None if mph is None else round(mph * _KT_PER_MPH),
        advisory=_advisory(forecast.get("advNum")),
        issued_at=_iso(forecast.get("issuance")),
        updated_at=_iso(raw.get("lastUpdate")),
        url=url,
        alert_level=None,
        country=None,
        track=[],
        cone=[],
        forecast_points=[],
        geometry_advisory=None,
        geometry_status="none",
    )


def _parse_nhc(data: object) -> list[Storm] | None:
    """The NHC storm list, or None when the answer is not a CurrentStorms document.

    An empty ``activeStorms`` list is a real answer (a quiet ocean). A storm
    that fails a check is skipped rather than costing the rest of the list.
    """
    if not isinstance(data, dict):
        return None
    items = data.get("activeStorms")
    if not isinstance(items, list):
        return None
    storms: list[Storm] = []
    seen: set[str] = set()
    for item in items[:_MAX_STORMS]:
        storm = _parse_nhc_storm(item)
        if storm is None or storm.id in seen:
            continue
        seen.add(storm.id)
        storms.append(storm)
    return storms


def _line(coords: object, ring: bool) -> list[LonLat] | None:
    if not isinstance(coords, list) or not (4 if ring else 2) <= len(coords) <= _MAX_PART_POINTS:
        return None
    out: list[LonLat] = []
    for pair in coords:
        if not isinstance(pair, list | tuple) or len(pair) < 2:
            return None
        where = _position(pair[0], pair[1])
        if where is None:
            return None
        out.append(where)
    return out


def _parts(geometry: object, kind: str) -> list[list[LonLat]]:
    """Track lines (kind 'lin') or cone outer rings (kind 'pgn') from a GeoJSON geometry."""
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "lin":
        lines = [coords] if gtype == "LineString" else coords if gtype == "MultiLineString" else []
        ring = False
    else:
        polys = [coords] if gtype == "Polygon" else coords if gtype == "MultiPolygon" else []
        if not isinstance(polys, list):
            return []
        lines = [p[0] for p in polys if isinstance(p, list) and p]
        ring = True
    if not isinstance(lines, list):
        return []
    out = []
    for part in lines[:_MAX_PARTS]:
        parsed = _line(part, ring)
        if parsed is not None:
            out.append(parsed)
    return out


@dataclass
class _Geometry:
    advisory: str | None = None
    track: list[list[LonLat]] = field(default_factory=list)
    cone: list[list[LonLat]] = field(default_factory=list)
    points: list[ForecastPoint] = field(default_factory=list)


def _parse_gis(collections: dict[str, object]) -> dict[str, _Geometry] | None:
    """Forecast points, track and cone per storm id, or None when a layer is unusable."""
    found: dict[str, _Geometry] = {}
    for kind, payload in collections.items():
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            return None
        features = payload.get("features")
        if not isinstance(features, list):
            return None
        for feature in features[: _MAX_STORMS * _MAX_FORECAST_POINTS]:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties")
            props = props if isinstance(props, dict) else {}
            source = props.get("idp_source")
            match = _IDP_SOURCE.match(source) if isinstance(source, str) else None
            if match is None or match.group(3).lower() != kind:
                continue
            geo = found.setdefault(match.group(1).lower(), _Geometry())
            geo.advisory = _advisory(match.group(2)) or geo.advisory
            if kind == "pts":
                point = feature.get("geometry")
                coords = point.get("coordinates") if isinstance(point, dict) else None
                where = (
                    _position(coords[0], coords[1])
                    if isinstance(coords, list) and len(coords) >= 2
                    else None
                )
                if where is not None and len(geo.points) < _MAX_FORECAST_POINTS:
                    geo.points.append(
                        ForecastPoint(
                            lon=where[0],
                            lat=where[1],
                            tau_h=_number(props.get("tau"), (0.0, 240.0)),
                            wind_kt=_number(props.get("maxwind"), (0.0, 250.0)),
                        )
                    )
            elif kind == "lin":
                geo.track.extend(_parts(feature.get("geometry"), "lin"))
            else:
                geo.cone.extend(_parts(feature.get("geometry"), "pgn"))
    for geo in found.values():
        geo.points.sort(key=lambda p: -1.0 if p.tau_h is None else p.tau_h)
    return found


def _attach(storms: list[Storm], gis: dict[str, _Geometry] | None) -> list[Storm]:
    out = []
    for storm in storms:
        if gis is None:
            out.append(storm.model_copy(update={"geometry_status": "unavailable"}))
            continue
        geo = gis.get(storm.id)
        if geo is None or not (geo.track or geo.cone or geo.points):
            out.append(storm.model_copy(update={"geometry_status": "none"}))
            continue
        current = geo.advisory is None or geo.advisory == storm.advisory
        out.append(
            storm.model_copy(
                update={
                    "track": geo.track,
                    "cone": geo.cone,
                    "forecast_points": geo.points,
                    "geometry_advisory": geo.advisory,
                    "geometry_status": "current" if current else "older advisory",
                }
            )
        )
    return out


def _gdacs_current(props: dict, now: datetime) -> bool:
    flag = props.get("iscurrent")
    if flag is not True and not (isinstance(flag, str) and flag.strip().lower() == "true"):
        return False
    stamp = _iso(props.get("datemodified")) or _iso(props.get("todate"))
    if stamp is None:
        return True
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return now - when <= GDACS_MAX_AGE


def _parse_gdacs(data: object, now: datetime) -> list[Storm] | None:
    """Current GDACS tropical cyclone events, or None when the answer is not a feed."""
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        return None
    features = data.get("features")
    if not isinstance(features, list):
        return None
    storms: list[Storm] = []
    seen: set[str] = set()
    for feature in features[: _MAX_STORMS * 8]:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            continue
        if props.get("eventtype") != "TC" or not _gdacs_current(props, now):
            continue
        event_id = _number(props.get("eventid"), (1.0, 1e12))
        coords = geometry.get("coordinates")
        where = (
            _position(coords[0], coords[1])
            if geometry.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2
            else None
        )
        name = _text(props.get("eventname"), _NAME_MAX_CHARS) or _text(
            props.get("name"), _NAME_MAX_CHARS
        )
        if event_id is None or where is None or not name:
            continue
        storm_id = f"gdacs-{int(event_id)}"
        if storm_id in seen:
            continue
        seen.add(storm_id)
        severity = props.get("severitydata")
        severity = severity if isinstance(severity, dict) else {}
        unit = _text(severity.get("severityunit"), 16).lower()
        speed = _number(severity.get("severity"), (0.0, 500.0))
        wind = None
        if speed is not None and unit in ("km/h", "kmh", "km/hr"):
            wind = float(round(speed / _KMH_PER_KT))
        elif speed is not None and unit in ("kt", "kts", "knots"):
            wind = float(round(speed))
        level = intensity_for(wind)
        url_block = props.get("url")
        url_block = url_block if isinstance(url_block, dict) else {}
        alert = _text(props.get("alertlevel"), 8).capitalize()
        storms.append(
            Storm(
                id=storm_id,
                name=name,
                source="GDACS",
                agency=_text(props.get("source"), 16) or "GDACS",
                basin=None,
                classification=None,
                category=_category_label(None, level),
                intensity=level,
                wind_kt=wind,
                wind_basis="event peak",
                pressure_hpa=None,
                lon=where[0],
                lat=where[1],
                movement_dir_deg=None,
                movement_speed_kt=None,
                advisory=None,
                issued_at=_iso(props.get("fromdate")),
                updated_at=_iso(props.get("datemodified")) or _iso(props.get("todate")),
                url=_https_url(url_block.get("report"), ("www.gdacs.org", "gdacs.org")),
                alert_level=alert if alert in ("Green", "Orange", "Red") else None,
                country=_text(props.get("country"), _TEXT_MAX_CHARS) or None,
                track=[],
                cone=[],
                forecast_points=[],
                geometry_advisory=None,
                geometry_status="none",
            )
        )
    return storms


def _distance_km(a: Storm, b: Storm) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(_wrap_lon(b.lon - a.lon))
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _base_name(name: str) -> str:
    return _GDACS_SUFFIX.sub("", name.strip()).upper()


def _merge(nhc: list[Storm], gdacs: list[Storm]) -> list[Storm]:
    """NHC storms, then the GDACS events that are not one of them."""
    names = {_base_name(s.name) for s in nhc}
    extra = [
        g
        for g in gdacs
        if _base_name(g.name) not in names and all(_distance_km(g, s) > DEDUPE_KM for s in nhc)
    ]
    return nhc + extra


# --------------------------------------------------------------------------
# Cyclones: fetch and cache
# --------------------------------------------------------------------------


async def _fetch_nhc_status() -> object:
    return await _get_json(NHC_STATUS_URL, timeout_s=CYCLONE_TIMEOUT_S, purpose="cyclones")


async def _fetch_nhc_layer(layer_id: int) -> object:
    query = urlencode(
        {
            "where": "1=1",
            "outFields": "idp_source,advisnum,tau,maxwind,gust",
            "outSR": "4326",
            "geometryPrecision": "3",
            "f": "geojson",
        }
    )
    url = f"{NHC_GIS_URL}/{layer_id}/query?{query}"
    return await _get_json(url, timeout_s=CYCLONE_TIMEOUT_S, purpose="cyclones")


async def _fetch_gdacs() -> object:
    return await _get_json(GDACS_URL, timeout_s=CYCLONE_TIMEOUT_S, purpose="cyclones")


async def _load_nhc() -> list[Storm] | None:
    storms = _parse_nhc(await _fetch_nhc_status())
    if not storms:
        return storms
    kinds = list(NHC_LAYERS)
    try:
        answers = await asyncio.gather(*(_fetch_nhc_layer(NHC_LAYERS[k]) for k in kinds))
        gis = _parse_gis(dict(zip(kinds, answers, strict=True)))
    except Exception:
        gis = None
    return _attach(storms, gis)


@dataclass
class _CycloneState:
    attempted_at: float | None = None
    nhc: list[Storm] | None = None
    gdacs: list[Storm] | None = None
    nhc_stale: bool = False
    gdacs_stale: bool = False
    lock: asyncio.Lock | None = None
    lock_loop: asyncio.AbstractEventLoop | None = None


_cyclones = _CycloneState()


def clear_cyclone_cache() -> None:
    """Forget both sources' copies and the last attempt. Used by the tests."""
    global _cyclones
    _cyclones = _CycloneState()


def _cyclone_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _cyclones.lock is None or _cyclones.lock_loop is not loop:
        _cyclones.lock = asyncio.Lock()
        _cyclones.lock_loop = loop
    return _cyclones.lock


def _cyclones_due() -> bool:
    at = _cyclones.attempted_at
    if at is None:
        return True
    failed = _cyclones.nhc_stale or _cyclones.gdacs_stale
    return time.monotonic() - at >= (CYCLONE_RETRY_S if failed else CYCLONE_TTL_S)


async def _refresh_cyclones() -> None:
    try:
        nhc, gdacs = await asyncio.gather(_load_nhc(), _fetch_gdacs(), return_exceptions=True)
    finally:
        _cyclones.attempted_at = time.monotonic()
    nhc_list = None if isinstance(nhc, BaseException) else nhc
    gdacs_list = (
        None
        if isinstance(gdacs, BaseException)
        else _parse_gdacs(gdacs, datetime.fromtimestamp(_wall_time(), tz=UTC))
    )
    if nhc_list is not None:
        _cyclones.nhc, _cyclones.nhc_stale = nhc_list, False
    else:
        _cyclones.nhc_stale = True
    if gdacs_list is not None:
        _cyclones.gdacs, _cyclones.gdacs_stale = gdacs_list, False
    else:
        _cyclones.gdacs_stale = True


def _source_state(good: list[Storm] | None, stale: bool) -> str:
    if good is None:
        return "unavailable"
    return "stale" if stale else "ok"


@router.get(
    "/api/weather/cyclones",
    response_model=CycloneResponse,
    summary="Active tropical cyclones: NOAA NHC (with track and cone) and GDACS (all basins)",
)
async def weather_cyclones() -> CycloneResponse:
    if _cyclones_due():
        async with _cyclone_lock():
            if _cyclones_due():
                await _refresh_cyclones()
    nhc, gdacs = _cyclones.nhc, _cyclones.gdacs
    if nhc is None and gdacs is None:
        raise HTTPException(status_code=502, detail="NHC and GDACS cyclone feeds unavailable")
    storms = _merge(nhc or [], gdacs or [])
    nhc_state = _source_state(nhc, _cyclones.nhc_stale)
    gdacs_state = _source_state(gdacs, _cyclones.gdacs_stale)
    return CycloneResponse(
        storms=storms,
        count=len(storms),
        sources={"nhc": nhc_state, "gdacs": gdacs_state},
        coverage=CYCLONE_COVERAGE,
        source=CYCLONE_SOURCE,
        stale="stale" in (nhc_state, gdacs_state) or "unavailable" in (nhc_state, gdacs_state),
    )
