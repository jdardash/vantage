"""Space environment routes: the aurora oval, and space weather for drag and conjunctions.

Endpoints:

* GET /api/aurora                       the current OVATION aurora probability map,
  thresholded for the browser
* GET /api/space-weather/summary        Kp, NOAA G/S/R scales now and for three days,
  observed F10.7 and the 45-day Ap and F10.7 forecast (sda.spaceweather)
* GET /api/space-weather/conjunction    storm warning for one conjunction's TCA and
  perigees; advisory text only, never a change to Pc or risk

GET /api/space-weather itself is the HUD strip in sda.routes.worldview (Kp,
solar wind, alerts), registered before this router, so the endpoints here live
under it rather than on it. The space weather products are cached for fifteen
minutes in sda.spaceweather with the same stale-on-error rules as the aurora.

Source: NOAA Space Weather Prediction Center, OVATION Prime short-term forecast
(``ovation_aurora_latest.json``), public domain and keyless. The product is a
1-degree geographic grid, 360 longitudes (0..359 east) by 181 latitudes
(-90..90), each cell holding the probability in percent of visible aurora at
that place. That is about 65,000 rows, most of them zero, so the server keeps
only the cells at or above a threshold and hands the browser those.

SWPC refreshes the product about every five minutes. This server asks for it at
most once per ten minutes, whatever the outcome: a failed or malformed fetch
keeps the last good copy in service, flagged ``stale``, and the endpoint answers
502 only while no good copy has ever arrived.

Fetching and number validation reuse the SWPC helpers in sda.routes.worldview
(``_get_json``, ``_swpc_number``), the same ones behind /api/space-weather.
Coordinates are geodetic degrees, longitudes returned in -180..180.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda import spaceweather
from sda.decay import orbit_perigee_km
from sda.models import TLERecord
from sda.routes.worldview import _get_json, _swpc_number

router = APIRouter(tags=["Space"])

OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
OVATION_TIMEOUT_S = 20.0
AURORA_CACHE_TTL_S = 600.0
AURORA_SOURCE = "NOAA SWPC OVATION Prime"
DEFAULT_MIN_PROB = 5
MIN_PROB_FLOOR = 1
MIN_PROB_CEIL = 100

#: What a row can physically be. Longitudes arrive 0..359; a feed already on
#: -180..180 is accepted too, and both are folded onto -180..180.
_LON_RANGE = (-180.0, 360.0)
_LAT_RANGE = (-90.0, 90.0)
_PROB_RANGE = (0.0, 100.0)
_COLUMNS = ("lon", "lat", "prob")
_TIME_MAX_CHARS = 40


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------

#: One grid cell, [lon, lat, prob]. Integral values stay integers in the JSON,
#: which keeps a several-thousand-cell oval about a third smaller on the wire.
Cell = tuple[int | float, int | float, int | float]


class HemisphereMax(BaseModel):
    north: float = Field(description="Highest probability (percent) north of the equator")
    south: float = Field(description="Highest probability (percent) south of the equator")


class AuroraResponse(BaseModel):
    observation_time: str = Field(description="OVATION 'Observation Time', as published")
    forecast_time: str = Field(description="OVATION 'Forecast Time', as published")
    cells: list[Cell] = Field(
        description="[lon -180..180, lat -90..90, probability percent] at or above min"
    )
    count: int
    max_prob: float = Field(description="Highest probability anywhere in the feed")
    hemisphere_max: HemisphereMax
    source: str
    stale: bool = Field(
        description="True when the last fetch failed and this is the last good copy"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ovation:
    """One parsed OVATION product. ``cells`` holds every valid cell with prob >= 1."""

    observation_time: str
    forecast_time: str
    cells: tuple[Cell, ...]
    max_prob: float
    north_max: float
    south_max: float


def _compact(value: float) -> int | float:
    """An integral float as an int, anything else rounded to three decimals."""
    return int(value) if value.is_integer() else round(value, 3)


def _feed_time(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip()[:_TIME_MAX_CHARS] if isinstance(value, str) else ""


def _parse_ovation(data: object) -> _Ovation | None:
    """The product as cells, or None when it holds no valid row at all.

    Every row is checked: it must be a three-item list of finite numbers in
    range (fill values and booleans are refused by ``_swpc_number``). A bad row
    is skipped, never raised, so one corrupt line costs one cell and not the map.
    """
    if not isinstance(data, dict):
        return None
    rows = data.get("coordinates")
    if not isinstance(rows, list):
        return None
    cells: list[Cell] = []
    valid = 0
    top = north = south = 0.0
    for row in rows:
        if not isinstance(row, list | tuple) or len(row) != len(_COLUMNS):
            continue
        record = dict(zip(_COLUMNS, row, strict=True))
        lon = _swpc_number(record, "lon", within=_LON_RANGE)
        lat = _swpc_number(record, "lat", within=_LAT_RANGE)
        prob = _swpc_number(record, "prob", within=_PROB_RANGE)
        if lon is None or lat is None or prob is None:
            continue
        valid += 1
        if lon > 180.0:
            lon -= 360.0
        top = max(top, prob)
        if lat > 0.0:
            north = max(north, prob)
        elif lat < 0.0:
            south = max(south, prob)
        if prob >= MIN_PROB_FLOOR:
            cells.append((_compact(lon), _compact(lat), _compact(prob)))
    if valid == 0:
        return None
    return _Ovation(
        observation_time=_feed_time(data, "Observation Time"),
        forecast_time=_feed_time(data, "Forecast Time"),
        cells=tuple(cells),
        max_prob=top,
        north_max=north,
        south_max=south,
    )


# --------------------------------------------------------------------------
# Fetch and cache
# --------------------------------------------------------------------------


@dataclass
class _AuroraState:
    attempted_at: float | None = None
    good: _Ovation | None = None
    stale: bool = False
    lock: asyncio.Lock | None = None
    lock_loop: asyncio.AbstractEventLoop | None = None


_state = _AuroraState()


def clear_aurora_cache() -> None:
    """Forget the cached product and the last attempt. Used by the tests."""
    global _state
    _state = _AuroraState()


def _lock() -> asyncio.Lock:
    """One lock per event loop, so a test client's fresh loop never inherits one."""
    loop = asyncio.get_running_loop()
    if _state.lock is None or _state.lock_loop is not loop:
        _state.lock = asyncio.Lock()
        _state.lock_loop = loop
    return _state.lock


