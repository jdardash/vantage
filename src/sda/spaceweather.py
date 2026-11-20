"""NOAA SWPC space weather: the 45-day F10.7 and Ap forecast, Kp, NOAA scales, F10.7.

Four keyless products from the NOAA Space Weather Prediction Center
(https://services.swpc.noaa.gov), all public domain:

* ``/json/45-day-forecast.json``            daily Ap and F10.7, today and 44 days on
* ``/products/noaa-planetary-k-index.json`` 3-hourly planetary Kp, the last seven days
* ``/products/noaa-scales.json``            G, S and R scales: yesterday ("-1"), now ("0"),
  today and the next two days ("1".."3", G as a level, S and R as probabilities)
* ``/json/f107_cm_flux.json``               observed 10.7 cm flux (Penticton), with the
  90-day mean on the daily noon reading

Every parser is a pure function of the decoded JSON. A row that is not the
shape it should be, or holds a value outside what the quantity can physically
be (SWPC marks a missing reading with a fill value such as -1 or -99999), is
skipped, never raised, so one corrupt row costs one row.

The products are fetched together at most once per fifteen minutes, whatever
the outcome. Each product keeps its last good copy: a failed or malformed fetch
serves that copy flagged stale, and a product that has never arrived is
reported missing.

The module also holds the storm check for conjunctions
(:func:`check_conjunction_storm`): a low-perigee event whose TCA falls on a day
with a G1 or stronger geomagnetic storm, forecast or observed, carries a
warning that in-track error grows during storms. The check is advisory text
only. It never changes a miss distance, a Pc or a risk level.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

SWPC_BASE_URL = "https://services.swpc.noaa.gov"
FORECAST_45_DAY_URL = f"{SWPC_BASE_URL}/json/45-day-forecast.json"
KP_URL = f"{SWPC_BASE_URL}/products/noaa-planetary-k-index.json"
SCALES_URL = f"{SWPC_BASE_URL}/products/noaa-scales.json"
F107_URL = f"{SWPC_BASE_URL}/json/f107_cm_flux.json"

#: Product name -> URL, in the order they are fetched.
PRODUCTS: dict[str, str] = {
    "forecast": FORECAST_45_DAY_URL,
    "kp": KP_URL,
    "scales": SCALES_URL,
    "f107": F107_URL,
}

SOURCE = "NOAA SWPC"
FORECAST_SOURCE = "NOAA SWPC 45-day Ap and F10.7 forecast"
FETCH_TIMEOUT_S = 15.0
CACHE_TTL_S = 900.0

#: What each reading can physically be. Daily Ap tops out at 400 nT; F10.7 has
#: not been below about 60 SFU in the record or above about 400 SFU.
AP_RANGE = (0.0, 400.0)
F107_RANGE = (50.0, 400.0)
KP_RANGE = (0.0, 9.0)
SCALE_RANGE = (0.0, 5.0)
PROB_RANGE = (0.0, 100.0)

#: The forecast is 45 days from its issue date. The parser keeps days from
#: FORECAST_LEAD_DAYS before the issue date to MAX_FORECAST_DAYS after it (a
#: tolerant reader); anything outside is junk. sda.decay uses at most 45 days of
#: it. A forecast without a readable issue date is refused.
MAX_FORECAST_DAYS = 60
FORECAST_LEAD_DAYS = 2
_TEXT_MAX_CHARS = 40

#: Kp threshold for each NOAA G level (https://www.swpc.noaa.gov/noaa-scales-explanation).
#: Kp is published in thirds (5- = 4.67, 5o = 5.00, 5+ = 5.33) and each NOAA
#: level covers the whole Kp band, minus included: G1 from 5-, G2 from 6-, G3
#: from 7-, G4 from 8- through 9- (NOAA: "Kp = 8, including a 9-"), G5 at 9o.
#: The thresholds sit a hair under the thirds so 4.666 and 4.67 both count.
_G_FROM_KP = ((8.99, 5), (7.66, 4), (6.66, 3), (5.66, 2), (4.66, 1))

#: Storm check thresholds. Below 600 km drag dominates the in-track error
#: budget, which is what a storm inflates.
STORM_PERIGEE_LIMIT_KM = 600.0
STORM_MIN_G = 1
#: A Kp reading counts as "near TCA" when its 3-hour interval lies within this
#: much of TCA (so the interval holding TCA and the one either side of it).
KP_NEAR_TCA = timedelta(hours=3)
KP_INTERVAL = timedelta(hours=3)

T = TypeVar("T")


# --------------------------------------------------------------------------
# Parsed products
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastDay:
    """One day of the 45-day forecast. Either value may be missing."""

    day: date
    ap: float | None
    f107: float | None


@dataclass(frozen=True)
class Forecast45:
    issued: str
    days: tuple[ForecastDay, ...]


@dataclass(frozen=True)
class KpReading:
    """Planetary Kp for the 3-hour interval starting at ``time`` (UTC)."""

    time: datetime
    kp: float


@dataclass(frozen=True)
class ScaleDay:
    """One entry of noaa-scales.json.

    ``offset`` is SWPC's key: -1 is yesterday's observed maximum, 0 is the
    current observed level, 1 to 3 are the forecast for today and the next two
    days (for those, S and R come as probabilities and their scale is None).
    """

    offset: int
    day: date
    time: str
    g: int | None
    s: int | None
    r: int | None
    r_minor_prob: int | None
    r_major_prob: int | None
    s_prob: int | None

    @property
    def observed(self) -> bool:
        return self.offset <= 0


@dataclass(frozen=True)
class F107Observed:
    time: datetime
    flux: float
    ninety_day_mean: float | None


@dataclass(frozen=True)
class SpaceWeather:
    """Everything the cache holds, at one moment.

    ``failed`` names the products whose most recent fetch failed or did not
    parse. A failed product with an older good copy is ``stale``; one with no
    copy at all is ``missing``.
    """

    forecast: Forecast45 | None = None
    kp: tuple[KpReading, ...] = ()
    scales: tuple[ScaleDay, ...] = ()
    f107: F107Observed | None = None
    failed: frozenset[str] = frozenset()

    @property
    def missing(self) -> frozenset[str]:
        present = {
            "forecast": self.forecast is not None,
            "kp": bool(self.kp),
            "scales": bool(self.scales),
            "f107": self.f107 is not None,
        }
        return frozenset(name for name, ok in present.items() if not ok)

    @property
    def stale(self) -> frozenset[str]:
        return self.failed - self.missing

    @property
    def empty(self) -> bool:
        return len(self.missing) == len(PRODUCTS)


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------


def _number(value: object, within: tuple[float, float]) -> float | None:
    """A finite number inside *within* from a JSON number or numeric string."""
    if value is None or isinstance(value, bool):
        return None  # float(True) is 1.0, and a flag is not a reading
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    low, high = within
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def _level(value: object, within: tuple[float, float]) -> int | None:
    """An integral value inside *within* as an int ("2", 2 and 2.0 all read as 2)."""
    number = _number(value, within)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _text(value: object) -> str:
    return value.strip()[:_TEXT_MAX_CHARS] if isinstance(value, str) else ""


def parse_time(value: object) -> datetime | None:
    """An SWPC time tag as an aware UTC datetime.

    Accepts the T and the space forms, with or without a trailing Z or an
    offset, and a bare date. A tag with no zone is UTC, as SWPC publishes.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _TEXT_MAX_CHARS:
        return None
    text = text.replace(" ", "T", 1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _records(data: object) -> list[dict]:
    """Product rows as dicts, from a list of objects or a header row plus value rows."""
    if not isinstance(data, list):
        return []
    header = data[0] if data else None
    if isinstance(header, list) and header and all(isinstance(k, str) for k in header):
        return [dict(zip(header, row, strict=False)) for row in data[1:] if isinstance(row, list)]
    return [row for row in data if isinstance(row, dict)]


def _sub(entry: dict, key: str) -> dict:
    value = entry.get(key)
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def parse_45_day_forecast(data: object) -> Forecast45 | None:
    """The 45-day forecast as one entry per day, or None when no day is usable.

    The product is ``{"issued": ..., "data": [{"time", "metric", "value"}, ...]}``
    with one row per day per metric (``ap`` in nT, ``f107`` in SFU). The first
    good value for a day and metric wins. Days outside the plausible window
    around the issue date (see MAX_FORECAST_DAYS) are dropped, so a
    far-future junk row cannot stretch the forecast. Without a readable
    ``issued`` the rows cannot be placed in time and the product is refused.
    """
    if not isinstance(data, dict):
        return None
    rows = data.get("data")
    if not isinstance(rows, list):
        return None
    ap: dict[date, float] = {}
    f107: dict[date, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = parse_time(row.get("time"))
        metric = row.get("metric")
        if stamp is None or not isinstance(metric, str):
            continue
        name = metric.strip().lower()
        if name == "ap":
            target, within = ap, AP_RANGE
        elif name in ("f107", "f10.7"):
            target, within = f107, F107_RANGE
        else:
            continue
        value = _number(row.get("value"), within)
        if value is not None:
            target.setdefault(stamp.date(), value)
    issued = _text(data.get("issued"))
    stamp = parse_time(issued)
    if stamp is None:
        return None
    low = stamp.date() - timedelta(days=FORECAST_LEAD_DAYS)
    high = stamp.date() + timedelta(days=MAX_FORECAST_DAYS)
    days = [d for d in sorted(set(ap) | set(f107)) if low <= d <= high][:MAX_FORECAST_DAYS]
    if not days:
        return None
    return Forecast45(
        issued=issued,
        days=tuple(ForecastDay(day=d, ap=ap.get(d), f107=f107.get(d)) for d in days),
    )


def parse_kp(data: object) -> tuple[KpReading, ...] | None:
    """Planetary Kp readings oldest first, or None when there is none."""
    readings: dict[datetime, float] = {}
    for rec in _records(data):
        stamp = parse_time(rec.get("time_tag"))
        if stamp is None:
            continue
        for key in ("Kp", "kp_index", "kp"):
            kp = _number(rec.get(key), KP_RANGE)
            if kp is not None:
                readings.setdefault(stamp, kp)
                break
    if not readings:
        return None
    return tuple(KpReading(time=t, kp=readings[t]) for t in sorted(readings))


def parse_scales(data: object) -> tuple[ScaleDay, ...] | None:
    """The NOAA scales entries ordered by offset, or None when none is usable."""
    if not isinstance(data, dict):
        return None
    entries: dict[int, ScaleDay] = {}
    for key, entry in data.items():
        try:
            offset = int(str(key).strip())
        except ValueError:
            continue
        if not -7 <= offset <= 7 or offset in entries or not isinstance(entry, dict):
            continue
        stamp = parse_time(entry.get("DateStamp"))
        if stamp is None:
            continue
        g, s, r = _sub(entry, "G"), _sub(entry, "S"), _sub(entry, "R")
        entries[offset] = ScaleDay(
            offset=offset,
            day=stamp.date(),
            time=_text(entry.get("TimeStamp")),
            g=_level(g.get("Scale"), SCALE_RANGE),
            s=_level(s.get("Scale"), SCALE_RANGE),
            r=_level(r.get("Scale"), SCALE_RANGE),
            r_minor_prob=_level(r.get("MinorProb"), PROB_RANGE),
            r_major_prob=_level(r.get("MajorProb"), PROB_RANGE),
            s_prob=_level(s.get("Prob"), PROB_RANGE),
        )
    if not entries:
        return None
    return tuple(entries[k] for k in sorted(entries))


def parse_f107(data: object) -> F107Observed | None:
    """The newest observed F10.7 and the newest 90-day mean, or None.

    The product lists three readings a day (morning, noon, afternoon); only the
    noon one carries ``ninety_day_mean``. Rows at another frequency are skipped.
    """
    newest: tuple[datetime, float] | None = None
    mean: tuple[datetime, float] | None = None
    for rec in _records(data):
        stamp = parse_time(rec.get("time_tag"))
        if stamp is None:
            continue
        if rec.get("frequency") is not None and _number(
            rec.get("frequency"), (2799.0, 2801.0)
        ) is None:
            continue
        flux = _number(rec.get("flux"), F107_RANGE)
        if flux is not None and (newest is None or stamp > newest[0]):
            newest = (stamp, flux)
        avg = _number(rec.get("ninety_day_mean"), F107_RANGE)
        if avg is not None and (mean is None or stamp > mean[0]):
            mean = (stamp, avg)
    if newest is None:
        return None
    return F107Observed(time=newest[0], flux=newest[1], ninety_day_mean=mean[1] if mean else None)


# --------------------------------------------------------------------------
# Summary helpers
# --------------------------------------------------------------------------


def g_from_kp(kp: float | None) -> int | None:
    """NOAA G level for a Kp value (0 below Kp 5-, i.e. 4.67), None for no reading."""
    if kp is None or not math.isfinite(kp):
        return None
    for threshold, level in _G_FROM_KP:
        if kp >= threshold:
            return level
    return 0


def g_label(level: int | None) -> str | None:
    return None if level is None else f"G{level}"


def latest_kp(kp: Sequence[KpReading]) -> KpReading | None:
    return max(kp, key=lambda r: r.time) if kp else None


def scale_now(scales: Sequence[ScaleDay]) -> ScaleDay | None:
    """The current observed entry (offset 0)."""
    return next((s for s in scales if s.offset == 0), None)


def scales_ahead(scales: Sequence[ScaleDay]) -> list[ScaleDay]:
    """The forecast entries for today and the next two days (offsets 1 to 3)."""
    return [s for s in scales if 1 <= s.offset <= 3]


def max_level(levels: Iterable[int | None]) -> int | None:
    known = [lv for lv in levels if lv is not None]
    return max(known) if known else None


# --------------------------------------------------------------------------
# Storm check for conjunctions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StormCheck:
    """What the space weather says about one conjunction's TCA.

    ``low_perigee`` is True when a known perigee is under
    STORM_PERIGEE_LIMIT_KM, False only when both perigees are known and
    neither is, and None when that cannot be told (a perigee missing and the
    known one not low). ``covered`` is False when no product reaches the TCA
    at all (a TCA more than three days out, for instance).

    ``status`` is the one field to branch on:

    * ``"warning"``: a low perigee meets a G1 or stronger storm; ``warning``
      holds the text.
    * ``"clear"``: neither perigee is low, or the TCA is known to be quiet.
    * ``"unknown"``: a perigee is unknown and a storm is on, or no product
      reaches the TCA. Unknown is not quiet; ``reason`` says which.
    """

    tca: datetime
    tca_day: date
    min_perigee_km: float | None
    low_perigee: bool | None
    g_scales_tca_day: int | None
    kp_near_tca: float | None
    level: int | None
    basis: str
    covered: bool
    status: str
    warning: str | None
    reason: str | None


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _finite(value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def check_conjunction_storm(
    tca: datetime,
    perigee_a_km: float | None,
    perigee_b_km: float | None,
    *,
    scales: Sequence[ScaleDay] = (),
    kp: Sequence[KpReading] = (),
) -> StormCheck:
    """Storm context for a conjunction at *tca* between objects with these perigees.

    Sources, strongest wins: the NOAA scales entries dated on the TCA day
    (forecast for offsets 1 to 3, observed for -1 and 0), and the observed
    planetary Kp whose 3-hour interval lies within three hours of TCA,
    converted to a G level. A perigee that is None or not finite is unknown,
    and an unknown perigee is never taken as high. The result is advisory
    only and never changes the event's Pc or risk.
    """
    tca = _as_utc(tca)
    day = tca.date()
    perigees = [p for p in (_finite(perigee_a_km), _finite(perigee_b_km)) if p is not None]
    min_perigee = min(perigees) if perigees else None
    low: bool | None
    if min_perigee is not None and min_perigee < STORM_PERIGEE_LIMIT_KM:
        low = True
    elif len(perigees) == 2:
        low = False
    else:
        low = None

    on_day = [s for s in scales if s.day == day and s.g is not None]
    g_scales = max_level(s.g for s in on_day)
    near = [
        value for r in kp
        if (value := _number(r.kp, KP_RANGE)) is not None
        and r.time - KP_NEAR_TCA <= tca <= r.time + KP_INTERVAL + KP_NEAR_TCA
    ]
    kp_near = max(near) if near else None
    g_kp = g_from_kp(kp_near)
    level = max_level((g_scales, g_kp))

    basis = ""
    if level is not None:
        if g_kp is not None and g_kp == level and (g_scales is None or g_kp > g_scales):
            basis = "observed_kp"
        elif any(s.observed and s.g == level for s in on_day):
            basis = "noaa_scales_observed"
        else:
            basis = "noaa_scales_forecast"

    storm = level is not None and level >= STORM_MIN_G
    warning: str | None = None
    reason: str | None = None
    if storm and low and min_perigee is not None:
        status = "warning"
        label = g_label(level)
        if basis == "observed_kp":
            lead = f"Observed Kp {kp_near:.2f} ({label}) within 3 hours of TCA"
        elif basis == "noaa_scales_observed":
            lead = f"NOAA SWPC reports a {label} geomagnetic storm on the TCA day ({day})"
        else:
            lead = f"NOAA SWPC forecasts a {label} geomagnetic storm on the TCA day ({day})"
        warning = (
            f"{lead}, and a perigee of {min_perigee:.0f} km is under "
            f"{STORM_PERIGEE_LIMIT_KM:.0f} km. In-track position error grows during "
            "storms, so the TLE-based miss distance and Pc for this event are less "
            "reliable than usual. This warning does not change the Pc or the risk level."
        )
    elif low is False or (level is not None and not storm):
        status = "clear"
    else:
        status = "unknown"
        if level is None:
            reason = (
                f"No NOAA SWPC scale or Kp reading reaches the TCA day ({day}); "
                "storm conditions at TCA are unknown, not quiet."
            )
        else:
            reason = (
                f"NOAA SWPC shows {g_label(level)} on the TCA day ({day}) but a perigee "
                "is unknown; give both perigees to decide whether the warning applies."
            )

    return StormCheck(
        tca=tca,
        tca_day=day,
        min_perigee_km=min_perigee,
        low_perigee=low,
        g_scales_tca_day=g_scales,
        kp_near_tca=kp_near,
        level=level,
        basis=basis,
        covered=level is not None,
        status=status,
        warning=warning,
        reason=reason,
    )


# --------------------------------------------------------------------------
# Fetch and cache
# --------------------------------------------------------------------------


@dataclass
class _State:
    attempted_at: float | None = None
    forecast: Forecast45 | None = None
    kp: tuple[KpReading, ...] = ()
    scales: tuple[ScaleDay, ...] = ()
    f107: F107Observed | None = None
    failed: set[str] = field(default_factory=set)
    lock: asyncio.Lock | None = None
    lock_loop: asyncio.AbstractEventLoop | None = None


_state = _State()


def clear_cache() -> None:
    """Forget every cached product and the last attempt. Used by the tests."""
    global _state
    _state = _State()


def _lock() -> asyncio.Lock:
    """One lock per event loop, so a test client's fresh loop never inherits one."""
    loop = asyncio.get_running_loop()
    if _state.lock is None or _state.lock_loop is not loop:
        _state.lock = asyncio.Lock()
        _state.lock_loop = loop
    return _state.lock


def _due() -> bool:
    at = _state.attempted_at
    return at is None or time.monotonic() - at >= CACHE_TTL_S


async def _fetch_json(url: str) -> object:
    """Decoded JSON from *url*, or None on any failure. The only network call here.

    Reuses the proxy helper behind the globe layers (same User-Agent, same
    "None on any failure" contract), imported here so this module does not pull
    in the route layer when only the parsers or the storm check are wanted.
    """
    from sda.routes.worldview import _get_json

    return await _get_json(url, timeout_s=FETCH_TIMEOUT_S, purpose="space weather")


def _safe(parse: Callable[[object], T | None], raw: object) -> T | None:
    if isinstance(raw, BaseException):
        return None
    try:
        return parse(raw)
    except Exception:
        return None


def _mark(name: str, ok: bool) -> None:
    if ok:
        _state.failed.discard(name)
    else:
        _state.failed.add(name)


async def _refresh() -> None:
    try:
        raws = await asyncio.gather(
            *(_fetch_json(url) for url in PRODUCTS.values()), return_exceptions=True
        )
    finally:
        _state.attempted_at = time.monotonic()
    raw = dict(zip(PRODUCTS, raws, strict=True))

    forecast = _safe(parse_45_day_forecast, raw["forecast"])
    if forecast is not None:
        _state.forecast = forecast
    _mark("forecast", forecast is not None)

    kp = _safe(parse_kp, raw["kp"])
    if kp is not None:
        _state.kp = kp
    _mark("kp", kp is not None)

    scales = _safe(parse_scales, raw["scales"])
    if scales is not None:
        _state.scales = scales
    _mark("scales", scales is not None)

    f107 = _safe(parse_f107, raw["f107"])
    if f107 is not None:
        _state.f107 = f107
    _mark("f107", f107 is not None)


def snapshot() -> SpaceWeather:
    """What the cache holds now, without fetching."""
    return SpaceWeather(
        forecast=_state.forecast,
        kp=_state.kp,
        scales=_state.scales,
        f107=_state.f107,
        failed=frozenset(_state.failed),
    )


async def current() -> SpaceWeather:
    """The cached products, fetching all four when the fifteen minutes are up.

    The attempt time is stamped when the fetch ends, success or failure, so a
    failure waits out the same fifteen minutes as a success. Requests that
    arrive while a fetch is in flight wait on the lock for that one fetch.
    """
    if _due():
        async with _lock():
            if _due():
                await _refresh()
    return snapshot()
