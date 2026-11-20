"""Conjunction detection and risk classification pipeline.

Two phases: a coarse screen on a 60 s grid, then a 1 s SGP4 refinement of
every candidate encounter.

The coarse screen is sound for its step (up to ``_MAX_COARSE_STEP_SECONDS``).
Each coarse interval [t_k, t_k+1] is tested with the closest approach of the
straight chord between the pair's two sampled relative positions, against the
threshold plus a derived allowance for how far the true relative path can bow
away from that chord (see ``coarse_gate_allowance_km``). The allowance uses each
object's own non-point-mass acceleration, estimated from its coarse samples. A
pair is skipped on an interval only when it provably cannot come within the
threshold during that interval. Intervals where an object's SGP4 validity
changes (decay, error flips, states that are not physical) are screened
separately from physical scan states with a speed bound. Consecutive passing
intervals of one pair form one encounter, so a pair that meets twice in the
window is refined twice.

Two strategies find the candidate pairs. "allpairs" evaluates every surviving
pair on every interval (blocked NumPy); "spatial" queries a per-step spatial
index (scipy cKDTree when importable, otherwise a NumPy uniform-grid hash) with
a radius proven to contain every pair that can pass the gate, then applies the
same gate. Both are preceded by a radial-shell prefilter whose bands are bounded
from the coarse samples (not the TLE mean elements), and both produce the same
encounters; see tests/test_screening.py and tests/test_screening_recall.py.

Frames and units follow the project rule: ECI (TEME from SGP4) positions in km
and velocities in km/s, WGS72 gravity model, Julian dates as (jd, fr) pairs.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import (
    CollisionProbability,
    ConjunctionEvent,
    RegimeSurveyResult,
    RiskLevel,
    TLERecord,
)
from sda.probability import compute_pc_for_conjunction, sigma_from_tle_age
from sda.propagator import (
    build_satrec,
    datetime_to_jd,
    propagate_catalog_numpy,
    propagate_window_numpy,
)
from sda.screening import (
    SPATIAL_AUTO_MIN_OBJECTS,
    FormationRule,
    classify_regime,
    default_threshold_for_regime,
    formation_rules_with_extras,
    match_formation,
    pair_regime,
    query_pairs_within,
    risk_score,
    shells_overlap,
)
from sda.tle_store import TLEStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sgp4.api import Satrec

logger = logging.getLogger(__name__)

# Memory ceiling for one block of the blocked all-pairs chord tensors.
_ALLPAIRS_BLOCK_BYTES = 64 * 1024 * 1024
# Most (a, b) index pairs the all-pairs path materialises at once before the
# shell prefilter (about 16 MiB of indices plus 32 MiB of band lookups), so its
# memory stays flat in the catalog size instead of growing as n^2 / 2.
_ALLPAIRS_PAIR_CHUNK = 1 << 20

# (index_a, index_b, min coarse distance km, coarse step index)
CoarseCandidate = tuple[int, int, float, int]

# --------------------------------------------------------------------------- #
# Coarse gate constants
# --------------------------------------------------------------------------- #

# Default coarse sampling step. The gate below is sound for any step up to
# _MAX_COARSE_STEP_SECONDS; the step trades SGP4 calls against the gate width.
# The LEO allowance grows roughly as the cube of the step (path length times
# dt^2): 0.34 km at 30 s, 2.6 km at 60 s, 21 km at 120 s, 318 km at 300 s, so
# long steps stay sound but pass far more pairs to the 1 s refinement
# (500 objects, 2 h: 10 encounters at 60 s, 3,099 at 300 s).
DEFAULT_COARSE_STEP_SECONDS = 60.0

# Largest coarse step the gate is derived for. The allowance bounds each
# object's path over an interval by its chord displacement, and an arc of angle
# theta is longer than its chord by theta / (2 sin(theta / 2)). The fastest
# angular rate of any valid state is sqrt(2 mu / R) / R = 1.75e-3 rad/s (escape
# speed at the WGS72 radius), so at 300 s theta is at most 0.53 rad and the
# ratio at most 1.012, inside the safety factor. Far longer steps break the
# bound (over a whole orbit the chord shrinks to nothing) and are refused.
_MAX_COARSE_STEP_SECONDS = 300.0

# Largest gravity-gradient (tidal) magnitude a pair can feel: the point-mass
# gradient tensor mu/r^3 (3 rr^T - I) has largest eigenvalue 2 mu / r^3, taken
# at the WGS72 Earth radius as the lowest radius any valid SGP4 state reaches.
# Two objects d km apart therefore accelerate relative to each other by at most
# GRADIENT * d. 2 * 398600.8 / 6378.135^3 = 3.0725e-6 s^-2.
_GRAVITY_GRADIENT_MAX_S2 = 2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**3

# Largest point-mass gravity any valid state feels: mu / R^2 at the WGS72
# radius, 9.80e-3 km/s^2. Bounds a single object's own path curvature.
_POINT_MASS_GRAVITY_MAX_KM_S2 = EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**2

# Each object's acceleration beyond point-mass gravity (J2, drag, SGP4 theory
# terms) is estimated per coarse interval from its own samples
# (``_non_point_mass_accel_km_s2``); a pair's relative term is the sum of its two
# objects' values, with no credit for cancellation. Never below this floor per
# object: J2 alone reaches 3 * J2 * mu / R^2 = 3.18e-5 km/s^2 at the WGS72
# surface (J2 = 1.082616e-3).
_NON_POINT_MASS_FLOOR_KM_S2 = 5.0e-5

# The estimate is a weighted mean of the acceleration over two steps, not a
# maximum, and high-drag SGP4 output has sub-minute velocity kinks (measured:
# 1 s second differences up to 100 times the 60 s estimate). What the gate
# needs is the path's departure from the chord, an integral: a velocity jump dv
# at fraction u of an interval raises the larger end estimate by at least
# dv / (2 dt) and moves the path off the chord by at most u (1 - u) dt dv, which
# is at most 4 times dt^2 / 8 times that estimate. Measured over the sweep below
# (139,217 intervals at 60 s, 27,769 at 300 s): departure up to 4.32 times
# dt^2 / 8 * estimate, so 1.08 times with this factor, inside the safety factor.
_NON_POINT_MASS_ESTIMATE_FACTOR = 4.0

# Per-object value on an interval whose estimate cannot be formed: one next to a
# sample SGP4 failed on or that was dropped as unphysical, where elements are
# breaking down (decay). Measured 2026-11-19: SGP4 over B* 1e-4 to 1.0, perigee
# 150 to 600 km and e 0.0005 to 0.7 (213 element sets, each propagated 7 days
# or to its first SGP4 error, 5 s second differences, physical speeds only)
# peaked at 2.33e-2 km/s^2 (B* 0.5, perigee 150 km, e 0.1, in its last hours);
# doubled. Ordinary elements stay near the J2 floor; high-drag elements reach
# 1e-4 to 1e-2 and get their own estimate, not this constant.
_NON_POINT_MASS_FALLBACK_KM_S2 = 5.0e-2

# Multiplier on the whole curvature allowance. Covers the arc-to-chord ratio of
# each object's path over one step (at most 1.012 at the 300 s cap), the
# acceleration estimate being a weighted mean over two steps rather than a
# maximum, second-order terms the bound drops, and margin.
_GATE_SAFETY_FACTOR = 2.0

# A coarse-grid state whose displacement over one interval implies a speed
# above this is not a physical Earth-bound state: escape speed at the WGS72
# Earth radius is sqrt(2 mu / R) = 11.18 km/s, and every bound orbit above the
# surface is slower everywhere. SGP4 can return such states with error code 0
# for elements propagated far past their epoch. They are screened as invalid
# samples, exactly like an SGP4 error, so one runaway object cannot inflate the
# gate or the spatial query radius of every other pair.
_MAX_PHYSICAL_SPEED_KM_S = 12.0

# Fine refinement grid (s). Each grid minimum is then polished to sub-second.
_REFINE_STEP_SECONDS = 1.0

# Polishing beside an SGP4 gap: the validity edge is bisected this many times
# (1 s / 4096 = 0.24 ms, under 4 m at 15 km/s) and the separation minimised
# over the valid side by this many golden-section steps (2 s to about 1 us).
_GAP_EDGE_BISECTIONS = 12
_GOLDEN_SECTION_STEPS = 30

# Spacing (s) at which an object is scanned with SGP4 inside every coarse
# interval where one or both of its samples failed, to find physical states.
_VALIDITY_SCAN_SECONDS = 1.0

# A state is physical only if SGP4 also returns code 0 this many seconds before
# or after it and the position moves at most _MAX_PHYSICAL_SPEED_KM_S over that
# second. SGP4's velocity output is not evidence: after an object has decayed,
# SGP4 can return code-0 states whose positions move 75 to 255,000 km/s while
# the reported velocity stays under 8 km/s (safety review, 2026-11-19). States
# that fail this are treated exactly like SGP4 errors, in the screen and in the
# refinement, so a decayed object's debris states never anchor or report an
# approach, whatever the window phase.
_PHYSICAL_PROBE_SECONDS = 1.0

# Encounter spans up to this long are refined on the 1 s grid end to end (the
# old fixed +/- 5 min window). Longer spans are profiled first.
_FINE_SPAN_MAX_SECONDS = 600.0

# Longest profile step for a longer span (``_long_span_samples``). Every profile
# interval is re-sampled on the 1 s grid unless a bound shows it wholly above
# the threshold, or wholly within it and nowhere below the lowest sample of its
# run. At 60 s the bound's allowance is about 0.09 km for an ordinary pair
# (both objects at the non-point-mass floor), so only intervals that come within
# about that of the threshold or of a run's lowest sample are re-sampled.
_LONG_SPAN_PROFILE_SECONDS = 60.0

# A separation minimum that lies beyond an encounter span (the pair is still
# closing at a window edge) is found by walking outward at the coarse step in
# chunks of this many steps until the separation grows again, for at most this
# long. HEAD searched +/- 5 min around one coarse sample.
_EDGE_SEARCH_CHUNK_STEPS = 60
_EDGE_SEARCH_MAX_SECONDS = 7.0 * 86400.0


def coarse_gate_allowance_km(
    threshold_km: float,
    interval_seconds: float,
    path_km: float | np.ndarray,
    accel_km_s2: float | np.ndarray = 2.0 * _NON_POINT_MASS_FLOOR_KM_S2,
) -> float | np.ndarray:
    """Curvature allowance added to the threshold for one pair on one interval.

    Let x(t) be the pair's true (SGP4) relative position and L(t) the straight
    chord between x(t_k) and x(t_k+1), dt = t_k+1 - t_k. Linear interpolation
    error gives |x(t) - L(t)| <= dt^2 / 8 * A for vector functions too (Green's
    function form), where A bounds the relative acceleration |x''| on the
    interval. A <= GRADIENT * D + A0, where D bounds the pair's separation during
    the interval and A0 (``accel_km_s2``) is the sum of the two objects'
    non-point-mass accelerations. If the pair comes within the threshold
    anywhere in the interval, every other point of the interval is at most the
    relative path length further away, and that is at most the two objects' own
    path lengths, here their one-interval displacements summed (``path_km`` =
    |dp_a| + |dp_b|; arc over chord is at most 1.012 for steps up to
    ``_MAX_COARSE_STEP_SECONDS``): D <= threshold + path_km.

        allowance = SAFETY * dt^2 / 8 * (GRADIENT * (threshold + path_km) + A0)

    Any pair whose separation reaches the threshold inside the interval then has
    a chord closest approach of at most threshold + allowance / SAFETY, which is
    what makes the gate sound on intervals where both objects have both samples
    valid (other intervals: ``_validity_boundary_hits``). The bound assumes each
    object's SGP4 path is smooth across the interval with the acceleration
    ``accel_km_s2`` estimates. Elementwise over arrays. At 60 s and 10 km with
    A0 at the floor (1e-4 km/s^2 for the pair), two LEO objects (path
    2 x 474 km): 2.74 km; GEO pair (2 x 184 km): 1.14 km.
    """
    if interval_seconds <= 0.0:
        shaped = np.asarray(path_km) + np.asarray(accel_km_s2)
        return np.zeros_like(shaped, dtype=np.float64) if shaped.ndim else 0.0
    accel = _GRAVITY_GRADIENT_MAX_S2 * (threshold_km + path_km) + accel_km_s2
    return _GATE_SAFETY_FACTOR * interval_seconds**2 / 8.0 * accel


@dataclass(frozen=True, slots=True)
class CoarseEncounter:
    """One run of consecutive coarse intervals on which a pair passes the gate.

    Interval ``k`` spans coarse samples ``k`` and ``k + 1``. ``best_interval``
    and ``best_fraction`` locate the smallest chord closest approach of the run
    (fraction 0 is sample ``k``, 1 is sample ``k + 1``).
    """

    index_a: int
    index_b: int
    first_interval: int
    last_interval: int
    best_interval: int
    best_fraction: float
    chord_distance_km: float


class _GateHits(NamedTuple):
    """Every (pair, interval) passing the coarse gate, as parallel arrays."""

    index_a: np.ndarray
    index_b: np.ndarray
    interval: np.ndarray
    distance_km: np.ndarray
    fraction: np.ndarray


# (tca, miss_distance_km, relative_velocity_km_s,
#  pos_primary, vel_primary, pos_secondary, vel_secondary)
RefinedApproach = tuple[datetime, float, float, tuple, tuple, tuple, tuple]


# Historical conjunction event storage (bounded deque)
_conjunction_history: deque[ConjunctionEvent] = deque(maxlen=1000)


def get_conjunction_history(
    norad_id: int | None = None,
    limit: int = 100,
) -> list[ConjunctionEvent]:
    """Retrieve historical conjunction events, optionally filtered by NORAD ID."""
    events = list(_conjunction_history)
    if norad_id is not None:
        events = [e for e in events if e.primary == norad_id or e.secondary == norad_id]
    events.sort(key=lambda e: e.tca, reverse=True)
    return events[:limit]


def clear_conjunction_history() -> int:
    """Clear all historical conjunction events. Returns count cleared."""
    count = len(_conjunction_history)
    _conjunction_history.clear()
    return count


def classify_risk(miss_distance_km: float, relative_velocity_km_s: float) -> RiskLevel:
    """Assign risk level based on miss distance and relative velocity."""
    if miss_distance_km < 0.5:
        return RiskLevel.CRITICAL
    if miss_distance_km < 1.0:
        return RiskLevel.HIGH
    if miss_distance_km < 5.0 and relative_velocity_km_s > 10.0:
        return RiskLevel.HIGH
    if miss_distance_km < 5.0:
        return RiskLevel.MODERATE
    if miss_distance_km < 10.0:
        return RiskLevel.LOW
    return RiskLevel.NEGLIGIBLE


def _tle_age_hours(tle: TLERecord, at: datetime) -> float:
    """Age of a TLE (hours) at a given time; clamps negative ages to zero."""
    epoch = tle.epoch if tle.epoch.tzinfo else tle.epoch.replace(tzinfo=UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (at - epoch).total_seconds() / 3600.0)


class _PairSamples(NamedTuple):
    """Both objects sampled on one fine grid, aligned on shared instants."""

    times: list[datetime]
    offsets: np.ndarray  # integer grid index of each kept sample
    pos_a: np.ndarray
    vel_a: np.ndarray
    pos_b: np.ndarray
    vel_b: np.ndarray
    distance_km: np.ndarray


def _physical_mask(
    satrec: Satrec, jd: float, fr: float, seconds: np.ndarray, positions: np.ndarray
) -> np.ndarray:
    """Which code-0 states (``positions`` km at ``seconds`` after (jd, fr)) are physical.

    Physical: a finite position, and SGP4 code 0 with a finite position
    ``_PHYSICAL_PROBE_SECONDS`` before or after the instant, the position moving
    at most ``_MAX_PHYSICAL_SPEED_KM_S`` between the two.
    """
    seconds = np.asarray(seconds, dtype=np.float64).reshape(-1)
    here = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = int(seconds.size)
    if n == 0:
        return np.zeros(0, dtype=bool)
    probe = _PHYSICAL_PROBE_SECONDS
    instants = np.concatenate([seconds - probe, seconds + probe])
    err, r, _v = satrec.sgp4_array(np.full(instants.size, jd), fr + instants / 86400.0)
    pos = np.asarray(r, dtype=np.float64)
    limit = _MAX_PHYSICAL_SPEED_KM_S * probe
    with np.errstate(invalid="ignore", over="ignore"):
        ok = (np.asarray(err) == 0) & np.all(np.isfinite(pos), axis=1)
        moved_before = np.sqrt(np.sum((here - pos[:n]) ** 2, axis=1))
        moved_after = np.sqrt(np.sum((pos[n:] - here) ** 2, axis=1))
        physical: np.ndarray = np.all(np.isfinite(here), axis=1) & (
            (ok[:n] & (moved_before <= limit)) | (ok[n:] & (moved_after <= limit))
        )
    return physical


def _physical_states(
    satrec: Satrec, jd: float, fr: float, seconds: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SGP4 states ``seconds`` after the (jd, fr) instant, and which are physical.

    A state is physical when SGP4 returns code 0 for it and ``_physical_mask``
    holds. Returns (physical, positions km, velocities km/s), one row per
    instant.
    """
    seconds = np.asarray(seconds, dtype=np.float64).reshape(-1)
    err, r, v = satrec.sgp4_array(np.full(seconds.size, jd), fr + seconds / 86400.0)
    pos = np.asarray(r, dtype=np.float64).reshape(-1, 3)
    vel = np.asarray(v, dtype=np.float64).reshape(-1, 3)
    physical = (np.asarray(err) == 0) & _physical_mask(satrec, jd, fr, seconds, pos)
    return physical, pos, vel


