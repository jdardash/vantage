"""Atmospheric drag decay and orbital lifetime estimation.

Estimates remaining orbital lifetime for LEO satellites based on
altitude, ballistic coefficient (BSTAR), and solar activity proxy.

Reference: King-Hele, "Satellite Orbits in an Atmosphere" (1987).

Space weather forecast (optional)
---------------------------------
With a single F10.7 value the decay rate is the TLE's own rate (from the
mean-motion derivative) scaled by ``_solar_activity_factor``, and the lifetime
is the altitude budget divided by that rate, implicitly counted from the TLE
epoch. With a daily forecast (NOAA SWPC's 45-day F10.7 and Ap forecast, see
sda.spaceweather) the same budget is spent day by day, starting on the TLE
epoch day, because the budget is the perigee height at epoch: each day's rate
is the TLE rate times the solar factor for that day's F10.7 times a
geomagnetic factor for that day's Ap.

* Days before the first forecast day run at the caller's single F10.7 and Ap 8.
* A day the forecast leaves out keeps the previous day's F10.7 but never its
  Ap: Ap falls back to 8, so a storm value is never stretched over a gap.
* Forecast days before the TLE epoch day, or 45 or more days after the
  count-from day (FORECAST_HORIZON_DAYS), are ignored, so one far-future row
  cannot stretch the window.
* After the last forecast day the rate holds at Ap 8 and F10.7 = the 81-day
  mean when the caller has one, else the mean of the forecast's own F10.7
  values (never a single day's), which is the single-value estimate again.

Time runs continuously from the TLE epoch instant (forecast day D holds from
D 00:00 to D+1 00:00 UTC), and the lifetime is reported from the later of the
epoch and the requested start instant, so the time an old element set has
already spent is subtracted, not forgotten. When that time uses up the whole
budget the estimate floors at one day and the note says to refresh the
elements.

The geomagnetic factor follows Jacchia (1971), "Revised static models of the
thermosphere and exosphere with empirical temperature profiles", SAO Special
Report 332:

* nighttime minimum global exospheric temperature
  Tc = 379 + 3.24 F-bar + 1.3 (F - F-bar) K (F the daily F10.7, F-bar its
  81-day mean; with no mean, F-bar = F);
* daily-Ap geomagnetic correction dT = Ap + 100 (1 - exp(-0.08 Ap)) K.

Temperature is turned into density with an isothermal atomic-oxygen scale
height H = k T / (m_O g) above Jacchia's fixed 120 km boundary, g taken at the
midpoint of the column, so at perigee altitude h the density ratio between
exospheric temperatures T and T_ref is exp((h - 120 km) (1/H(T_ref) - 1/H(T))).
The factor is normalized to Ap 8 (AP_REFERENCE, the unsettled-quiet background
SWPC forecasts on non-storm days), so a forecast of constant F10.7 and Ap 8
gives the same lifetime as the single-value estimate.

This is a first-order approximation, good to an order of magnitude where
atomic oxygen dominates. Its known limits:

* Tc is Jacchia's nighttime minimum. The diurnal bulge is not modelled; only
  the ratio of two temperatures on the same day enters the factor.
* SR 332 also applies a direct geomagnetic density term at low altitude
  (below about 350 km). That term is not modelled.
* The isothermal form overstates the storm effect. A cross-check during
  review against an integrated Jacchia 1971 temperature profile (Bates-Walker
  form, atomic oxygen in diffusive equilibrium) put the storm excess (factor
  minus 1) roughly 20 to 105 percent high between 300 and 600 km for F10.7 70
  to 250 and Ap 30 to 400 (14 to 75 percent in log density). Storm days so err
  toward a shorter lifetime: read the storm effect as an upper bound.
* Above the oxygen-helium transition lighter helium dominates and the oxygen
  form overstates the change. The transition sits near 500 km at deep solar
  minimum and well above 600 km at high activity, so the factor is held at its
  value at :func:`geomagnetic_factor_top_km` (450 km at F-bar 70 SFU, rising
  linearly to 600 km at F-bar 150 SFU and above).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import DecayEstimate, DecayForecastUse, TLERecord
from sda.propagator import build_satrec

# Atmospheric density model (simplified exponential, Harris-Priester reference)
# (base_altitude_km, scale_height_km, density_kg_m3_at_base)
ATMOSPHERE_LAYERS = [
    (100, 5.9, 5.3e-7),
    (150, 18.0, 2.1e-9),
    (200, 28.0, 2.5e-10),
    (250, 35.0, 6.1e-11),
    (300, 40.0, 1.8e-11),
    (350, 45.0, 6.0e-12),
    (400, 53.0, 2.2e-12),
    (450, 62.0, 8.2e-13),
    (500, 70.0, 3.2e-13),
    (600, 88.0, 5.2e-14),
    (700, 120.0, 8.5e-15),
    (800, 170.0, 1.4e-15),
    (900, 220.0, 2.5e-16),
    (1000, 300.0, 4.8e-17),
]

MIN_DECAY_RATE_KM_DAY = 1e-8

#: Daily Ap the geomagnetic factor is normalized to (factor 1 at this value).
AP_REFERENCE = 8.0
#: NOAA daily Ap activity levels: quiet < 8, unsettled 8-15, active 16-29,
#: minor storm 30-49, major storm 50-99, severe storm 100 and up.
STORM_DAY_AP = 30.0
AP_RANGE = (0.0, 400.0)
F107_RANGE = (50.0, 400.0)

JACCHIA_BOUNDARY_KM = 120.0
#: The geomagnetic factor is held at its value at this altitude: the low-flux
#: value at F-bar <= _TOP_FLUX_LOW_SFU, rising linearly to the high-flux value
#: at F-bar >= _TOP_FLUX_HIGH_SFU (it tracks the oxygen-helium transition).
GEOMAGNETIC_FACTOR_TOP_KM = 600.0
GEOMAGNETIC_FACTOR_TOP_LOW_FLUX_KM = 450.0
_TOP_FLUX_LOW_SFU = 70.0
_TOP_FLUX_HIGH_SFU = 150.0
#: Forecast days this many days or more after the count-from day are ignored:
#: SWPC's product covers 45 days from its issue date, so a row further out is
#: not part of the forecast.
FORECAST_HORIZON_DAYS = 45
_DAY_S = 86400.0

AP_SCALING = (
    "Jacchia 1971 (SAO Special Report 332): dT = Ap + 100(1 - exp(-0.08 Ap)) K on "
    "Tc = 379 + 3.24 Fbar + 1.3 (F - Fbar) K; density by isothermal atomic-oxygen "
    "scale height above 120 km, held above 450 km (Fbar 70) to 600 km (Fbar 150+); "
    "factor 1 at Ap 8, which also fills forecast gaps and the time after the forecast; "
    "the isothermal form overstates storms, so treat the storm effect as an upper bound"
)

_BOLTZMANN_J_K = 1.380649e-23
_ATOMIC_OXYGEN_KG = 15.999 * 1.66053906660e-27
_G0_M_S2 = 9.80665


class DailyDrivers(Protocol):
    """One forecast day: anything with ``day``, ``ap`` and ``f107`` attributes."""

    @property
    def day(self) -> date: ...

    @property
    def ap(self) -> float | None: ...

    @property
    def f107(self) -> float | None: ...


def _atmospheric_density(altitude_km: float) -> float:
    """Estimate atmospheric density at given altitude using exponential model."""
    if altitude_km < 100:
        return 1.2  # sea level-ish
    if altitude_km > 1200:
        return 0.0  # negligible drag

    # Find bracketing layer
    for i in range(len(ATMOSPHERE_LAYERS) - 1):
        h0, H0, rho0 = ATMOSPHERE_LAYERS[i]
        h1, _, _ = ATMOSPHERE_LAYERS[i + 1]
        if h0 <= altitude_km < h1:
            return rho0 * math.exp(-(altitude_km - h0) / H0)

    # Above last defined layer
    h0, H0, rho0 = ATMOSPHERE_LAYERS[-1]
    return rho0 * math.exp(-(altitude_km - h0) / H0)


def _solar_activity_factor(f107: float = 150.0) -> float:
    """Scale factor for atmospheric density based on F10.7 solar flux.

    F10.7 ranges: ~70 (solar min) to ~250 (solar max).
    Density roughly scales 5-10x between min and max at 400 km.
    """
    # Normalize to moderate activity (F10.7 = 150)
    return math.pow(f107 / 150.0, 1.5)


# --------------------------------------------------------------------------
# Geomagnetic (Ap) density factor
# --------------------------------------------------------------------------


def jacchia_ap_temperature_delta_k(ap: float) -> float:
    """Jacchia 1971 daily-Ap exospheric temperature correction, kelvin.

    dT = Ap + 100 (1 - exp(-0.08 Ap)), Ap clamped to 0..400.
    """
    ap = min(max(ap, AP_RANGE[0]), AP_RANGE[1])
    return ap + 100.0 * (1.0 - math.exp(-0.08 * ap))


def jacchia_night_temperature_k(f107: float, f107_mean: float | None = None) -> float:
    """Jacchia 1971 nighttime minimum global exospheric temperature, kelvin.

    Tc = 379 + 3.24 F-bar + 1.3 (F - F-bar), with F-bar = F when no mean is given.
    """
    fbar = f107 if f107_mean is None else f107_mean
    return max(300.0, 379.0 + 3.24 * fbar + 1.3 * (f107 - fbar))


def geomagnetic_factor_top_km(f107_mean: float) -> float:
    """Altitude above which the geomagnetic factor is held, km, for 81-day mean F10.7.

    450 km at 70 SFU or less, 600 km at 150 SFU or more, linear between: the
    oxygen-helium transition falls toward 500 km at deep solar minimum, and the
    isothermal atomic-oxygen form overstates the storm change above it.
    """
    span = _TOP_FLUX_HIGH_SFU - _TOP_FLUX_LOW_SFU
    fraction = min(max((f107_mean - _TOP_FLUX_LOW_SFU) / span, 0.0), 1.0)
    low, high = GEOMAGNETIC_FACTOR_TOP_LOW_FLUX_KM, GEOMAGNETIC_FACTOR_TOP_KM
    return low + fraction * (high - low)


def geomagnetic_density_factor(
    ap: float,
    altitude_km: float,
    f107: float = 150.0,
    f107_mean: float | None = None,
) -> float:
    """Density at *altitude_km* for daily *ap*, relative to the same day at Ap 8.

    See the module docstring for the model. Returns 1 at or below the 120 km
    boundary, for a non-finite input, and at Ap = AP_REFERENCE exactly. Above
    geomagnetic_factor_top_km(F-bar) the factor is held at its value there.
    """
    values = (ap, altitude_km, f107) + ((f107_mean,) if f107_mean is not None else ())
    if not all(math.isfinite(v) for v in values):
        return 1.0
    fbar = f107 if f107_mean is None else f107_mean
    h = min(altitude_km, geomagnetic_factor_top_km(fbar))
    if h <= JACCHIA_BOUNDARY_KM:
        return 1.0
    tc = jacchia_night_temperature_k(f107, f107_mean)
    t_ref = tc + jacchia_ap_temperature_delta_k(AP_REFERENCE)
    t_day = tc + jacchia_ap_temperature_delta_k(ap)
    mid_km = 0.5 * (JACCHIA_BOUNDARY_KM + h)
    g = _G0_M_S2 * (EARTH_RADIUS_KM / (EARTH_RADIUS_KM + mid_km)) ** 2
    km_per_kelvin = _BOLTZMANN_J_K / (_ATOMIC_OXYGEN_KG * g) / 1000.0
    return math.exp((h - JACCHIA_BOUNDARY_KM) / km_per_kelvin * (1.0 / t_ref - 1.0 / t_day))


# --------------------------------------------------------------------------
# Orbit geometry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrbitGeometry:
    semi_major_axis_km: float
    perigee_km: float
    apogee_km: float
    eccentricity: float
    period_min: float


def _geometry(sat: Any) -> OrbitGeometry:
    """Mean orbit size from a Satrec's Kozai mean motion (WGS72 mu and radius)."""
    eccentricity = sat.ecco
    mean_motion_rev_day = sat.no_kozai * (1440.0 / (2 * math.pi))  # rad/min -> rev/day
    period_min = 1440.0 / mean_motion_rev_day if mean_motion_rev_day > 0 else 90.0

    # Semi-major axis from period
    period_s = period_min * 60.0
    a_km = (EARTH_MU_KM3_S2 * (period_s / (2 * math.pi)) ** 2) ** (1 / 3)

    return OrbitGeometry(
        semi_major_axis_km=a_km,
        perigee_km=a_km * (1 - eccentricity) - EARTH_RADIUS_KM,
        apogee_km=a_km * (1 + eccentricity) - EARTH_RADIUS_KM,
        eccentricity=eccentricity,
        period_min=period_min,
    )