def _due() -> bool:
    at = _state.attempted_at
    return at is None or time.monotonic() - at >= AURORA_CACHE_TTL_S


async def _fetch_ovation() -> object:
    """The raw OVATION JSON, or None on any failure. The only network call here."""
    return await _get_json(OVATION_URL, timeout_s=OVATION_TIMEOUT_S, purpose="aurora")


async def _current() -> tuple[_Ovation | None, bool]:
    """The last good product and whether it is stale, fetching when the TTL is up.

    The attempt time is stamped when the fetch ends, success or failure, so a
    failure waits out the same ten minutes as a success. Requests that arrive
    while a fetch is in flight wait on the lock for that one fetch instead of
    starting their own. The full grid takes a few hundred milliseconds to check
    row by row, so that runs in a worker thread rather than on the event loop.
    """
    if _due():
        async with _lock():
            if _due():
                try:
                    parsed = await asyncio.to_thread(_parse_ovation, await _fetch_ovation())
                except Exception:
                    parsed = None
                finally:
                    _state.attempted_at = time.monotonic()
                if parsed is not None:
                    _state.good, _state.stale = parsed, False
                else:
                    _state.stale = True
    return _state.good, _state.stale


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------


@router.get(
    "/api/aurora",
    response_model=AuroraResponse,
    summary="NOAA SWPC OVATION aurora probability map, cells at or above a threshold",
)
async def aurora(
    min_prob: int = Query(
        DEFAULT_MIN_PROB,
        alias="min",
        ge=MIN_PROB_FLOOR,
        le=MIN_PROB_CEIL,
        description="Keep cells whose aurora probability (percent) is at least this",
    ),
) -> AuroraResponse:
    product, stale = await _current()
    if product is None:
        raise HTTPException(status_code=502, detail="SWPC OVATION aurora feed unavailable")
    cells = [cell for cell in product.cells if cell[2] >= min_prob]
    return AuroraResponse(
        observation_time=product.observation_time,
        forecast_time=product.forecast_time,
        cells=cells,
        count=len(cells),
        max_prob=product.max_prob,
        hemisphere_max=HemisphereMax(north=product.north_max, south=product.south_max),
        source=AURORA_SOURCE,
        stale=stale,
    )


# --------------------------------------------------------------------------
# Space weather: summary and the conjunction storm check
# --------------------------------------------------------------------------


class ScaleDayOut(BaseModel):
    date: str = Field(description="UTC date of the forecast entry")
    g_scale: str | None = Field(description="Forecast NOAA G scale, G0..G5")
    r_minor_prob: int | None = Field(description="Chance (percent) of an R1-R2 radio blackout")
    r_major_prob: int | None = Field(description="Chance (percent) of an R3 or stronger blackout")
    s_prob: int | None = Field(description="Chance (percent) of an S1 or stronger radiation storm")


