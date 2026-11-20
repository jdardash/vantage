"""Summit conditions for any mountain on Earth, scored per day over the summit hours.

A screen, not a go/no-go: which peak, which day, and what the risk is. Each day over the
summit hours (10:00 to 14:00 local) is GO, MARG, NO or ? with the reasons and the numbers
behind it, and a day is GO only when every source required for that point was read.

Ported from PeakWatch in trip-runbook (trips/Sierra-Summit-2026/tools/peakwatch.py and the
helpers it imports from tools/summit_check.py and tools/allpeaks_check.py), MIT licence, read
at trip-runbook commit 071902d73cc11ab398bf56303aa0a3fa1bc033f2. Copied, with the limits of
tools/peakwatch.toml: at_height, sane, sane_height, model_day (split here into summit_series
and model_day so the meteogram and the score share one code path), wind_chill_f, local_window,
in_window, summit_p, members, ens_p, the wind-odds checks and scorer, in_ring, in_geometry,
avy_zone, avy_days, danger_max, strip_html, product_day, avy_products, avy_for_day,
alert_level, alerts_for, fires_near, nws_day, sun_times and assess.

What changed to make it work anywhere:

* Pressure levels. PeakWatch read 700/600/500 hPa, right for 12,000-14,500 ft California
  summits. Here three levels are picked per summit from 1000 to 200 hPa (level_band), so a
  1,300 m Scottish hill and an 8,849 m Himalayan summit are both bracketed. For 3,600-4,800 m
  the pick is PeakWatch's 700/600/500.
* The exposed-wind rule. Inside the calibration region it is PeakWatch's verbatim (a summit
  below the 600 hPa height takes at least the 600 hPa wind; found at Mammoth's summit
  anemometer about 840 m under that level). Elsewhere the same idea is applied to the next
  pressure level above the summit when that level is within EXPOSED_REACH_M, about the
  distance it was found at. That is one station's finding stretched, calibrated nowhere else.
* Calibrations. tools/wind_odds.json (Reno, Oakland, Las Vegas soundings at 600 hPa),
  tools/summit_odds.json (Mammoth's summit anemometer) and the White Mountain ensemble table
  are applied only inside CAL_REGION (California and Nevada) and only for a summit whose band
  reaches 600 hPa, the level they were fitted on. Elsewhere the reasons say "wind odds not
  calibrated here", the plain median limits decide, and the ensemble odds are the raw share
  of members at the wind line.
* Local time comes from the point's own time zone (Open-Meteo's timezone=auto).
* US-only extras (NWS alerts and thunder, avalanche.org, NIFC wildfires) run only where the
  NWS covers the point. Smoke uses the US AQI in the US and the European AQI elsewhere.
* Left out: OpenSnow (account-based), CDEC snow depth, the Inyo road sheet and Caltrans
  (California-only), and the change log and notifications (PeakWatch's batch-run features).

Geodetic degrees in; nothing here touches the orbital frames. Units inside follow PeakWatch
(ft, mph, F, inches) because every limit was written in them.
"""

from __future__ import annotations

import html
import math
import re
import statistics
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

import httpx

# --------------------------------------------------------------------------
# Sources, units, limits
# --------------------------------------------------------------------------

USER_AGENT = "vantage/0.2 (worldview summit conditions)"
OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OM_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OM_AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OM_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OM_META_URL = "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json"
OM_ENS_META_URL = (
    "https://ensemble-api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json"
)
NWS_POINT_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
AVY_URL = "https://api.avalanche.org/v2/public/products/map-layer"
AVY_LIST_URL = "https://api.avalanche.org/v2/public/products"
AVY_PRODUCT_URL = "https://api.avalanche.org/v2/public/product/{}"
FIRES_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)

MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless"]
SHORT = {
    "ecmwf_ifs025": "ECMWF",
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON",
    "gem_seamless": "GEM",
}
FORECAST_DAYS = 8  # one more than the 7 scored, as PeakWatch pulls
PAST_DAYS = 3  # the pull reaches 3 days back, so storm snow before day 0 counts
AIR_DAYS = 5  # CAMS reaches about 4.5 days
BATCH = 20  # points per Open-Meteo request
SUMMIT_HOURS = (10, 14)  # local, [10:00, 14:00)
WIDE_HOURS = (6, 18)  # precipitation, snow, CAPE and smoke are read over the whole day
ALERT_HOURS = (0, 18)  # an alert in force before dawn still hits a pre-dawn start
THAW_HOURS = (10, 16)  # the day before: summit above freezing in these hours ...
DAWN_HOURS = (3, 9)  # ... and below by these: melt-freeze, hard icy snow early
STORM_H = 72
LAPSE_F_PER_FT = 6.5 * 1.8 / 3280.84  # standard lapse, for a summit outside the band
FT_PER_M = 1 / 0.3048

# Standard-atmosphere heights (m) of the levels the four models all carry.
STD_HEIGHT_M = {
    1000: 111,
    925: 762,
    850: 1457,
    700: 3012,
    600: 4206,
    500: 5574,
    400: 7185,
    300: 9164,
    250: 10363,
    200: 11784,
}
PRESSURES = tuple(STD_HEIGHT_M)
BAND_MARGIN_M = 600  # the band's lowest level sits at least this far under the summit
STD_600_FT = 13800  # PeakWatch: 600 hPa in the standard atmosphere, exposed_wind's fallback
EXPOSED_REACH_M = 850  # outside the region: the level above counts when this close

# Physical bounds (PeakWatch's SANE, widened from California to every climate). A value
# outside is a unit or decoding fault, dropped and counted rather than scored.
SANE = {
    "wind": (0.0, 300.0),
    "temp": (-130.0, 130.0),
    "cloud": (0.0, 100.0),
    "snow": (0.0, 30.0),
    "precip": (0.0, 15.0),
    "cape": (-50.0, 8000.0),
    "gust": (0.0, 300.0),
    "aqi": (0.0, 5000.0),
}
# Plausible height (ft) of each level: PeakWatch's 700/600/500 bounds, widened for polar
# winter lows and tropical highs. A metre value labelled ft lands outside.
HEIGHT_FT = {
    1000: (-3000.0, 2500.0),
    925: (0.0, 4500.0),
    850: (2500.0, 6500.0),
    700: (6500.0, 12500.0),
    600: (10500.0, 16500.0),
    500: (14500.0, 21000.0),
    400: (19500.0, 26500.0),
    300: (25500.0, 33500.0),
    250: (29500.0, 37500.0),
    200: (34000.0, 42500.0),
}

# tools/peakwatch.toml [limits].
LIMITS: dict[str, float] = {
    "wind_warn_mph": 21.75,  # PLAN.md 35 km/h
    "wind_fail_mph": 30,
    "chill_warn_f": -9,  # PLAN.md -23 C
    "chill_fail_f": -19,
    "snow_warn_in": 0.1,
    "snow_fail_in": 0.4,
    "precip_warn_in": 0.02,
    "p_warn": 0.25,
    "p_fail": 0.50,
    "aqi_warn": 101,  # US AQI, unhealthy for sensitive groups
    "aqi_fail": 151,  # US AQI, unhealthy
    # Not in PeakWatch: the European AQI bands holding about the same PM2.5 (US 101 is about
    # 35 ug/m3, inside EAQI "poor" 60-80; US 151 is about 55 ug/m3, EAQI "very poor").
    "eaqi_warn": 60,
    "eaqi_fail": 80,
    "fire_warn_mi": 15,
    "fire_fail_mi": 5,
    "fire_max_age_days": 90,
    "avy_warn_level": 2,
    "avy_fail_level": 3,
    "storm_snow_warn_in": 6,
    "storm_snow_fail_in": 12,
    "cloud_pct": 80,
    "cloud_warn_h": 2,
    "cape_warn": 250,
    "cape_fail": 800,
    "wind_odds_warn": 0.15,  # applied only where calibrated
    "summit_odds_warn": 0.25,  # applied only where calibrated
}
ALERT_FAIL = frozenset(
    {
        "Blizzard Warning",
        "Winter Storm Warning",
        "High Wind Warning",
        "Extreme Cold Warning",
        "Wind Chill Warning",
        "Ice Storm Warning",
        "Avalanche Warning",
        "Extreme Heat Warning",
        "Excessive Heat Warning",
        "Flash Flood Warning",
        "Dust Storm Warning",
        "Heavy Snow Warning",
    }
)
AVY_NAMES = {
    -1: "no rating",
    0: "no rating",
    1: "1 Low",
    2: "2 Moderate",
    3: "3 Considerable",
    4: "4 High",
    5: "5 Extreme",
}
SCREEN_NOTE = (
    "A screen, not a go/no-go: which peak, which day, and what the risk is. Read the "
    "forecasts it names and judge the mountain yourself before any attempt."
)

# The calibrations, copied from trip-runbook tools/wind_odds.json and tools/summit_odds.json.
# Only the fields the scorer reads.
WIND_ODDS: dict[str, Any] = {
    "limit_mph": 21.75,
    "run_age_h": 6,
    "trained": "2026-04-02 to 2026-11-08 00Z runs vs IGRA2 soundings at Reno, Oakland, "
    "Las Vegas, 600 hPa",
    "bands": [
        {"lead_days": [0, 1], "intercept": -9.16587, "coef_median": 0.43169,
         "coef_spread": -0.00703},
        {"lead_days": [2, 3], "intercept": -6.57885, "coef_median": 0.3026,
         "coef_spread": 0.00625},
        {"lead_days": [4, 5], "intercept": -4.32345, "coef_median": 0.18518,
         "coef_spread": 0.02254},
        {"lead_days": [6, 7], "intercept": -2.75172, "coef_median": 0.11955,
         "coef_spread": 0.00868},
    ],
}
SUMMIT_ODDS: dict[str, Any] = {
    "limit_mph": 21.75,
    "run_age_h": 6,
    "trained": "Mammoth summit anemometer (11,053 ft, exposed), 2026 runs and four cool "
    "seasons 2022-26",
    "bands": [
        {"lead_days": [0, 1], "intercept": -2.9361, "coef_median": 0.10281,
         "coef_spread": -0.02577},
        {"lead_days": [2, 3], "intercept": -3.91575, "coef_median": 0.16188,
         "coef_spread": -0.04346},
        {"lead_days": [4, 5], "intercept": -2.96143, "coef_median": 0.1073,
         "coef_spread": -0.0081},
        {"lead_days": [6, 7], "intercept": -2.22779, "coef_median": 0.06073,
         "coef_spread": 0.01768},
    ],
    "coef_cool": 0.82676,
    "cool_months": [10, 11, 12, 1, 2, 3, 4],
}
# summit_check.py: WM-P's ERA5 share of Oct-Nov middays with White Mountain's summit at
# 35 km/h or more, by the 600 hPa wind in km/h. A proxy off that summit, region-only here.
SUMMIT_P_TABLE = (
    (18, 0.02), (27, 0.29), (35, 0.44), (45, 0.70), (54, 0.80), (72, 0.92), (math.inf, 0.97)
)