def _instant_jd_pairs(start: datetime, seconds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(jd, fr) arrays for the instants ``seconds`` after ``start``, each equal
    bit for bit to ``datetime_to_jd(start + timedelta(seconds=s))``: the pair
    ``propagate_window_numpy``, hence ``_sample_pair``, gives SGP4 for that
    instant.

    ``fr(start) + s / 86400`` is not that pair: it differs in the last bit at
    about one instant in thirteen, enough to flip SGP4's error code at the edge
    of a gap (safety review, 2026-11-20). Whole-second offsets are converted
    with integer microseconds in ``jday``'s own order of operations; any other
    offset goes through ``datetime_to_jd`` one instant at a time.
    """
    seconds = np.asarray(seconds, dtype=np.float64).reshape(-1)
    if seconds.size == 0 or not bool(
        np.all((seconds == np.floor(seconds)) & (np.abs(seconds) < 2.0**40))
    ):
        pairs = [datetime_to_jd(start + timedelta(seconds=s)) for s in seconds.tolist()]
        jd_list = np.asarray([p[0] for p in pairs], dtype=np.float64)
        fr_list = np.asarray([p[1] for p in pairs], dtype=np.float64)
        return jd_list, fr_list
    into_day = ((start.hour * 60 + start.minute) * 60 + start.second) * 1_000_000
    micros = into_day + start.microsecond + seconds.astype(np.int64) * 1_000_000
    day, micros = np.divmod(micros, 86_400_000_000)
    hour, micros = np.divmod(micros, 3_600_000_000)
    minute, micros = np.divmod(micros, 60_000_000)
    second, micros = np.divmod(micros, 1_000_000)
    sec = second + micros / 1e6  # datetime_to_jd: dt.second + dt.microsecond / 1e6
    fr: np.ndarray = (sec + minute * 60.0 + hour * 3600.0) / 86400.0
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    days, which = np.unique(day, return_inverse=True)
    jd_of_day = np.asarray(
        [datetime_to_jd(midnight + timedelta(days=int(d)))[0] for d in days.tolist()],
        dtype=np.float64,
    )
    jd: np.ndarray = jd_of_day[which.reshape(-1)]
    return jd, fr


def _fine_grid_physical(satrec: Satrec, start: datetime, seconds: np.ndarray) -> np.ndarray:
    """Whether one object's state ``seconds`` after ``start`` is one the fine
    grid keeps, (n,).

    The test ``_sample_pair`` applies at the same floating-point times: SGP4
    at each instant's own ``datetime_to_jd`` pair (``_instant_jd_pairs``),
    code 0, and ``_physical_mask`` with its probes anchored at ``start``.
    """
    seconds = np.asarray(seconds, dtype=np.float64).reshape(-1)
    jd_i, fr_i = _instant_jd_pairs(start, seconds)
    err, r, _v = satrec.sgp4_array(jd_i, fr_i)
    pos = np.asarray(r, dtype=np.float64).reshape(-1, 3)
    jd, fr = datetime_to_jd(start)
    physical: np.ndarray = (np.asarray(err) == 0) & _physical_mask(
        satrec, jd, fr, seconds, pos
    )
    return physical


def _sample_pair(
    tle_a: TLERecord,
    tle_b: TLERecord,
    start: datetime,
    span_seconds: float,
    step_seconds: float,
    probe_anchor: datetime | None = None,
) -> _PairSamples:
    """SGP4 for both objects every ``step_seconds`` from ``start`` to at least
    ``start + span_seconds``, keeping only instants where both states are
    physical (code 0 and ``_physical_mask``); the rest are gaps, like SGP4
    errors. The position-rate probes are timed from ``probe_anchor`` (default
    ``start``), so a stretch of a longer grid sampled on its own keeps exactly
    the instants that grid keeps."""
    n_target = max(1, math.ceil(span_seconds / step_seconds - 1e-9) + 1)
    # Half a step of slack so the sample count never floors one short.
    hours = ((n_target - 1) * step_seconds + 0.5 * step_seconds) / 3600.0
    anchor = start if probe_anchor is None else probe_anchor
    jd, fr = datetime_to_jd(anchor)
    sampled = []
    for tle in (tle_a, tle_b):
        pos, vel, when = propagate_window_numpy(tle, start, hours, step_seconds)
        when = list(when)[: len(pos)]
        pos, vel = np.asarray(pos).reshape(-1, 3), np.asarray(vel).reshape(-1, 3)
        if when:
            seconds = np.asarray([(t - anchor).total_seconds() for t in when])
            keep = _physical_mask(build_satrec(tle), jd, fr, seconds, pos)
            pos, vel = pos[keep], vel[keep]
            when = [t for t, k in zip(when, keep.tolist(), strict=True) if k]
        sampled.append((pos, vel, when))
    (pos_a, vel_a, times_a), (pos_b, vel_b, times_b) = sampled

    if times_a == times_b:
        idx_a = idx_b = np.arange(len(times_a), dtype=np.int64)
        times = times_a
    else:
        # Failed and non-physical instants are dropped, so align on the time.
        where_b = {t: i for i, t in enumerate(times_b)}
        kept = [(i, where_b[t]) for i, t in enumerate(times_a) if t in where_b]
        idx_a = np.asarray([i for i, _ in kept], dtype=np.int64)
        idx_b = np.asarray([j for _, j in kept], dtype=np.int64)
        times = [times_a[i] for i in idx_a.tolist()]

    pa, va = pos_a[idx_a], vel_a[idx_a]
    pb, vb = pos_b[idx_b], vel_b[idx_b]
    offsets = np.asarray(
        [round((t - start).total_seconds() / step_seconds) for t in times], dtype=np.int64
    )
    rel = pa - pb
    distance = np.sqrt(np.sum(rel * rel, axis=1)) if len(times) else np.zeros(0)
    return _PairSamples(times, offsets, pa, va, pb, vb, distance)


def _state_tuple(values: np.ndarray) -> tuple[float, float, float]:
    return (float(values[0]), float(values[1]), float(values[2]))


def _approach_at(samples: _PairSamples, i: int) -> RefinedApproach:
    """The sampled state of fine-grid sample ``i`` as a refined approach."""
    return (
        samples.times[i],
        float(samples.distance_km[i]),
        float(np.linalg.norm(samples.vel_a[i] - samples.vel_b[i])),
        _state_tuple(samples.pos_a[i]),
        _state_tuple(samples.vel_a[i]),
        _state_tuple(samples.pos_b[i]),
        _state_tuple(samples.vel_b[i]),
    )


def _polish_minimum(
    tle_a: TLERecord,
    tle_b: TLERecord,
    samples: _PairSamples,
    i: int,
    step_seconds: float,
    satrecs: dict[str, Satrec],
) -> RefinedApproach:
    """Sub-step time of closest approach around fine-grid minimum ``i``.

    Squared distance is quadratic in time for straight-line relative motion, so
    the vertex of the parabola through the three grid values at i - 1, i, i + 1
    is the closest approach; over one 1 s grid step the curvature error is
    centimetres. Uses positions only. The result is confirmed with one SGP4
    evaluation of both objects and kept only if that real SGP4 separation is
    smaller than the grid value, so the reported miss is always an SGP4
    separation at the reported TCA, never an extrapolation. A minimum without
    both neighbour samples (grid end or SGP4 gap) goes to ``_polish_beside_gap``.
    """
    best = _approach_at(samples, i)
    n = len(samples.times)
    has_left = i > 0 and int(samples.offsets[i] - samples.offsets[i - 1]) == 1
    has_right = i < n - 1 and int(samples.offsets[i + 1] - samples.offsets[i]) == 1
    if not (has_left and has_right):
        return _polish_beside_gap(
            tle_a, tle_b, samples, i, step_seconds, satrecs, has_left, has_right
        )

    q0 = float(samples.distance_km[i - 1]) ** 2
    q1 = float(samples.distance_km[i]) ** 2
    q2 = float(samples.distance_km[i + 1]) ** 2
    curvature = q0 - 2.0 * q1 + q2
    if not curvature > 0.0:
        return best
    shift = min(1.0, max(-1.0, 0.5 * (q0 - q2) / curvature)) * step_seconds
    if abs(shift) < 1e-6:
        return best

    polished = _pair_state_at(tle_a, tle_b, samples.times[i] + timedelta(seconds=shift), satrecs)
    if polished is None or not polished[1] < best[1]:
        return best
    return polished


def _pair_state_at(
    tle_a: TLERecord, tle_b: TLERecord, at: datetime, satrecs: dict[str, Satrec]
) -> RefinedApproach | None:
    """Both objects' SGP4 states at one instant, or None unless both are
    physical (``_physical_states``)."""
    if "a" not in satrecs:
        satrecs["a"] = build_satrec(tle_a)
        satrecs["b"] = build_satrec(tle_b)
    jd, fr = datetime_to_jd(at)
    zero = np.zeros(1)
    ok_a, pos_a, vel_a = _physical_states(satrecs["a"], jd, fr, zero)
    ok_b, pos_b, vel_b = _physical_states(satrecs["b"], jd, fr, zero)
    if not (bool(ok_a[0]) and bool(ok_b[0])):
        return None
    pa, va, pb, vb = pos_a[0], vel_a[0], pos_b[0], vel_b[0]
    return (
        at,
        float(np.linalg.norm(pa - pb)),
        float(np.linalg.norm(va - vb)),
        _state_tuple(pa),
        _state_tuple(va),
        _state_tuple(pb),
        _state_tuple(vb),
    )


def _validity_edge(
    tle_a: TLERecord,
    tle_b: TLERecord,
    valid_at: datetime,
    invalid_at: datetime,
    satrecs: dict[str, Satrec],
) -> datetime:
    """Latest instant from ``valid_at`` toward ``invalid_at`` where both objects
    are still physical, by bisection (``_GAP_EDGE_BISECTIONS`` halvings)."""
    good, bad = valid_at, invalid_at
    for _ in range(_GAP_EDGE_BISECTIONS):
        mid = good + (bad - good) / 2
        if _pair_state_at(tle_a, tle_b, mid, satrecs) is None:
            bad = mid
        else:
            good = mid
    return good


def _golden_section_minimum(
    tle_a: TLERecord,
    tle_b: TLERecord,
    lo: datetime,
    hi: datetime,
    satrecs: dict[str, Satrec],
) -> RefinedApproach | None:
    """Smallest SGP4 separation found on [lo, hi] by golden-section search.

    Returns the closest state actually evaluated (never an interpolation), or
    None if no evaluated instant propagated. Instants where SGP4 fails count as
    infinitely far.
    """
    width = max(0.0, (hi - lo).total_seconds())
    best: RefinedApproach | None = None

    def separation(x: float) -> float:
        nonlocal best
        state = _pair_state_at(tle_a, tle_b, lo + timedelta(seconds=x), satrecs)
        if state is None:
            return math.inf
        if best is None or state[1] < best[1]:
            best = state
        return state[1]

    separation(0.0)
    separation(width)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    x0, x3 = 0.0, width
    x1, x2 = x3 - ratio * width, x0 + ratio * width
    f1, f2 = separation(x1), separation(x2)
    for _ in range(_GOLDEN_SECTION_STEPS):
        if f1 <= f2:
            x3, x2, f2 = x2, x1, f1
            x1 = x3 - ratio * (x3 - x0)
            f1 = separation(x1)
        else:
            x0, x1, f1 = x1, x2, f2
            x2 = x0 + ratio * (x3 - x0)
            f2 = separation(x2)
    return best


def _polish_beside_gap(
    tle_a: TLERecord,
    tle_b: TLERecord,
    samples: _PairSamples,
    i: int,
    step_seconds: float,
    satrecs: dict[str, Satrec],
    has_left: bool,
    has_right: bool,
) -> RefinedApproach:
    """Polish a grid minimum that lacks a neighbour sample on one or both sides.

    A missing neighbour is the end of the grid or an instant where one of the
    objects is not physical (an SGP4 failure or debris state). The true minimum
    can then lie between the sample and that instant, up to one step away,
    where the raw sample overstates the miss by up to the relative speed times
    one step. Each side reaches one step out, or only to the validity edge
    (bisected) where a state stops being physical on the way,
    and the separation is minimised over that stretch by golden-section search.
    The result replaces the grid sample only if its SGP4 separation is smaller.
    """
    best = _approach_at(samples, i)
    centre = samples.times[i]
    step = timedelta(seconds=step_seconds)
    reach: list[datetime] = []
    for has_neighbour, far in ((has_left, centre - step), (has_right, centre + step)):
        if has_neighbour or _pair_state_at(tle_a, tle_b, far, satrecs) is not None:
            reach.append(far)
        else:
            reach.append(_validity_edge(tle_a, tle_b, centre, far, satrecs))
    found = _golden_section_minimum(tle_a, tle_b, reach[0], reach[1], satrecs)
    if found is None or not found[1] < best[1]:
        return best
    return found


def _local_minima(distance_km: np.ndarray, offsets: np.ndarray | None = None) -> np.ndarray:
    """Indices of grid local minima; a flat stretch counts once, at its start.

    With ``offsets`` (grid index of each sample), samples on either side of a
    gap where SGP4 failed are not compared: each contiguous run is searched on
    its own, and a run end whose inner neighbour is larger counts as a minimum
    (the pair is still closing where one object stops propagating).
    """
    n = int(distance_km.shape[0])
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    left = np.ones(n, dtype=bool)
    right = np.ones(n, dtype=bool)
    left[1:] = distance_km[1:] < distance_km[:-1]
    right[:-1] = distance_km[:-1] <= distance_km[1:]
    if offsets is not None and n > 1:
        gap = np.diff(offsets) != 1
        left[1:] |= gap
        right[:-1] |= gap
    return np.nonzero(left & right)[0]


def _fine_minimum_near(
    tle_a: TLERecord,
    tle_b: TLERecord,
    centre: datetime,
    half_width_seconds: float,
    step_seconds: float,
    satrecs: dict[str, Satrec],
) -> RefinedApproach | None:
    """Polished minimum of a ``step_seconds`` grid over centre +/- half width.

    The grid reaches one fine step past each side, so a minimum inside the
    half width is an interior grid minimum and gets polished.
    """
    reach = half_width_seconds + step_seconds
    fine = _sample_pair(
        tle_a, tle_b, centre - timedelta(seconds=reach), 2.0 * reach, step_seconds
    )
    if len(fine.times) == 0:
        return None
    i = int(np.argmin(fine.distance_km))
    return _polish_minimum(tle_a, tle_b, fine, i, step_seconds, satrecs)


def _outward_minimum(
    tle_a: TLERecord,
    tle_b: TLERecord,
    edge: datetime,
    direction: int,
    walk_seconds: float,
    step_seconds: float,
    satrecs: dict[str, Satrec],
) -> RefinedApproach | None:
    """First separation minimum met walking from ``edge`` (direction +1 later, -1 earlier).

    Called when a refinement grid's smallest separation sits on the end of its
    span, i.e. the pair is still closing there. Samples every ``walk_seconds``
    until the separation grows, which brackets the minimum within one walk step
    of the last closing sample, then polishes it on the fine grid. If SGP4 stops
    returning states or ``_EDGE_SEARCH_MAX_SECONDS`` pass first, the closest
    sample reached is refined in the same way and returned (with a log line).
    """
    chunk = _EDGE_SEARCH_CHUNK_STEPS * walk_seconds
    origin = edge
    walked = 0.0
    prev_t: datetime | None = None
    prev_d = math.inf
    while walked < _EDGE_SEARCH_MAX_SECONDS:
        lo = origin if direction > 0 else origin - timedelta(seconds=chunk)
        samples = _sample_pair(tle_a, tle_b, lo, chunk, walk_seconds)
        n = len(samples.times)
        expected = np.arange(n, dtype=np.int64)
        complete = n == _EDGE_SEARCH_CHUNK_STEPS + 1 and bool(
            np.all(samples.offsets == expected)
        )
        order = range(n) if direction > 0 else range(n - 1, -1, -1)
        for i in order:
            d = float(samples.distance_km[i])
            if d > prev_d and prev_t is not None:
                return _fine_minimum_near(
                    tle_a, tle_b, prev_t, walk_seconds, step_seconds, satrecs
                )
            prev_t, prev_d = samples.times[i], d
        if not complete:
            # A gap in this chunk (SGP4 failed or a state was not physical).
            # The chunk's samples beyond it were already compared, so the walk
            # can cross one gap; ``straddles`` in the caller checks the result.
            break
        origin = lo + timedelta(seconds=chunk) if direction > 0 else lo
        walked += chunk
    if prev_t is None:
        return None
    logger.info(
        "refinement: %d/%d still closing %.0f s from the span edge; reporting the closest sample",
        tle_a.norad_id, tle_b.norad_id, walked,
    )
    return _fine_minimum_near(tle_a, tle_b, prev_t, walk_seconds, step_seconds, satrecs)


def _profile_allowance_km(
    positions: np.ndarray, present: np.ndarray, interval_seconds: float
) -> np.ndarray:
    """How far a pair's relative position can stray from its chord over each
    profile interval, km.

    ``positions`` (2, m + 3, 3) holds both objects at m + 1 profile samples
    with one slot before and one after, ``present`` (m + 3,) marks the filled
    slots: the layout of ``_non_point_mass_accel_km_s2``. Returns (m,), 0 on
    an interval with an end missing (the caller re-samples those).

    The same interpolation bound as the gate (``coarse_gate_allowance_km``):
    the relative position x stays within c * sup|x''| of its chord, c =
    SAFETY * dt^2 / 8, and |x''| <= GRADIENT * |x| + A0, A0 the sum of the two
    objects' non-point-mass estimates. Here |x| is bounded by the interval
    itself, not the threshold plus the objects' paths: the chord is never
    further from the origin than its farther end (separation M), so
    sup|x| <= M + allowance, which gives

        allowance = c * (GRADIENT * M + A0) / (1 - c * GRADIENT).

    At 60 s, c * GRADIENT = 2.8e-3; ``interval_seconds`` must stay below 1,141 s.
    """
    n_intervals = int(positions.shape[1]) - 3
    accel = _non_point_mass_accel_km_s2(
        positions, np.vstack([present, present]), interval_seconds
    ).sum(axis=0)
    rel = positions[0] - positions[1]
    separation = np.sqrt(np.sum(rel * rel, axis=1))
    lo, hi = slice(1, n_intervals + 1), slice(2, n_intervals + 2)
    both = present[lo] & present[hi]
    farther = np.where(both, np.maximum(separation[lo], separation[hi]), 0.0)
    c = _GATE_SAFETY_FACTOR * interval_seconds**2 / 8.0
    scale = c / (1.0 - c * _GRAVITY_GRADIENT_MAX_S2)
    return np.where(both, scale * (_GRAVITY_GRADIENT_MAX_S2 * farther + accel), 0.0)


def _intervals_physical(
    tle_a: TLERecord,
    tle_b: TLERecord,
    start: datetime,
    candidates: np.ndarray,
    ratio: int,
    step_seconds: float,
    same_elements: bool = False,
) -> np.ndarray:
    """Whether both objects are physical at every fine instant inside each
    candidate profile interval, (m,); False where ``candidates`` is False.

    Profile interval k runs from fine offset k * ratio to (k + 1) * ratio after
    ``start``; its two ends are profile samples, already known physical. The
    instants between them are checked with ``_fine_grid_physical``, the test
    ``_sample_pair`` applies on a fine grid from ``start`` at the same
    floating-point times, so an interval passes exactly when the fine-only
    path would have no gap there. With ``same_elements`` both objects return
    the same states, so one is checked. Exact for a whole-second
    ``step_seconds`` (the pipeline refines at ``_REFINE_STEP_SECONDS``, 1 s):
    with a fractional step the probe offsets here would be raw products, not
    the microsecond-rounded ``total_seconds()`` ``_sample_pair`` uses.
    """
    result = np.zeros(candidates.size, dtype=bool)
    chosen = np.flatnonzero(candidates)
    if chosen.size == 0:
        return result
    inner = np.arange(1, ratio, dtype=np.int64)
    seconds = ((chosen[:, None] * ratio + inner[None, :]) * step_seconds).reshape(-1)
    physical = np.ones(seconds.size, dtype=bool)
    for tle in (tle_a,) if same_elements else (tle_a, tle_b):
        physical &= _fine_grid_physical(build_satrec(tle), start, seconds)
    result[chosen] = physical.reshape(chosen.size, inner.size).all(axis=1)
    return result


# Everything SGP4 propagates from (with the same gravity model and mode, as
# ``build_satrec`` gives every object): two records equal in all of these
# return identical states at every instant.
_PROPAGATION_FIELDS = (
    "jdsatepoch", "jdsatepochF", "ndot", "nddot", "bstar",
    "inclo", "nodeo", "ecco", "argpo", "mo", "no_kozai",
)


def _same_elements(tle_a: TLERecord, tle_b: TLERecord) -> bool:
    """Whether two element sets propagate to identical states (a docked vehicle
    published with its station's elements, for example)."""
    sat_a, sat_b = build_satrec(tle_a), build_satrec(tle_b)
    return all(getattr(sat_a, f) == getattr(sat_b, f) for f in _PROPAGATION_FIELDS)


def _long_span_samples(
    tle_a: TLERecord,
    tle_b: TLERecord,
    start: datetime,
    span_seconds: float,
    threshold_km: float,
    profile_seconds: float,
    step_seconds: float,
    same_elements: bool = False,
) -> tuple[_PairSamples, np.ndarray]:
    """A long span sampled every ``step_seconds`` wherever the pair could cross
    the threshold or reach a new low, and every ``profile_seconds`` elsewhere.

    The span is profiled every ``profile_seconds`` (a whole number of fine
    steps). On each profile interval the separation lies between the chord's
    closest approach to the origin minus ``_profile_allowance_km`` (lower) and
    the larger end separation plus it (upper), each object's non-point-mass
    term estimated from its own profile samples exactly as the gate estimates
    it (``_non_point_mass_accel_km_s2``; measured 2026-11-19 over 806,159
    profile intervals of 2,320 co-orbiting pair windows at 60 s and 30 s, B*
    1e-5 to 0.1, perigee 160 to 800 km, e 0.0005 to 0.1, fresh and in the last
    hours before decay: chord departure at most 0.43 of the allowance). With
    ``same_elements`` (``_same_elements``) there is no allowance: the
    separation is 0 at every instant. An interval is re-sampled every
    ``step_seconds`` unless it is settled and

    - wholly above the threshold (lower > threshold), or
    - wholly within it (upper <= threshold) and nowhere below the lowest
      profile sample of its run of such intervals (lower >= that sample).

    Settled means both end samples and both samples beyond them are present,
    so each end has an acceleration estimate of its own: the first and last
    intervals (whose outer neighbour is not sampled) and every interval next
    to a missing sample (a state not physical, an SGP4 gap) are always
    re-sampled. An interval within the threshold is also left at the profile
    step only if both objects are physical at every fine instant inside it
    (``_intervals_physical``, at the fine grid's own floating-point times),
    so a gap the profile samples straddle splits the run there exactly as on
    the fine grid. Such gaps occur: code 6 on a grazing perigee, and code 1 on
    a drag-decaying near-circular orbit, whose mean eccentricity carries a
    once-per-orbit B* * C5 * sin(M) term in SGP4 (sgp4 ``propagation.py``
    ``tempe``) and dips below -0.001 for under a minute at a time, or for a
    few milliseconds on the orbit where the dip first appears (safety review,
    2026-11-20). Intervals wholly above the threshold are not checked: they
    are never joined, and the samples on both sides of a gap inside one are
    above the threshold either way. Re-sampled runs time their probes from
    ``start`` as well, so every fine sample kept is one the fine-only path
    keeps.

    Returns the samples on the fine grid (``offsets`` in fine steps from
    ``start``, clipped to the span) and ``joined``: for each pair of
    neighbouring samples, whether the separation is known to stay on their
    side of the threshold between them, either because they are adjacent fine
    samples or because they bound a settled interval left at the profile step
    that is wholly within the threshold. Neighbours across a missing sample or
    across an interval wholly above the threshold are not joined. A sample at
    the end of an interval left wholly within the threshold is never the
    lowest of its stretch: with any allowance the lower bound is below both
    end samples, so the interval next to the lowest sample of a run is always
    re-sampled (identical element sets, with no allowance, are 0 km at every
    sample).
    """
    ratio = max(1, int(round(profile_seconds / step_seconds)))
    profile_seconds = ratio * step_seconds
    profile = _sample_pair(tle_a, tle_b, start, span_seconds, profile_seconds)
    last_fine = max(0, math.ceil(span_seconds / step_seconds - 1e-9))
    n_intervals = max(1, math.ceil(span_seconds / profile_seconds - 1e-9))

    # Per profile offset (with one empty slot before and after for the
    # estimate's neighbours): both objects' positions and the pair separation.
    extended = n_intervals + 3
    positions = np.zeros((2, extended, 3))
    present = np.zeros(extended, dtype=bool)
    distance = np.full(extended, np.inf)
    slots = profile.offsets.astype(np.int64) + 1
    inside_grid = slots < extended
    slots = slots[inside_grid]
    positions[0, slots] = profile.pos_a[inside_grid]
    positions[1, slots] = profile.pos_b[inside_grid]
    present[slots] = True
    distance[slots] = profile.distance_km[inside_grid]

    lo, hi = slice(1, n_intervals + 1), slice(2, n_intervals + 2)
    both = present[lo] & present[hi]
    rel = positions[0] - positions[1]
    nearest, _ = _chord_closest_approach(rel[lo], rel[hi])
    farther = np.maximum(distance[lo], distance[hi])
    allowance = _profile_allowance_km(positions, present, profile_seconds)
    if same_elements:
        allowance = np.zeros_like(allowance)  # the separation is 0 at every instant
    lower = np.where(both, nearest - allowance, -np.inf)
    upper = np.where(both, farther + allowance, np.inf)
    # Bounds are trusted only where both ends carry an estimate of their own
    # (the samples either side of the interval are present). An interval
    # within the threshold may be joined, so it must also have no gap inside
    # (checked before runs are formed: a gap splits a run's stretch).
    settled = both & present[:n_intervals] & present[3 : n_intervals + 3]
    above = settled & (lower > threshold_km)
    within = settled & (upper <= threshold_km)
    within &= _intervals_physical(
        tle_a, tle_b, start, within, ratio, step_seconds, same_elements
    )

    # Run minima: an interval wholly within the threshold is left at the
    # profile step only if nothing in it can be lower than the lowest sample of
    # its run of such intervals (the run is part of one stretch).
    keep_within = np.zeros(n_intervals, dtype=bool)
    k = 0
    while k < n_intervals:
        if not within[k]:
            k += 1
            continue
        end = k
        while end + 1 < n_intervals and within[end + 1]:
            end += 1
        run_low = float(np.min(distance[k + 1 : end + 3]))
        keep_within[k : end + 1] = lower[k : end + 1] >= run_low
        k = end + 1
    coarse = above | keep_within

    # Union of the profile samples and the fine re-sampling, keyed by fine offset.
    rows: dict[int, tuple[datetime, np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]] = {}

    def add(samples: _PairSamples, first: int, scale: int) -> None:
        for j, t in enumerate(samples.times):
            fine = first + scale * int(samples.offsets[j])
            if fine <= last_fine:
                rows[fine] = (
                    t, samples.pos_a[j], samples.vel_a[j], samples.pos_b[j],
                    samples.vel_b[j], float(samples.distance_km[j]),
                )

    add(profile, 0, ratio)
    k = 0
    while k < n_intervals:
        if coarse[k]:
            k += 1
            continue
        end = k
        while end + 1 < n_intervals and not coarse[end + 1]:
            end += 1
        run_start = start + timedelta(seconds=k * profile_seconds)
        run_seconds = (end + 1 - k) * profile_seconds
        add(
            _sample_pair(tle_a, tle_b, run_start, run_seconds, step_seconds, start),
            k * ratio,
            1,
        )
        k = end + 1

    order = sorted(rows)
    offsets = np.asarray(order, dtype=np.int64)
    table = [rows[o] for o in order]
    samples = _PairSamples(
        [row[0] for row in table],
        offsets,
        np.asarray([row[1] for row in table]).reshape(-1, 3),
        np.asarray([row[2] for row in table]).reshape(-1, 3),
        np.asarray([row[3] for row in table]).reshape(-1, 3),
        np.asarray([row[4] for row in table]).reshape(-1, 3),
        np.asarray([row[5] for row in table], dtype=np.float64),
    )
    gaps = np.diff(offsets)
    left = offsets[:-1]
    whole_interval = (gaps == ratio) & (left % ratio == 0)
    interval = np.clip(left // ratio, 0, n_intervals - 1)
    joined = (gaps == 1) | (whole_interval & keep_within[interval])
    return samples, joined


def _refine_encounter(
    tle_a: TLERecord,
    tle_b: TLERecord,
    span_start: datetime,
    span_end: datetime,
    threshold_km: float,
    bracket_seconds: float = DEFAULT_COARSE_STEP_SECONDS,
    step_seconds: float = _REFINE_STEP_SECONDS,
) -> list[RefinedApproach]:
    """Every distinct approach within ``threshold_km`` of one encounter.

    The span is sampled from one fine step before ``span_start`` to one fine
    step after ``span_end``. A span up to ``_FINE_SPAN_MAX_SECONDS`` (the old
    +/- 5 min window; every fast encounter) is sampled with SGP4 every
    ``step_seconds``; a minimum inside the span is then always an interior grid
    minimum, and is polished to sub-second (``_polish_minimum``).

    A longer span means the pair passed the gate on many consecutive coarse
    intervals, which only slow relative motion does (co-orbiting objects). It
    is profiled every ``bracket_seconds`` (the coarse step), at most
    ``_LONG_SPAN_PROFILE_SECONDS``, and every profile interval is re-sampled
    every ``step_seconds`` unless an interpolation bound shows it wholly above
    the threshold, or wholly within it and nowhere below the lowest sample of
    its run (``_long_span_samples``). The first and last intervals and those
    next to a missing sample are always re-sampled, so both span ends are fine
    samples as on a short span, and an interval left within the threshold has
    no SGP4 gap or non-physical state inside it. The fine grid then
    covers every place the pair can cross the threshold or reach a stretch's
    lowest point, as it does for a short span, while a co-orbiting pair far
    inside or far outside the threshold keeps about one sample per profile
    step.

    A minimum on an end of the sampled span means the pair is still closing
    there and the true minimum lies outside the span; it is followed outward
    until bracketed (``_outward_minimum``). An approach found that way (the
    walk can cross an SGP4 gap) is kept only if the pair is physical and
    within the threshold at that end of the span, i.e. the approach straddles
    it. Otherwise the polished minimum of the span's own 1 s grid near that
    end replaces it, and counts if within the threshold: inside the window the
    gate puts every sub-threshold instant in a passing interval, hence in the
    span, and at a window edge an approach that is not within the threshold at
    the edge lies wholly outside the window. Identical element sets
    (``_same_elements``, a docked vehicle published with its station's
    elements) are 0 km apart at every instant and are not followed: a stretch
    open at the span start is reported at the span start.

    An approach is a maximal stretch where the separation stays at or under the
    threshold, so two dips separated by an excursion above it are reported
    separately, while a co-orbiting pair that never leaves the threshold is
    reported once, at its closest point. Excursions are found on the 1 s grid;
    a long span's intervals left at the profile step are wholly within the
    threshold or wholly above it.
    """
    pad = timedelta(seconds=step_seconds)
    sample_start = span_start - pad
    span_seconds = max(0.0, (span_end - span_start).total_seconds()) + 2.0 * step_seconds
    sample_end = sample_start + timedelta(seconds=span_seconds)
    walk_seconds = (
        bracket_seconds if bracket_seconds > step_seconds else DEFAULT_COARSE_STEP_SECONDS
    )
    satrecs: dict[str, Satrec] = {}
    same = _same_elements(tle_a, tle_b)
    fine_only = (
        span_seconds <= _FINE_SPAN_MAX_SECONDS + 2.0 * step_seconds
        or bracket_seconds <= step_seconds
    )
    if fine_only:
        profile = _sample_pair(tle_a, tle_b, sample_start, span_seconds, step_seconds)
        joined = np.diff(profile.offsets) == 1
    else:
        profile, joined = _long_span_samples(
            tle_a, tle_b, sample_start, span_seconds, threshold_km,
            min(bracket_seconds, _LONG_SPAN_PROFILE_SECONDS), step_seconds, same,
        )
    n = len(profile.times)
    if n == 0:
        return []
    last_offset = max(0, math.ceil(span_seconds / step_seconds - 1e-9))
    # Neighbours that are not joined count as separated by a gap, both for the
    # local minima and for grouping minima into one stretch.
    chain = np.concatenate(([0], np.cumsum(np.where(joined, 1, 2)))).astype(np.int64)
    fine_link = np.diff(profile.offsets) == 1
    coarse_link = joined & ~fine_link

    def straddles(approach: RefinedApproach) -> bool:
        # Outside the sampled span (only the outward walk reaches there, and
        # it can cross an SGP4 gap) an approach counts only if the pair is
        # physical and within the threshold at the nearer end of the span.
        when = approach[0]
        if sample_start <= when <= sample_end:
            return True
        at_end = _pair_state_at(
            tle_a, tle_b, sample_start if when < sample_start else sample_end, satrecs
        )
        return at_end is not None and at_end[1] <= threshold_km

    def in_span_minimum(centre: datetime) -> RefinedApproach | None:
        # Polished minimum of the fine grid over the sampled span within two
        # fine steps of centre, a sample of the span's fine grid.
        reach = timedelta(seconds=2.0 * step_seconds)
        lo, hi = max(sample_start, centre - reach), min(sample_end, centre + reach)
        fine = _sample_pair(tle_a, tle_b, lo, (hi - lo).total_seconds(), step_seconds)
        inside = sum(1 for t in fine.times if t <= hi)
        if inside == 0:
            return None
        fine = fine._replace(
            times=fine.times[:inside], offsets=fine.offsets[:inside],
            pos_a=fine.pos_a[:inside], vel_a=fine.vel_a[:inside],
            pos_b=fine.pos_b[:inside], vel_b=fine.vel_b[:inside],
            distance_km=fine.distance_km[:inside],
        )
        k = int(np.argmin(fine.distance_km))
        polished = _polish_minimum(tle_a, tle_b, fine, k, step_seconds, satrecs)
        return polished if straddles(polished) else _approach_at(fine, k)

    candidates: list[tuple[int, RefinedApproach]] = []
    for i in _local_minima(profile.distance_km, chain).tolist():
        at_start = i == 0 and int(profile.offsets[i]) == 0
        at_end = i == n - 1 and int(profile.offsets[i]) == last_offset
        approach: RefinedApproach | None = None
        if at_start or at_end:
            if float(profile.distance_km[i]) > threshold_km:
                continue
            if same:
                # Identical element sets are 0 km apart at every instant, so
                # there is nothing to follow: a stretch open at the span start
                # is reported there, one open at its end at the last sample.
                at = _pair_state_at(tle_a, tle_b, span_start, satrecs) if at_start else None
                approach = at or _approach_at(profile, i)
            else:
                approach = _outward_minimum(
                    tle_a, tle_b, profile.times[i], -1 if at_start else 1,
                    walk_seconds, step_seconds, satrecs,
                )
        elif (i > 0 and coarse_link[i - 1]) or (i < n - 1 and coarse_link[i]):
            # Next to an interval left at the profile step: never the lowest
            # point of its stretch (``_long_span_samples``), so not polished.
            approach = _approach_at(profile, i)
        else:
            approach = _polish_minimum(tle_a, tle_b, profile, i, step_seconds, satrecs)
        if approach is not None and not straddles(approach):
            # Found outside the span without reaching into it (above the
            # threshold or not physical at the span end, for example across an
            # SGP4 gap): what lies inside the span near this minimum counts.
            approach = in_span_minimum(profile.times[i])
        if approach is not None:
            candidates.append((i, approach))

    kept = [(i, approach) for i, approach in candidates if approach[1] <= threshold_km]
    grouped: list[tuple[int, RefinedApproach]] = []
    for i, approach in kept:
        if grouped:
            prev_i, prev = grouped[-1]
            between = profile.distance_km[prev_i : i + 1]
            contiguous = bool(np.all(np.diff(chain[prev_i : i + 1]) == 1))
            merged = contiguous and float(np.max(between)) <= threshold_km
            if merged:
                if approach[1] < prev[1]:
                    grouped[-1] = (i, approach)
                continue
        grouped.append((i, approach))
    return [approach for _, approach in grouped]


def _refine_conjunction(
    tle_a: TLERecord,
    tle_b: TLERecord,
    coarse_tca: datetime,
    window_minutes: float = 5.0,
    step_seconds: float = 1.0,
) -> tuple[datetime, float, float, tuple, tuple, tuple, tuple]:
    """Closest approach within +/- ``window_minutes`` of a single coarse time.

    Kept for callers that hold one coarse time; the pipeline itself refines
    whole encounters with ``_refine_encounter``. The grid minimum is polished to
    sub-second like every other refinement.

    Returns (refined_tca, miss_distance_km, relative_velocity_km_s,
             pos_primary, vel_primary, pos_secondary, vel_secondary).
    """
    start = coarse_tca - timedelta(minutes=window_minutes)
    samples = _sample_pair(tle_a, tle_b, start, 2 * window_minutes * 60.0, step_seconds)
    if len(samples.times) == 0:
        return coarse_tca, float("inf"), 0.0, (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)
    min_idx = int(np.argmin(samples.distance_km))
    return _polish_minimum(tle_a, tle_b, samples, min_idx, step_seconds, {})


# --------------------------------------------------------------------------- #
# Phase 1: coarse screening strategies
# --------------------------------------------------------------------------- #


def resolve_screening_method(method: str, n_objects: int) -> str:
    """Turn the caller's ``method`` into a concrete strategy name.

    "auto" picks "spatial" once the catalog reaches SPATIAL_AUTO_MIN_OBJECTS
    objects (64), below which the blocked all-pairs tensor is faster than
    building an index at every coarse step.
    """
    if method == "auto":
        return "spatial" if n_objects >= SPATIAL_AUTO_MIN_OBJECTS else "allpairs"
    if method in {"allpairs", "spatial"}:
        return method
    raise ValueError(f"unknown screening method {method!r}")


def _allpairs_block_pairs(n_steps: int) -> int:
    """Pairs per block so the block's relative-position, chord and closest-point
    tensors (about four (pairs, steps, 3) float64 arrays) stay under the
    memory ceiling."""
    per_pair = max(1, n_steps * 3 * 8 * 4)
    return max(1, _ALLPAIRS_BLOCK_BYTES // per_pair)


def _upper_triangle_rows(
    n_objects: int, max_pairs: int
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """The pairs (a, b), a < b, of ``n_objects`` in row order, a few rows at a time.

    Each chunk holds whole rows and at most ``max_pairs`` pairs (one row if a
    single row is longer). Concatenated, the chunks equal
    ``np.triu_indices(n_objects, k=1)``.
    """
    row = 0
    while row < n_objects - 1:
        stop = row + 1
        count = n_objects - 1 - row
        while stop < n_objects - 1 and count + (n_objects - 1 - stop) <= max_pairs:
            count += n_objects - 1 - stop
            stop += 1
        rows = np.arange(row, stop, dtype=np.int64)
        lengths = n_objects - 1 - rows
        index_a = np.repeat(rows, lengths)
        row_first = np.repeat(np.cumsum(lengths) - lengths, lengths)
        index_b = index_a + 1 + (np.arange(count, dtype=np.int64) - row_first)
        yield index_a, index_b
        row = stop


def _chord_closest_approach(r0: np.ndarray, r1: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Closest approach to the origin of the straight chord r0 -> r1.

    ``r0`` and ``r1`` are relative positions (..., 3) at the two ends of a coarse
    interval. Returns (distance_km, fraction) with fraction in [0, 1] (0 at r0).
    Elementwise only, so the all-pairs and spatial paths get bit-identical
    values for the same pair and interval.

    Adapted from SIMPLETON ``analyticalMin``
    (https://github.com/Salad109/SIMPLETON, MIT, (c) 2025 Salad109,
    src/main/java/io/salad109/conjunctiondetector/conjunction/internal/ScanService.java,
    commit bae7a0522174611195cf52e734387069b6ee7042).
    """
    dr = r1 - r0
    r_dot_d = r0[..., 0] * dr[..., 0] + r0[..., 1] * dr[..., 1] + r0[..., 2] * dr[..., 2]
    d_dot_d = dr[..., 0] * dr[..., 0] + dr[..., 1] * dr[..., 1] + dr[..., 2] * dr[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        fraction = np.where(d_dot_d > 0.0, -r_dot_d / d_dot_d, 0.0)
    fraction = np.clip(np.nan_to_num(fraction, nan=0.0), 0.0, 1.0)
    point = r0 + fraction[..., None] * dr
    distance = np.sqrt(
        point[..., 0] * point[..., 0]
        + point[..., 1] * point[..., 1]
        + point[..., 2] * point[..., 2]
    )
    return distance, fraction


def _empty_hits() -> _GateHits:
    def ints() -> np.ndarray:
        return np.zeros(0, dtype=np.int64)

    def floats() -> np.ndarray:
        return np.zeros(0, dtype=np.float64)

    return _GateHits(ints(), ints(), ints(), floats(), floats())


def _concat_hits(parts: list[_GateHits]) -> _GateHits:
    if not parts:
        return _empty_hits()
    return _GateHits(*(np.concatenate([getattr(p, f) for p in parts]) for f in _GateHits._fields))


def _interval_displacement_km(positions: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Each object's displacement over each coarse interval, (n_objects, n_intervals).

    Zero where either end is invalid. A pair's chord length is at most the sum
    of its two objects' displacements: |dr| <= |dp_a| + |dp_b|.
    """
    step = positions[:, 1:, :] - positions[:, :-1, :]
    both = valid[:, 1:] & valid[:, :-1]
    with np.errstate(invalid="ignore", over="ignore"):
        length = np.sqrt(np.sum(step * step, axis=2))
    return np.where(both & np.isfinite(length), length, 0.0)


def _non_point_mass_accel_km_s2(
    positions: np.ndarray, valid: np.ndarray, interval_seconds: float
) -> np.ndarray:
    """Each object's acceleration beyond point-mass gravity on each interval, km/s^2.

    ``positions`` and ``valid`` carry one extra sample before the window and one
    after it, so every in-window sample has both neighbours: shape (n, m + 3)
    for m intervals. Returns (n, m).

    At a sample k with valid neighbours, Numerov's identity
    p[k+1] - 2 p[k] + p[k-1] = dt^2 / 12 * (a[k-1] + 10 a[k] + a[k+1]) + O(dt^6)
    gives a weighted mean of the true acceleration a over [t_k-1, t_k+1];
    subtracting the same weighting of point-mass gravity leaves the weighted
    mean of everything else (J2, drag, SGP4 theory terms). An interval takes
    ``_NON_POINT_MASS_ESTIMATE_FACTOR`` times the larger of its two end values
    and never less than ``_NON_POINT_MASS_FLOOR_KM_S2``. An interval with an
    end that has no value (a neighbouring sample failed or was dropped:
    elements breaking down) takes ``_NON_POINT_MASS_FALLBACK_KM_S2``. Intervals
    the object is not valid on are never screened and get 0.
    """
    n_objects = int(positions.shape[0])
    n_intervals = int(positions.shape[1]) - 3
    if n_intervals < 1:
        return np.zeros((n_objects, max(0, n_intervals)))
    if interval_seconds <= 0.0:
        return np.zeros((n_objects, n_intervals))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        radius = np.sqrt(np.sum(positions * positions, axis=2))
        gravity = -EARTH_MU_KM3_S2 * positions / (radius**3)[..., None]
        second = (positions[:, 2:] - 2.0 * positions[:, 1:-1] + positions[:, :-2]) / (
            interval_seconds**2
        )
        weighted = (gravity[:, :-2] + 10.0 * gravity[:, 1:-1] + gravity[:, 2:]) / 12.0
        excess = np.sqrt(np.sum((second - weighted) ** 2, axis=2))
    # excess[:, q] belongs to in-window sample q (extended sample q + 1).
    known = valid[:, :-2] & valid[:, 1:-1] & valid[:, 2:] & np.isfinite(excess)
    lo_known, hi_known = known[:, :-1], known[:, 1:]
    larger = np.maximum(
        np.where(lo_known, excess[:, :-1], 0.0), np.where(hi_known, excess[:, 1:], 0.0)
    )
    per_interval = np.where(
        lo_known & hi_known,
        np.maximum(_NON_POINT_MASS_ESTIMATE_FACTOR * larger, _NON_POINT_MASS_FLOOR_KM_S2),
        _NON_POINT_MASS_FALLBACK_KM_S2,
    )
    screened = valid[:, 1:-2] & valid[:, 2:-1]
    return np.where(screened, per_interval, 0.0)


def _radial_bands_km(
    positions: np.ndarray, valid: np.ndarray, interval_seconds: float, accel_km_s2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Lower and upper bounds on each object's geocentric radius over the window.

    Bounded from the samples, so no allowance for mean versus osculating
    elements or decay is needed: SGP4 radii were measured up to 317 km below the
    mean-element perigee in a 6 h window. On an interval the true position stays
    within SAFETY * dt^2 / 8 * (mu / R^2 + non-point-mass) of the chord between
    the two samples (the gate's interpolation bound for one object); the chord's
    radius lies between its closest approach to the Earth's centre and its
    larger end radius. Objects with no valid interval get an empty band.
    """
    ok = valid[:, :-1] & valid[:, 1:]
    p0, p1 = positions[:, :-1, :], positions[:, 1:, :]
    with np.errstate(invalid="ignore", over="ignore"):
        nearest, _ = _chord_closest_approach(p0, p1)
        r0 = np.sqrt(np.sum(p0 * p0, axis=2))
        r1 = np.sqrt(np.sum(p1 * p1, axis=2))
    sag = (
        _GATE_SAFETY_FACTOR * interval_seconds**2 / 8.0
        * (_POINT_MASS_GRAVITY_MAX_KM_S2 + accel_km_s2)
    )
    low = np.where(ok, nearest - sag, np.inf).min(axis=1, initial=np.inf)
    high = np.where(ok, np.maximum(r0, r1) + sag, -np.inf).max(axis=1, initial=-np.inf)
    return low, high


def _pair_gate_km(
    threshold_km: float,
    interval_seconds: float,
    disp_a: np.ndarray,
    disp_b: np.ndarray,
    accel_a: np.ndarray | float = _NON_POINT_MASS_FLOOR_KM_S2,
    accel_b: np.ndarray | float = _NON_POINT_MASS_FLOOR_KM_S2,
) -> np.ndarray:
    """Gate distance for each pair-interval: threshold plus its own allowance.

    Elementwise and written once, so both screening paths compute bit-identical
    gates for the same pair and interval.
    """
    allowance = coarse_gate_allowance_km(
        threshold_km, interval_seconds, disp_a + disp_b, np.asarray(accel_a) + np.asarray(accel_b)
    )
    return threshold_km + np.asarray(allowance)


def _coarse_hits_allpairs(
    positions: np.ndarray,
    valid: np.ndarray,
    band_low_km: np.ndarray,
    band_high_km: np.ndarray,
    displacement_km: np.ndarray,
    accel_km_s2: np.ndarray,
    threshold_km: float,
    interval_seconds: float,
    shell_margin_km: float,
    shell_prefilter: bool,
) -> _GateHits:
    """Every surviving pair (a < b) tested on every interval, in pair blocks.

    Pairs are generated a few rows at a time (``_upper_triangle_rows``), so
    memory does not grow with the square of the catalog. Pairs the shell
    prefilter drops are never propagated into the chord tensors. Per pair and
    interval the values are computed with the same elementwise operations as
    the spatial path, so both yield the same hits.
    """
    n_objects, n_steps = int(positions.shape[0]), int(positions.shape[1])
    if n_objects < 2 or n_steps < 2:
        return _empty_hits()

    parts: list[_GateHits] = []
    block = _allpairs_block_pairs(n_steps)
    for index_a, index_b in _upper_triangle_rows(
        n_objects, max(block, _ALLPAIRS_PAIR_CHUNK)
    ):
        if shell_prefilter:
            overlap = np.asarray(
                shells_overlap(
                    band_low_km[index_a], band_high_km[index_a],
                    band_low_km[index_b], band_high_km[index_b],
                    shell_margin_km,
                )
            )
            index_a, index_b = index_a[overlap], index_b[overlap]

        for lo in range(0, int(index_a.size), block):
            a = index_a[lo : lo + block]
            b = index_b[lo : lo + block]
            rel = positions[a] - positions[b]
            with np.errstate(invalid="ignore", over="ignore"):
                dist, frac = _chord_closest_approach(rel[:, :-1, :], rel[:, 1:, :])
            interval_valid = valid[a, :-1] & valid[b, :-1] & valid[a, 1:] & valid[b, 1:]
            gates = _pair_gate_km(
                threshold_km, interval_seconds,
                displacement_km[a], displacement_km[b], accel_km_s2[a], accel_km_s2[b],
            )
            with np.errstate(invalid="ignore"):
                keep = interval_valid & (dist <= gates)

            pi, pk = np.nonzero(keep)
            if pi.size:
                parts.append(
                    _GateHits(
                        a[pi].astype(np.int64),
                        b[pi].astype(np.int64),
                        pk.astype(np.int64),
                        dist[pi, pk],
                        frac[pi, pk],
                    )
                )
    return _concat_hits(parts)


def _coarse_hits_spatial(
    positions: np.ndarray,
    valid: np.ndarray,
    band_low_km: np.ndarray,
    band_high_km: np.ndarray,
    displacement_km: np.ndarray,
    accel_km_s2: np.ndarray,
    threshold_km: float,
    interval_seconds: float,
    shell_margin_km: float,
    shell_prefilter: bool,
    spatial_backend: str = "auto",
) -> _GateHits:
    """Per-sample neighbour query, then the same chord gate as all-pairs.

    With M_j the largest object displacement and N_j the largest per-object
    non-point-mass acceleration on interval j, every pair's gate on j is at most
    G_j = threshold + allowance(path 2 M_j, acceleration 2 N_j) (the allowance
    grows with both). Query radius at sample k: max over its two intervals j of
    G_j + M_j. A pair passing its gate on interval j has a chord point within
    G_j of the origin, so its nearer chord end is within
    G_j + |dr| / 2 <= G_j + M_j: every passing interval has at least one end
    inside the query radius, and is evaluated from there.
    Index-pair ordering, prefilter and the per-interval formula match the
    all-pairs path, so the two produce the same hits. Adapted from the satguard
    vectorized screen (MIT) for the per-step index.
    """
    n_objects, n_steps = int(positions.shape[0]), int(positions.shape[1])
    n_intervals = n_steps - 1
    if n_objects < 2 or n_intervals < 1:
        return _empty_hits()

    max_disp = displacement_km.max(axis=0)
    max_accel = accel_km_s2.max(axis=0)
    reach = (
        _pair_gate_km(threshold_km, interval_seconds, max_disp, max_disp, max_accel, max_accel)
        + max_disp
    )
    parts: list[_GateHits] = []
    for sample in range(n_steps):
        adjacent = [j for j in (sample - 1, sample) if 0 <= j < n_intervals]
        radius = max(float(reach[j]) for j in adjacent)
        # Pad against rounding at the boundary; it can only add pairs.
        radius = radius * (1.0 + 1e-9) + 1e-6
        live = np.nonzero(valid[:, sample])[0]
        if live.size < 2:
            continue
        pairs = query_pairs_within(positions[live, sample, :], radius, backend=spatial_backend)
        if pairs.shape[0] == 0:
            continue

        ia = live[pairs[:, 0]]
        ib = live[pairs[:, 1]]
        lo = np.minimum(ia, ib)
        hi = np.maximum(ia, ib)
        if shell_prefilter:
            overlap = np.asarray(
                shells_overlap(
                    band_low_km[lo], band_high_km[lo], band_low_km[hi], band_high_km[hi],
                    shell_margin_km,
                )
            )
            lo, hi = lo[overlap], hi[overlap]
            if lo.size == 0:
                continue

        for j in adjacent:
            ok = valid[lo, j] & valid[hi, j] & valid[lo, j + 1] & valid[hi, j + 1]
            a, b = lo[ok], hi[ok]
            if a.size == 0:
                continue
            r0 = positions[a, j, :] - positions[b, j, :]
            r1 = positions[a, j + 1, :] - positions[b, j + 1, :]
            dist, frac = _chord_closest_approach(r0, r1)
            gates = _pair_gate_km(
                threshold_km, interval_seconds,
                displacement_km[a, j], displacement_km[b, j],
                accel_km_s2[a, j], accel_km_s2[b, j],
            )
            keep = dist <= gates
            if np.any(keep):
                parts.append(
                    _GateHits(
                        a[keep].astype(np.int64),
                        b[keep].astype(np.int64),
                        np.full(int(keep.sum()), j, dtype=np.int64),
                        dist[keep],
                        frac[keep],
                    )
                )

    hits = _concat_hits(parts)
    if hits.index_a.size == 0:
        return hits
    # An interval found from both of its ends appears twice with equal values.
    key = (hits.index_a * n_objects + hits.index_b) * n_intervals + hits.interval
    _, first = np.unique(key, return_index=True)
    return _GateHits(*(getattr(hits, f)[first] for f in _GateHits._fields))


def _confirm_physical_samples(
    tles: list[TLERecord],
    first_sample: datetime,
    interval_seconds: float,
    valid: np.ndarray,
) -> np.ndarray:
    """Validity mask with coarse samples that are not physical cleared.

    Checked (``_physical_states``) for every object that has at least one
    failed sample on the grid: SGP4's code-0 states with non-physical motion
    come after an element set has decayed, and those objects all fail
    somewhere. An object valid on every sample is taken as physical; its
    interval steps have already passed ``_drop_unphysical_states``.
    """
    if interval_seconds <= 0.0 or valid.shape[1] == 0:
        return valid
    suspect = np.nonzero(valid.any(axis=1) & ~valid.all(axis=1))[0]
    if suspect.size == 0:
        return valid
    cleaned = valid.copy()
    jd, fr = datetime_to_jd(first_sample)
    for obj in suspect.tolist():
        samples = np.nonzero(valid[obj])[0]
        physical, _pos, _vel = _physical_states(
            build_satrec(tles[obj]), jd, fr, samples * interval_seconds
        )
        cleaned[obj, samples[~physical]] = False
    return cleaned


def _validity_scan_anchors(
    tles: list[TLERecord],
    start: datetime,
    valid: np.ndarray,
    interval_seconds: float,
) -> dict[tuple[int, int], np.ndarray]:
    """Physical states inside every coarse interval where an object lacks a valid sample.

    SGP4 validity can change inside an interval (decay, error flips, states
    cleared as not physical), so an object with one or both samples failed can
    still be physical for part of the interval. Each such interval is scanned
    from its first sample to its last at a spacing of at most
    ``_VALIDITY_SCAN_SECONDS``. A scan state is physical when it and a
    neighbouring scan state both have SGP4 code 0 and finite positions, and the
    position moves at most ``_MAX_PHYSICAL_SPEED_KM_S`` over the spacing; a
    code-0 scan state without such a neighbour is tested with
    ``_physical_mask``. The interval's valid coarse samples are physical too.
    Returns {(object, interval): (m, 3) positions km of its physical scan
    states}; intervals without any are left out.
    """
    anchors: dict[tuple[int, int], np.ndarray] = {}
    if interval_seconds <= 0.0 or valid.shape[1] < 2:
        return anchors
    special = ~valid[:, :-1] | ~valid[:, 1:]
    if not special.any():
        return anchors
    per_interval = max(1, math.ceil(interval_seconds / _VALIDITY_SCAN_SECONDS - 1e-9))
    spacing = interval_seconds / per_interval
    offsets = spacing * np.arange(per_interval + 1, dtype=np.float64)
    limit = _MAX_PHYSICAL_SPEED_KM_S * spacing
    jd, fr = datetime_to_jd(start)
    for obj in np.nonzero(special.any(axis=1))[0].tolist():
        intervals = np.nonzero(special[obj])[0]
        seconds = (intervals[:, None] * interval_seconds + offsets[None, :]).ravel()
        satrec = build_satrec(tles[obj])
        err, r, _v = satrec.sgp4_array(np.full(seconds.size, jd), fr + seconds / 86400.0)
        shape = (intervals.size, offsets.size)
        pos = np.asarray(r, dtype=np.float64).reshape(*shape, 3)
        with np.errstate(invalid="ignore", over="ignore"):
            ok = (np.asarray(err).reshape(shape) == 0) & np.all(np.isfinite(pos), axis=2)
            moved = np.sqrt(np.sum((pos[:, 1:] - pos[:, :-1]) ** 2, axis=2))
            segment = ok[:, 1:] & ok[:, :-1] & (moved <= limit)
        physical = np.zeros(shape, dtype=bool)
        physical[:, 1:] |= segment
        physical[:, :-1] |= segment
        # A valid coarse sample is physical (``_confirm_physical_samples`` checked
        # it, since this object fails on the grid) and anchors both intervals it
        # bounds, however soon after it the object fails or before it recovers.
        physical[:, 0] |= valid[obj, intervals] & ok[:, 0]
        physical[:, -1] |= valid[obj, intervals + 1] & ok[:, -1]
        # A code-0 scan state with no code-0 scan neighbour is tested directly.
        lone = ok & ~physical
        if lone.any():
            physical[lone] = _physical_mask(
                satrec, jd, fr, seconds.reshape(shape)[lone], pos[lone]
            )
        for row in np.nonzero(physical.any(axis=1))[0].tolist():
            anchors[(obj, int(intervals[row]))] = pos[row, physical[row]]
    return anchors


def _anchor_reach_km(threshold_km: float, interval_seconds: float) -> float:
    """Anchor distance within which ``_validity_boundary_hits`` keeps a pair.

    Each object of a pair that comes within the threshold at an instant of the
    interval has an anchor that it reaches from there at no more than
    ``_MAX_PHYSICAL_SPEED_KM_S`` (V) within one interval, so the two anchors lie
    within threshold + 2 V dt (1,450 km at 60 s and a 10 km threshold).
    """
    return threshold_km + 2.0 * _MAX_PHYSICAL_SPEED_KM_S * interval_seconds


def _validity_boundary_hits(
    positions: np.ndarray,
    valid: np.ndarray,
    anchors: dict[tuple[int, int], np.ndarray],
    threshold_km: float,
    interval_seconds: float,
) -> _GateHits:
    """Pairs on intervals where an object lacks a valid sample.

    The chord gate needs both samples of both objects. On an interval where an
    object has a failed sample, its anchors are its physical scan states
    (``_validity_scan_anchors``, at most ``_VALIDITY_SCAN_SECONDS`` apart); an
    object valid at both samples has those two samples as anchors. A physical
    approach instant lies in a stretch holding an anchor of the same interval
    (a physical scan state or a valid coarse sample), or between an object's
    two valid samples, and each object moves at most V from there, so the
    pair's anchors are within ``_anchor_reach_km``. Pairs with any anchor pair
    that close pass and are refined like any other encounter. The shell
    prefilter is not applied: its bands only cover full intervals. Shared by
    both screening paths, so they keep identical hits.
    """
    n_objects, n_steps = int(valid.shape[0]), int(valid.shape[1])
    if n_objects < 2 or n_steps < 2 or interval_seconds <= 0.0 or not anchors:
        return _empty_hits()
    reach = _anchor_reach_km(threshold_km, interval_seconds)
    by_interval: dict[int, list[tuple[int, np.ndarray]]] = {}
    for (obj, j), points in anchors.items():
        by_interval.setdefault(j, []).append((obj, points))

    parts: list[_GateHits] = []
    for j, scanned in sorted(by_interval.items()):
        regular = valid[:, j] & valid[:, j + 1]
        ends = np.where(regular[:, None, None], positions[:, j : j + 2], np.nan)
        for a, points in scanned:
            nearest = np.full(n_objects, np.inf)
            for end in (0, 1):
                diff = points[:, None, :] - ends[None, :, end, :]
                with np.errstate(invalid="ignore"):
                    dist = np.sqrt(np.sum(diff * diff, axis=2)).min(axis=0)
                nearest = np.fmin(nearest, dist)
            for b, other in scanned:
                if b != a:
                    diff = points[:, None, :] - other[None, :, :]
                    closest = float(np.sqrt(np.sum(diff * diff, axis=2)).min())
                    nearest[b] = min(float(nearest[b]), closest)
            nearest[a] = np.inf
            others = np.nonzero(nearest <= reach)[0]
            if others.size == 0:
                continue
            parts.append(
                _GateHits(
                    np.minimum(others, a).astype(np.int64),
                    np.maximum(others, a).astype(np.int64),
                    np.full(others.size, j, dtype=np.int64),
                    nearest[others],
                    np.full(others.size, 0.5),
                )
            )
    hits = _concat_hits(parts)
    if hits.index_a.size == 0:
        return hits
    key = (hits.index_a * n_objects + hits.index_b) * (n_steps - 1) + hits.interval
    _, first = np.unique(key, return_index=True)
    return _GateHits(*(getattr(hits, f)[first] for f in _GateHits._fields))


def _group_encounters(hits: _GateHits) -> list[CoarseEncounter]:
    """Split each pair's passing intervals into runs of consecutive intervals.

    One run is one encounter; the best interval of a run is its smallest chord
    distance, the earliest on a tie. Grouping by gaps follows SIMPLETON
    ``groupAndReduce`` (MIT, ScanService.java, commit bae7a05, see
    ``_chord_closest_approach``).
    """
    if hits.index_a.size == 0:
        return []
    order = np.lexsort((hits.interval, hits.index_b, hits.index_a))
    ia = hits.index_a[order]
    ib = hits.index_b[order]
    k = hits.interval[order]
    dist = hits.distance_km[order]
    frac = hits.fraction[order]

    new_run = np.ones(ia.size, dtype=bool)
    new_run[1:] = (ia[1:] != ia[:-1]) | (ib[1:] != ib[:-1]) | (k[1:] != k[:-1] + 1)
    starts = np.nonzero(new_run)[0]
    ends = np.append(starts[1:], ia.size) - 1
    run_id = np.cumsum(new_run) - 1

    by_best = np.lexsort((k, dist, run_id))
    _, first_of_run = np.unique(run_id[by_best], return_index=True)
    best = by_best[first_of_run]

    return [
        CoarseEncounter(
            index_a=int(ia[s]),
            index_b=int(ib[s]),
            first_interval=int(k[s]),
            last_interval=int(k[e]),
            best_interval=int(k[b]),
            best_fraction=float(frac[b]),
            chord_distance_km=float(dist[b]),
        )
        for s, e, b in zip(starts.tolist(), ends.tolist(), best.tolist(), strict=True)
    ]


def _drop_unphysical_states(
    positions: np.ndarray, valid: np.ndarray, interval_seconds: float
) -> np.ndarray:
    """Validity mask with both ends of any faster-than-escape interval cleared.

    See ``_MAX_PHYSICAL_SPEED_KM_S``. Such samples are treated like SGP4 errors.
    """
    if interval_seconds <= 0.0 or positions.shape[1] < 2:
        return valid
    step = positions[:, 1:, :] - positions[:, :-1, :]
    with np.errstate(invalid="ignore", over="ignore"):
        length = np.sqrt(np.sum(step * step, axis=2))
    both = valid[:, 1:] & valid[:, :-1]
    runaway = both & ~(length <= _MAX_PHYSICAL_SPEED_KM_S * interval_seconds)
    if not np.any(runaway):
        return valid
    cleaned = valid.copy()
    cleaned[:, :-1] &= ~runaway
    cleaned[:, 1:] &= ~runaway
    logger.info(
        "coarse screen: %d object(s) with SGP4 states faster than %.1f km/s treated as invalid",
        int(np.any(runaway, axis=1).sum()), _MAX_PHYSICAL_SPEED_KM_S,
    )
    return cleaned


def _coarse_grid(
    tles: list[TLERecord],
    start: datetime,
    hours: float,
    coarse_step_seconds: float,
) -> tuple[np.ndarray, np.ndarray, list[datetime], float]:
    """Catalog positions on a coarse grid whose last in-window sample is the window end.

    The step is shrunk just enough that a whole number of intervals spans the
    window, so the gate covers every instant from ``start`` to the end. The
    position and validity arrays carry one extra sample before ``start`` and
    one after the end (for ``_non_point_mass_accel_km_s2``); ``times`` lists the
    in-window samples only. Returns (positions, valid, times, interval_seconds).
    """
    window_s = max(0.0, hours * 3600.0)
    if window_s <= 0.0:
        positions, _vel, valid, times = propagate_catalog_numpy(tles, start, 0.0, 1.0)
        # A zero-length window is one instant: a zero-length interval on it.
        return (
            np.repeat(positions[:, :1, :], 4, axis=1),
            np.repeat(valid[:, :1], 4, axis=1),
            [times[0], times[0]],
            0.0,
        )
    n_intervals = max(1, math.ceil(window_s / coarse_step_seconds - 1e-9))
    step = window_s / n_intervals
    lead = timedelta(seconds=step)
    # Half a step of slack so the sample count never floors one short.
    grid_hours = (window_s + 2.5 * step) / 3600.0
    positions, _vel, valid, times = propagate_catalog_numpy(tles, start - lead, grid_hours, step)
    keep = n_intervals + 3
    return positions[:, :keep], valid[:, :keep], times[1 : n_intervals + 2], step


def screen_encounters(
    tles: list[TLERecord],
    start: datetime,
    hours: float = 24.0,
    threshold_km: float = 10.0,
    method: str = "auto",
    shell_prefilter: bool = True,
    coarse_step_seconds: float = DEFAULT_COARSE_STEP_SECONDS,
    spatial_backend: str = "auto",
) -> tuple[list[CoarseEncounter], list[datetime], str]:
    """Phase 1: every encounter that can come within ``threshold_km``.

    Returns (encounters, times, resolved_method). Interval ``k`` of an encounter
    spans ``times[k]`` to ``times[k + 1]``. A coarse sample is valid when SGP4
    returns code 0 and a finite position, the step to a valid neighbour sample
    implies at most ``_MAX_PHYSICAL_SPEED_KM_S`` (``_drop_unphysical_states``)
    and, for an object that fails anywhere on the grid, the state is physical
    (``_confirm_physical_samples``). For any step up to
    ``_MAX_COARSE_STEP_SECONDS``, a pair whose SGP4 separation reaches the
    threshold at an instant where both objects' states are physical (code 0,
    moving at most ``_MAX_PHYSICAL_SPEED_KM_S`` over the surrounding second) is
    kept on the interval holding that instant:

    - where both objects have both samples of the interval valid, by the chord
      gate (``coarse_gate_allowance_km``, which assumes the path between the
      two samples is smooth); the shell prefilter only drops pairs whose
      sampled radial bands (``_radial_bands_km``) stay further apart than the
      widest gate, which no gate-passing pair can do;
    - where an object has a failed sample, by the anchor test of
      ``_validity_boundary_hits`` on its physical states from a scan of the
      whole interval at ``_VALIDITY_SCAN_SECONDS`` (``_validity_scan_anchors``).

    Not covered: a physical stretch (under two scan spacings long) in which no
    scan state and no coarse sample is physical, and motion faster than the
    speed bound between physical states less than a second apart. States that
    are not physical (nonzero code, or a decayed object's code-0 debris states)
    are never screened or reported.
    """
    resolved = resolve_screening_method(method, len(tles))
    if not 0.0 < coarse_step_seconds <= _MAX_COARSE_STEP_SECONDS:
        raise ValueError(
            f"coarse_step_seconds must be in (0, {_MAX_COARSE_STEP_SECONDS:g}] s, "
            f"got {coarse_step_seconds!r}"
        )
    if len(tles) < 2 or hours < 0.0:
        return [], [], resolved

    positions_ext, valid_ext, times, interval_s = _coarse_grid(
        tles, start, hours, coarse_step_seconds
    )
    if len(times) < 2:
        return [], [], resolved

    valid_ext = _drop_unphysical_states(positions_ext, valid_ext, interval_s)
    valid_ext = _confirm_physical_samples(
        tles, times[0] - timedelta(seconds=interval_s), interval_s, valid_ext
    )
    accel = _non_point_mass_accel_km_s2(positions_ext, valid_ext, interval_s)
    positions = positions_ext[:, 1:-1]
    valid = valid_ext[:, 1:-1]
    displacement = _interval_displacement_km(positions, valid)

    widest_disp = float(displacement.max()) if displacement.size else 0.0
    widest_accel = float(accel.max()) if accel.size else 0.0
    widest_gate = float(
        _pair_gate_km(
            threshold_km, interval_s, np.asarray(widest_disp), np.asarray(widest_disp),
            widest_accel, widest_accel,
        )
    )
    shell_margin = widest_gate * (1.0 + 1e-9) + 1e-6
    band_low, band_high = _radial_bands_km(positions, valid, interval_s, accel)

    if resolved == "spatial":
        hits = _coarse_hits_spatial(
            positions, valid, band_low, band_high, displacement, accel, threshold_km,
            interval_s, shell_margin, shell_prefilter, spatial_backend,
        )
    else:
        hits = _coarse_hits_allpairs(
            positions, valid, band_low, band_high, displacement, accel, threshold_km,
            interval_s, shell_margin, shell_prefilter,
        )
    anchors = _validity_scan_anchors(tles, times[0], valid, interval_s)
    boundary = _validity_boundary_hits(positions, valid, anchors, threshold_km, interval_s)
    if boundary.index_a.size:
        hits = _concat_hits([hits, boundary])
    return _group_encounters(hits), times, resolved


def screen_coarse(
    tles: list[TLERecord],
    start: datetime,
    hours: float = 24.0,
    coarse_margin_km: float = 100.0,
    method: str = "auto",
    shell_prefilter: bool = True,
    coarse_step_seconds: float = DEFAULT_COARSE_STEP_SECONDS,
    spatial_backend: str = "auto",
) -> tuple[list[CoarseCandidate], list[datetime], str]:
    """Phase 1 collapsed to one candidate per pair (its closest encounter).

    Returns (candidates, times, resolved_method). Uses the same sound gate as
    ``screen_encounters`` with ``coarse_margin_km`` as the distance: every pair
    that can come within it between samples is returned. Each candidate carries
    indices into ``tles``, the pair's smallest chord closest approach in km, and
    the coarse sample nearest that approach. Exposed so
    scripts/bench_screening.py can time the screen without the refinement; the
    pipeline uses ``screen_encounters``, which keeps every encounter.
    """
    encounters, times, resolved = screen_encounters(
        tles, start, hours=hours, threshold_km=coarse_margin_km, method=method,
        shell_prefilter=shell_prefilter, coarse_step_seconds=coarse_step_seconds,
        spatial_backend=spatial_backend,
    )
    best: dict[tuple[int, int], CoarseEncounter] = {}
    for enc in encounters:
        key = (enc.index_a, enc.index_b)
        prior = best.get(key)
        if prior is None or enc.chord_distance_km < prior.chord_distance_km:
            best[key] = enc
    candidates: list[CoarseCandidate] = [
        (
            enc.index_a,
            enc.index_b,
            enc.chord_distance_km,
            enc.best_interval + (1 if enc.best_fraction > 0.5 else 0),
        )
        for enc in best.values()
    ]
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates, times, resolved


# --------------------------------------------------------------------------- #
# Full pipeline
# --------------------------------------------------------------------------- #


def find_conjunctions(
    store: TLEStore,
    norad_ids: list[int] | None = None,
    hours: float = 24.0,
    threshold_km: float = 10.0,
    start: datetime | None = None,
    compute_probability: bool = True,
    method: str = "auto",
    shell_prefilter: bool = True,
    formation_rules: list[FormationRule] | tuple[FormationRule, ...] | None = None,
    spatial_backend: str = "auto",
    coarse_step_seconds: float = DEFAULT_COARSE_STEP_SECONDS,
) -> list[ConjunctionEvent]:
    """Run the full conjunction detection pipeline.

    Phase 1: coarse screen (``screen_encounters``), all-pairs or spatially
    indexed, with a gate that is sound for the step (``coarse_step_seconds`` up
    to 300 s): no pair that comes within ``threshold_km`` between samples is
    skipped (coverage and its limits: ``screen_encounters``).
    Phase 2: each encounter is refined on a 1 s SGP4 grid over the coarse
    intervals it spans and every grid minimum is polished to sub-second. An
    approach straddling a window edge (within the threshold at the edge) is
    followed past the edge to its real closest point. Only physical SGP4
    states are used (``_physical_states``): a decayed object's code-0 debris
    states never produce an event. One event is reported per distinct
    approach within the threshold (a stretch at or under it, resolved at 1 s),
    so a pair can appear more than once.

    ``method`` is "auto" (spatial above SPATIAL_AUTO_MIN_OBJECTS objects),
    "allpairs" or "spatial"; all three yield the same events. Events matching a
    known co-orbiting formation are flagged, never dropped.
    """
    if start is None:
        start = datetime.now(UTC)

    # Get satellites to analyze
    tles: list[TLERecord]
    if norad_ids:
        unique_ids = list(dict.fromkeys(norad_ids))
        tles = [t for nid in unique_ids if (t := store.get(nid)) is not None]
    else:
        tles = store.get_all()

    if len(tles) < 2:
        return []

    # Phase 1: coarse screen, one entry per encounter (sound gate, see
    # coarse_gate_allowance_km)
    encounters, times, resolved_method = screen_encounters(
        tles,
        start,
        hours=hours,
        threshold_km=threshold_km,
        method=method,
        shell_prefilter=shell_prefilter,
        coarse_step_seconds=coarse_step_seconds,
        spatial_backend=spatial_backend,
    )

    rules = formation_rules_with_extras(formation_rules)
    regimes = [classify_regime(t) for t in tles]
    interval_s = (times[1] - times[0]).total_seconds() if len(times) > 1 else 0.0

    # Phase 2: fine refinement over each encounter's own intervals. An approach
    # inside the window lies in a passing interval, so the span holds it; one
    # straddling a window edge is followed outward until its minimum is
    # bracketed (``_refine_encounter``), as the old +/- 5 min search intended.
    events: list[ConjunctionEvent] = []
    for encounter in encounters:
        index_a, index_b = encounter.index_a, encounter.index_b
        tle_a, tle_b = tles[index_a], tles[index_b]
        approaches = _refine_encounter(
            tle_a, tle_b,
            times[encounter.first_interval], times[encounter.last_interval + 1],
            threshold_km,
            bracket_seconds=interval_s,
        )

        for tca, miss_dist, rel_vel, pos_p, vel_p, pos_s, vel_s in approaches:
            risk = classify_risk(miss_dist, rel_vel)

            # Compute collision probability with TLE-age-scaled covariance:
            # stale TLEs get a larger position sigma (Vallado 2013, Ch. 9)
            pc_result = None
            if compute_probability:
                try:
                    pc_data = compute_pc_for_conjunction(
                        pos_primary_km=pos_p,
                        vel_primary_km_s=vel_p,
                        pos_secondary_km=pos_s,
                        vel_secondary_km_s=vel_s,
                        sigma_primary_km=sigma_from_tle_age(_tle_age_hours(tle_a, tca)),
                        sigma_secondary_km=sigma_from_tle_age(_tle_age_hours(tle_b, tca)),
                    )
                    pc_result = CollisionProbability(**pc_data)
                except Exception as exc:
                    logger.warning(
                        "Pc computation failed for %d/%d: %s",
                        tle_a.norad_id, tle_b.norad_id, exc,
                    )

            formation = match_formation(
                tle_a.norad_id, tle_a.name, tle_a.line1,
                tle_b.norad_id, tle_b.name, tle_b.line1,
                rules=rules,
            )

            event = ConjunctionEvent(
                primary=tle_a.norad_id,
                secondary=tle_b.norad_id,
                primary_name=tle_a.name,
                secondary_name=tle_b.name,
                tca=tca,
                miss_distance_km=round(miss_dist, 4),
                relative_velocity_km_s=round(rel_vel, 4),
                risk=risk,
                collision_probability=pc_result,
                formation_excluded=formation is not None,
                formation_label=None if formation is None else formation.label,
                screening_method=resolved_method,
                regime=pair_regime(regimes[index_a], regimes[index_b]),
            )
            event.risk_score = risk_score(event, now=start)
            events.append(event)
            _conjunction_history.append(event)

    # Sort: CRITICAL first, then by miss distance
    risk_order = {
        RiskLevel.CRITICAL: 0,
        RiskLevel.HIGH: 1,
        RiskLevel.MODERATE: 2,
        RiskLevel.LOW: 3,
        RiskLevel.NEGLIGIBLE: 4,
    }
    events.sort(key=lambda e: (risk_order[e.risk], e.miss_distance_km))
    return events


def screen_regime(
    store: TLEStore,
    regime: str,
    hours: float = 24.0,
    threshold_km: float | None = None,
    start: datetime | None = None,
    compute_probability: bool = True,
    method: str = "auto",
    include_formations: bool = True,
    limit: int | None = None,
    formation_rules: list[FormationRule] | tuple[FormationRule, ...] | None = None,
) -> RegimeSurveyResult:
    """All-vs-all survey restricted to one orbit regime, ranked by miss distance.

    Design follows orbveil's regime survey (Apache-2.0), implemented here from
    the TLE mean elements. The coarse threshold defaults per regime (GEO is the
    widest) because station-keeping boxes and along-track uncertainty grow with
    altitude. Formation pairs are flagged in the events; ``include_formations``
    controls whether they are listed at all.
    """
    label = regime.upper()
    effective_threshold = (
        default_threshold_for_regime(label) if threshold_km is None else float(threshold_km)
    )
    members = [t for t in store.get_all() if classify_regime(t) == label]

    events: list[ConjunctionEvent] = []
    resolved = resolve_screening_method(method, len(members))
    if len(members) >= 2:
        events = find_conjunctions(
            store,
            norad_ids=[t.norad_id for t in members],
            hours=hours,
            threshold_km=effective_threshold,
            start=start,
            compute_probability=compute_probability,
            method=method,
            formation_rules=formation_rules,
        )
    if not include_formations:
        events = [e for e in events if not e.formation_excluded]
    events.sort(key=lambda e: e.miss_distance_km)
    if limit is not None:
        events = events[:limit]

    for event in events:
        event.regime = label

    return RegimeSurveyResult(
        regime=label,
        threshold_km=effective_threshold,
        hours=hours,
        objects_screened=len(members),
        screening_method=resolved,
        events=events,
    )