class ForecastDayOut(BaseModel):
    date: str
    ap: float | None = Field(description="Forecast daily Ap, nT")
    f107: float | None = Field(description="Forecast F10.7, SFU")


class SpaceWeatherSummary(BaseModel):
    kp_now: float | None = Field(description="Newest observed planetary Kp")
    kp_time: str | None = Field(description="Start of the Kp interval, UTC ISO 8601")
    kp_g_scale: str | None = Field(description="G scale the newest Kp corresponds to")
    g_scale_now: str | None = Field(description="NOAA scales: current G level")
    g_scale_next_3_days_max: str | None = Field(
        description="Strongest G level NOAA forecasts for today and the next two days"
    )
    next_3_days: list[ScaleDayOut]
    s_scale_now: str | None
    r_scale_now: str | None
    scales_time: str | None
    storm_now: bool = Field(description="G1 or stronger now (NOAA scales or the newest Kp)")
    storm_forecast: bool = Field(description="G1 or stronger forecast in the next three days")
    f107_observed: float | None = Field(description="Newest observed F10.7, SFU")
    f107_time: str | None
    f107_90day_mean: float | None
    forecast_issued: str | None
    forecast: list[ForecastDayOut] = Field(description="45-day daily Ap and F10.7 forecast")
    forecast_peak_ap: float | None
    stale: bool = Field(description="True when any product is the last good copy after a failure")
    stale_products: list[str]
    missing_products: list[str]
    source: str


class StormWarningOut(BaseModel):
    g_scale: str
    basis: str = Field(
        description="noaa_scales_forecast, noaa_scales_observed or observed_kp"
    )
    message: str


class ConjunctionStormResponse(BaseModel):
    tca: str
    tca_day: str
    perigee_a_km: float | None
    perigee_b_km: float | None
    perigee_limit_km: float
    low_perigee: bool = Field(description="Either perigee is under the limit")
    status: str = Field(
        description="warning (low perigee meets a G1+ storm), clear, or unknown "
        "(no product reaches the TCA day). Unknown is not quiet."
    )
    reason: str | None = Field(description="Why the status is unknown, else null")
    g_scale_tca_day: str | None = Field(description="Strongest NOAA G level dated on the TCA day")
    kp_near_tca: float | None = Field(description="Highest observed Kp within 3 hours of TCA")
    level: str | None = Field(description="Strongest of the two, as a G level")
    covered: bool = Field(
        description="False when no product reaches the TCA: unknown, not quiet"
    )
    storm: StormWarningOut | None = Field(
        description="Set when a low-perigee event meets a G1+ storm. Advisory only: "
        "the event's miss distance, Pc and risk level are unchanged."
    )
    stale: bool
    source: str


def _label(prefix: str, level: int | None) -> str | None:
    return None if level is None else f"{prefix}{level}"


def _summary(sw: spaceweather.SpaceWeather) -> SpaceWeatherSummary:
    kp = spaceweather.latest_kp(sw.kp)
    kp_g = spaceweather.g_from_kp(kp.kp if kp else None)
    now = spaceweather.scale_now(sw.scales)
    ahead = spaceweather.scales_ahead(sw.scales)
    g_ahead = spaceweather.max_level(s.g for s in ahead)
    g_now = now.g if now else None
    days = sw.forecast.days if sw.forecast else ()
    aps = [d.ap for d in days if d.ap is not None]
    peak_ap = max(aps) if aps else None
    return SpaceWeatherSummary(
        kp_now=kp.kp if kp else None,
        kp_time=kp.time.isoformat() if kp else None,
        kp_g_scale=spaceweather.g_label(kp_g),
        g_scale_now=spaceweather.g_label(g_now),
        g_scale_next_3_days_max=spaceweather.g_label(g_ahead),
        next_3_days=[
            ScaleDayOut(
                date=s.day.isoformat(),
                g_scale=spaceweather.g_label(s.g),
                r_minor_prob=s.r_minor_prob,
                r_major_prob=s.r_major_prob,
                s_prob=s.s_prob,
            )
            for s in ahead
        ],
        s_scale_now=_label("S", now.s) if now else None,
        r_scale_now=_label("R", now.r) if now else None,
        scales_time=f"{now.day.isoformat()} {now.time}".strip() if now else None,
        storm_now=max(g_now or 0, kp_g or 0) >= spaceweather.STORM_MIN_G,
        storm_forecast=(g_ahead or 0) >= spaceweather.STORM_MIN_G,
        f107_observed=sw.f107.flux if sw.f107 else None,
        f107_time=sw.f107.time.isoformat() if sw.f107 else None,
        f107_90day_mean=sw.f107.ninety_day_mean if sw.f107 else None,
        forecast_issued=sw.forecast.issued or None if sw.forecast else None,
        forecast=[
            ForecastDayOut(date=d.day.isoformat(), ap=d.ap, f107=d.f107) for d in days
        ],
        forecast_peak_ap=peak_ap,
        stale=bool(sw.stale),
        stale_products=sorted(sw.stale),
        missing_products=sorted(sw.missing),
        source=spaceweather.SOURCE,
    )