# California and Nevada, where the two odds files and the White Mountain table were fitted
# (soundings at Reno, Oakland and Las Vegas; the anemometer at Mammoth). A [lon, lat] ring.
CAL_REGION_NAME = "California and Nevada"
CAL_REGION = [
    [-124.8, 42.0], [-114.04, 42.0], [-114.04, 36.15], [-114.75, 35.1], [-114.45, 34.3],
    [-114.72, 32.72], [-117.12, 32.53], [-120.5, 33.5], [-125.0, 40.0], [-124.8, 42.0],
]
# Boxes that may hold a US point. Inside one the NWS is asked; its 404 says "not covered",
# which is how Canadian and Mexican points inside the lower-48 box drop out.
US_BOXES = (
    (24.3, 49.5, -125.0, -66.8),
    (51.0, 71.6, -180.0, -129.9),
    (51.0, 53.5, 172.0, 180.0),
    (18.8, 22.4, -160.7, -154.6),
    (17.6, 18.6, -67.5, -64.5),
    (13.2, 20.6, 144.5, 146.2),
    (-14.6, -14.1, -171.0, -169.4),
)

# Cache lifetimes (s): forecasts 1 h, ensemble and air quality 3 h, NWS alerts 10 min.
TTL = {
    "om-fc": 3600.0,
    "om-ens": 3 * 3600.0,
    "om-aq": 3 * 3600.0,
    "om-elev": 30 * 86400.0,
    "om-meta": 900.0,
    "nws-point": 30 * 86400.0,
    "nws-alerts": 600.0,
    "nws-fc": 3 * 3600.0,
    "avy": 3600.0,
    "avy-list": 3600.0,
    "avy-prod": 3600.0,
    "fires": 3 * 3600.0,
}
# Stale-on-error: a failed fetch falls back to the last copy, never older than this
# (tools/peakwatch.toml [max_stale_hours]). Past it the source counts as not read.
MAX_STALE = {
    "om-fc": 12 * 3600.0,
    "om-ens": 24 * 3600.0,
    "om-aq": 24 * 3600.0,
    "om-elev": math.inf,
    "om-meta": 0.0,
    "nws-point": math.inf,
    "nws-alerts": 3 * 3600.0,
    "nws-fc": 12 * 3600.0,
    "avy": 24 * 3600.0,
    "avy-list": 24 * 3600.0,
    "avy-prod": 24 * 3600.0,
    "fires": 24 * 3600.0,
}
RETRY_AFTER_S = 120.0  # a failed key is not asked again sooner than this
CACHE_CAP = 6000
TIMEOUT_S = {"om": 60.0, "nws": 15.0, "avy": 30.0, "fires": 30.0}