def orbit_perigee_km(tle: TLERecord) -> float:
    """Perigee altitude above the WGS72 equatorial radius, km, as estimate_decay reports it."""
    return _geometry(build_satrec(tle)).perigee_km


# --------------------------------------------------------------------------
# Forecast integration
# --------------------------------------------------------------------------


def _in_range(value: object, within: tuple[float, float]) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not within[0] <= number <= within[1]:
        return None
    return number


def _as_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return None


def _as_instant(value: object) -> datetime | None:
    """An aware UTC datetime; a bare date is its midnight UTC, a naive datetime is UTC."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


@dataclass(frozen=True)
class _Segment:
    """A stretch at constant drivers, in days since the TLE epoch instant."""

    kind: str  # "lead" (before the forecast), "day" (one forecast day), "after"
    begin: float
    end: float  # math.inf for "after"
    f107: float
    ap: float
    rate: float
    day: date | None = None


@dataclass(frozen=True)
class _ForecastRun:
    lifetime_days: float  # counted from count_from; zero or less means already down
    rate_at_count_from: float
    use: DecayForecastUse
    note: str


def _integrate_forecast(
    forecast: Sequence[DailyDrivers],
    *,
    base_rate: float,
    perigee_km: float,
    f107_carry: float,
    f107_mean: float | None,
    epoch: datetime,
    count_from: datetime,
    budget_km: float,
    rate_floor: float,
) -> _ForecastRun:
    """Spend the altitude budget from the TLE epoch instant, one forecast day at a time.

    Time runs continuously from the epoch instant; forecast day D holds from
    D 00:00 to D+1 00:00 UTC. The stretch before the first listed day runs at
    the carry-in F10.7 and Ap 8. Listed days and the gaps between them are
    walked one calendar day at a time (F10.7 carries over a gap, Ap does not).
    After the last listed day the rate holds at the after-window drivers. The
    lifetime comes back counted from *count_from* (an instant at or after the
    epoch).
    """
    f107_mean = _in_range(f107_mean, F107_RANGE)
    epoch_day = epoch.date()
    horizon = count_from.date() + timedelta(days=FORECAST_HORIZON_DAYS)
    by_day: dict[date, DailyDrivers] = {}
    for entry in forecast:
        day = _as_day(getattr(entry, "day", None))
        if day is not None and epoch_day <= day < horizon:
            by_day.setdefault(day, entry)
    window = sorted(by_day)

    def rate_for(f107: float, ap: float) -> float:
        storm = geomagnetic_density_factor(ap, perigee_km, f107, f107_mean)
        return max(base_rate * _solar_activity_factor(f107) * storm, MIN_DECAY_RATE_KM_DAY)

    def since_epoch(moment: datetime) -> float:
        return (moment - epoch).total_seconds() / _DAY_S

    segments: list[_Segment] = []
    listed_f107: list[float] = []
    if window:
        first = since_epoch(_as_instant(window[0]) or epoch)
        if first > 0.0:
            lead = rate_for(f107_carry, AP_REFERENCE)
            segments.append(_Segment("lead", 0.0, first, f107_carry, AP_REFERENCE, lead))
        f107 = f107_carry
        for i in range((window[-1] - window[0]).days + 1):
            day = window[0] + timedelta(days=i)
            listed = by_day.get(day)
            ap = AP_REFERENCE
            if listed is not None:
                day_f107 = _in_range(listed.f107, F107_RANGE)
                if day_f107 is not None:
                    f107 = day_f107
                    listed_f107.append(day_f107)
                day_ap = _in_range(listed.ap, AP_RANGE)
                if day_ap is not None:
                    ap = day_ap
            begin = since_epoch(_as_instant(day) or epoch)
            segments.append(
                _Segment("day", max(begin, 0.0), begin + 1.0, f107, ap, rate_for(f107, ap), day)
            )

    # After the window: the 81-day mean, else the mean of the listed forecast
    # F10.7 (one day's value, junk or not, never sets the rest of the lifetime),
    # else the carry-in; always Ap 8.
    if f107_mean is not None:
        f107_after = f107_mean
    elif listed_f107:
        f107_after = sum(listed_f107) / len(listed_f107)
    else:
        f107_after = f107_carry
    after_begin = segments[-1].end if segments else 0.0
    segments.append(
        _Segment("after", after_begin, math.inf, f107_after, AP_REFERENCE,
                 rate_for(f107_after, AP_REFERENCE))
    )

    remaining = budget_km
    end_t = 0.0
    end_index = len(segments) - 1
    for index, seg in enumerate(segments):
        spend = max(seg.rate, rate_floor)
        if seg.end == math.inf or spend * (seg.end - seg.begin) >= remaining:
            end_t = seg.begin + remaining / spend
            end_index = index
            break
        remaining -= spend * (seg.end - seg.begin)
    reached = segments[: end_index + 1]
    ended_in = segments[end_index].kind

    t_count = since_epoch(count_from)
    t_rate = min(t_count, end_t)
    rate_seg = next(
        (s for s in reached if s.begin <= t_rate < s.end), segments[end_index]
    )

    days = [s for s in reached if s.kind == "day"]
    f_seen = [s.f107 for s in days]
    ap_seen = [s.ap for s in days]
    storm_days = sum(1 for a in ap_seen if a >= STORM_DAY_AP)
    from_count = end_t - t_count
    gone = from_count <= 0.0
    use = DecayForecastUse(
        first_day=days[0].day.isoformat() if days and days[0].day else None,
        last_day=days[-1].day.isoformat() if days and days[-1].day else None,
        days_used=len(days),
        reentry_within_window=ended_in == "day",
        reentry_before_forecast=ended_in == "lead",
        f107_min=min(f_seen) if f_seen else None,
        f107_max=max(f_seen) if f_seen else None,
        peak_ap=max(ap_seen) if ap_seen else None,
        storm_days=storm_days,
        f107_after_window=round(f107_after, 1),
        ap_after_window=AP_REFERENCE,
        ap_reference=AP_REFERENCE,
        ap_scaling=AP_SCALING,
        tle_epoch=epoch.isoformat(),
        counted_from=count_from.isoformat(),
        tle_age_days=round(t_count, 3),
        reentry_before_counted_from=gone,
    )

    after = f"F10.7 {f107_after:.0f} SFU and Ap {AP_REFERENCE:.0f}"
    if ended_in == "lead":
        note = (
            f"Reentry before the first forecast day ({window[0].isoformat()}), "
            f"at F10.7 {f107_carry:.0f} SFU and Ap {AP_REFERENCE:.0f}"
        )
    elif days:
        note = (
            f"Daily forecast: F10.7 {min(f_seen):.0f}-{max(f_seen):.0f} SFU, peak daily Ap "
            f"{max(ap_seen):.0f} over {len(days)} days"
        )
        if storm_days:
            note += f", storm drag on {storm_days} days (Ap >= {STORM_DAY_AP:.0f})"
        note += "; reentry inside the window" if ended_in == "day" else f"; then {after}"
    else:
        last = horizon - timedelta(days=1)
        note = (
            f"Forecast covers no day from {epoch_day.isoformat()} to {last.isoformat()}; "
            f"{after} used throughout"
        )
    when = count_from.strftime("%Y-%m-%d %H:%M UTC")
    if t_count > 0.0:
        note += f"; counted from {when}, {t_count:.1f} days after the TLE epoch"
    if gone:
        note += f"; these elements predict reentry before {when}, refresh the TLE"
    return _ForecastRun(
        lifetime_days=from_count, rate_at_count_from=rate_seg.rate, use=use, note=note
    )


# --------------------------------------------------------------------------
# Estimate
# --------------------------------------------------------------------------


def estimate_decay(
    tle: TLERecord,
    f107_solar_flux: float = 150.0,
    *,
    forecast: Sequence[DailyDrivers] | None = None,
    f107_mean: float | None = None,
    start: date | datetime | None = None,
) -> DecayEstimate:
    """Estimate orbital decay rate and remaining lifetime.

    Parameters
    ----------
    tle : TLE record for the satellite
    f107_solar_flux : F10.7 solar radio flux in SFU (default 150 = moderate).
        Must be finite (ValueError otherwise). With a forecast, this is only
        the F10.7 for the time between the TLE epoch and the first forecast
        day, and for leading forecast days without an F10.7 value.
    forecast : optional daily F10.7 (SFU) and Ap (nT), for example
        ``sda.spaceweather.Forecast45.days``. Integrated from the TLE epoch
        instant (module docstring): a day without an F10.7 keeps the previous
        one, a day without an Ap uses Ap 8, and days before the epoch day or
        45 or more days after the count-from day are ignored.
    f107_mean : optional mean F10.7, Jacchia's F-bar (an 81-day mean; the
        decay route passes SWPC's observed 90-day mean as the nearest
        published stand-in). Used after the forecast window too.
    start : the instant the lifetime counts from (default now, UTC; a bare
        date is its midnight UTC). With a forecast the lifetime is counted
        from the later of this and the TLE epoch; without one, from the TLE
        epoch as before.

    Returns
    -------
    DecayEstimate with lifetime prediction; ``space_weather_forecast`` records
    how the forecast was used when one was given, and decay_rate_km_per_day is
    then the rate on the count-from day.
    """
    if not math.isfinite(f107_solar_flux):
        raise ValueError("f107_solar_flux must be a finite number of SFU")
    sat = build_satrec(tle)
    geo = _geometry(sat)
    eccentricity = geo.eccentricity
    period_min = geo.period_min
    a_km = geo.semi_major_axis_km
    perigee_km = geo.perigee_km
    apogee_km = geo.apogee_km
    altitude_km = (perigee_km + apogee_km) / 2.0

    bstar = sat.bstar

    # Use mean motion derivative (ndot) from TLE for decay rate
    # ndot is in rev/day^2 (stored as rev/day^2/2 in TLE line 1)
    # da/dt = -(2/3) * a * (ndot / n) where n = mean motion
    ndot = sat.ndot  # rad/min/min (already doubled in sgp4)
    n_rad_min = sat.no_kozai

    # Convert ndot to useful units: da/dt in km/day
    if abs(n_rad_min) > 1e-15:
        # da/dt = -(2/3) * a * (ndot/n)
        # ndot in rad/min^2, n in rad/min: ratio ndot/n in 1/min, * 1440 for 1/day
        ratio = ndot / n_rad_min  # 1/min
        da_dt_km_per_day = abs((2.0 / 3.0) * a_km * ratio * 1440.0)
    else:
        da_dt_km_per_day = 0.0

    # Altitude budget: down to 150 km, or the whole perigee height once it is
    # already under 180 km (with a 0.1 km/day floor on the rate there).
    if perigee_km < 180:
        budget_km, rate_floor = perigee_km, 0.1
    else:
        budget_km, rate_floor = perigee_km - 150.0, 0.0

    forecast_use: DecayForecastUse | None = None
    if forecast is None:
        # Apply solar activity scaling, clamp to reasonable values
        solar_scale = _solar_activity_factor(f107_solar_flux)
        decay_rate = max(da_dt_km_per_day * solar_scale, MIN_DECAY_RATE_KM_DAY)
        lifetime_days = max(1.0, budget_km / max(decay_rate, rate_floor))

        # Solar activity note
        if f107_solar_flux < 100:
            solar_note = "Solar minimum, reduced atmospheric drag, longer lifetime"
        elif f107_solar_flux < 180:
            solar_note = "Moderate solar activity, nominal drag conditions"
        else:
            solar_note = "Solar maximum, elevated atmospheric drag, shorter lifetime"
    else:
        epoch = _as_instant(tle.epoch) or datetime.now(UTC)
        start_at = _as_instant(start) or datetime.now(UTC)
        run = _integrate_forecast(
            forecast,
            base_rate=da_dt_km_per_day,
            perigee_km=perigee_km,
            f107_carry=f107_solar_flux,
            f107_mean=f107_mean,
            epoch=epoch,
            count_from=max(epoch, start_at),
            budget_km=budget_km,
            rate_floor=rate_floor,
        )
        decay_rate = run.rate_at_count_from
        lifetime_days = max(1.0, run.lifetime_days)
        solar_note = run.note
        forecast_use = run.use

    # Categorize
    if lifetime_days < 7:
        category = "days"
        risk = "IMMINENT"
    elif lifetime_days < 30:
        category = "weeks"
        risk = "HIGH"
    elif lifetime_days < 365:
        category = "months"
        risk = "MODERATE"
    elif lifetime_days < 3650:
        category = "years"
        risk = "LOW"
    elif lifetime_days < 36500:
        category = "decades"
        risk = "MINIMAL"
    else:
        category = "stable"
        risk = "MINIMAL"

    return DecayEstimate(
        norad_id=tle.norad_id,
        name=tle.name,
        altitude_km=round(altitude_km, 1),
        perigee_km=round(perigee_km, 1),
        apogee_km=round(apogee_km, 1),
        eccentricity=round(eccentricity, 6),
        period_min=round(period_min, 2),
        bstar=bstar,
        decay_rate_km_per_day=round(decay_rate, 6),
        estimated_lifetime_days=round(lifetime_days, 1),
        estimated_lifetime_category=category,
        reentry_risk=risk,
        solar_activity_note=solar_note,
        space_weather_forecast=forecast_use,
    )