@router.get(
    "/api/space-weather/summary",
    response_model=SpaceWeatherSummary,
    summary="NOAA SWPC Kp, G/S/R scales, observed F10.7 and the 45-day Ap and F10.7 forecast",
)
async def space_weather_summary() -> SpaceWeatherSummary:
    sw = await spaceweather.current()
    if sw.empty:
        raise HTTPException(status_code=502, detail="NOAA SWPC space weather unavailable")
    return _summary(sw)


def _catalog_tle(norad_id: int) -> TLERecord | None:
    """The catalog's element set for *norad_id*. Tests replace this."""
    from sda.api import store

    return store.get(norad_id)


def _perigee(given: float | None, norad_id: int | None) -> float | None:
    if given is not None or norad_id is None:
        return given
    tle = _catalog_tle(norad_id)
    if tle is None:
        raise HTTPException(status_code=404, detail=f"Satellite {norad_id} not found")
    return round(orbit_perigee_km(tle), 1)


@router.get(
    "/api/space-weather/conjunction",
    response_model=ConjunctionStormResponse,
    summary="Geomagnetic storm warning for a conjunction's TCA and the two perigees",
)
async def space_weather_conjunction(
    tca: Annotated[
        datetime, Query(description="Time of closest approach, ISO 8601 (UTC if no zone)")
    ],
    perigee_a: float | None = Query(
        None,
        ge=-1000.0,
        le=1_000_000.0,
        allow_inf_nan=False,
        description="Perigee altitude of object A, km",
    ),
    perigee_b: float | None = Query(
        None,
        ge=-1000.0,
        le=1_000_000.0,
        allow_inf_nan=False,
        description="Perigee altitude of object B, km",
    ),
    primary: int | None = Query(
        None, ge=1, description="NORAD ID of A; its catalog perigee fills a missing perigee_a"
    ),
    secondary: int | None = Query(
        None, ge=1, description="NORAD ID of B; its catalog perigee fills a missing perigee_b"
    ),
) -> ConjunctionStormResponse:
    """Whether a geomagnetic storm overlaps this conjunction, for a low-perigee pair.

    A warning is returned when either perigee is under 600 km and the NOAA G
    scale dated on the TCA day, or the observed Kp within three hours of TCA,
    is G1 or stronger. It states that in-track error grows during storms, so
    the TLE-based miss distance and Pc are less reliable. Nothing about the
    event's Pc or risk classification is computed or changed here.

    Both perigees are required (directly or through a NORAD ID): with one
    unknown, a storm-day event could not be told apart from a clear one.
    """
    perigee_a_km = _perigee(perigee_a, primary)
    perigee_b_km = _perigee(perigee_b, secondary)
    if perigee_a_km is None or perigee_b_km is None:
        raise HTTPException(
            status_code=422,
            detail="Give both perigees: perigee_a and perigee_b (km), or primary and "
            "secondary NORAD IDs from the catalog",
        )
    sw = await spaceweather.current()
    if not sw.scales and not sw.kp:
        raise HTTPException(status_code=502, detail="NOAA SWPC scales and Kp unavailable")
    check = spaceweather.check_conjunction_storm(
        tca, perigee_a_km, perigee_b_km, scales=sw.scales, kp=sw.kp
    )
    storm = None
    if check.warning is not None:
        storm = StormWarningOut(
            g_scale=spaceweather.g_label(check.level) or "",
            basis=check.basis,
            message=check.warning,
        )
    return ConjunctionStormResponse(
        tca=check.tca.isoformat(),
        tca_day=check.tca_day.isoformat(),
        perigee_a_km=perigee_a_km,
        perigee_b_km=perigee_b_km,
        perigee_limit_km=spaceweather.STORM_PERIGEE_LIMIT_KM,
        low_perigee=bool(check.low_perigee),
        status=check.status,
        reason=check.reason,
        g_scale_tca_day=spaceweather.g_label(check.g_scales_tca_day),
        kp_near_tca=check.kp_near_tca,
        level=spaceweather.g_label(check.level),
        covered=check.covered,
        storm=storm,
        stale=bool({"scales", "kp"} & sw.stale),
        source=spaceweather.SOURCE,
    )