class UpstreamError(Exception):
    """A fetch that failed; status is the HTTP code when there was one."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------
# Clock, HTTP and cache (tests replace _now and the transport)
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


_TRANSPORT: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Route every upstream call through this transport (tests pass a MockTransport)."""
    global _TRANSPORT
    _TRANSPORT = transport


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    """One GET, JSON back, or UpstreamError. Redirects are followed (the NWS uses them)."""
    try:
        with httpx.Client(
            transport=_TRANSPORT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            r = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError(type(exc).__name__) from exc
    if r.status_code != 200:
        raise UpstreamError(f"HTTP {r.status_code}", r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise UpstreamError("not JSON") from exc


@dataclass
class _Entry:
    data: Any
    fetched: float
    run: float | None = None


@dataclass
class Read:
    """How one source fared for one point: fresh, cached, stale, failed or skipped."""

    state: str
    fetched: float | None = None
    run: float | None = None
    error: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in ("fresh", "cached", "stale")


class _Cache:
    def __init__(self) -> None:
        self._d: dict[tuple[Any, ...], _Entry] = {}
        self._failed: dict[tuple[Any, ...], tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[Any, ...]) -> _Entry | None:
        with self._lock:
            return self._d.get(key)

    def put(self, key: tuple[Any, ...], data: Any, now: float, run: float | None) -> None:
        with self._lock:
            self._d[key] = _Entry(data, now, run)
            self._failed.pop(key, None)
            if len(self._d) > CACHE_CAP:
                oldest = sorted(self._d.items(), key=lambda kv: kv[1].fetched)
                for k, _ in oldest[: len(self._d) - CACHE_CAP]:
                    del self._d[k]

    def fail(self, key: tuple[Any, ...], now: float, err: str) -> None:
        with self._lock:
            self._failed[key] = (now, err)

    def recent_failure(self, key: tuple[Any, ...], now: float) -> str | None:
        with self._lock:
            hit = self._failed.get(key)
        return hit[1] if hit and now - hit[0] < RETRY_AFTER_S else None

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._failed.clear()

    def __len__(self) -> int:
        return len(self._d)


CACHE = _Cache()
_FETCH_LOCK = threading.Lock()
COUNTS: Counter[str] = Counter()  # upstream requests made, by source (read by the tests)


def clear_cache() -> None:
    """Forget every cached copy and failure, and zero the request counts."""
    CACHE.clear()
    COUNTS.clear()


def _fresh(entry: _Entry | None, kind: str, now: float, run: float | None) -> bool:
    """A copy younger than its TTL and from the model run now served (when that is known)."""
    if entry is None or now - entry.fetched >= TTL[kind]:
        return False
    return run is None or entry.run is None or entry.run == run


def _fallback(key: tuple[Any, ...], kind: str, now: float, err: str) -> tuple[Any, Read]:
    """The last copy when it is inside MAX_STALE, else nothing and a failed read."""
    entry = CACHE.get(key)
    if entry is not None and now - entry.fetched <= MAX_STALE[kind]:
        return entry.data, Read("stale", entry.fetched, entry.run, err)
    if entry is not None:
        age = (now - entry.fetched) / 3600
        err = f"{err}; last copy {age:.0f} h old, past the {MAX_STALE[kind] / 3600:.0f} h limit"
    return None, Read("failed", None, None, err)


def _cached_get(
    key: tuple[Any, ...],
    kind: str,
    url: str,
    params: dict[str, Any] | None = None,
    run: float | None = None,
    timeout: float = 30.0,
    keep_status: tuple[int, ...] = (),
) -> tuple[Any, Read]:
    """One source through the cache: fresh copy, new fetch, or stale copy on failure.

    A status in keep_status is an answer, not a failure (the NWS 404 for a point it does
    not cover), and is cached as {"__status__": code}.
    """
    now = _now().timestamp()
    entry = CACHE.get(key)
    if entry is not None and _fresh(entry, kind, now, run):
        return entry.data, Read("cached", entry.fetched, entry.run)
    recent = CACHE.recent_failure(key, now)
    if recent is not None:
        return _fallback(key, kind, now, recent)
    COUNTS[kind] += 1
    try:
        data = _get_json(url, params, timeout)
    except UpstreamError as exc:
        if exc.status not in keep_status:
            CACHE.fail(key, now, str(exc))
            return _fallback(key, kind, now, str(exc))
        data = {"__status__": exc.status}
    CACHE.put(key, data, now, run)
    return data, Read("fresh", now, run)


def _model_run(url: str) -> float | None:
    """The newest model run Open-Meteo serves (unix s), or None when meta.json is not read.

    PEAKMAP.md: on 2026-11-15 these files answered 404 for every model, so a missing run
    only means the cache falls back to its TTL; it never blocks a forecast.
    """
    data, read = _cached_get(("om-meta", url), "om-meta", url, timeout=10.0)
    if not read.ok or not isinstance(data, dict):
        return None
    v = data.get("last_run_initialisation_time")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


# --------------------------------------------------------------------------
# Geometry and places
# --------------------------------------------------------------------------


def round_point(lat: float, lon: float) -> tuple[float, float]:
    """The 0.01 degree cache cell: about 1.1 km, inside one model grid cell."""
    lon = ((lon + 180.0) % 360.0) - 180.0
    return round(lat, 2), round(lon, 2)


def in_ring(lat: float, lon: float, ring: Sequence[Sequence[float]]) -> bool:
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_geometry(lat: float, lon: float, geom: Any) -> bool:
    if not isinstance(geom, dict):
        return False
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    return any(
        bool(poly)
        and in_ring(lat, lon, poly[0])
        and not any(in_ring(lat, lon, hole) for hole in poly[1:])
        for poly in polys
    )


def in_cal_region(lat: float, lon: float) -> bool:
    return in_ring(lat, lon, CAL_REGION)


def maybe_us(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in US_BOXES)


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a 6371 km sphere (the haversine PeakWatch used)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


def level_band(elev_m: float) -> tuple[int, int, int]:
    """Three pressure levels around a summit: the lowest at least BAND_MARGIN_M under it."""
    idx = [k for k, p in enumerate(PRESSURES) if STD_HEIGHT_M[p] <= elev_m - BAND_MARGIN_M]
    i = min(max(idx, default=0), len(PRESSURES) - 3)
    a, b, c = PRESSURES[i : i + 3]
    return a, b, c


def ensemble_level(elev_m: float, calibrated: bool) -> int:
    """The ensemble level read: 600 hPa where calibrated (PeakWatch), else the nearest."""
    if calibrated:
        return 600
    return min(PRESSURES[:8], key=lambda p: abs(STD_HEIGHT_M[p] - elev_m))


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


@dataclass
class Zone:
    tz: tzinfo
    name: str
    exact: bool  # False: a fixed UTC offset stands in for the zone's rules


def zone_of(block: Any, lon: float) -> Zone:
    """The point's time zone from an Open-Meteo answer (timezone=auto).

    With a time-zone database (zoneinfo plus tzdata) the zone's own rules apply, daylight
    saving included. Without one, the UTC offset Open-Meteo reports for now is used for the
    whole week, and Zone.exact says so. With no answer at all, solar time to the hour.
    """
    if isinstance(block, dict) and isinstance(block.get("utc_offset_seconds"), int | float):
        name = str(block.get("timezone") or "")
        try:
            from zoneinfo import ZoneInfo

            return Zone(ZoneInfo(name), name, True)
        except Exception:  # no tz database on this machine, or an odd name
            off = timedelta(seconds=int(block["utc_offset_seconds"]))
            return Zone(timezone(off), name or "UTC", False)
    off_h = round(lon / 15.0)
    return Zone(timezone(timedelta(hours=off_h)), f"solar UTC{off_h:+d}", False)


def local_window(day: date, hours: tuple[int, int], tz: tzinfo) -> tuple[datetime, datetime]:
    """[day hh:00, day hh:00) local, as aware UTC datetimes."""
    start = datetime.combine(day, time(hours[0]), tz)
    end = datetime.combine(day, time(hours[1]), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def times_of(hourly: Any) -> list[datetime]:
    """Open-Meteo hourly times (timeformat=unixtime) as aware UTC datetimes."""
    return [datetime.fromtimestamp(float(t), UTC) for t in hourly.get("time") or []]


def in_window(
    times: Sequence[datetime], vals: Sequence[Any] | None, start: datetime, end: datetime
) -> list[Any]:
    return [
        v for t, v in zip(times, vals or [], strict=False) if start <= t < end and v is not None
    ]


def utc(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def sun_times(day: date, lat: float, lon: float) -> tuple[datetime | None, datetime | None, str]:
    """Sunrise and sunset (aware UTC) by the NOAA general solar position method, about 1 min.

    summit_check.sun_times, plus the polar cases it never met: (None, None, "polar night")
    or (None, None, "midnight sun").
    """
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    phi = math.radians(max(-89.99, min(89.99, lat)))
    x = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(
        phi
    ) * math.tan(decl)
    if x > 1:
        return None, None, "polar night"
    if x < -1:
        return None, None, "midnight sun"
    ha = math.degrees(math.acos(x))
    midnight = datetime.combine(day, time(0), UTC)
    rise = midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    set_ = midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise, set_, ""


# --------------------------------------------------------------------------
# Turning a forecast into summit numbers (PeakWatch)
# --------------------------------------------------------------------------


def at_height(
    z: float, levels: Iterable[tuple[float | None, float | None]], lapse: float | None = None
) -> float | None:
    """Value at height z (ft), linear in height between pressure levels [(height ft, value)].

    Outside the band it holds the nearest level, or for temperature moves along `lapse`
    (per ft, colder upward).
    """
    pts = sorted((h, v) for h, v in levels if h is not None and v is not None)
    if not pts:
        return None
    if z <= pts[0][0]:
        return pts[0][1] + (lapse * (pts[0][0] - z) if lapse else 0)
    if z >= pts[-1][0]:
        return pts[-1][1] - (lapse * (z - pts[-1][0]) if lapse else 0)
    for (z0, v0), (z1, v1) in zip(pts, pts[1:], strict=False):
        if z0 <= z <= z1:
            return v0 if z1 == z0 else v0 + (v1 - v0) * (z - z0) / (z1 - z0)
    return None


def sane(v: Any, kind: str, rejects: Counter[str] | None = None) -> float | None:
    """v when it sits inside SANE[kind], else None, counted so the answer can say so."""
    if v is None or isinstance(v, bool) or not isinstance(v, int | float):
        return None
    lo, hi = SANE[kind]
    if lo <= v <= hi:
        return float(v)
    if rejects is not None:
        rejects[kind] += 1
    return None


def sane_height(z: Any, lv: int, rejects: Counter[str] | None = None) -> float | None:
    """A pressure level's height in ft when plausible for that level."""
    if z is None or isinstance(z, bool) or not isinstance(z, int | float):
        return None
    lo, hi = HEIGHT_FT[lv]
    if lo <= z <= hi:
        return float(z)
    if rejects is not None:
        rejects[f"{lv} hPa height"] += 1
    return None


def wind_chill_f(t_f: float, v_mph: float | None) -> float:
    """NWS wind chill (2001). Defined at 50 F or below and 3 mph or above."""
    if v_mph is None or v_mph < 3 or t_f > 50:
        return t_f
    k = float(v_mph**0.16)
    return 35.74 + 0.6215 * t_f - 35.75 * k + 0.4275 * t_f * k


def freezing_level_ft(pts: Iterable[tuple[float | None, float | None]]) -> float | None:
    """Height (ft) where the air crosses 32 F, from the levels' heights and temperatures.

    Not in PeakWatch. Between levels it is linear in height; under or over the band it
    follows the standard lapse; never below sea level.
    """
    zt = sorted((z, t) for z, t in pts if z is not None and t is not None)
    if not zt:
        return None
    z0, t0 = zt[0]
    if t0 <= 32:
        return max(0.0, z0 - (32 - t0) / LAPSE_F_PER_FT)
    for (za, ta), (zb, tb) in zip(zt, zt[1:], strict=False):
        if ta > 32 >= tb:
            return za + (ta - 32) * (zb - za) / (ta - tb)
    ztop, ttop = zt[-1]
    return ztop + (ttop - 32) / LAPSE_F_PER_FT


def _level_heights(
    h: dict[str, Any], units: dict[str, Any], m: str, lv: int, n: int
) -> list[Any]:
    zk = f"geopotential_height_{lv}hPa_{m}"
    vals = list(h.get(zk) or [None] * n)
    if units.get(zk) not in ("ft", None):
        vals = [None if z is None else z / 0.3048 for z in vals]
    return vals


def _clean(
    vals: Sequence[Any], n: int, bounds: tuple[float, float], key: str,
    rejects: Counter[str] | None,
) -> list[float | None]:
    """The first n values of a column through the sane() test in one pass (no call per
    value; a 50-summit batch is about a million values), short columns padded with None."""
    lo, hi = bounds
    out: list[float | None] = []
    bad = 0
    m = min(n, len(vals))
    for i in range(m):
        v = vals[i]
        if v is None or isinstance(v, bool) or not isinstance(v, int | float):
            out.append(None)
        elif lo <= v <= hi:
            out.append(float(v))
        else:
            out.append(None)
            bad += 1
    out.extend([None] * (n - m))
    if bad and rejects is not None:
        rejects[key] += bad
    return out


def summit_series(
    block: Any,
    elev_ft: float,
    levels: Sequence[int],
    mode: str,
    rejects: Counter[str] | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Every model's hourly summit-height numbers from one Open-Meteo block.

    mode "ca600": PeakWatch's exposed rule verbatim (a summit below the 600 hPa height, or
    STD_600_FT without one, takes at least the 600 hPa wind). mode "next": the wind of the
    next level above the summit, when that level is within EXPOSED_REACH_M. mode "": plain
    interpolation. `until` stops the series at that instant (the end of the last day being
    scored), so a two-day verdict does not work through eight days of hours. Returns
    {"times": [...], "models": {model: {var: [per hour]}}}; a model with no data at all is
    still listed, all None, and model_day drops it.
    """
    if not isinstance(block, dict) or not isinstance(block.get("hourly"), dict):
        return {"times": [], "models": {}}
    h = block["hourly"]
    units = block.get("hourly_units") or {}
    times = times_of(h)
    if until is not None:
        times = [t for t in times if t < until]
    n = len(times)
    reach_ft = EXPOSED_REACH_M * FT_PER_M
    out: dict[str, Any] = {}
    for m in MODELS:
        zs = {lv: _clean(_level_heights(h, units, m, lv, n), n, HEIGHT_FT[lv],
                         f"{lv} hPa height", rejects) for lv in levels}
        ws = {lv: _clean(h.get(f"wind_speed_{lv}hPa_{m}") or [], n, SANE["wind"], "wind",
                         rejects) for lv in levels}
        ts = {lv: _clean(h.get(f"temperature_{lv}hPa_{m}") or [], n, SANE["temp"], "temp",
                         rejects) for lv in levels}
        cs = {lv: _clean(h.get(f"cloud_cover_{lv}hPa_{m}") or [], n, SANE["cloud"], "cloud",
                         rejects) for lv in levels}
        wind: list[float | None] = []
        temp: list[float | None] = []
        cloud: list[float | None] = []
        fz: list[float | None] = []
        for i in range(n):
            lw, lt, lc = [], [], []
            for lv in levels:
                z = zs[lv][i]
                lw.append((z, ws[lv][i]))
                lt.append((z, ts[lv][i]))
                lc.append((z, cs[lv][i]))
            w = at_height(elev_ft, lw)
            if w is not None and mode == "ca600" and 600 in levels:
                z6, w6 = lw[list(levels).index(600)]
                if w6 is not None and elev_ft < (z6 if z6 is not None else STD_600_FT):
                    w = max(w, w6)
            elif w is not None and mode == "next":
                for lv, (z, wl) in zip(levels, lw, strict=True):
                    zz = z if z is not None else STD_HEIGHT_M[lv] * FT_PER_M
                    if zz > elev_ft:
                        if wl is not None and zz - elev_ft <= reach_ft:
                            w = max(w, wl)
                        break
            wind.append(w)
            temp.append(at_height(elev_ft, lt, LAPSE_F_PER_FT))
            cloud.append(at_height(elev_ft, lc))
            fz.append(freezing_level_ft(lt))

        def col(var: str, kind: str, m: str = m) -> list[float | None]:
            return _clean(h.get(f"{var}_{m}") or [], n, SANE[kind], kind, rejects)

        out[m] = {
            "wind": wind,
            "temp": temp,
            "cloud": cloud,
            "fz": fz,
            "precip": col("precipitation", "precip"),
            "snow": col("snowfall", "snow"),
            "cape": col("cape", "cape"),
            "gust": col("wind_gusts_10m", "gust"),
        }
    return {"times": times, "models": out}


def model_day(
    series: dict[str, Any],
    day: date,
    tz: tzinfo,
    hours: tuple[int, int] = SUMMIT_HOURS,
    cloud_pct: float = LIMITS["cloud_pct"],
) -> dict[str, dict[str, Any]]:
    """{model: per-day summit numbers} (PeakWatch model_day, over summit_series).

    Over the summit hours, at summit height: wind (max), temp and chill (min), cloud_h
    (hours at or over cloud_pct, None without cloud data), fz (highest freezing level), gust
    (the model's surface gust, a note). Over 06-18: precip, snow, cape. snow72: the snow in
    the 72 h before 06:00, None when the pull covers fewer than 66 of those hours. A model
    short of any summit hour is left out.
    """
    times = series["times"]
    start, end = local_window(day, hours, tz)
    ws, we = local_window(day, WIDE_HOURS, tz)
    t0, t1 = local_window(day - timedelta(days=1), THAW_HOURS, tz)
    d0, d1 = local_window(day, DAWN_HOURS, tz)
    n_h = hours[1] - hours[0]
    out: dict[str, dict[str, Any]] = {}
    for m, s in series["models"].items():
        rows: list[tuple[float, float]] = []
        cloud: list[float] = []
        fzs: list[float] = []
        gusts: list[float] = []
        for i, t in enumerate(times):
            if not start <= t < end:
                continue
            if s["wind"][i] is not None and s["temp"][i] is not None:
                rows.append((s["wind"][i], s["temp"][i]))
            if s["cloud"][i] is not None:
                cloud.append(s["cloud"][i])
            if s["fz"][i] is not None:
                fzs.append(s["fz"][i])
            if s["gust"][i] is not None:
                gusts.append(s["gust"][i])
        if len(rows) < n_h:
            continue
        thaw = in_window(times, s["temp"], t0, t1)
        dawn = in_window(times, s["temp"], d0, d1)
        past = in_window(times, s["snow"], ws - timedelta(hours=STORM_H), ws)
        cape = in_window(times, s["cape"], ws, we)
        out[m] = {
            "wind": max(w for w, _ in rows),
            "temp": min(t for _, t in rows),
            "chill": min(wind_chill_f(t, w) for w, t in rows),
            "cloud_h": sum(1 for c in cloud if c >= cloud_pct) if len(cloud) >= n_h else None,
            "precip": sum(in_window(times, s["precip"], ws, we)),
            "snow": sum(in_window(times, s["snow"], ws, we)),
            "snow72": sum(past) if len(past) >= STORM_H - 6 else None,
            "cape": max(0.0, max(cape)) if cape else None,  # ECMWF sends -10 for no CAPE
            "thaw": max(thaw) if thaw else None,
            "dawn": min(dawn) if dawn else None,
            "fz": max(fzs) if fzs else None,
            "gust": max(gusts) if gusts else None,
        }
    return out


def valid_odds(m: Any) -> bool:
    """tools/peakwatch.py load_wind_odds's checks, on an in-memory dict."""
    try:
        lim = m["limit_mph"]
        ok = (
            isinstance(lim, int | float)
            and not isinstance(lim, bool)
            and math.isfinite(lim)
            and bool(m["bands"])
            and all(
                len(b["lead_days"]) == 2
                and all(isinstance(x, int) for x in b["lead_days"])
                and all(
                    isinstance(b[k], int | float) and math.isfinite(b[k])
                    for k in ("intercept", "coef_median", "coef_spread")
                )
                for b in m["bands"]
            )
        )
        if "coef_cool" in m or "cool_months" in m:
            c, months = m.get("coef_cool"), m.get("cool_months")
            ok = (
                ok
                and isinstance(c, int | float)
                and not isinstance(c, bool)
                and math.isfinite(c)
                and isinstance(months, list)
                and bool(months)
                and all(
                    isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 12
                    for x in months
                )
            )
        return bool(ok)
    except (KeyError, TypeError):
        return False


def _odds_band(model: dict[str, Any], lead_day: int) -> dict[str, Any] | None:
    bands = model["bands"]
    hit = next((b for b in bands if b["lead_days"][0] <= lead_day <= b["lead_days"][1]), None)
    if hit is not None:
        return dict(hit)
    return dict(bands[-1]) if lead_day > bands[-1]["lead_days"][1] else None


def wind_odds(
    model: dict[str, Any] | None,
    mods: dict[str, dict[str, Any]],
    lead_h: float | None,
    day: date | None = None,
) -> float | None:
    """P(the wind reaches model['limit_mph']) from the models' median and spread at this lead.

    PeakWatch: needs 2 models; a lead between two lead days takes the higher odds; a model
    with coef_cool adds it when `day` falls in its cool_months.
    """
    if not model or len(mods) < 2 or lead_h is None:
        return None
    winds = [m["wind"] for m in mods.values()]
    med, spread = statistics.median(winds), max(winds) - min(winds)
    cool = (
        float(model.get("coef_cool", 0.0))
        if day is not None and day.month in model.get("cool_months", ())
        else 0.0
    )
    ps: list[float] = []
    for lead_day in {max(0, int(lead_h // 24)), max(0, int((lead_h + 6) // 24))}:
        band = _odds_band(model, lead_day)
        if band:
            z = band["intercept"] + band["coef_median"] * med + band["coef_spread"] * spread
            ps.append(1 / (1 + math.exp(-max(-40.0, min(40.0, z + cool)))))
    return max(ps) if ps else None


def summit_p(wind_kmh: float) -> float:
    """WM-P's share of Oct-Nov middays with the summit at 35 km/h or more, at this wind."""
    return next(p for top, p in SUMMIT_P_TABLE if wind_kmh < top)


def members(hourly: dict[str, Any], var: str) -> dict[int, list[Any]]:
    """{member id: values} for an Open-Meteo ensemble variable (control is member 0)."""
    out = {}
    for key, vals in hourly.items():
        hit = re.fullmatch(rf"{re.escape(var)}(?:_member(\d+))?", key)
        if hit:
            out[int(hit.group(1) or 0)] = vals
    return out


def ens_p(
    block: Any,
    day: date,
    tz: tzinfo,
    level: int,
    calibrated: bool,
    hours: tuple[int, int] = SUMMIT_HOURS,
    limit_mph: float = LIMITS["wind_warn_mph"],
) -> tuple[float | None, int]:
    """ECMWF ensemble strong-wind odds over the summit hours, and the members read.

    Calibrated (PeakWatch): the mean over members of the WM-P summit probability at each
    member's peak 600 hPa wind. Elsewhere: the share of members whose peak wind at `level`
    reaches the wind line, raw and uncalibrated.
    """
    if not isinstance(block, dict) or not isinstance(block.get("hourly"), dict):
        return None, 0
    h = block["hourly"]
    times = times_of(h)
    start, end = local_window(day, hours, tz)
    ps = []
    for vals in members(h, f"wind_speed_{level}hPa").values():
        win = [sane(v, "wind") for v in in_window(times, vals, start, end)]
        good = [x for x in win if x is not None]
        if len(good) >= hours[1] - hours[0]:
            if calibrated:
                ps.append(summit_p(max(good) * 1.609344))
            else:
                ps.append(1.0 if max(good) >= limit_mph else 0.0)
    return (sum(ps) / len(ps), len(ps)) if ps else (None, 0)


def aqi_day(block: Any, day: date, tz: tzinfo, var: str) -> float | None:
    """The day's highest air-quality index over 06-18 local, or None past the forecast."""
    if not isinstance(block, dict) or not isinstance(block.get("hourly"), dict):
        return None
    h = block["hourly"]
    start, end = local_window(day, WIDE_HOURS, tz)
    vals = [sane(v, "aqi") for v in in_window(times_of(h), h.get(var), start, end)]
    good = [x for x in vals if x is not None]
    return max(good) if good else None


# --------------------------------------------------------------------------
# Avalanche zones, alerts, fires, NWS text (PeakWatch)
# --------------------------------------------------------------------------


def avy_zone(lat: float, lon: float, features: Any) -> dict[str, Any] | None:
    """The avalanche.org zone containing the point, as a dict, or None."""
    for f in features or []:
        if isinstance(f, dict) and in_geometry(lat, lon, f.get("geometry")):
            pr = f.get("properties") or {}
            return {
                "center": pr.get("center_id"),
                "zone": pr.get("name"),
                "level": pr.get("danger_level"),
                "off_season": pr.get("off_season"),
                "start": pr.get("start_date"),
                "end": pr.get("end_date"),
                "link": pr.get("link"),
                "advice": pr.get("travel_advice"),
            }
    return None


def avy_days(z: dict[str, Any] | None, days: Sequence[date]) -> set[date]:
    """The days a zone's rating is valid for: start to end date (an end at midnight
    excludes that day), else today."""
    if not z or not isinstance(z.get("level"), int) or z["level"] < 1:
        return set()
    try:
        s, e = datetime.fromisoformat(z["start"]), datetime.fromisoformat(z["end"])
    except (TypeError, ValueError):
        return {days[0]}
    last = e.date() - timedelta(days=1) if e.time() == time(0) else e.date()
    return {d for d in days if s.date() <= d <= last}


def strip_html(s: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def danger_max(row: dict[str, Any]) -> int | None:
    """The highest rated band of one avalanche.org danger row, or None when none is rated."""
    vals = [
        int(v)
        for v in (row.get(k) for k in ("lower", "middle", "upper"))
        if isinstance(v, int) and v >= 1
    ]
    return max(vals) if vals else None


def product_day(published: str, tz: tzinfo) -> date:
    """The day a forecast's "current" rating is for: its publish day, or the next one when
    issued at or after noon (local to the point)."""
    t = utc(published).astimezone(tz)
    return t.date() + timedelta(days=1 if t.hour >= 12 else 0)


def avy_products(centers: Iterable[str | None], now: datetime, tz: tzinfo) -> dict[str, Any]:
    """{center: its newest avalanche.org product}, as PeakWatch reads it.

    A live forecast carries the danger by elevation band for its day ("current") and the
    next ("tomorrow") and the avalanche problems; a pre-season summary carries text only. A
    center whose list cannot be read is left out.
    """
    out: dict[str, Any] = {}
    today = now.astimezone(tz).date()
    for c in sorted(x for x in centers if x):
        params = {
            "avalanche_center_id": c,
            "date_start": (today - timedelta(days=21)).isoformat(),
            "date_end": (today + timedelta(days=1)).isoformat(),
        }
        got, read = _cached_get(("avy-list", c, today.isoformat()), "avy-list", AVY_LIST_URL,
                                params, timeout=TIMEOUT_S["avy"])
        if not read.ok or not isinstance(got, list):
            continue
        prods = [
            x
            for x in got
            if isinstance(x, dict)
            and x.get("product_type") in ("forecast", "summary")
            and x.get("published_time")
        ]
        live = [x for x in prods if x.get("expires_time") and utc(x["expires_time"]) > now]
        x = max(live or prods, key=lambda x: utc(x["published_time"]), default=None)
        item: dict[str, Any] = {
            "center": c, "live": bool(live), "type": None, "current": None, "tomorrow": None,
            "problems": [], "bottom_line": "", "published": None, "expires": None, "day": None,
        }
        if x:
            item.update(
                type=x["product_type"],
                published=x["published_time"],
                expires=x.get("expires_time"),
                day=product_day(x["published_time"], tz),
                bottom_line=strip_html(x.get("bottom_line"))[:600],
            )
        if x and live and x["product_type"] == "forecast":
            d, dread = _cached_get(("avy-prod", x.get("id")), "avy-prod",
                                   AVY_PRODUCT_URL.format(x.get("id")), timeout=TIMEOUT_S["avy"])
            if dread.ok and isinstance(d, dict):
                for row in d.get("danger") or []:
                    if isinstance(row, dict) and row.get("valid_day") in ("current", "tomorrow"):
                        item[row["valid_day"]] = danger_max(row)
                item["problems"] = [
                    q["name"]
                    for q in d.get("forecast_avalanche_problems") or []
                    if isinstance(q, dict) and q.get("name")
                ]
                item["bottom_line"] = strip_html(d.get("bottom_line"))[:600] or item["bottom_line"]
        out[c] = item
    return out


def avy_for_day(
    zone: dict[str, Any] | None, zone_days: set[date], prod: dict[str, Any] | None, day: date
) -> dict[str, Any] | None:
    """{center, level, problems} rating one day: a live forecast's band ratings win (they
    reach tomorrow); otherwise the map layer's rating on the days its range covers."""
    if zone is None:
        return None
    if prod and prod.get("live") and prod.get("type") == "forecast" and prod.get("day"):
        lvl = (
            prod.get("current")
            if day == prod["day"]
            else prod.get("tomorrow")
            if day == prod["day"] + timedelta(days=1)
            else None
        )
        if lvl is not None:
            return {"center": zone["center"], "level": lvl, "problems": prod.get("problems") or []}
    if day in zone_days:
        return {"center": zone["center"], "level": zone["level"], "problems": []}
    return None


def alert_level(event: str, fail_events: Iterable[str] = ALERT_FAIL) -> str:
    if event in fail_events:
        return "fail"
    return "warn" if re.search(r"Warning|Watch|Advisory", event) else "info"


def alerts_for(
    point: dict[str, Any], alerts: Iterable[Any], start: datetime, end: datetime
) -> list[tuple[str, str]]:
    """[(event, headline)] of active alerts on this NWS point's zones overlapping [start, end)."""
    zones = {point.get("forecastZone"), point.get("county"), point.get("fireWeatherZone")}
    zones.discard(None)
    out = []
    for a in alerts or []:
        pr = a.get("properties", {}) if isinstance(a, dict) else {}
        if not zones & set(pr.get("affectedZones") or []):
            continue
        a0 = pr.get("onset") or pr.get("effective")
        a1 = pr.get("ends") or pr.get("expires")
        if (a0 and utc(a0) >= end) or (a1 and utc(a1) <= start):
            continue
        out.append((str(pr.get("event", "?")), str(pr.get("headline") or "")))
    return out


def fires_near(
    lat: float, lon: float, fires: Any, now: datetime, max_age_days: float
) -> list[tuple[str, float, Any, Any]]:
    """[(name, miles, acres, % contained)] of uncontained wildfires, nearest first."""
    out = []
    for f in fires or []:
        if not isinstance(f, dict):
            continue
        a, g = f.get("attributes") or {}, f.get("geometry") or {}
        if g.get("x") is None or g.get("y") is None or (a.get("PercentContained") or 0) >= 100:
            continue
        found = a.get("FireDiscoveryDateTime")
        if isinstance(found, int | float) and now - datetime.fromtimestamp(
            found / 1000, UTC
        ) > timedelta(days=max_age_days):
            continue
        mi = km(lat, lon, g["y"], g["x"]) / 1.609344
        out.append((str(a.get("IncidentName", "?")), mi, a.get("IncidentSize"),
                    a.get("PercentContained")))
    return sorted(out, key=lambda x: x[1])


def nws_day(fc: Any, day: date) -> str | None:
    """The daytime period for a local date: 'Sunny, 18F, W 25 to 35 mph', or None."""
    periods = fc.get("periods") if isinstance(fc, dict) else None
    for per in periods or []:
        try:
            start = datetime.fromisoformat(per["startTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.date() == day and per.get("isDaytime"):
            return (
                f"{per.get('shortForecast', '')}, {per.get('temperature')}"
                f"{per.get('temperatureUnit', '')}, {per.get('windSpeed', '')}"
            )
    return None


# --------------------------------------------------------------------------
# Scoring (PeakWatch assess, OpenSnow inputs removed, the AQI scale added)
# --------------------------------------------------------------------------

Flag = tuple[str, str]
P_CALIBRATED = "summit-wind p {:.2f} (ECMWF ens, White Mountain table, a proxy)"


def assess(
    L: dict[str, float],
    mods: dict[str, dict[str, Any]] | None = None,
    p: float | None = None,
    nws_text: str | None = None,
    alerts: Iterable[tuple[str, str]] = (),
    fail_events: Iterable[str] = ALERT_FAIL,
    avy: dict[str, Any] | None = None,
    fires: Iterable[tuple[str, float, Any, Any]] = (),
    aqi: float | None = None,
    aqi_scale: str = "US AQI",
    gaps: Iterable[tuple[str, str]] = (),
    required: Iterable[str] = (),
    extra: Iterable[Flag] = (),
    odds: tuple[float, float] | None = None,
    summit_odds: tuple[float, float] | None = None,
    p_template: str = P_CALIBRATED,
) -> tuple[str, list[Flag]]:
    """(verdict, [(level, text)]) for one day. Any fail is NO, any warn is MARG; no model
    forecast is '?'. A gap in a `required` group is a warn, so a day is GO only when every
    required source was read and clear; any other gap is an info note."""
    flags: list[Flag] = []
    required = set(required)
    fail_events = set(fail_events)

    def add(level: str, text: str) -> None:
        flags.append((level, text))

    def above(v: float | None, warn: float, fail: float, template: str) -> None:
        if v is not None and v >= warn:
            add("fail" if v >= fail else "warn", template.format(v))

    def below(v: float | None, warn: float, fail: float, template: str) -> None:
        if v is not None and v <= warn:
            add("fail" if v <= fail else "warn", template.format(v))

    if mods:
        wind = statistics.median(m["wind"] for m in mods.values())
        chill = statistics.median(m["chill"] for m in mods.values())
        snow = max(m["snow"] for m in mods.values())
        precip = max(m["precip"] for m in mods.values())
        above(wind, L["wind_warn_mph"], L["wind_fail_mph"], "wind {:.0f} mph (models)")
        below(chill, L["chill_warn_f"], L["chill_fail_f"], "chill {:.0f} F (models)")
        if snow >= L["snow_warn_in"]:
            add("fail" if snow >= L["snow_fail_in"] else "warn", f"snow {snow:.1f} in (models)")
        elif precip >= L["precip_warn_in"]:
            add("warn", f"precip {precip:.2f} in (models)")
        for m, x in mods.items():
            name = SHORT.get(m, m)
            if x["wind"] >= L["wind_fail_mph"] > wind:
                add("warn", f"{name} alone has wind {x['wind']:.0f} mph (median {wind:.0f})")
            if x["chill"] <= L["chill_fail_f"] < chill:
                add("warn", f"{name} alone has chill {x['chill']:.0f} F (median {chill:.0f})")
        clouds = [m["cloud_h"] for m in mods.values() if m.get("cloud_h") is not None]
        if clouds and statistics.median(clouds) >= L.get("cloud_warn_h", 2):
            add("warn", f"summit in cloud {statistics.median(clouds):.1f} h of the summit "
                        "hours (models): whiteout")
        capes = [m["cape"] for m in mods.values() if m.get("cape") is not None]
        if capes:
            above(statistics.median(capes), L.get("cape_warn", 250), L.get("cape_fail", 800),
                  "CAPE {:.0f} J/kg (models): thunderstorm fuel")
        storm = [m["snow72"] for m in mods.values() if m.get("snow72") is not None]
        if storm:
            above(max(storm), L.get("storm_snow_warn_in", 6), L.get("storm_snow_fail_in", 12),
                  "{:.0f} in of snow in the 72 h before (models)")
        else:
            add("info", "snow in the 72 h before not known (models do not reach back)")
        thaw = [m["thaw"] for m in mods.values() if m.get("thaw") is not None]
        dawn = [m["dawn"] for m in mods.values() if m.get("dawn") is not None]
        if thaw and dawn and statistics.median(thaw) > 32 > statistics.median(dawn):
            add("info", f"melt-freeze: summit {statistics.median(thaw):.0f} F the afternoon "
                        f"before, {statistics.median(dawn):.0f} F at dawn; any snow is hard "
                        "and icy early")
        if (
            odds
            and odds[0] is not None
            and "wind_odds_warn" in L
            and odds[0] >= L["wind_odds_warn"]
            and wind < L["wind_warn_mph"]
        ):
            add("warn", f"wind odds {odds[0]:.0%} of {odds[1]:g} mph or more (models, "
                        "backtested at this lead)")
        if (
            summit_odds
            and summit_odds[0] is not None
            and "summit_odds_warn" in L
            and summit_odds[0] >= L["summit_odds_warn"]
            and wind < L["wind_warn_mph"]
        ):
            add("warn", f"summit wind odds {summit_odds[0]:.0%} of {summit_odds[1]:g} mph or "
                        "more (fitted to a summit anemometer, at this lead and season)")
    above(p, L["p_warn"], L["p_fail"], p_template)
    if nws_text and re.search(r"thunder", nws_text, re.I):
        add("warn", "thunder in the NWS forecast")
    for event, _ in alerts:
        add(alert_level(event, fail_events), event)
    lvl = avy.get("level") if avy is not None else None
    if avy is not None and isinstance(lvl, int) and lvl >= L["avy_warn_level"]:
        probs = f": {', '.join(avy.get('problems') or [])}" if avy.get("problems") else ""
        text = f"avalanche {AVY_NAMES.get(lvl, lvl)} ({avy['center']})"
        add("fail" if lvl >= L["avy_fail_level"] else "warn", text + probs)
    for name, mi, _, _ in fires:
        if mi <= L["fire_warn_mi"]:
            add("fail" if mi <= L["fire_fail_mi"] else "warn", f"wildfire {name} {mi:.0f} mi")
    if aqi_scale == "European AQI":
        above(aqi, L["eaqi_warn"], L["eaqi_fail"], "European AQI {:.0f} (smoke, dust)")
    else:
        above(aqi, L["aqi_warn"], L["aqi_fail"], "US AQI {:.0f} (smoke)")
    for group, text in gaps:
        add("warn" if group in required else "info", text)
    for level, text in extra:
        add(level, text)
    if not mods:
        return "?", flags + [("info", "no model forecast")]
    levels = {lv for lv, _ in flags}
    return ("NO" if "fail" in levels else "MARG" if "warn" in levels else "GO"), flags


def top_reason(verdict: str, flags: Sequence[Flag], numbers: dict[str, Any]) -> str:
    """The one line a map marker shows: the first NO reason, else the first MARG reason."""
    for want in ("fail", "warn"):
        for level, text in flags:
            if level == want:
                return text
    if verdict == "GO":
        w, c = numbers.get("wind_mph"), numbers.get("chill_f")
        if w is not None and c is not None:
            return f"no limit crossed: wind {w:.0f} mph, chill {c:.0f} F (models)"
        return "no limit crossed"
    if verdict == "?":
        return "no model forecast"
    return next((t for lv, t in flags if lv == "info"), "")


# --------------------------------------------------------------------------
# Fetching for a set of points
# --------------------------------------------------------------------------


@dataclass
class Point:
    lat: float
    lon: float
    elev_m: float | None
    name: str = ""
    elev_source: str = "given"
    cell: tuple[float, float] = (0.0, 0.0)
    band: tuple[int, int, int] = (700, 600, 500)
    ens_level: int = 600
    in_region: bool = False
    calibrated: bool = False
    maybe_us: bool = False

    def __post_init__(self) -> None:
        self.cell = round_point(self.lat, self.lon)
        self.in_region = in_cal_region(self.lat, self.lon)
        self.maybe_us = maybe_us(self.lat, self.lon)
        self.set_elev(self.elev_m, self.elev_source)

    def set_elev(self, elev_m: float | None, source: str) -> None:
        """Pick the levels for this height; the calibrations need a band reaching 600 hPa."""
        self.elev_m, self.elev_source = elev_m, source
        if elev_m is not None:
            self.band = level_band(elev_m)
            self.calibrated = self.in_region and 600 in self.band
            self.ens_level = ensemble_level(elev_m, self.calibrated)


@dataclass
class Gathered:
    det: Any = None
    det_read: Read = field(default_factory=lambda: Read("failed"))
    ens: Any = None
    ens_read: Read = field(default_factory=lambda: Read("failed"))
    aq: Any = None
    aq_read: Read = field(default_factory=lambda: Read("failed"))
    in_us: bool | None = False
    nws_point: dict[str, Any] | None = None
    nws_point_read: Read = field(default_factory=lambda: Read("skipped"))
    alerts: list[Any] | None = None
    alerts_read: Read = field(default_factory=lambda: Read("skipped"))
    nws_fc: Any = None
    fc_read: Read = field(default_factory=lambda: Read("skipped"))
    avy_features: Any = None
    avy_read: Read = field(default_factory=lambda: Read("skipped"))
    avy_prods: dict[str, Any] = field(default_factory=dict)
    fires: Any = None
    fires_read: Read = field(default_factory=lambda: Read("skipped"))


def _om_common(cells: Sequence[tuple[float, float]]) -> dict[str, Any]:
    return {
        "latitude": ",".join(f"{c[0]:.2f}" for c in cells),
        "longitude": ",".join(f"{c[1]:.2f}" for c in cells),
        "timezone": "auto",
        "timeformat": "unixtime",
    }


def forecast_params(band: tuple[int, ...]) -> dict[str, Any]:
    """The deterministic pull: the same for the drawer and the verdicts, so they share it."""
    hourly = [
        f"{v}_{lv}hPa"
        for lv in band
        for v in ("wind_speed", "temperature", "geopotential_height", "cloud_cover")
    ]
    hourly += ["precipitation", "snowfall", "cape", "wind_gusts_10m"]
    return {
        "models": ",".join(MODELS),
        "hourly": ",".join(hourly),
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "forecast_days": FORECAST_DAYS,
        "past_days": PAST_DAYS,
    }


def ensemble_params(level: int) -> dict[str, Any]:
    return {
        "models": "ecmwf_ifs025",
        "hourly": f"wind_speed_{level}hPa",
        "wind_speed_unit": "mph",
        "forecast_days": FORECAST_DAYS,
    }


def air_params(_group: Any) -> dict[str, Any]:
    return {"hourly": "us_aqi,european_aqi", "forecast_days": AIR_DAYS}


def _om_batch(
    kind: str,
    url: str,
    groups: dict[Any, list[tuple[float, float]]],
    params_for: Callable[[Any], dict[str, Any]],
    run: float | None,
) -> dict[tuple[Any, ...], tuple[Any, Read]]:
    """Open-Meteo for many cells at once: comma-separated lists, BATCH cells a request.

    groups maps a variable set (a level band, an ensemble level) to its cells. Each cell is
    cached on its own under (kind, lat, lon, group); only cells without a fresh copy are
    asked for.
    """
    now = _now().timestamp()
    out: dict[tuple[Any, ...], tuple[Any, Read]] = {}
    for grp, cells in groups.items():
        want = []
        for cell in dict.fromkeys(cells):
            key = (kind, cell[0], cell[1], grp)
            entry = CACHE.get(key)
            recent = CACHE.recent_failure(key, now)
            if entry is not None and _fresh(entry, kind, now, run):
                out[key] = (entry.data, Read("cached", entry.fetched, entry.run))
            elif recent is not None:
                out[key] = _fallback(key, kind, now, recent)
            else:
                want.append(cell)
        for i in range(0, len(want), BATCH):
            chunk = want[i : i + BATCH]
            COUNTS[kind] += 1
            try:
                got = _get_json(url, {**_om_common(chunk), **params_for(grp)}, TIMEOUT_S["om"])
                blocks = got if isinstance(got, list) else [got]
                if len(blocks) != len(chunk) or not all(isinstance(b, dict) for b in blocks):
                    raise UpstreamError(f"{len(blocks)} answers for {len(chunk)} points")
            except UpstreamError as exc:
                for cell in chunk:
                    key = (kind, cell[0], cell[1], grp)
                    CACHE.fail(key, now, str(exc))
                    out[key] = _fallback(key, kind, now, str(exc))
                continue
            for cell, block in zip(chunk, blocks, strict=True):
                key = (kind, cell[0], cell[1], grp)
                CACHE.put(key, block, now, run)
                out[key] = (block, Read("fresh", now, run))
    return out


ELEV_SOURCE = "Open-Meteo elevation (90 m DEM)"


def resolve_elevations(points: Sequence[Point]) -> None:
    """Fill a missing summit height from Open-Meteo's elevation API, batched by 100."""
    missing = []
    for pt in points:
        if pt.elev_m is not None:
            continue
        entry = CACHE.get(("om-elev", *pt.cell))
        if entry is not None:
            pt.set_elev(float(entry.data), ELEV_SOURCE)
        else:
            missing.append(pt)
    if not missing:
        return
    now = _now().timestamp()
    cells = list(dict.fromkeys(pt.cell for pt in missing))
    for i in range(0, len(cells), 100):
        chunk = cells[i : i + 100]
        COUNTS["om-elev"] += 1
        params = {
            "latitude": ",".join(f"{c[0]:.2f}" for c in chunk),
            "longitude": ",".join(f"{c[1]:.2f}" for c in chunk),
        }
        try:
            got = _get_json(OM_ELEVATION_URL, params, TIMEOUT_S["om"])
            vals = got.get("elevation") if isinstance(got, dict) else None
            if not isinstance(vals, list) or len(vals) != len(chunk):
                raise UpstreamError("elevation answer malformed")
        except UpstreamError:
            continue
        for cell, v in zip(chunk, vals, strict=True):
            if isinstance(v, int | float) and math.isfinite(v):
                CACHE.put(("om-elev", *cell), float(v), now, None)
    for pt in missing:
        entry = CACHE.get(("om-elev", *pt.cell))
        if entry is not None:
            pt.set_elev(float(entry.data), ELEV_SOURCE)


def zone_states(props: dict[str, Any]) -> set[str]:
    """Two-letter areas of the point's NWS zones (.../CAZ519 -> CA), for the alert feeds."""
    out = set()
    for k in ("forecastZone", "county", "fireWeatherZone"):
        v = props.get(k)
        if isinstance(v, str):
            hit = re.search(r"/([A-Z]{2})[CZ]\d{3}$", v)
            if hit:
                out.add(hit.group(1))
    return out


def _gkey(pt: Point) -> tuple[float, float, float]:
    return (pt.cell[0], pt.cell[1], round(pt.elev_m or 0.0, 1))


def gather(points: Sequence[Point]) -> dict[tuple[float, float, float], Gathered]:
    """Every source for every point, batched and cached, keyed by _gkey. One at a time, so
    two requests for the same cells share one upstream fetch."""
    with _FETCH_LOCK:
        return _gather(points)


def _gather(points: Sequence[Point]) -> dict[tuple[float, float, float], Gathered]:
    resolve_elevations(points)
    pts = [pt for pt in points if pt.elev_m is not None]
    out: dict[tuple[float, float, float], Gathered] = {}
    if not pts:
        return out
    now = _now()
    fc_run = _model_run(OM_META_URL)
    ens_run = _model_run(OM_ENS_META_URL)
    by_band: dict[Any, list[tuple[float, float]]] = {}
    by_lvl: dict[Any, list[tuple[float, float]]] = {}
    for pt in pts:
        by_band.setdefault(pt.band, []).append(pt.cell)
        by_lvl.setdefault(pt.ens_level, []).append(pt.cell)
    det = _om_batch("om-fc", OM_FORECAST_URL, by_band, forecast_params, fc_run)
    ens = _om_batch("om-ens", OM_ENSEMBLE_URL, by_lvl, ensemble_params, ens_run)
    aq = _om_batch("om-aq", OM_AIR_URL, {"aqi": [pt.cell for pt in pts]}, air_params, None)
    none: tuple[Any, Read] = (None, Read("failed", error="not fetched"))
    for pt in pts:
        g = Gathered()
        lat, lon = pt.cell
        g.det, g.det_read = det.get(("om-fc", lat, lon, pt.band), none)
        g.ens, g.ens_read = ens.get(("om-ens", lat, lon, pt.ens_level), none)
        g.aq, g.aq_read = aq.get(("om-aq", lat, lon, "aqi"), none)
        out[_gkey(pt)] = g

    us_pts = [pt for pt in pts if pt.maybe_us]
    if us_pts:
        _gather_us(us_pts, out, now)
    return out


def _gather_us(
    us_pts: Sequence[Point], out: dict[tuple[float, float, float], Gathered], now: datetime
) -> None:
    """The NWS, avalanche.org and NIFC reads for points the NWS may cover."""

    def point_of(pt: Point) -> tuple[Point, Any, Read]:
        data, read = _cached_get(
            ("nws-point", *pt.cell),
            "nws-point",
            NWS_POINT_URL.format(lat=pt.cell[0], lon=pt.cell[1]),
            timeout=TIMEOUT_S["nws"],
            keep_status=(404,),
        )
        return pt, data, read

    with ThreadPoolExecutor(6) as ex:
        looked = list(ex.map(point_of, us_pts))
    states: set[str] = set()
    fc_urls: set[str] = set()
    for pt, data, read in looked:
        g = out[_gkey(pt)]
        g.nws_point_read = read
        if not read.ok:
            g.in_us = None  # inside a US box but the NWS did not answer: unknown
            continue
        if isinstance(data, dict) and data.get("__status__") == 404:
            g.in_us = False
            continue
        props = data.get("properties") if isinstance(data, dict) else None
        if not isinstance(props, dict):
            g.in_us = None
            g.nws_point_read = Read("failed", read.fetched, None, "NWS point answer malformed")
            continue
        g.in_us, g.nws_point = True, props
        states |= zone_states(props)
        if isinstance(props.get("forecast"), str):
            fc_urls.add(props["forecast"])
    if not any(out[_gkey(pt)].in_us is not False for pt in us_pts):
        return

    feeds = {
        st: _cached_get(("nws-alerts", st), "nws-alerts", NWS_ALERTS_URL, {"area": st},
                        timeout=TIMEOUT_S["nws"])
        for st in sorted(states)
    }

    def forecast_of(url: str) -> tuple[str, tuple[Any, Read]]:
        return url, _cached_get(("nws-fc", url), "nws-fc", url, timeout=TIMEOUT_S["nws"])

    with ThreadPoolExecutor(4) as ex:
        fcs = dict(ex.map(forecast_of, sorted(fc_urls)))

    avy_data, avy_read = _cached_get(("avy",), "avy", AVY_URL, timeout=TIMEOUT_S["avy"])
    features = avy_data.get("features") if isinstance(avy_data, dict) else None
    if avy_read.ok and not isinstance(features, list):
        avy_read = Read("failed", avy_read.fetched, None, "avalanche.org answer malformed")
    fire_params = {
        "where": "IncidentTypeCategory='WF'",
        "outSR": 4326,
        "f": "json",
        "outFields": "IncidentName,IncidentSize,PercentContained,FireDiscoveryDateTime",
        "returnGeometry": "true",
    }
    fires_data, fires_read = _cached_get(("fires",), "fires", FIRES_URL, fire_params,
                                         timeout=TIMEOUT_S["fires"])
    fire_list = fires_data.get("features") if isinstance(fires_data, dict) else None
    if fires_read.ok and not isinstance(fire_list, list):
        fires_read = Read("failed", fires_read.fetched, None, "NIFC answer malformed")

    for pt in us_pts:
        g = out[_gkey(pt)]
        if g.in_us is False:
            continue
        g.avy_features, g.avy_read = (features if avy_read.ok else None), avy_read
        g.fires, g.fires_read = (fire_list if fires_read.ok else None), fires_read
        if g.nws_point is None:
            continue
        mine = [feeds[s] for s in sorted(zone_states(g.nws_point)) if s in feeds]
        if mine and all(
            r.ok and isinstance(d, dict) and isinstance(d.get("features"), list) for d, r in mine
        ):
            g.alerts = [a for d, _ in mine for a in d["features"]]
            g.alerts_read = min((r for _, r in mine), key=lambda r: r.fetched or 0.0)
        else:
            bad = next((r for _, r in mine if not r.ok), None)
            err = bad.error if bad else "no NWS alert zone for this point"
            g.alerts_read = Read("failed", None, None, err)
        url = g.nws_point.get("forecast")
        if isinstance(url, str) and url in fcs:
            data, read = fcs[url]
            props = data.get("properties") if isinstance(data, dict) else None
            g.nws_fc, g.fc_read = (props if read.ok and isinstance(props, dict) else None), read
        else:
            g.fc_read = Read("failed", None, None, "no NWS point forecast for this point")
        zone = avy_zone(pt.lat, pt.lon, g.avy_features)
        if zone and zone.get("center"):
            tz = zone_of(g.det or g.ens or g.aq, pt.lon).tz
            g.avy_prods = avy_products([zone["center"]], now, tz)


# --------------------------------------------------------------------------
# Scoring a point
# --------------------------------------------------------------------------


def _r(x: Any, nd: int = 1) -> float | None:
    return None if x is None else round(float(x), nd)


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _med(vals: Iterable[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return float(statistics.median(v)) if v else None


def _hhmm(t: datetime | None, tz: tzinfo) -> str | None:
    return None if t is None else t.astimezone(tz).strftime("%H:%M")


def _source(
    sid: str, name: str, read: Read, required: bool, now: float, note: str | None = None
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": name,
        "required": required,
        "read": read.ok,
        "state": read.state,
        "fetched": _iso(read.fetched),
        "age_min": None if read.fetched is None else max(0, round((now - read.fetched) / 60)),
        "run": _iso(read.run),
        "error": read.error,
        "note": note or read.note,
    }


def score_point(pt: Point, g: Gathered, days: int = 7, with_hourly: bool = True) -> dict[str, Any]:
    """The full answer for one point: per day the verdict, reasons, numbers and reads; the
    hourly series for the meteogram; and every source with its fetch time and state."""
    now = _now()
    nowts = now.timestamp()
    rejects: Counter[str] = Counter()
    det = g.det if g.det_read.ok else None
    ens = g.ens if g.ens_read.ok else None
    aq = g.aq if g.aq_read.ok else None
    zone = zone_of(det or ens or aq, pt.lon)
    tz = zone.tz
    elev_m = float(pt.elev_m or 0.0)
    elev_ft = elev_m * FT_PER_M
    mode = "ca600" if pt.calibrated else "next"
    today = now.astimezone(tz).date()
    day_list = [today + timedelta(days=i) for i in range(days)]
    until = datetime.combine(day_list[-1] + timedelta(days=1), time(0), tz).astimezone(UTC)
    series = summit_series(det, elev_ft, pt.band, mode, rejects, until)
    us = bool(pt.maybe_us and g.in_us is not False)
    aqi_var, aqi_scale = ("us_aqi", "US AQI") if us else ("european_aqi", "European AQI")
    required = {"models", "om-ens"} | ({"nws-alerts", "avy"} if us else set())
    alerts_ok = g.alerts_read.ok and g.alerts is not None
    avy_ok = g.avy_read.ok
    zone_avy = avy_zone(pt.lat, pt.lon, g.avy_features) if us and avy_ok else None
    zone_days = avy_days(zone_avy, day_list)
    prod = g.avy_prods.get(zone_avy["center"]) if zone_avy else None
    near = fires_near(pt.lat, pt.lon, g.fires, now, LIMITS["fire_max_age_days"]) if us else []
    odds_model = WIND_ODDS if pt.calibrated and valid_odds(WIND_ODDS) else None
    summit_model = SUMMIT_ODDS if pt.calibrated and valid_odds(SUMMIT_ODDS) else None
    p_template = P_CALIBRATED if pt.calibrated else (
        "strong-wind share {:.2f} (ECMWF ens members at " f"{LIMITS['wind_warn_mph']:g} mph "
        f"at {pt.ens_level} hPa, not calibrated)"
    )
    out_days: list[dict[str, Any]] = []
    for i, day in enumerate(day_list):
        start, end = local_window(day, SUMMIT_HOURS, tz)
        mods = model_day(series, day, tz)
        pr, n_mem = ens_p(ens, day, tz, pt.ens_level, pt.calibrated)
        aqi = aqi_day(aq, day, tz, aqi_var)
        al = (
            alerts_for(g.nws_point or {}, g.alerts or [], *local_window(day, ALERT_HOURS, tz))
            if us and alerts_ok
            else []
        )
        text = nws_day(g.nws_fc, day) if us else None
        avd = avy_for_day(zone_avy, zone_days, prod, day) if us else None
        gaps: list[tuple[str, str]] = []
        extra: list[Flag] = []
        if len(mods) < 2:
            gaps.append(("models", f"{len(mods)} of {len(MODELS)} models read"))
        if pr is None:
            gaps.append(("om-ens", "ECMWF ensemble not read"))
        if us:
            if g.in_us is None:
                gaps.append(("nws-alerts", "NWS alerts not read (the NWS point lookup failed)"))
            elif not alerts_ok or not (g.nws_point or {}).get("forecastZone"):
                gaps.append(("nws-alerts", "NWS alerts not read"))
            if i <= 1 and not avy_ok:
                gaps.append(("avy", "avalanche.org not read"))
            if not g.fires_read.ok:
                gaps.append(("fires", "NIFC wildfires not read"))
            if not text and g.in_us:
                gaps.append(("nws-fc", "NWS point forecast not read for this day"))
            if avd is None and avy_ok and i <= 1:
                where = (
                    f"{zone_avy['center']} has no rating for this day"
                    if zone_avy
                    else "outside every avalanche forecast zone"
                )
                extra.append(("info", f"no avalanche rating ({where}): judge the snowpack "
                                      "yourself"))
        elif i <= 1:
            extra.append(("info", "no avalanche forecast checked outside the US: judge the "
                                  "snowpack yourself"))
        if not g.aq_read.ok:
            gaps.append(("om-aq", "air quality not read"))
        elif aqi is None:
            extra.append(("info", "no air-quality forecast this far out"))
        if not pt.calibrated:
            extra.append(("info", "wind odds not calibrated here: the plain median limits "
                                  "decide"))
        mid = start + (end - start) / 2
        lead_h = (mid - now).total_seconds() / 3600 + float(WIND_ODDS["run_age_h"])
        wp = wind_odds(odds_model, mods, lead_h)
        sp = wind_odds(summit_model, mods, lead_h, day)
        fzs = _med(m.get("fz") for m in mods.values())
        if fzs is not None:
            rel = fzs - elev_ft
            side = "above" if rel >= 0 else "below"
            extra.append(("info", f"freezing level {fzs:,.0f} ft ({fzs * 0.3048:,.0f} m), "
                                  f"{abs(rel):,.0f} ft {side} the summit (models)"))
        verdict, flags = assess(
            LIMITS,
            mods,
            pr,
            text,
            al,
            ALERT_FAIL,
            avd,
            near,
            aqi,
            aqi_scale,
            gaps,
            required,
            extra,
            (wp, WIND_ODDS["limit_mph"]) if wp is not None else None,
            (sp, SUMMIT_ODDS["limit_mph"]) if sp is not None else None,
            p_template,
        )
        rise, set_, polar = sun_times(day, pt.lat, pt.lon)
        winds = [m["wind"] for m in mods.values()]
        snow72 = [m["snow72"] for m in mods.values() if m.get("snow72") is not None]
        numbers: dict[str, Any] = {
            "wind_mph": _r(_med(winds)),
            "wind_by_model": {SHORT[m]: _r(x["wind"]) for m, x in mods.items()},
            "wind_spread_mph": _r(max(winds) - min(winds)) if winds else None,
            "chill_f": _r(_med(m["chill"] for m in mods.values())),
            "temp_f": _r(_med(m["temp"] for m in mods.values())),
            "snow_in": _r(max((m["snow"] for m in mods.values()), default=None), 2),
            "precip_in": _r(max((m["precip"] for m in mods.values()), default=None), 2),
            "snow72_in": _r(max(snow72, default=None)),
            "cape": _r(_med(m.get("cape") for m in mods.values()), 0),
            "cloud_h": _r(_med(m.get("cloud_h") for m in mods.values())),
            "freezing_level_ft": _r(fzs, 0),
            "freezing_level_m": _r(None if fzs is None else fzs * 0.3048, 0),
            "gust_surface_mph": _r(_med(m.get("gust") for m in mods.values())),
            "ens_p": _r(pr, 2),
            "ens_members": n_mem,
            "wind_odds": _r(wp, 2),
            "summit_odds": _r(sp, 2),
            "aqi": _r(aqi, 0),
            "aqi_scale": aqi_scale,
            "avalanche": (
                f"{avd['center']} {AVY_NAMES.get(avd['level'], avd['level'])}"
                if avd
                else f"{zone_avy['center']} no rating"
                if zone_avy
                else None
            ),
            "alerts": [a for a, _ in al],
            "nearest_fire": f"{near[0][0]} {near[0][1]:.0f} mi" if near else None,
            "nws_day": text,
            "models_read": len(mods),
        }
        read = {"models": len(mods) >= 2, "om-ens": pr is not None, "om-aq": aqi is not None}
        if us:
            read.update(
                {
                    "nws-alerts": alerts_ok and g.in_us is True,
                    "avy": avy_ok,
                    "fires": g.fires_read.ok,
                    "nws-fc": bool(text),
                }
            )
        out_days.append(
            {
                "date": day.isoformat(),
                "label": f"{day:%a} {day.day}",
                "verdict": verdict,
                "top_reason": top_reason(verdict, flags, numbers),
                "reasons": [{"level": lv, "text": t} for lv, t in flags],
                "unread": [t for grp, t in gaps if grp in required],
                "numbers": numbers,
                "read": read,
                "summit_hours": {
                    "start": _iso(start.timestamp()),
                    "end": _iso(end.timestamp()),
                    "passed": now >= end,
                },
                "sunrise": _hhmm(rise, tz),
                "sunset": _hhmm(set_, tz),
                "polar": polar or None,
            }
        )

    band = "/".join(str(x) for x in pt.band)
    sources = [
        _source("models", f"Open-Meteo ECMWF, GFS, ICON, GEM at {band} hPa", g.det_read,
                True, nowts),
        _source("om-ens", f"Open-Meteo ECMWF ensemble at {pt.ens_level} hPa", g.ens_read,
                True, nowts),
        _source("om-aq", f"Open-Meteo air quality (CAMS), {aqi_scale}", g.aq_read, False,
                nowts),
    ]
    if us:
        avy_note = (
            f"zone {zone_avy['center']} {zone_avy['zone']}"
            if zone_avy
            else "outside every forecast zone"
            if avy_ok
            else None
        )
        sources += [
            _source("nws-alerts", "NWS alerts (api.weather.gov)",
                    g.alerts_read if g.in_us else g.nws_point_read, True, nowts),
            _source("nws-fc", "NWS point forecast (thunder)", g.fc_read, False, nowts),
            _source("avy", "avalanche.org danger", g.avy_read, True, nowts, avy_note),
            _source("fires", "NIFC WFIGS wildfires", g.fires_read, False, nowts),
        ]
    else:
        why = "outside the US: not checked"
        for sid, name in (
            ("nws-alerts", "NWS alerts"),
            ("nws-fc", "NWS point forecast"),
            ("avy", "avalanche.org danger"),
            ("fires", "NIFC WFIGS wildfires"),
        ):
            sources.append(_source(sid, name, Read("skipped", note=why), False, nowts))

    result: dict[str, Any] = {
        "name": pt.name,
        "lat": pt.lat,
        "lon": pt.lon,
        "elev_m": _r(elev_m, 0),
        "elev_ft": _r(elev_ft, 0),
        "elev_source": pt.elev_source,
        "grid_elev_m": _r(det.get("elevation") if isinstance(det, dict) else None, 0),
        "timezone": zone.name,
        "tz_exact": zone.exact,
        "tz_note": None
        if zone.exact
        else (
            "local hours use today's UTC offset for the whole week (no time-zone database on "
            "this server), so a daylight-saving change inside the week shifts later days by "
            "an hour"
        ),
        "summit_hours_local": f"{SUMMIT_HOURS[0]:02d}:00-{SUMMIT_HOURS[1]:02d}:00",
        "generated": now.isoformat(timespec="seconds"),
        "screen_note": SCREEN_NOTE,
        "in_us": g.in_us if pt.maybe_us else False,
        "calibration": {
            "region": CAL_REGION_NAME,
            "applied": pt.calibrated,
            "wind_odds": WIND_ODDS["trained"] if pt.calibrated else None,
            "summit_odds": SUMMIT_ODDS["trained"] if pt.calibrated else None,
            "note": None if pt.calibrated else "wind odds not calibrated here",
        },
        "levels_hpa": list(pt.band),
        "ens_level_hpa": pt.ens_level,
        "exposed_rule": (
            "a summit below the 600 hPa height takes at least the 600 hPa wind (PeakWatch)"
            if pt.calibrated
            else f"the summit takes at least the wind of the next level above it within "
            f"{EXPOSED_REACH_M} m (PeakWatch's rule extended, not calibrated here)"
        ),
        "aqi_scale": aqi_scale,
        "days": out_days,
        "sources": sources,
        "limits": dict(LIMITS),
        "units": {"wind": "mph", "temp": "F", "precip": "in", "height": "ft"},
        "rejects": dict(rejects),
    }
    if with_hourly:
        result["hourly"] = hourly_series(series, day_list, tz)
    return result


def hourly_series(series: dict[str, Any], day_list: Sequence[date], tz: tzinfo) -> dict[str, Any]:
    """The meteogram: every hour of the scored days, medians and range across the models."""
    keys = ("t", "local", "wind", "wind_min", "wind_max", "gust", "temp", "chill", "cloud",
            "precip", "snow", "freezing_ft")
    out: dict[str, list[Any]] = {k: [] for k in keys}
    if not day_list:
        return out
    t0 = local_window(day_list[0], (0, 0), tz)[0]
    t1 = local_window(day_list[-1] + timedelta(days=1), (0, 0), tz)[0]
    mods = list(series["models"].values())
    for i, t in enumerate(series["times"]):
        if not t0 <= t < t1:
            continue
        ws = [s["wind"][i] for s in mods if s["wind"][i] is not None]
        chills = [
            wind_chill_f(s["temp"][i], s["wind"][i])
            for s in mods
            if s["temp"][i] is not None and s["wind"][i] is not None
        ]
        out["t"].append(int(t.timestamp()))
        out["local"].append(t.astimezone(tz).strftime("%Y-%m-%dT%H:%M"))
        out["wind"].append(_r(_med(ws)))
        out["wind_min"].append(_r(min(ws)) if ws else None)
        out["wind_max"].append(_r(max(ws)) if ws else None)
        out["gust"].append(_r(_med(s["gust"][i] for s in mods)))
        out["temp"].append(_r(_med(s["temp"][i] for s in mods)))
        out["chill"].append(_r(_med(chills)))
        out["cloud"].append(_r(_med(s["cloud"][i] for s in mods), 0))
        out["precip"].append(_r(_med(s["precip"][i] for s in mods), 3))
        out["snow"].append(_r(_med(s["snow"][i] for s in mods), 3))
        out["freezing_ft"].append(_r(_med(s["fz"][i] for s in mods), 0))
    return out


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _label(lat: float, lon: float) -> str:
    return f"{abs(lat):.3f} {'N' if lat >= 0 else 'S'} {abs(lon):.3f} {'E' if lon >= 0 else 'W'}"


def conditions(
    lat: float, lon: float, elev_m: float | None = None, name: str = "", days: int = 7
) -> dict[str, Any]:
    """Everything the drawer shows for one point. Raises UpstreamError when there is no
    summit height and the elevation lookup failed."""
    pt = Point(lat, lon, elev_m, name or _label(lat, lon), "given" if elev_m is not None else "")
    got = gather([pt])
    if pt.elev_m is None:
        raise UpstreamError("no summit height given and the elevation lookup failed")
    return score_point(pt, got[_gkey(pt)], days=max(1, min(7, days)))


def verdicts(items: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{id: {verdict, day, when, top_reason}} for today's summit hours, or tomorrow's once
    today's have passed, from one batched fetch."""
    pts: list[tuple[str, Point]] = []
    for it in items:
        e = it.get("elev_m")
        pt = Point(float(it["lat"]), float(it["lon"]), None if e is None else float(e),
                   str(it.get("name") or ""), "given" if e is not None else "")
        pts.append((str(it["id"]), pt))
    got = gather([p for _, p in pts])
    out: dict[str, dict[str, Any]] = {}
    for pid, pt in pts:
        if pt.elev_m is None:
            out[pid] = {"verdict": "?", "day": None, "when": None,
                        "top_reason": "no summit height and the elevation lookup failed"}
            continue
        res = score_point(pt, got[_gkey(pt)], days=2, with_hourly=False)
        d0 = res["days"][0]
        pick, when = (res["days"][1], "tomorrow") if d0["summit_hours"]["passed"] else (
            d0, "today")
        out[pid] = {"verdict": pick["verdict"], "day": pick["date"], "when": when,
                    "top_reason": pick["top_reason"]}
    return out
