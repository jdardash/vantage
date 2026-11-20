"""Recall of the conjunction screen on fast and repeated close approaches.

The coarse screen used to keep a pair only when one 60 s sample fell within
threshold * 10 km. Two objects closing at 14 km/s cover 840 km per step, so an
approach under 1 km could sit between samples 150 to 220 km apart and was never
refined, and a pair kept at most one encounter per window. These tests lock the
sound coarse gate in ``sda.conjunction`` (``coarse_gate_allowance_km``) and the
per-encounter refinement against an independent dense reference. The last
section holds regressions from the safety review: minima beside a coarse
sample, approaches straddling a window edge, an approach only the allowance
keeps, the step cap, high-drag objects and the sampled radial bands.

Data: 32 objects drawn from the 2,000-object synthetic LEO catalog of
scripts/bench_screening.py (the gap report's case), screened for two hours from
2026-01-01T00:00Z. Offline and deterministic.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from sda import conjunction as cj
from sda.conjunction import coarse_gate_allowance_km, find_conjunctions, screen_encounters
from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import ConjunctionEvent, RiskLevel
from sda.propagator import build_satrec, datetime_to_jd
from sda.tle_store import TLEStore

START = datetime(2026, 1, 1, tzinfo=UTC)
HOURS = 2.0
THRESHOLD_KM = 10.0

# Independent reference: SGP4 every 5 s, every pair within 100 km refined on a
# 1 s grid. Sound for closing speeds up to (100 - 10) / 2.5 = 36 km/s, above the
# 22.4 km/s two-body maximum for bound orbits.
REF_STEP_S = 5.0
REF_RADIUS_KM = 100.0

# Matching tolerances between the pipeline and the reference.
TCA_TOL_S = 1.0
MISS_TOL_KM = 1.0e-3
VREL_TOL_KM_S = 1.0e-2

SUBSET_TLES = """\
SYNTH 70000
1 70000U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9997
2 70000  49.7343 137.5080 0006186 218.8034  73.3980 15.40857293    15
SYNTH 70086
1 70086U 13067B   26081.25673732  .00013092  00000+0  24096-3 0  9998
2 70086  85.8281  83.1960 0003303  95.4652 265.6260 15.54770332    17
SYNTH 70114
1 70114U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9993
2 70114  52.9343 333.4200 0006186 218.8034 160.7700 15.42870271    12
SYNTH 70178
1 70178U 15052A   26081.23666757  .00001905  00000+0  15067-3 0  9991
2 70178   5.4974 133.9320 0008475 235.2411 178.2420 14.86481839    14
SYNTH 70202
1 70202U 13067C   26081.25663059  .00013267  00000+0  24422-3 0  9996
2 70202  89.2280 194.1240 0003294  95.3041 139.7940 15.41443414    18
SYNTH 70236
1 70236U 08029A   26080.40802014  .00004079  00000+0  15270-3 0  9999
2 70236  26.7817 189.3960 0008270  13.7192 115.3260 15.21134764    10
SYNTH 70482
1 70482U 15052A   26081.23666757  .00001905  00000+0  15067-3 0  9999
2 70482   7.1974 176.3640 0008475 235.2411 171.2340 14.86630072    16
SYNTH 70516
1 70516U 25275A   26081.16296743  .00015089  00000+0  28686-3 0  9990
2 70516  52.1343 171.6360 0006186 218.8034 146.7660 15.42560582    19
SYNTH 70580
1 70580U 13067B   26081.25673732  .00013092  00000+0  24096-3 0  9997
2 70580  86.0281 332.1480 0003303  95.4652 164.2380 15.53066389    19
SYNTH 70620
1 70620U 13067C   26081.25663059  .00013267  00000+0  24422-3 0  9990
2 70620  85.9280  72.4680 0003294  95.3041 220.1580 15.43612061    14
SYNTH 70808
1 70808U 13067B   26081.25673732  .00013092  00000+0  24096-3 0  9990
2 70808  88.3281   3.9720 0003303  95.4652 338.9820 15.41448597    18
SYNTH 70859
1 70859U 25204A   26081.16296743  .00015089  00000+0  28686-3 0  9992
2 70859  53.6343 176.8800 0006186 218.8034 122.2800 15.48754361    12
SYNTH 70950
1 70950U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9991
2 70950  50.4343  90.1080 0006186 218.8034 321.4980 15.47205916    15
SYNTH 71000
1 71000U 13067C   26081.25663059  .00013267  00000+0  24422-3 0  9993
2 71000  87.0280 125.5080 0003294  95.3041  31.3980 15.55539620    12
SYNTH 71200
1 71200U 25275A   26081.16296743  .00015089  00000+0  28686-3 0  9991
2 71200  50.8343 267.1080 0006186 218.8034 310.9980 15.54638451    19
SYNTH 71216
1 71216U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9997
2 71216  52.4343 307.2360 0006186 218.8034  45.3660 15.41476671    15
SYNTH 71228
1 71228U 13067C   26081.25663059  .00013267  00000+0  24422-3 0  9995
2 71228  89.3280 157.3320 0003294  95.3041 206.1420 15.43921868    16
SYNTH 71352
1 71352U 25275A   26081.16296743  .00015089  00000+0  28686-3 0  9999
2 71352  49.6343 288.3240 0006186 218.8034 307.4940 15.46896227    11
SYNTH 71482
1 71482U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9992
2 71482  50.3343 164.3640 0006186 218.8034 129.2340 15.51386717    19
SYNTH 71494
1 71494U 13067C   26081.25663059  .00013267  00000+0  24422-3 0  9990
2 71494  87.2280  14.4600 0003294  95.3041 290.0100 15.53835683    19
SYNTH 71502
1 71502U 26031A   26081.16296743  .00015089  00000+0  28686-3 0  9997
2 71502  52.3343  34.5240 0006186 218.8034 157.1940 15.54483606    12
SYNTH 71565
1 71565U 04047A   26080.82427166  .00059144  00000+0  67680-3 0  9993
2 71565  19.3523  57.5280 0003768 173.8637 101.2680 15.60527684    18
SYNTH 71583
1 71583U 25241A   26073.34423285  .00013198  00000+0  56794-3 0  9992
2 71583  52.2357  12.6720 0005732 187.3443 342.4320 15.25803534    10
SYNTH 71596
1 71596U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9998
2 71596  53.5343   0.2760 0006186 218.8034 216.6060 15.53399695    12
SYNTH 71642
1 71642U 08029A   26080.40802014  .00004079  00000+0  15270-3 0  9991
2 71642  23.8817 205.6440 0008270  13.7192 352.9140 15.19916028    19
SYNTH 71723
1 71723U 15078A   26081.28781794  .00001601  00000+0  56754-4 0  9992
2 71723  95.6581 183.7920 0013139  31.4291 178.1520 15.23960384    19
SYNTH 71733
1 71733U 25204A   26081.16296743  .00015089  00000+0  28686-3 0  9994
2 71733  50.8343 118.8720 0006186 218.8034 192.1320 15.43334805    15
SYNTH 71768
1 71768U 26031A   26081.16296743  .00015089  00000+0  28686-3 0  9991
2 71768  50.2343 251.6520 0006186 218.8034 241.0620 15.48754361    16
SYNTH 71787
1 71787U 21035A   26081.25526573  .00025791  00000+0  29763-3 0  9996
2 71787  41.9659 344.3040 0005233 353.6422 195.6240 15.64588956    17
SYNTH 71789
1 71789U 22143A   26081.25526573  .00025791  00000+0  29763-3 0  9999
2 71789  42.1659 259.3200 0005233 353.6422 342.4200 15.64901218    13
SYNTH 71834
1 71834U 13067B   26081.25673732  .00013092  00000+0  24096-3 0  9990
2 71834  88.4281 327.1800 0003303  95.4652  45.3300 15.43927060    10
SYNTH 71939
1 71939U 21035A   26081.25526573  .00025791  00000+0  29763-3 0  9995
2 71939  40.7659   5.5200 0005233 353.6422 192.1200 15.56782405    11
"""


def _tle_block(norad_ids: set[int]) -> str:
    lines = SUBSET_TLES.splitlines()
    out: list[str] = []
    for i in range(0, len(lines), 3):
        if int(lines[i + 1][2:7]) in norad_ids:
            out.extend(lines[i : i + 3])
    return "\n".join(out) + "\n"


def _store(norad_ids: set[int] | None = None) -> TLEStore:
    store = TLEStore()
    text = SUBSET_TLES if norad_ids is None else _tle_block(norad_ids)
    store.load_from_text(text)
    return store


def _jd_arrays(times: list[datetime]) -> tuple[np.ndarray, np.ndarray]:
    pairs = [datetime_to_jd(t) for t in times]
    return np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])


def _golden_minimum(sat_a, sat_b, lo: datetime, width_s: float) -> tuple[datetime, float, float]:
    """Smallest SGP4 separation on [lo, lo + width_s] by golden-section search.

    Returns (time, separation km, relative speed km/s). Independent of the
    pipeline's parabola polish.
    """

    def state(x: float) -> tuple[float, float]:
        jd, fr = datetime_to_jd(lo + timedelta(seconds=x))
        err_a, r_a, v_a = sat_a.sgp4(jd, fr)
        err_b, r_b, v_b = sat_b.sgp4(jd, fr)
        if err_a or err_b:
            return math.inf, 0.0
        return (
            float(np.linalg.norm(np.subtract(r_a, r_b))),
            float(np.linalg.norm(np.subtract(v_a, v_b))),
        )

    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    x0, x3 = 0.0, width_s
    x1, x2 = x3 - ratio * (x3 - x0), x0 + ratio * (x3 - x0)
    f1, f2 = state(x1)[0], state(x2)[0]
    for _ in range(40):
        if f1 <= f2:
            x3, x2, f2 = x2, x1, f1
            x1 = x3 - ratio * (x3 - x0)
            f1 = state(x1)[0]
        else:
            x0, x1, f1 = x1, x2, f2
            x2 = x0 + ratio * (x3 - x0)
            f2 = state(x2)[0]
    x = 0.5 * (x0 + x3)
    miss, vrel = state(x)
    return lo + timedelta(seconds=x), miss, vrel


def _refine_run(sat_a, sat_b, lo: datetime, n_seconds: int) -> list[tuple[datetime, float, float]]:
    """Approaches within the threshold on a 1 s grid from ``lo``.

    Every interior grid minimum is polished by golden section over its two
    neighbouring seconds. An approach is a maximal stretch of the grid at or
    under the threshold; its miss is the lowest polished minimum inside it.
    """
    times = [lo + timedelta(seconds=m) for m in range(n_seconds + 1)]
    jd, fr = _jd_arrays(times)
    err_a, r_a, _ = sat_a.sgp4_array(jd, fr)
    err_b, r_b, _ = sat_b.sgp4_array(jd, fr)
    d = np.linalg.norm(r_a - r_b, axis=1)
    d[(err_a != 0) | (err_b != 0)] = np.inf
    minima: list[tuple[int, datetime, float, float]] = []
    for m in range(1, d.size - 1):
        if d[m] < d[m - 1] and d[m] <= d[m + 1]:
            tca, miss, vrel = _golden_minimum(sat_a, sat_b, times[m - 1], 2.0)
            if miss <= THRESHOLD_KM:
                minima.append((m, tca, miss, vrel))
    groups: list[tuple[int, datetime, float, float]] = []
    for item in minima:
        if groups and float(np.max(d[groups[-1][0] : item[0] + 1])) <= THRESHOLD_KM:
            if item[2] < groups[-1][2]:
                groups[-1] = item
        else:
            groups.append(item)
    return [(tca, miss, vrel) for _, tca, miss, vrel in groups]


def _dense_reference(store: TLEStore) -> list[dict]:
    """Every approach within the threshold, found without the coarse gate."""
    tles = store.get_all()
    satrecs = [build_satrec(t) for t in tles]
    n_samples = round(HOURS * 3600.0 / REF_STEP_S) + 1
    times = [START + timedelta(seconds=REF_STEP_S * k) for k in range(n_samples)]
    jd, fr = _jd_arrays(times)
    pos = np.empty((len(tles), n_samples, 3))
    ok = np.empty((len(tles), n_samples), dtype=bool)
    for i, sat in enumerate(satrecs):
        err, r, _ = sat.sgp4_array(jd, fr)
        pos[i] = r
        ok[i] = err == 0
    pad = timedelta(seconds=REF_STEP_S)
    found: list[dict] = []
    for a in range(len(tles)):
        for b in range(a + 1, len(tles)):
            d = np.linalg.norm(pos[a] - pos[b], axis=1)
            near = np.nonzero(ok[a] & ok[b] & (d <= REF_RADIUS_KM))[0]
            if near.size == 0:
                continue
            cut = np.nonzero(np.diff(near) > 1)[0]
            for k0, k1 in zip(
                np.r_[near[0], near[cut + 1]].tolist(),
                np.r_[near[cut], near[-1]].tolist(),
                strict=True,
            ):
                span = round((k1 - k0) * REF_STEP_S + 2 * REF_STEP_S)
                for tca, miss, vrel in _refine_run(
                    satrecs[a], satrecs[b], times[k0] - pad, span
                ):
                    found.append({
                        "pair": frozenset((tles[a].norad_id, tles[b].norad_id)),
                        "tca": tca, "miss": miss, "vrel": vrel,
                    })
    return found


@pytest.fixture(scope="module")
def subset_store() -> TLEStore:
    return _store()


@pytest.fixture(scope="module")
def reference(subset_store: TLEStore) -> list[dict]:
    return _dense_reference(subset_store)


@pytest.fixture(scope="module")
def pipeline_events(subset_store: TLEStore) -> list[ConjunctionEvent]:
    return find_conjunctions(
        subset_store, hours=HOURS, threshold_km=THRESHOLD_KM, start=START,
        compute_probability=False,
    )


def _events_for(events: list[ConjunctionEvent], a: int, b: int) -> list[ConjunctionEvent]:
    return sorted(
        (e for e in events if {e.primary, e.secondary} == {a, b}), key=lambda e: e.tca
    )


def _matches(event: ConjunctionEvent, ref: dict) -> bool:
    return (
        frozenset((event.primary, event.secondary)) == ref["pair"]
        and abs((event.tca - ref["tca"]).total_seconds()) <= TCA_TOL_S
        and abs(event.miss_distance_km - ref["miss"]) <= MISS_TOL_KM
        and abs(event.relative_velocity_km_s - ref["vrel"]) <= VREL_TOL_KM_S
    )


# --------------------------------------------------------------------------- #
# The gate allowance
# --------------------------------------------------------------------------- #


def test_gate_allowance_follows_its_derivation():
    gradient = 2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**3
    assert gradient == pytest.approx(3.0725e-6, rel=1e-4)
    expected = 2.0 * 60.0**2 / 8.0 * (gradient * (10.0 + 948.0) + 1.0e-4)
    got = coarse_gate_allowance_km(10.0, 60.0, 948.0)
    assert got == pytest.approx(expected, rel=1e-12)
    assert got == pytest.approx(2.739, abs=1e-3)


def test_gate_allowance_is_elementwise_and_zero_without_an_interval():
    paths = np.array([0.0, 368.0, 948.0])
    got = coarse_gate_allowance_km(10.0, 60.0, paths)
    assert isinstance(got, np.ndarray)
    assert got.shape == paths.shape
    assert np.all(np.diff(got) > 0.0)
    assert got[1] == pytest.approx(1.135, abs=1e-3)  # GEO pair, 2 x 184 km
    assert coarse_gate_allowance_km(10.0, 0.0, 948.0) == 0.0
    assert np.all(coarse_gate_allowance_km(10.0, 0.0, paths) == 0.0)
    # Grows with the square of the step.
    assert coarse_gate_allowance_km(10.0, 120.0, 948.0) > 3.9 * coarse_gate_allowance_km(
        10.0, 60.0, 948.0
    )


def test_chord_deviation_stays_inside_the_unscaled_bound():
    """Measured SGP4 curvature never exceeds the bound the gate is built on.

    For every pair of the subset and every 60 s interval of the window, the
    largest distance between the true relative position (1 s samples) and the
    chord through the interval's end samples must be at most
    dt^2 / 8 * (GRADIENT * D + A0), D the largest separation on the interval,
    before the safety factor is applied.
    """
    tles = _store().get_all()
    satrecs = [build_satrec(t) for t in tles]
    step = 60
    n_intervals = round(HOURS * 3600.0) // step
    times = [START + timedelta(seconds=s) for s in range(n_intervals * step + 1)]
    jd, fr = _jd_arrays(times)
    pos = np.stack([sat.sgp4_array(jd, fr)[1] for sat in satrecs])
    idx = step * np.arange(n_intervals)[:, None] + np.arange(step + 1)[None, :]
    frac = (np.arange(step + 1) / step)[None, None, :, None]
    seg = pos[:, idx, :]  # (objects, intervals, step + 1, 3)
    ends = pos[:, ::step, :]
    chord = ends[:, :-1, None, :] + frac * (ends[:, 1:, None, :] - ends[:, :-1, None, :])
    # A chord is linear in its end points, so a pair's departure from its
    # relative chord is the difference of the two objects' own departures.
    off = seg - chord
    worst = 0.0
    for a in range(len(tles) - 1):
        dev = off[a][None] - off[a + 1 :]
        rel = seg[a][None] - seg[a + 1 :]
        deviation = np.sqrt(np.einsum("pikx,pikx->pik", dev, dev).max(axis=2))
        separation = np.sqrt(np.einsum("pikx,pikx->pik", rel, rel).max(axis=2))
        bound = coarse_gate_allowance_km(0.0, float(step), separation) / cj._GATE_SAFETY_FACTOR
        worst = max(worst, float(np.max(deviation / bound)))
    assert worst <= 1.0, f"chord deviation reached {worst:.3f} of the unscaled bound"


# --------------------------------------------------------------------------- #
# Fast close approaches the old gate missed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("a", "b"), [(70086, 71200), (70950, 71768)])
def test_old_sixty_second_samples_sit_far_from_the_fast_approaches(a: int, b: int):
    """Documents the defect: no 60 s sample came within 10 x threshold."""
    sat_a, sat_b = (build_satrec(t) for t in _store({a, b}).get_all())
    times = [START + timedelta(seconds=60 * k) for k in range(round(HOURS * 60) + 1)]
    jd, fr = _jd_arrays(times)
    d = np.linalg.norm(sat_a.sgp4_array(jd, fr)[1] - sat_b.sgp4_array(jd, fr)[1], axis=1)
    assert float(d.min()) > 10.0 * THRESHOLD_KM


def test_fast_head_on_approach_and_second_encounter_of_one_pair():
    store = _store({70086, 71200})
    events = _events_for(
        find_conjunctions(store, hours=HOURS, threshold_km=THRESHOLD_KM, start=START),
        70086, 71200,
    )
    assert len(events) == 2, [(e.tca, e.miss_distance_km) for e in events]
    second, first = events[0], events[1]

    assert first.miss_distance_km == pytest.approx(0.6702, abs=1e-3)
    assert first.relative_velocity_km_s == pytest.approx(14.24, abs=0.01)
    assert first.relative_velocity_km_s > 10.0
    assert first.risk == RiskLevel.HIGH
    assert abs((first.tca - datetime(2026, 1, 1, 1, 27, 41, 420000, tzinfo=UTC))
               .total_seconds()) <= 0.5

    assert second.miss_distance_km == pytest.approx(7.4044, abs=1e-3)
    assert second.risk == RiskLevel.LOW
    assert abs((second.tca - datetime(2026, 1, 1, 0, 41, 15, 640000, tzinfo=UTC))
               .total_seconds()) <= 0.5


def test_second_fast_pair_under_one_km_is_found():
    store = _store({70950, 71768})
    events = _events_for(
        find_conjunctions(store, hours=HOURS, threshold_km=THRESHOLD_KM, start=START),
        70950, 71768,
    )
    assert len(events) == 1
    event = events[0]
    assert event.miss_distance_km == pytest.approx(0.8909, abs=1e-3)
    assert event.relative_velocity_km_s == pytest.approx(11.68, abs=0.01)
    assert event.risk == RiskLevel.HIGH
    assert abs((event.tca - datetime(2026, 1, 1, 0, 21, 14, 860000, tzinfo=UTC))
               .total_seconds()) <= 0.5


# --------------------------------------------------------------------------- #
# Dense reference comparison
# --------------------------------------------------------------------------- #


def test_reference_case_is_not_trivial(reference: list[dict]):
    assert len(reference) >= 30
    assert sum(1 for r in reference if r["miss"] < 1.0 and r["vrel"] > 10.0) >= 2
    pairs = [r["pair"] for r in reference]
    assert any(pairs.count(p) >= 2 for p in pairs)


def test_dense_reference_recall_is_complete(
    reference: list[dict], pipeline_events: list[ConjunctionEvent]
):
    missed = [r for r in reference if not any(_matches(e, r) for e in pipeline_events)]
    assert not missed, f"{len(missed)} of {len(reference)} reference approaches missed: {missed}"


def test_every_reported_event_is_a_reference_approach(
    reference: list[dict], pipeline_events: list[ConjunctionEvent]
):
    extra = [e for e in pipeline_events if not any(_matches(e, r) for r in reference)]
    assert not extra, [(e.primary, e.secondary, e.tca, e.miss_distance_km) for e in extra]
    assert len(pipeline_events) == len(reference)


def test_events_sorted_critical_first_then_by_miss(pipeline_events: list[ConjunctionEvent]):
    order = {
        RiskLevel.CRITICAL: 0, RiskLevel.HIGH: 1, RiskLevel.MODERATE: 2,
        RiskLevel.LOW: 3, RiskLevel.NEGLIGIBLE: 4,
    }
    keys = [(order[e.risk], e.miss_distance_km) for e in pipeline_events]
    assert len(keys) > 1
    assert keys == sorted(keys)


@pytest.mark.parametrize("backend", ["grid", "auto"])
def test_allpairs_and_spatial_screens_agree(backend: str):
    tles = _store().get_all()

    def run(method: str) -> list[tuple]:
        encounters, _times, resolved = screen_encounters(
            tles, START, hours=HOURS, threshold_km=THRESHOLD_KM, method=method,
            spatial_backend=backend,
        )
        assert resolved == method
        return sorted(
            (e.index_a, e.index_b, e.first_interval, e.last_interval, e.best_interval,
             round(e.chord_distance_km, 9))
            for e in encounters
        )

    allpairs = run("allpairs")
    assert allpairs
    assert run("spatial") == allpairs


# --------------------------------------------------------------------------- #
# Window edges and invalid states
# --------------------------------------------------------------------------- #


def test_window_ending_inside_a_coarse_step_keeps_the_approach():
    """The 0.67 km approach is 3.6 s before a window end that is no step multiple."""
    store = _store({70086, 71200})
    end = datetime(2026, 1, 1, 1, 27, 45, tzinfo=UTC)
    hours = (end - START).total_seconds() / 3600.0
    events = find_conjunctions(
        store, hours=hours, threshold_km=THRESHOLD_KM, start=START, compute_probability=False,
    )
    assert any(e.miss_distance_km == pytest.approx(0.6702, abs=1e-3) for e in events)


def test_window_shorter_than_one_step_keeps_the_approach():
    store = _store({70086, 71200})
    start = datetime(2026, 1, 1, 1, 27, 30, tzinfo=UTC)
    events = find_conjunctions(
        store, hours=36.0 / 3600.0, threshold_km=THRESHOLD_KM, start=start,
        compute_probability=False,
    )
    assert len(events) == 1
    assert events[0].miss_distance_km == pytest.approx(0.6702, abs=1e-3)


def test_runaway_states_are_treated_as_invalid():
    positions = np.zeros((3, 4, 3))
    positions[:, :, 0] = 7000.0
    positions[1, :, 1] = 11.0 * 60.0 * np.arange(4)  # 11 km/s: physical
    positions[2, 2, 0] = 1.0e9  # one runaway sample
    valid = np.ones((3, 4), dtype=bool)
    cleaned = cj._drop_unphysical_states(positions, valid, 60.0)
    assert cleaned[0].all()
    assert cleaned[1].all()
    assert cleaned[2].tolist() == [True, False, False, False]
    assert valid.all()  # input untouched


def test_screen_rejects_a_non_positive_step():
    with pytest.raises(ValueError, match="coarse_step_seconds"):
        screen_encounters(_store().get_all(), START, hours=1.0, coarse_step_seconds=0.0)


def test_screen_rejects_a_step_above_the_derived_cap():
    tles = _store({70086, 71200}).get_all()
    with pytest.raises(ValueError, match="coarse_step_seconds"):
        screen_encounters(
            tles, START, hours=1.0, coarse_step_seconds=301.0
        )


# --------------------------------------------------------------------------- #
# Regressions found by the safety review
# --------------------------------------------------------------------------- #

FAST_TCA = datetime(2026, 1, 1, 1, 27, 41, 420000, tzinfo=UTC)

# Two objects in planes 0.3 degrees apart at the same phase (built with
# sgp4init, written with sgp4.exporter.export_tle). Their separation swings
# between about 0.1 and 35 km each revolution and stays under 10 km for about
# 4 minutes either side of each node crossing. 1 s brute force: 0.100 km at
# 00:46:28, the polished minimum 0.0992 km at 00:46:28.20.
SLOW_PAIR_TLES = """\
OBJ90001
1 90001U          26001.00000000  .00000000  00000-0  10000-4 0    07
2 90001  51.6000  30.0000 0001000   0.0000   0.0000 15.48881812    04
OBJ90002
1 90002U          26001.00000000  .00000000  00000-0  10000-4 0    08
2 90002  51.9000  30.0000 0001000   0.0000   0.0030 15.48881812    01
"""
SLOW_TCA = datetime(2026, 1, 1, 0, 46, 28, 201842, tzinfo=UTC)

# High-drag element sets from the sweep behind the non-point-mass constants
# (sgp4init at inclination 63.4 deg, written with export_tle): B* 0.2 at a
# 200 km perigee with e 0.02 (the worst 60 s interval of the sweep, 1.08 times
# dt^2 / 8 times its estimate) and at 150 km with e 0.1, both of which SGP4
# decays within 5.5 hours, plus a fast eccentric orbit (e 0.7, 300 km perigee).
DRAG_TLES = """\
DRAG91001
1 91001U          26001.00000000  .00000000  00000-0  20000+0 0    04
2 91001  63.4000   0.0000 0200000  90.0000   0.0000 15.78657429    01
DRAG91003
1 91003U          26001.00000000  .00000000  00000-0  20000+0 0    06
2 91003  63.4000   0.0000 1000000  90.0000   0.0000 14.05345253    00
DRAG91004
1 91004U          26001.00000000  .00000000  00000-0  10000-3 0    00
2 91004  63.4000   0.0000 7000000  90.0000   0.0000  2.61397872    00
"""
DRAG_START = datetime(2026, 1, 1, tzinfo=UTC)
DRAG_HOURS = 5.5


def _store_from(text: str) -> TLEStore:
    store = TLEStore()
    store.load_from_text(text)
    return store


def _screen_inputs(tles: list, start: datetime, hours: float):
    """The screen's own per-object inputs at the default step.

    Returns in-window positions and validity, the interval length and each
    object's non-point-mass estimate per interval, exactly as screen_encounters
    builds them.
    """
    positions, valid, _times, dt = cj._coarse_grid(
        tles, start, hours, cj.DEFAULT_COARSE_STEP_SECONDS
    )
    valid = cj._drop_unphysical_states(positions, valid, dt)
    accel = cj._non_point_mass_accel_km_s2(positions, valid, dt)
    return positions[:, 1:-1], valid[:, 1:-1], dt, accel


@pytest.mark.parametrize("threshold_km", [0.75, 1.0, 2.0])
@pytest.mark.parametrize(
    ("start", "hours"),
    [
        (datetime(2026, 1, 1, 0, 57, 41, 370000, tzinfo=UTC), 0.999822),
        (datetime(2026, 1, 1, 0, 57, 41, tzinfo=UTC), 1.0),
        (datetime(2026, 1, 1, 0, 57, 41, 800000, tzinfo=UTC), 1.0),
    ],
)
def test_fast_minimum_beside_a_coarse_sample_survives_a_small_threshold(
    start: datetime, hours: float, threshold_km: float
):
    """A 14.24 km/s approach within half a second of a coarse sample.

    Under a 0.75 to 2 km threshold the pair is inside it for well under a
    second, so the encounter span can end next to the minimum. The span is
    refined with a one-step pad and the minimum is polished, so the 0.6702 km
    approach is reported wherever the samples fall.
    """
    events = find_conjunctions(
        _store({70086, 71200}), hours=hours, threshold_km=threshold_km, start=start,
        compute_probability=False,
    )
    assert len(events) == 1, [(e.tca, e.miss_distance_km) for e in events]
    assert events[0].miss_distance_km == pytest.approx(0.6702, abs=1e-3)
    assert events[0].risk == RiskLevel.HIGH
    assert abs((events[0].tca - FAST_TCA).total_seconds()) <= 0.5


@pytest.mark.parametrize("lead_s", [90, 150, 210, 270])
@pytest.mark.parametrize("edge", ["end", "start"])
def test_slow_approach_straddling_a_window_edge_is_followed_to_its_minimum(
    edge: str, lead_s: int
):
    """The pair is inside the threshold at a window edge and its minimum lies past it.

    The refinement walks outward from the edge until the separation grows and
    reports the true minimum, not the edge sample (3.6, 6.0 and 8.3 km at 90,
    150 and 210 s from the minimum). At 270 s the pair is 10.66 km apart at
    the edge: the approach lies wholly outside the window and is not reported.
    """
    if edge == "end":
        start = SLOW_TCA - timedelta(seconds=lead_s + 1200)
    else:
        start = SLOW_TCA + timedelta(seconds=lead_s)
    events = find_conjunctions(
        _store_from(SLOW_PAIR_TLES), hours=20.0 / 60.0, threshold_km=THRESHOLD_KM,
        start=start, compute_probability=False,
    )
    if lead_s == 270:
        assert events == [], [(e.tca, e.miss_distance_km) for e in events]
        return
    assert len(events) == 1, [(e.tca, e.miss_distance_km) for e in events]
    assert events[0].miss_distance_km == pytest.approx(0.0992, abs=1e-3)
    assert events[0].risk == RiskLevel.CRITICAL
    assert abs((events[0].tca - SLOW_TCA).total_seconds()) <= 0.5


def test_near_threshold_approach_depends_on_the_gate_allowance(monkeypatch):
    """A 5.4108 km approach against a 5.413 km threshold.

    Its chord closest approach is above the threshold, so only the curvature
    allowance keeps it. Forcing the allowance to zero must lose it; this test
    fails if the allowance is removed or reduced to nothing.
    """
    store = _store({70178, 70482})
    tca = datetime(2026, 1, 1, 0, 40, 34, tzinfo=UTC)

    def near() -> list[ConjunctionEvent]:
        return [
            e for e in find_conjunctions(
                store, hours=HOURS, threshold_km=5.413, start=START, compute_probability=False,
            )
            if abs((e.tca - tca).total_seconds()) <= 5.0
        ]

    kept = near()
    assert len(kept) == 1
    assert kept[0].miss_distance_km == pytest.approx(5.4108, abs=1e-3)
    assert abs((kept[0].tca - tca).total_seconds()) <= 0.5

    monkeypatch.setattr(
        cj, "coarse_gate_allowance_km",
        lambda thr, dt, path, accel=0.0: np.zeros_like(np.asarray(path, dtype=np.float64)),
    )
    assert near() == []


@pytest.mark.parametrize("step_s", [30.0, 120.0, 300.0])
def test_dense_reference_recall_holds_at_other_steps(reference: list[dict], step_s: float):
    events = find_conjunctions(
        _store(), hours=HOURS, threshold_km=THRESHOLD_KM, start=START,
        compute_probability=False, coarse_step_seconds=step_s,
    )
    missed = [r for r in reference if not any(_matches(e, r) for e in events)]
    assert not missed, f"step {step_s} s missed {len(missed)} of {len(reference)}: {missed}"


def test_non_point_mass_estimate_bounds_high_drag_chord_departure():
    """Each object's departure from its chord beyond point-mass gravity.

    Integrated from 1 s second differences through the interval's Green's
    function, it must stay within SAFETY * dt^2 / 8 times the object's own
    estimate on every screened interval, including those of decaying high-drag
    objects. Without the estimate factor the worst interval here reaches 4.3.
    """
    tles = _store_from(DRAG_TLES).get_all()
    _pos, _valid, dt, accel = _screen_inputs(tles, DRAG_START, DRAG_HOURS)
    step = round(dt)
    assert step == pytest.approx(dt)
    n_intervals = accel.shape[1]
    seconds = np.arange(-1, n_intervals * step + 2, dtype=float)
    jd, fr = _jd_arrays([DRAG_START + timedelta(seconds=float(s)) for s in seconds])
    t = np.arange(step + 1, dtype=float)[:, None]
    s = np.arange(step + 1, dtype=float)[None, :]
    green = np.where(s <= t, s * (step - t), t * (step - s)) / step
    worst = 0.0
    for i, tle in enumerate(tles):
        err, r, _v = build_satrec(tle).sgp4_array(jd, fr)
        mid = r[1:-1]
        # excess[q]: acceleration beyond point-mass gravity at q seconds.
        excess = (r[2:] - 2.0 * mid + r[:-2]) + EARTH_MU_KM3_S2 * mid / (
            np.linalg.norm(mid, axis=1)[:, None] ** 3
        )
        ok = (err[2:] == 0) & (err[1:-1] == 0) & (err[:-2] == 0)
        for j in np.nonzero(accel[i] > 0.0)[0]:
            q = slice(j * step, j * step + step + 1)
            if not ok[q].all():
                continue
            departure = float(np.linalg.norm(green @ excess[q], axis=1).max())
            worst = max(worst, departure / (dt**2 / 8.0 * float(accel[i, j])))
    estimated = accel[(accel > 0.0) & (accel < cj._NON_POINT_MASS_FALLBACK_KM_S2)]
    # Really high drag: the raw estimate, before the factor, is far above J2.
    raw = float(estimated.max()) / cj._NON_POINT_MASS_ESTIMATE_FACTOR
    assert raw > 5.0 * cj._NON_POINT_MASS_FLOOR_KM_S2
    assert worst > 0.5  # the case is tight, not trivially inside
    assert worst <= cj._GATE_SAFETY_FACTOR, f"departure reached {worst:.3f} of dt^2/8 * estimate"


@pytest.mark.parametrize("case", ["subset", "drag"])
def test_sampled_radial_bands_contain_every_one_second_radius(case: str):
    """The shell prefilter's bands hold every radius the object reaches.

    Checked at 1 s on every interval the screen treats as valid, for the 32
    object subset and for decaying and eccentric objects.
    """
    if case == "subset":
        tles, start, hours = _store().get_all(), START, HOURS
    else:
        tles, start, hours = _store_from(DRAG_TLES).get_all(), DRAG_START, DRAG_HOURS
    positions, valid, dt, accel = _screen_inputs(tles, start, hours)
    low, high = cj._radial_bands_km(positions, valid, dt, accel)
    screened = valid[:, :-1] & valid[:, 1:]
    seconds = np.arange(round(hours * 3600.0) + 1, dtype=float)
    jd, fr = _jd_arrays([start + timedelta(seconds=float(s)) for s in seconds])
    interval = np.minimum((seconds / dt).astype(int), screened.shape[1] - 1)
    for i, tle in enumerate(tles):
        err, r, _v = build_satrec(tle).sgp4_array(jd, fr)
        use = (err == 0) & screened[i, interval]
        assert use.any()
        radius = np.linalg.norm(r[use], axis=1)
        assert low[i] <= radius.min(), (tle.norad_id, low[i], radius.min())
        assert radius.max() <= high[i], (tle.norad_id, high[i], radius.max())


# DEBRIS93011 decays (SGP4 error) at +6288.793 s from 2026-01-01; the last
# 60 s sample before that is +6240 s. CROSS93012 was fitted (sda.rv2tle) to
# cross its path at right angles at a chosen instant of that last partial
# interval; truth is the smallest 1 microsecond-grid SGP4 separation with both
# objects at code 0. HEAD's screen dropped every pair on that interval, and
# its fine grid stopped at the last whole second before the failure.
GAP_DEBRIS_TLE = """\
DEBRIS93011
1 93011U          26001.00000000  .00000000  00000-0  20000+0 0    07
2 93011   0.0000   0.0000 0000000   0.0000   0.0000 15.90816789    00
"""
GAP_CROSSERS = {
    # seconds after +6240: (line 1, line 2, truth TCA s, truth miss km)
    6: (
        "1 93012U 26001A   26001.07229167  .00000000  00000-0  00000-0 0  9997",
        "2 93012  90.0000  56.1476 0014996 311.4614  48.5386 16.03994017    19",
        6246.001948, 0.01884,
    ),
    25: (
        "1 93012U 26001A   26001.07251157  .00000000  00000-0  00000-0 0  9991",
        "2 93012  90.0000  57.4193 0014996 311.4598  48.5402 16.04049438    18",
        6265.002002, 0.01124,
    ),
    # 0.19 s before the failure: the minimum lies between the last whole
    # second and the failure, so polishing must stop at the validity edge.
    48.6: (
        "1 93012U 26001A   26001.07278472  .00000000  00000-0  00000-0 0  9990",
        "2 93012  90.0000  58.9988 0014997 311.4614  48.5386 16.04118456    14",
        6288.601445, 0.01878,
    ),
}


def _gap_store(lag_s: float) -> TLEStore:
    line1, line2, _tca, _miss = GAP_CROSSERS[lag_s]
    return _store_from(GAP_DEBRIS_TLE + f"CROSS93012\n{line1}\n{line2}\n")


@pytest.mark.parametrize("lag_s", sorted(GAP_CROSSERS))
def test_approach_before_an_sgp4_failure_is_screened_and_polished(lag_s: float):
    """A 10.97 km/s crossing inside the coarse interval where one object fails.

    The chord gate cannot use that interval (one sample is an SGP4 error); the
    anchor test of _validity_boundary_hits keeps it, and the refinement polishes
    up to the validity edge.
    """
    _l1, _l2, tca_s, miss_km = GAP_CROSSERS[lag_s]
    events = find_conjunctions(
        _gap_store(lag_s), hours=HOURS, threshold_km=THRESHOLD_KM, start=START,
        compute_probability=False,
    )
    near = [e for e in events if abs((e.tca - START).total_seconds() - tca_s) <= 0.5]
    assert len(near) == 1, [((e.tca - START).total_seconds(), e.miss_distance_km) for e in events]
    assert near[0].risk == RiskLevel.CRITICAL
    assert near[0].miss_distance_km == pytest.approx(miss_km, abs=1e-3)
    assert abs((near[0].tca - START).total_seconds() - tca_s) <= 0.01
    assert near[0].relative_velocity_km_s == pytest.approx(10.97, abs=0.01)


def test_approach_before_an_sgp4_failure_needs_the_boundary_screen(monkeypatch):
    """Without the validity-boundary anchor test the 25 s crossing is lost."""
    monkeypatch.setattr(
        cj, "_validity_boundary_hits", lambda *args, **kwargs: cj._empty_hits()
    )
    _l1, _l2, tca_s, _miss = GAP_CROSSERS[25]
    events = find_conjunctions(
        _gap_store(25), hours=HOURS, threshold_km=THRESHOLD_KM, start=START,
        compute_probability=False,
    )
    assert not [e for e in events if abs((e.tca - START).total_seconds() - tca_s) <= 60.0]


def test_valid_state_between_two_failed_samples_is_screened():
    """A validity island: both samples of an interval failed, states between are valid.

    Object 0's samples either side of the slow pair's crossing are marked
    failed. The 1 s scan of every interval with a failed sample finds the
    physical states (all 61 of the crossing interval, ends included) and the
    anchor test keeps the pair there; without that interval's anchors it is
    not screened.
    """
    tles = _store_from(SLOW_PAIR_TLES).get_all()
    positions, valid, dt, _accel = _screen_inputs(tles, START, 1.0)
    j = int((SLOW_TCA - START).total_seconds() // dt)
    valid = valid.copy()
    valid[0, j] = valid[0, j + 1] = False

    anchors = cj._validity_scan_anchors(tles, START, valid, dt)
    assert set(anchors) == {(0, j - 1), (0, j), (0, j + 1)}
    assert anchors[(0, j)].shape == (61, 3)
    # The scan starts on the interval's first sample.
    assert np.linalg.norm(anchors[(0, j)][0] - positions[0, j]) < 1e-3

    def pairs_on(hits) -> set[tuple[int, int, int]]:
        return set(
            zip(hits.index_a.tolist(), hits.index_b.tolist(), hits.interval.tolist(),
                strict=True)
        )

    with_island = cj._validity_boundary_hits(positions, valid, anchors, THRESHOLD_KM, dt)
    others = {key: pts for key, pts in anchors.items() if key != (0, j)}
    without = cj._validity_boundary_hits(positions, valid, others, THRESHOLD_KM, dt)
    assert (0, 1, j) in pairs_on(with_island)
    assert (0, 1, j) not in pairs_on(without)
    assert {(0, 1, j - 1), (0, 1, j + 1)} <= pairs_on(without)
    assert cj._validity_boundary_hits(positions, valid, {}, THRESHOLD_KM, dt).index_a.size == 0


@pytest.mark.parametrize("margin_km", [-1.0, 1.0])
def test_anchor_test_keeps_pairs_within_the_derived_reach(margin_km: float):
    """Object 0 failed at its second sample; its one physical scan state sits
    at reach +/- 1 km from both samples of a valid object 1."""
    dt = 60.0
    reach = cj._anchor_reach_km(THRESHOLD_KM, dt)
    assert reach >= THRESHOLD_KM + 2.0 * cj._MAX_PHYSICAL_SPEED_KM_S * dt
    positions = np.zeros((2, 2, 3))
    positions[1, :, 0] = 7000.0
    positions[1, 1, 1] = 300.0
    valid = np.array([[True, False], [True, True]])
    # Equidistant from (7000, 0, 0) and (7000, 300, 0).
    offset = math.sqrt((reach + margin_km) ** 2 - 150.0**2)
    anchor = np.array([[7000.0 - offset, 150.0, 0.0]])
    hits = cj._validity_boundary_hits(positions, valid, {(0, 0): anchor}, THRESHOLD_KM, dt)
    assert hits.index_a.size == (1 if margin_km < 0 else 0)


# DEBRIS93051 has decayed by about +900 s; SGP4 then returns code-0 states in
# islands whose positions move 75 to 87 km/s at 1,600 to 2,800 km altitude
# while its velocity output reads under 8 km/s. CROSS93052 passes 0.0137 km
# from one of them at +4193.855 s (both code 0). HEAD reported nothing; a
# velocity-only check reported CRITICAL or nothing depending on the window
# start (safety review round 3). Such states are not physical and never report.
DECAYED_PAIR_TLES = """\
DEBRIS93051
1 93051U          26001.00000000  .00000000  00000-0  45000+0 0    08
2 93051  28.5000   0.0000 0002000   0.0000   0.0000 16.19836428    05
CROSS93052
1 93052U 26001A   26001.04853999  .00000000  00000-0  00000-0 0  9994
2 93052  82.1562 323.1006 0006530 330.7083  10.2538 10.96440677    12
"""


def test_decayed_object_debris_states_never_report_whatever_the_window_phase():
    store = _store_from(DECAYED_PAIR_TLES)
    debris = build_satrec(store.get(93051))
    jd, fr = datetime_to_jd(START)
    physical, _pos, vel = cj._physical_states(debris, jd, fr, np.array([4193.855]))
    err, _r, _v = debris.sgp4(jd, fr + 4193.855 / 86400.0)
    assert err == 0 and np.linalg.norm(vel[0]) < 8.0  # SGP4 calls it a normal state
    assert not physical[0]
    for offset_s in (3000, 3010, 3020, 3030, 3040, 3050):
        events = find_conjunctions(
            store, hours=40.0 / 60.0, threshold_km=THRESHOLD_KM,
            start=START + timedelta(seconds=offset_s), compute_probability=False,
        )
        assert events == [], (offset_s, [(e.tca, e.miss_distance_km) for e in events])

    # The refinement drops the debris states, and the validity scan finds no
    # physical anchor for the debris around them (the crosser has them all).
    tles = [store.get(93051), store.get(93052)]
    tca = START + timedelta(seconds=4193.855)
    span = timedelta(seconds=60)
    assert cj._refine_encounter(tles[0], tles[1], tca - span, tca + span, THRESHOLD_KM) == []
    window = START + timedelta(seconds=4140)
    anchors = cj._validity_scan_anchors(tles, window, np.zeros((2, 3), dtype=bool), 60.0)
    assert sorted(anchors) == [(1, 0), (1, 1)]


def test_coarse_debris_sample_of_a_failing_object_is_cleared(monkeypatch):
    """A 50 s grid from +193.855 s puts sample 80 on the debris state at
    +4193.855 s (code 0) and samples 3 to 14 on the debris object's physical
    states before it decays. Only the debris sample is cleared; an object valid
    on every sample is left alone. The screen applies it to its own grid.
    """
    store = _store_from(DECAYED_PAIR_TLES)
    tles = [store.get(93051), store.get(93052)]
    first = START + timedelta(seconds=193.855)
    dt, n = 50.0, 81
    jd, fr = datetime_to_jd(first)
    seconds = dt * np.arange(n)
    valid = np.zeros((2, n), dtype=bool)
    for k, tle in enumerate(tles):
        err, r, _v = build_satrec(tle).sgp4_array(np.full(n, jd), fr + seconds / 86400.0)
        valid[k] = (np.asarray(err) == 0) & np.all(np.isfinite(r), axis=1)
    assert np.nonzero(valid[0])[0].tolist() == [3, 4, 5, 9, 10, 13, 14, 80]
    assert valid[1].all()
    cleaned = cj._confirm_physical_samples(tles, first, dt, valid)
    assert np.nonzero(cleaned[0])[0].tolist() == [3, 4, 5, 9, 10, 13, 14]
    assert cleaned[1].all()

    confirm = cj._confirm_physical_samples
    seen: list[tuple[np.ndarray, np.ndarray]] = []

    def spy(tles_, first_, dt_, valid_):
        out = confirm(tles_, first_, dt_, valid_)
        seen.append((valid_, out))
        return out

    monkeypatch.setattr(cj, "_confirm_physical_samples", spy)
    screen_encounters(tles, first, hours=(n - 1) * dt / 3600.0, coarse_step_seconds=dt)
    # The screen's grid has one extra sample either side of the window.
    assert len(seen) == 1
    before, after = seen[0]
    assert before[0, 81] and not after[0, 81]
    assert np.array_equal(after[0, 1:82], cleaned[0])


@pytest.mark.parametrize("lead_s", [20, 25])
def test_long_span_minimum_inside_the_first_profile_step_is_found(lead_s: int):
    """A 900 s span (profiled every 60 s) starting 20 or 25 s before the slow
    pair's 0.0992 km minimum, against a 1 km threshold.

    The profile's smallest sample is the span-start pad sample (0.85 or 1.05 km,
    the next is 1.57 or 1.37 km), so the minimum looks like one outside the
    span. The first profile interval has no acceleration estimate at its outer
    end and is always re-sampled on the 1 s grid, which finds it inside.
    """
    a, b = _store_from(SLOW_PAIR_TLES).get_all()
    found = cj._refine_encounter(
        a, b, SLOW_TCA - timedelta(seconds=lead_s),
        SLOW_TCA + timedelta(seconds=900 - lead_s), 1.0, bracket_seconds=60.0,
    )
    assert len(found) == 1
    assert found[0][1] == pytest.approx(0.0992, abs=1e-3)
    assert abs((found[0][0] - SLOW_TCA).total_seconds()) <= 0.5


# Co-orbiting pair (sgp4init, export_tle) under 10 km for the first 8.8 h of
# a 12 h window from START; closest 0.382 km at +11843.03 s (1e-4 s grid).
MERGE_PAIR_TLES = """\
OBJ95001
1 95001U          26001.00000000  .00000000  00000-0  10000-3 0    01
2 95001  70.0000   5.0000 0002000   0.0000   0.0000 14.89340181    00
OBJ95002
1 95002U          26001.00000000  .00000000  00000-0  10000-3 0    02
2 95002  70.0000   5.0000 0003000   0.0000   0.0450 14.89244143    02
"""


def test_long_encounter_reports_one_approach_at_its_closest_point():
    """A co-orbiting pair under 10 km for the first 8.8 hours of a 12 h window.

    Its separation dips every half revolution, always under the threshold, so
    the dips form one approach and only the closest (0.382 km at +11843.03 s,
    1e-4 s brute force) is reported. Keeping the first dip instead reports a
    4.71 km approach before the window.
    """
    store = _store_from(MERGE_PAIR_TLES)
    events = find_conjunctions(
        store, hours=12.0, threshold_km=THRESHOLD_KM, start=START, compute_probability=False,
    )
    found = [((e.tca - START).total_seconds(), e.miss_distance_km) for e in events]
    assert len(events) == 1, found
    assert events[0].miss_distance_km == pytest.approx(0.382, abs=1e-3)
    assert events[0].risk == RiskLevel.CRITICAL
    assert abs((events[0].tca - START).total_seconds() - 11843.03) <= 0.5


@pytest.mark.parametrize("side", ["start", "end"])
def test_closing_span_end_is_followed_only_when_it_straddles_the_threshold(side: str):
    """A span whose smallest sample is its end, with the minimum outside it.

    The pair is still closing at the span end. The outside minimum (0.0992 km)
    is followed and reported when the pair is within the threshold at the end
    (the approach straddles it), and dropped when it is not, inside the window
    or at a window edge alike.
    """
    a, b = _store_from(SLOW_PAIR_TLES).get_all()
    if side == "start":
        lo, hi = SLOW_TCA + timedelta(seconds=120), SLOW_TCA + timedelta(seconds=180)
    else:
        lo, hi = SLOW_TCA - timedelta(seconds=180), SLOW_TCA - timedelta(seconds=120)

    assert cj._refine_encounter(a, b, lo, hi, 1.0) == []
    found = cj._refine_encounter(a, b, lo, hi, THRESHOLD_KM)
    assert len(found) == 1
    assert found[0][1] == pytest.approx(0.0992, abs=1e-3)
    assert abs((found[0][0] - SLOW_TCA).total_seconds()) <= 0.5


def test_approach_wholly_before_the_window_is_not_reported_at_any_step():
    """70178/70482 are 273.6 km apart at the window start, closing on a
    4.96 km approach 462 s before it. At a 300 s step the first span starts at
    the window edge; the approach does not straddle it and is not reported, so
    every step gives the same two in-window events."""
    store = _store({70178, 70482})

    def events_at(step_s: float) -> list[tuple[float, float]]:
        return [
            (round((e.tca - START).total_seconds(), 1), e.miss_distance_km)
            for e in find_conjunctions(
                store, hours=HOURS, threshold_km=THRESHOLD_KM, start=START,
                compute_probability=False, coarse_step_seconds=step_s,
            )
        ]

    expected = events_at(60.0)
    assert all(tca >= 0.0 for tca, _miss in expected)
    assert sorted(miss for _tca, miss in expected) == pytest.approx([4.908, 5.4108], abs=1e-3)
    assert events_at(300.0) == expected


@pytest.mark.parametrize("offset_s", [0, 42, 49])
def test_long_encounter_splits_at_a_short_excursion_above_the_threshold(offset_s: int):
    """The OBJ95001/95002 pair against a 0.9339 km threshold.

    The separation rises above it for 49 s (+10371.2 to +10420.5 s, peak
    0.3 m over), between a 0.7744 km dip at +9578.9 s and the 0.382 km one at
    +11843.0 s. The long span's 60 s profile can step over the excursion; the
    intervals around it could cross the threshold and are re-sampled on the
    1 s grid, so both approaches are reported at every window phase.
    """
    store = _store_from(MERGE_PAIR_TLES)
    events = find_conjunctions(
        store, hours=12.0, threshold_km=0.9339, start=START + timedelta(seconds=offset_s),
        compute_probability=False,
    )
    found = sorted(
        (round((e.tca - START).total_seconds(), 1), e.miss_distance_km, e.risk) for e in events
    )
    assert len(found) == 2, found
    assert found[0][0] == pytest.approx(9578.9, abs=0.5)
    assert found[0][1] == pytest.approx(0.7744, abs=1e-3)
    assert found[0][2] == RiskLevel.HIGH
    assert found[1][0] == pytest.approx(11843.0, abs=0.5)
    assert found[1][1] == pytest.approx(0.382, abs=1e-3)
    assert found[1][2] == RiskLevel.CRITICAL


@pytest.mark.parametrize("n_objects", [0, 1, 2, 7, 50])
@pytest.mark.parametrize("max_pairs", [1, 5, 100, 10**6])
def test_allpairs_row_chunks_cover_the_upper_triangle_once(n_objects: int, max_pairs: int):
    chunks = list(cj._upper_triangle_rows(n_objects, max_pairs))
    index_a = np.concatenate([c[0] for c in chunks]) if chunks else np.zeros(0, np.int64)
    index_b = np.concatenate([c[1] for c in chunks]) if chunks else np.zeros(0, np.int64)
    expected_a, expected_b = np.triu_indices(n_objects, k=1)
    assert np.array_equal(index_a, expected_a)
    assert np.array_equal(index_b, expected_b)
    assert all(c[0].size <= max(max_pairs, n_objects - 1) for c in chunks)


def test_screen_scans_validity_islands_between_failed_samples(monkeypatch):
    """End to end: the samples either side of the 14.24 km/s approach fail.

    Object 0's coarse samples bounding the approach interval are marked failed
    (as an SGP4 error flip would), although SGP4 is valid in between. The scan
    finds the valid stretch and the approach is still reported; with the scan
    disabled the interval is never screened and the approach is lost.
    """
    real_grid = cj._coarse_grid
    j = int((FAST_TCA - START).total_seconds() // cj.DEFAULT_COARSE_STEP_SECONDS)

    def grid_with_failures(tles, start, hours, step):
        positions, valid, times, dt = real_grid(tles, start, hours, step)
        valid = valid.copy()
        valid[0, j + 1] = valid[0, j + 2] = False  # in-window samples j and j + 1
        return positions, valid, times, dt

    def fast_events() -> list[ConjunctionEvent]:
        return [
            e for e in find_conjunctions(
                _store({70086, 71200}), hours=HOURS, threshold_km=THRESHOLD_KM,
                start=START, compute_probability=False,
            )
            if abs((e.tca - FAST_TCA).total_seconds()) <= 5.0
        ]

    monkeypatch.setattr(cj, "_coarse_grid", grid_with_failures)
    kept = fast_events()
    assert len(kept) == 1
    assert kept[0].miss_distance_km == pytest.approx(0.6702, abs=1e-3)
    assert kept[0].risk == RiskLevel.HIGH

    monkeypatch.setattr(cj, "_validity_scan_anchors", lambda *args, **kwargs: {})
    assert fast_events() == []


def test_local_minima_treat_each_side_of_an_sgp4_gap_separately():
    """Samples either side of a gap are not compared.

    The pair is still closing when one object stops propagating (index 2), so
    that run end is a minimum to polish up to the failure, as is the first
    sample after the gap (index 3).
    """
    distance = np.array([5.0, 3.0, 1.0, 0.5, 2.0])
    offsets = np.array([0, 1, 2, 5, 6])
    assert cj._local_minima(distance, offsets).tolist() == [2, 3]
    assert cj._local_minima(distance, np.arange(5)).tolist() == [3]
    assert cj._local_minima(distance).tolist() == [3]


# Safety review round 4: an object whose physical stretch ends (SGP4 failure)
# or begins (recovery) under 1 s from a valid coarse sample. DEBRIS93051 fails
# from +46.0186 s; CROSS93062 passes it 0.0103 km away at +45.9668 s. FLIP93071
# is physical from +764.92 s; CROSS93072 passes it 0.0195 km away at +765.0219 s
# (truth on a 1e-4 s grid, both physical). The 1 s scan has no two code-0
# neighbours in either sliver, so HEAD and the round-3 code reported nothing.
SLIVER_AFTER_SAMPLE_TLES = """\
DEBRIS93051
1 93051U          26001.00000000  .00000000  00000-0  45000+0 0    08
2 93051  28.5000   0.0000 0002000   0.0000   0.0000 16.19836428    05
CROSS93062
1 93062U 26001A   26001.00053204  .00000000  00000-0  00000-0 0  9992
2 93062 177.9002  46.5030 0005674 157.2896 246.6180 16.20852194    12
"""
SLIVER_BEFORE_SAMPLE_TLES = """\
FLIP93071
1 93071U          26001.00000000  .00000000  00000-0  30000+0 0    04
2 93071  28.5000   0.0000 0005000   0.0000   0.0000 16.08851423    00
CROSS93072
1 93072U 26001A   26001.00885440  .00000000  00000-0  00000-0 0  9998
2 93072 155.4797 109.5925 0001852 171.0170 253.0737 16.14858500    11
"""
SLIVER_AFTER_TCA_S = 45.9668
SLIVER_BEFORE_TCA_S = 765.0219


def _sliver_events(text: str, start_s: float, hours: float, step_s: float, method: str):
    return [
        (round((e.tca - START).total_seconds(), 2), e.miss_distance_km, e.risk)
        for e in find_conjunctions(
            _store_from(text), hours=hours, threshold_km=THRESHOLD_KM,
            start=START + timedelta(seconds=start_s), compute_probability=False,
            coarse_step_seconds=step_s, method=method,
        )
    ]


def _assert_one_sliver_event(events, tca_s: float, miss_km: float) -> None:
    assert len(events) == 1, events
    tca, miss, risk = events[0]
    assert tca == pytest.approx(tca_s, abs=0.05)
    assert miss == pytest.approx(miss_km, abs=5e-4)
    assert risk == RiskLevel.CRITICAL


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("lead_s", [0.9, 0.5, 0.2])
def test_approach_after_a_valid_first_sample_before_a_failure_is_screened(
    lead_s: float, method: str
):
    """The window starts lead_s before the approach, 60 s step."""
    events = _sliver_events(
        SLIVER_AFTER_SAMPLE_TLES, SLIVER_AFTER_TCA_S - lead_s, 0.5, 60.0, method
    )
    _assert_one_sliver_event(events, SLIVER_AFTER_TCA_S, 0.0103)


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("lag_s", [0.85, 0.5])
def test_approach_after_a_recovery_before_a_valid_last_sample_is_screened(
    lag_s: float, method: str
):
    """The window ends lag_s after the approach, 60 s step."""
    events = _sliver_events(
        SLIVER_BEFORE_SAMPLE_TLES, SLIVER_BEFORE_TCA_S + lag_s - 1800.0, 0.5, 60.0, method
    )
    _assert_one_sliver_event(events, SLIVER_BEFORE_TCA_S, 0.0195)


@pytest.mark.parametrize(
    ("text", "start_s", "tca_s", "miss_km"),
    [
        (SLIVER_AFTER_SAMPLE_TLES, -554.9332, SLIVER_AFTER_TCA_S, 0.0103),
        (SLIVER_BEFORE_SAMPLE_TLES, 165.8719, SLIVER_BEFORE_TCA_S, 0.0195),
    ],
    ids=["failure", "recovery"],
)
def test_sliver_beside_a_mid_window_sample_is_screened_at_a_short_step(
    text: str, start_s: float, tca_s: float, miss_km: float
):
    """At a 10 s step the neighbouring interval's chord gate is too narrow to
    reach the sliver, so only the anchor test can keep the pair."""
    events = _sliver_events(text, start_s, 1200.0 / 3600.0, 10.0, "auto")
    _assert_one_sliver_event(events, tca_s, miss_km)


@pytest.mark.parametrize(
    ("threshold_km", "start_s", "step_s"),
    [(0.3840, 11893.3, 60.0), (0.3840, 11793.7 - 3600.0, 60.0), (0.6064, 13369.9, 300.0)],
    ids=["start-60", "end-60", "start-300"],
)
def test_long_span_reports_nothing_outside_the_window(
    threshold_km: float, start_s: float, step_s: float
):
    """OBJ95001/95002 dips to 0.382 km at +11843.0 s and 0.5964 km at
    +13221.8 s. Each window edge sits just outside the stretch where the pair
    is within the threshold, so the in-window minimum is above it (0.3841 or
    0.6065 km). The span's minimum sits on its end, so the pair is followed
    past the edge to the dip; it does not straddle the edge and is not
    reported.
    """
    events = find_conjunctions(
        _store_from(MERGE_PAIR_TLES), hours=1.0, threshold_km=threshold_km,
        start=START + timedelta(seconds=start_s), compute_probability=False,
        coarse_step_seconds=step_s,
    )
    assert events == [], [(e.tca, e.miss_distance_km) for e in events]


def test_scan_anchors_a_physical_state_without_a_code0_scan_neighbour(monkeypatch):
    """From +45.0668 s DEBRIS93051 is code 0 for under 1 s, so the 1 s scan of
    the interval has one code-0 state and no code-0 neighbour for it. Its own
    1 s probe shows it physical, so it anchors the interval even when the
    coarse sample there is not counted; a valid coarse sample anchors it
    without the probe.
    """
    debris = _store_from(SLIVER_AFTER_SAMPLE_TLES).get(93051)
    start = START + timedelta(seconds=45.0668)
    jd, fr = datetime_to_jd(start)
    err, r, _v = build_satrec(debris).sgp4(jd, fr)
    assert err == 0
    for valid in ([[False, False]], [[True, False]]):
        anchors = cj._validity_scan_anchors([debris], start, np.array(valid), 60.0)
        assert sorted(anchors) == [(0, 0)]
        assert np.allclose(anchors[(0, 0)], [r], atol=1e-6)

    def nothing_physical(satrec, jd_, fr_, seconds, positions):
        return np.zeros(np.asarray(seconds).size, dtype=bool)

    monkeypatch.setattr(cj, "_physical_mask", nothing_physical)
    assert cj._validity_scan_anchors([debris], start, np.array([[False, False]]), 60.0) == {}
    anchors = cj._validity_scan_anchors([debris], start, np.array([[True, False]]), 60.0)
    assert sorted(anchors) == [(0, 0)]


# Safety review round 5: GRAZE-A/B co-orbit with perigee 1.2 km below the WGS72
# surface; both return SGP4 code 6 from +609.89 s to +639.47 s and pass
# 0.0222 km apart at +665.95 s, after the gap.
GRAZE_PAIR_TLES = """\
GRAZE-A
1 94011U 26001A   26001.00000000 .00000000  00000-0  00000-0 0  999 3
2 94011  51.6000   0.0000 0230715 354.9000 319.3000 16.46186908    14
GRAZE-B
1 94012U 26001A   26001.00000000 .00000000  00000-0  00000-0 0  999 4
2 94012  51.6500   0.0000 0230715 354.9000 319.3000 16.46186908    10
"""


@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_approach_closing_into_an_sgp4_gap_at_the_window_end_is_reported(step_s: float):
    """A 0.25 h window ends at +609.39 s with the pair 0.3950 km apart and
    still closing. Following it past the span end finds the 0.0222 km dip
    across the gap; the pair is not physical at the span end, so that dip does
    not count and the span's own minimum is reported once instead: 0.3916 km at
    +609.88 s, the last physical instant before the gap (inside the 1 s pad).
    HEAD reported 0.395 km; the round-4 code reported nothing.
    """
    events = find_conjunctions(
        _store_from(GRAZE_PAIR_TLES), hours=0.25, threshold_km=THRESHOLD_KM,
        start=START - timedelta(seconds=290.61), compute_probability=False,
        coarse_step_seconds=step_s,
    )
    assert len(events) == 1, [(e.tca, e.miss_distance_km) for e in events]
    assert events[0].risk == RiskLevel.CRITICAL
    assert events[0].miss_distance_km == pytest.approx(0.3916, abs=1e-3)
    assert (events[0].tca - START).total_seconds() == pytest.approx(609.88, abs=0.05)


# The round-6 safety review's co-orbiting pair (sgp4init): OBJ95002 at e 0.0007,
# M 0.2250 deg, n 14.88859991. From +10061 s, against 3.0061 km, it dips to
# 1.9098 km at +11843.35 s, rises to a 3.0308 km peak (24.7 m over), and dips
# again to 2.9815 km at +13221.39 s (24.6 m under, +13068.1 to +13328.4 s).
SHALLOW_PAIR_TLES = """\
OBJ95001
1 95001U 26001A   26001.00000000 .00000000  00000-0  10000-3 0  999 7
2 95001  70.0000   5.0000 0002000   0.0000   0.0000 14.89340181    11
OBJ95002
1 95002U 26001A   26001.00000000 .00000000  00000-0  10000-3 0  999 8
2 95002  70.0000   5.0000 0007000   0.0000   0.2250 14.88859991    19
"""
SHALLOW_START_S = 10061.0
SHALLOW_THRESHOLD_KM = 3.0061


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_long_span_reports_a_shallow_dip_on_a_monotone_profile_run(step_s: float, method: str):
    """Both dips of the shallow pair are reported at every step.

    The round-6 code profiled this 1 h span every coarse step and re-sampled
    only around the profile's local minima. At 300 s its samples from +12760
    to +13660 s read 3.0031, 3.0080, 3.0255 and 3.6473 km, a monotone run with
    no local minimum, so the 2.9815 km MODERATE approach between them was lost.
    """
    events = find_conjunctions(
        _store_from(SHALLOW_PAIR_TLES), hours=1.0, threshold_km=SHALLOW_THRESHOLD_KM,
        start=START + timedelta(seconds=SHALLOW_START_S), compute_probability=False,
        method=method, coarse_step_seconds=step_s,
    )
    found = sorted(
        ((e.tca - START).total_seconds(), e.miss_distance_km, e.risk) for e in events
    )
    assert len(found) == 2, found
    assert found[0][0] == pytest.approx(11843.35, abs=0.05)
    assert found[0][1] == pytest.approx(1.9098, abs=1e-3)
    assert found[1][0] == pytest.approx(13221.39, abs=0.05)
    assert found[1][1] == pytest.approx(2.9815, abs=1e-3)
    assert [risk for _t, _m, risk in found] == [RiskLevel.MODERATE, RiskLevel.MODERATE]


@pytest.mark.parametrize("step_s", [60.0, 300.0])
def test_long_span_reports_the_merge_pair_dip_just_under_the_threshold(step_s: float):
    """MERGE_PAIR_TLES against 0.5984 km from +9741.80 s: 0.382 km CRITICAL at
    +11843.03 s and 0.5964 km HIGH at +13221.76 s, 2 m under the threshold.
    The round-6 code lost the second at a 300 s step."""
    events = find_conjunctions(
        _store_from(MERGE_PAIR_TLES), hours=1.0, threshold_km=0.5984,
        start=START + timedelta(seconds=9741.80), compute_probability=False,
        coarse_step_seconds=step_s,
    )
    found = sorted(
        ((e.tca - START).total_seconds(), e.miss_distance_km, e.risk) for e in events
    )
    assert len(found) == 2, found
    assert found[0][0] == pytest.approx(11843.03, abs=0.05)
    assert found[0][1] == pytest.approx(0.382, abs=1e-3)
    assert found[0][2] == RiskLevel.CRITICAL
    assert found[1][0] == pytest.approx(13221.76, abs=0.05)
    assert found[1][1] == pytest.approx(0.5964, abs=1e-3)
    assert found[1][2] == RiskLevel.HIGH


@pytest.mark.parametrize("threshold_km", [2.5, SHALLOW_THRESHOLD_KM, 3.2])
def test_long_span_intervals_left_at_the_profile_step_are_settled(threshold_km: float):
    """Every interval ``_long_span_samples`` leaves at its 60 s profile step is
    settled on the dense 1 s grid of the same hour.

    One that joins its two samples stays within the threshold and nowhere
    below the lowest sample of its stretch, so neither an excursion nor a
    stretch's minimum can hide in it. One that does not join them stays above
    the threshold, so no approach can hide in it.
    """
    a, b = _store_from(SHALLOW_PAIR_TLES).get_all()
    start = START + timedelta(seconds=SHALLOW_START_S)
    span_s = 3602.0
    samples, joined = cj._long_span_samples(a, b, start, span_s, threshold_km, 60.0, 1.0)
    dense = cj._sample_pair(a, b, start, span_s, 1.0)
    assert np.array_equal(dense.offsets, np.arange(int(span_s) + 1))
    d = dense.distance_km
    offsets = samples.offsets
    assert np.allclose(samples.distance_km, d[offsets], rtol=0.0, atol=1e-9)

    # Stretches: chains of joined samples within the threshold.
    within = samples.distance_km <= threshold_km
    stretch = np.cumsum(np.concatenate(([0], ~(joined & within[:-1] & within[1:]))))
    lowest = {
        int(s): float(samples.distance_km[(stretch == s) & within].min())
        for s in np.unique(stretch[within])
    }
    coarse = np.nonzero(np.diff(offsets) > 1)[0]
    assert coarse.size > 10
    assert len(offsets) < 0.5 * len(d)
    for k in coarse.tolist():
        inner = d[offsets[k] : offsets[k + 1] + 1]
        if joined[k]:
            assert inner.max() <= threshold_km
            assert inner.min() >= lowest[int(stretch[k])]
        else:
            assert inner.min() > threshold_km
    if threshold_km == SHALLOW_THRESHOLD_KM:
        assert joined[coarse].any() and not joined[coarse].all()


DOCKED_TLES = SHALLOW_PAIR_TLES.split("OBJ95002")[0] + """\
DOCKED95003
1 95003U 26001A   26001.00000000 .00000000  00000-0  10000-3 0  999 7
2 95003  70.0000   5.0000 0002000   0.0000   0.0000 14.89340181    11
"""


def test_identical_element_sets_are_reported_once_without_fine_sampling():
    """A docked vehicle published with its station's elements is 0 km from it
    at every instant. Its 6 h span stays at the profile step (no allowance
    applies) apart from the first and last intervals, which are always
    re-sampled; it is not followed past the window, and it is reported once,
    at the window start."""
    station, docked = _store_from(DOCKED_TLES).get_all()
    assert cj._same_elements(station, docked)
    samples, joined = cj._long_span_samples(
        station, docked, START, 21602.0, 1.0, 60.0, 1.0, same_elements=True
    )
    assert len(samples.times) == 61 + 359 + 2
    assert int(np.count_nonzero(np.diff(samples.offsets) == 60)) == 359
    assert bool(np.all(joined)) and float(np.max(samples.distance_km)) == 0.0

    events = find_conjunctions(
        _store_from(DOCKED_TLES), hours=6.0, threshold_km=1.0, start=START,
        compute_probability=False,
    )
    assert len(events) == 1
    assert events[0].miss_distance_km == 0.0
    assert events[0].tca == START


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [("bstar", " 10000-3 ", " 20000-3 "), ("mean anomaly", "0.0000 14.89", "0.0001 14.89")],
)
def test_element_sets_differing_in_one_field_are_not_identical(
    field: str, old: str, new: str
):
    station_text = DOCKED_TLES.split("DOCKED95003")[0]
    docked_text = "DOCKED95003" + DOCKED_TLES.split("DOCKED95003")[1]
    changed = docked_text.replace(old, new, 1)
    assert changed != docked_text, field
    station, docked = _store_from(station_text + changed).get_all()
    assert not cj._same_elements(station, docked)


# High-drag co-orbiting pair (sgp4init, export_tle): B* 0.1, perigee 250 km,
# e 0.0005 and 0.0007, 0.0013 to 32.3 km apart over the first 2 h.
HIGH_DRAG_PAIR_TLES = """\
DRAG-A
1 95021U          26001.00000000  .00000000  00000-0  10000+0 0    09
2 95021  51.6000  10.0000 0005000  30.0000   0.0000 16.07644935    05
DRAG-B
1 95022U          26001.00000000  .00000000  00000-0  10000+0 0    00
2 95022  51.6000  10.0000 0007000  30.0000   0.0000 16.07162425    07
"""


def test_profile_allowance_bounds_a_high_drag_pairs_chord_departure():
    """On every 60 s profile interval of the high-drag pair's first 2 h, the
    relative position stays within ``_profile_allowance_km`` of its chord on
    the 1 s grid; the closest it comes is 0.38 of the allowance."""
    a, b = _store_from(HIGH_DRAG_PAIR_TLES).get_all()
    h, m = 60.0, 120
    profile = cj._sample_pair(a, b, START - timedelta(seconds=h), (m + 2) * h, h)
    assert len(profile.times) == m + 3
    positions = np.stack([profile.pos_a, profile.pos_b])
    allowance = cj._profile_allowance_km(positions, np.ones(m + 3, dtype=bool), h)
    dense = cj._sample_pair(a, b, START, m * h, 1.0)
    assert len(dense.times) == int(m * h) + 1
    rel, chord_ends = dense.pos_a - dense.pos_b, positions[0] - positions[1]
    u = np.linspace(0.0, 1.0, int(h) + 1)[:, None]
    ratios = []
    for k in range(m):
        chord = (1.0 - u) * chord_ends[k + 1] + u * chord_ends[k + 2]
        segment = rel[k * int(h) : (k + 1) * int(h) + 1]
        ratios.append(float(np.max(np.linalg.norm(segment - chord, axis=1))) / allowance[k])
    assert max(ratios) <= 1.0
    assert max(ratios) > 0.3


def test_profile_allowance_is_the_derived_bound_with_the_gate_safety_factor():
    """The measured departure (0.38 here, 0.43 worst) sits inside the
    allowance only with the safety factor of 2: halving the allowance would
    still pass the departure test, so the formula itself is locked here,
    c * (GRADIENT * M + A0) / (1 - c * GRADIENT) with c = 2 * dt^2 / 8."""
    a, b = _store_from(HIGH_DRAG_PAIR_TLES).get_all()
    h, m = 60.0, 120
    profile = cj._sample_pair(a, b, START - timedelta(seconds=h), (m + 2) * h, h)
    assert len(profile.times) == m + 3
    positions = np.stack([profile.pos_a, profile.pos_b])
    present = np.ones(m + 3, dtype=bool)
    allowance = cj._profile_allowance_km(positions, present, h)
    accel = cj._non_point_mass_accel_km_s2(positions, np.vstack([present, present]), h)
    separation = np.linalg.norm(positions[0] - positions[1], axis=1)
    farther = np.maximum(separation[1 : m + 1], separation[2 : m + 2])
    gradient = 2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**3
    c = 2.0 * h * h / 8.0
    expected = c * (gradient * farther + accel.sum(axis=0)) / (1.0 - c * gradient)
    np.testing.assert_allclose(allowance, expected, rtol=1e-12)


@pytest.mark.parametrize("step_s", [60.0, 300.0])
def test_long_span_re_samples_both_sides_of_an_sgp4_gap(step_s: float):
    """GRAZE-A/B from +100 s for 0.25 h against 10 km: SGP4 fails for one of
    them from +609 to +640 s. The pair closes to 0.3916 km at +609.88 s, the
    last physical instant before the gap, and to 0.0222 km at +665.95 s after
    it. The profile samples inside the gap are missing, and the intervals
    beside it are re-sampled on the 1 s grid; left at the profile step they
    give 0.5991 km at +580 s and 0.2248 km at +698 s instead.
    """
    events = find_conjunctions(
        _store_from(GRAZE_PAIR_TLES), hours=0.25, threshold_km=THRESHOLD_KM,
        start=START + timedelta(seconds=100.0), compute_probability=False,
        coarse_step_seconds=step_s,
    )
    found = sorted(((e.tca - START).total_seconds(), e.miss_distance_km) for e in events)
    assert len(found) == 2, found
    assert found[0][0] == pytest.approx(609.88, abs=0.05)
    assert found[0][1] == pytest.approx(0.3916, abs=1e-3)
    assert found[1][0] == pytest.approx(665.95, abs=0.05)
    assert found[1][1] == pytest.approx(0.0222, abs=1e-3)


# The round-7 safety review's grazing pair: GRAZE_PAIR_TLES with argp 85 deg and
# e 0.0218. GRAZE-A returns SGP4 code 6 from +584 to +613 s, +5831 to +5862 s and
# +11078 to +11111 s; each gap lies inside one 60 s profile interval whose two
# ends are physical.
GRAZE_GAP_INSIDE_TLES = """\
GRAZE-A
1 94011U 26001A   26001.00000000 .00000000  00000-0  00000-0 0  999 3
2 94011  51.6000   0.0000 0218000  85.0000 319.3000 16.46186908    14
GRAZE-B
1 94012U 26001A   26001.00000000 .00000000  00000-0  00000-0 0  999 4
2 94012  51.6500   0.0000 0218000  85.0000 319.3000 16.46186908    10
"""


def _found(events: list[ConjunctionEvent]) -> list[tuple[float, float, RiskLevel]]:
    return sorted(((e.tca - START).total_seconds(), e.miss_distance_km, e.risk) for e in events)


def _assert_found(found, expected: list[tuple[float, float, RiskLevel]]) -> None:
    assert len(found) == len(expected), found
    for (t, miss, risk), (t_ref, miss_ref, risk_ref) in zip(found, expected, strict=True):
        assert t == pytest.approx(t_ref, abs=0.05), found
        assert miss == pytest.approx(miss_ref, abs=1e-3), found
        assert risk == risk_ref, found


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_long_span_splits_stretches_at_an_sgp4_gap_inside_one_profile_interval(
    step_s: float, method: str
):
    """From +511 s for 1.5 h against 10 km the pair is within the threshold
    from +511 to +583 s, +614 to +5830 s and +5863 s to the window end, split by
    the two SGP4 gaps (dense 1 s grid). The round-7 code left the 60 s profile
    intervals +570 to +630 s and +5814 to +5874 s at the profile step and
    joined their ends across each gap, so the three stretches merged and the
    0.0325 km CRITICAL approach at +1941.01 s was not reported. An interval
    with a state missing inside it is re-sampled, so the stretches stay apart,
    as on the fine-only path.
    """
    events = find_conjunctions(
        _store_from(GRAZE_GAP_INSIDE_TLES), hours=1.5, threshold_km=THRESHOLD_KM,
        start=START + timedelta(seconds=511.0), compute_probability=False,
        method=method, coarse_step_seconds=step_s,
    )
    _assert_found(_found(events), [
        (-608.28, 0.0072, RiskLevel.CRITICAL),
        (1941.01, 0.0325, RiskLevel.CRITICAL),
        (5862.38, 5.5572, RiskLevel.LOW),
    ])


@pytest.mark.parametrize("step_s", [60.0, 300.0])
def test_long_span_reports_every_stretch_between_three_sgp4_gaps(step_s: float):
    """From +415 s for 3.5 h: four stretches split by three gaps. The round-7
    code joined +5814 to +5874 s across the second gap and lost 0.1405 km
    CRITICAL at +7190.71 s."""
    events = find_conjunctions(
        _store_from(GRAZE_GAP_INSIDE_TLES), hours=3.5, threshold_km=THRESHOLD_KM,
        start=START + timedelta(seconds=415.0), compute_probability=False,
        coarse_step_seconds=step_s,
    )
    _assert_found(_found(events), [
        (-608.28, 0.0072, RiskLevel.CRITICAL),
        (1941.01, 0.0325, RiskLevel.CRITICAL),
        (7190.71, 0.1405, RiskLevel.CRITICAL),
        (12440.4, 0.2488, RiskLevel.CRITICAL),
    ])


# The round-8 safety review's drag-decaying near-circular pair (B* 0.05, e
# 0.0001, perigee about 270 km): SGP4's mean eccentricity carries a
# once-per-orbit B* * C5 * sin(M) term, and from +49217 to +49243 s it dips
# below -0.001 for A, which returns code 1 there. B has no errors.
CODE1_PAIR_TLES = """\
A
1 95001U          26001.00000000  .00000000  00000-0  50000-1 0    03
2 95001  20.0000  10.0000 0001003  30.0000   0.0000 15.90000000    02
B
1 95002U          26001.00000000  .00000000  00000-0  50000-1 0    04
2 95002  20.0200  10.0000 0001003  30.0000   0.0000 15.90000000    05
"""


def test_intervals_with_any_non_physical_fine_instant_are_found():
    """``_intervals_physical`` flags exactly the 60 s profile intervals holding
    a state missing from the 1 s grid, for a code 6 gap (GRAZE argp 85, from
    +43 s for 3 h, the alignment with both gaps wholly inside an interval) and
    a code 1 gap (from +47400 s for 1 h); it passes every interval of an
    ordinary LEO pair and says False wherever it was not asked."""
    h = 60.0
    cases = [
        (GRAZE_GAP_INSIDE_TLES, 43.0, 180, {(584 - 43) // 60, (5831 - 43) // 60}),
        (CODE1_PAIR_TLES, 47400.0, 60, {(49217 - 47400) // 60}),
        # The code 1 gap (+49217 to +49243 s) ending on an interval's first
        # interior instant, then starting on another's last.
        (CODE1_PAIR_TLES, 49242.0 - 1800.0, 60, {29, 30}),
        (CODE1_PAIR_TLES, 49158.0 - 1800.0, 60, {30, 31}),
        (SHALLOW_PAIR_TLES, 43.0, 180, set()),
    ]
    for text, first, m, expected in cases:
        a, b = _store_from(text).get_all()
        begin = START + timedelta(seconds=first)
        dense = cj._sample_pair(a, b, begin, m * h, 1.0)
        missing = np.setdiff1d(np.arange(int(m * h) + 1), dense.offsets)
        holding = {int(o) // int(h) for o in missing if o % int(h)}
        assert holding == expected, (first, sorted(holding))
        everything = np.ones(m, dtype=bool)
        clear = cj._intervals_physical(a, b, begin, everything, int(h), 1.0)
        assert set(np.flatnonzero(~clear).tolist()) == expected
        # Swapping the pair changes nothing; unasked intervals are False.
        assert bool(np.all(cj._intervals_physical(b, a, begin, everything, int(h), 1.0) == clear))
        some = np.zeros(m, dtype=bool)
        some[::7] = True
        partial = cj._intervals_physical(a, b, begin, some, int(h), 1.0)
        assert bool(np.all(partial == (clear & some)))


@pytest.mark.parametrize("position", [1, 2, 30, 58, 59])
def test_one_non_physical_instant_anywhere_inside_an_interval_flags_it(
    monkeypatch: pytest.MonkeyPatch, position: int
):
    """Every interior fine instant is checked: one state made non-physical at
    ``position`` seconds into interval 4 flags interval 4 and nothing else."""
    a, b = _store_from(SHALLOW_PAIR_TLES).get_all()
    begin = START + timedelta(seconds=10000.0)
    real = cj._fine_grid_physical
    target = 4 * 60 + position

    def one_bad(satrec, start, seconds):
        return real(satrec, start, seconds) & (np.asarray(seconds) != target)

    monkeypatch.setattr(cj, "_fine_grid_physical", one_bad)
    clear = cj._intervals_physical(a, b, begin, np.ones(10, dtype=bool), 60, 1.0)
    assert np.flatnonzero(~clear).tolist() == [4]


def test_runaway_code_0_states_inside_an_interval_flag_it():
    """The decayed debris returns code 0 with runaway positions from +4174 to
    +4214 s, between code 1 stretches. At a 20 s profile step the interval
    +4180 to +4200 s holds nothing but such states, so SGP4's codes alone
    would pass it; ``_intervals_physical`` agrees with the fine grid
    (``_sample_pair``) on every interval."""
    debris = _store_from(DECAYED_PAIR_TLES).get_all()[0]
    begin = START + timedelta(seconds=4100.0)
    ratio, m = 20, 15
    dense = cj._sample_pair(debris, debris, begin, m * ratio, 1.0)
    missing = np.setdiff1d(np.arange(m * ratio + 1), dense.offsets)
    holding = {int(o) // ratio for o in missing if o % ratio}
    clear = cj._intervals_physical(debris, debris, begin, np.ones(m, dtype=bool), ratio, 1.0)
    assert set(np.flatnonzero(~clear).tolist()) == holding

    sat = build_satrec(debris)
    jd, fr = datetime_to_jd(begin)
    inside = np.arange(81.0, 100.0)  # interval 4: +4181 to +4199 s
    codes, _r, _v = sat.sgp4_array(np.full(inside.size, jd), fr + inside / 86400.0)
    assert bool(np.all(np.asarray(codes) == 0))
    assert 4 in holding


def test_a_gap_splits_a_run_before_its_lowest_sample_is_taken(monkeypatch: pytest.MonkeyPatch):
    """Against 3.2 km the shallow pair's two dips (1.9098 km at +11843.35 s,
    2.9815 km at +13221.39 s, a 3.0308 km peak between) are one stretch, so
    only the first dip's interval is re-sampled. With a gap at the peak's
    interval the stretch splits, and the run on the far side must take its
    own lowest sample, not the first dip's, so the second dip is re-sampled
    too."""
    a, b = _store_from(SHALLOW_PAIR_TLES).get_all()
    begin = START + timedelta(seconds=SHALLOW_START_S - 1.0)
    first_dip, second_dip, peak_interval = 1783, 3161, 47

    def offsets() -> set[int]:
        samples, _joined = cj._long_span_samples(a, b, begin, 3602.0, 3.2, 60.0, 1.0)
        return set(samples.offsets.tolist())

    joined_run = offsets()
    assert {first_dip, first_dip + 1} <= joined_run
    assert second_dip not in joined_run

    real = cj._intervals_physical

    def gap_at_peak(tle_a, tle_b, start, candidates, ratio, step_seconds, *extra):
        clear = real(tle_a, tle_b, start, candidates, ratio, step_seconds, *extra)
        clear[peak_interval] = False
        return clear

    monkeypatch.setattr(cj, "_intervals_physical", gap_at_peak)
    split_run = offsets()
    assert {first_dip, first_dip + 1, second_dip, second_dip + 1} <= split_run


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_long_span_splits_stretches_at_a_code_1_gap_inside_one_profile_interval(
    step_s: float, method: str
):
    """From +47400 s for 1 h against 10 km the pair is within the threshold
    from +47400 to +49216 s and from +49244 s on (dense 1 s grid), split by
    A's code 1 gap. The round-8 code left the profile interval +49199 to
    +49259 s at the profile step and joined it across the gap, so only
    0.2819 km at +47936.06 s was reported and 0.2902 km CRITICAL at
    +50585.52 s, 44 min later, was lost."""
    events = find_conjunctions(
        _store_from(CODE1_PAIR_TLES), hours=1.0, threshold_km=THRESHOLD_KM,
        start=START + timedelta(seconds=47400.0), compute_probability=False,
        method=method, coarse_step_seconds=step_s,
    )
    _assert_found(_found(events), [
        (47936.06, 0.2819, RiskLevel.CRITICAL),
        (50585.52, 0.2902, RiskLevel.CRITICAL),
    ])


def test_identical_element_sets_split_at_a_code_1_gap(monkeypatch: pytest.MonkeyPatch):
    """Two copies of the round-8 review's B* 0.01 element set (75 h old at the
    window) both return code 1 from +273162 to +273203 s; each stretch either
    side is reported once, at its start, as on the fine-only path."""
    element_set = """\
1 95001U          26001.00000000  .00000000  00000-0  10000-1 0    03
2 95001  20.7840  10.0000 0001000  30.0000   0.0000 15.90000000    08
"""
    copy = element_set.replace("95001U", "95002U").replace("2 95001", "2 95002")
    store = _store_from("A\n" + element_set + "B\n" + copy)
    a, b = store.get_all()
    assert cj._same_elements(a, b)
    events = find_conjunctions(
        store, hours=1.0, threshold_km=THRESHOLD_KM,
        start=START + timedelta(seconds=271351.0), compute_probability=False,
    )
    _assert_found(_found(events), [
        (271351.0, 0.0, RiskLevel.CRITICAL),
        (273204.0, 0.0, RiskLevel.CRITICAL),
    ])
    # The interior check evaluates one copy; it flags the same intervals as
    # evaluating both (the gap falls in intervals 2 and 3 from +273000 s).
    begin = START + timedelta(seconds=273000.0)
    every = np.ones(10, dtype=bool)
    one = cj._intervals_physical(a, b, begin, every, 60, 1.0, same_elements=True)
    both = cj._intervals_physical(a, b, begin, every, 60, 1.0)
    assert np.flatnonzero(~one).tolist() == np.flatnonzero(~both).tolist() == [2, 3]
    # ... and the long span asks for that one copy.
    checked: list[int] = []
    real = cj._fine_grid_physical

    def count(satrec, start, seconds):
        checked.append(int(np.asarray(seconds).size))
        return real(satrec, start, seconds)

    monkeypatch.setattr(cj, "_fine_grid_physical", count)
    cj._long_span_samples(a, b, begin, 600.0, THRESHOLD_KM, 60.0, 1.0, same_elements=True)
    assert len(checked) == 1 and checked[0] > 0


# The round-9 safety review's pair: CODE1_PAIR_TLES tuned so that A's first
# code 1 dip lasts about 17 ms. From ULP_DIP_WINDOW - 1 s the 1 s grid's
# instant +1443 s falls inside it when timed as propagate_window_numpy times it
# (datetime_to_jd of the instant), and just outside it at fr(start) +
# 1443 / 86400, one ulp later.
ULP_DIP_PAIR_TLES = """\
A
1 95001U          26001.00000000  .00000000  00000-0  50000-1 0    03
2 95001  20.0017  10.0000 0001003  29.9883   0.0001 15.90000019    07
B
1 95002U          26001.00000000  .00000000  00000-0  50000-1 0    04
2 95002  20.0217  10.0000 0001003  29.9883   0.0001 15.90000019    00
"""
ULP_DIP_WINDOW = datetime(2026, 1, 1, 13, 16, 27, 769807, tzinfo=UTC)


def test_instant_jd_pairs_match_datetime_to_jd_bit_for_bit():
    """``_instant_jd_pairs`` gives each instant the (jd, fr) pair
    ``datetime_to_jd`` gives it, across days, microseconds, naive and
    non-UTC starts and fractional offsets; the start-anchored shortcut
    ``fr(start) + s / 86400`` does not."""
    rng = np.random.default_rng(7)
    starts = [
        ULP_DIP_WINDOW,
        datetime(2026, 3, 1, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 6, 30, 12, 0, 0, 1),
        datetime(2026, 12, 31, 18, 30, 0, 250000, tzinfo=timezone(timedelta(hours=-5))),
    ]
    # The first minute of a day with many start microseconds: there the
    # seconds and microseconds are fr's whole numerator, and summing them in
    # another order changes its last bit at about one combination in sixty.
    for micro in rng.choice(1_000_000, size=300, replace=False).tolist():
        start = datetime(2026, 1, 2, 0, 0, 0, int(micro), tzinfo=UTC)
        seconds = np.arange(60.0)
        jd, fr = cj._instant_jd_pairs(start, seconds)
        expected = [datetime_to_jd(start + timedelta(seconds=s)) for s in seconds.tolist()]
        assert list(zip(jd.tolist(), fr.tolist(), strict=True)) == expected
    for start in starts:
        seconds = np.sort(rng.choice(9 * 86400, size=400, replace=False)).astype(np.float64)
        jd, fr = cj._instant_jd_pairs(start, seconds)
        expected = [datetime_to_jd(start + timedelta(seconds=s)) for s in seconds.tolist()]
        assert list(zip(jd.tolist(), fr.tolist(), strict=True)) == expected
        fractional = seconds[:20] + 0.25
        jd, fr = cj._instant_jd_pairs(start, fractional)
        expected = [datetime_to_jd(start + timedelta(seconds=s)) for s in fractional.tolist()]
        assert list(zip(jd.tolist(), fr.tolist(), strict=True)) == expected
    empty_jd, empty_fr = cj._instant_jd_pairs(START, np.zeros(0))
    assert empty_jd.size == empty_fr.size == 0

    seconds = np.arange(3600.0)  # all on one day
    jd0, fr0 = datetime_to_jd(ULP_DIP_WINDOW)
    _jd, fr = cj._instant_jd_pairs(ULP_DIP_WINDOW, seconds)
    assert np.count_nonzero(fr != fr0 + seconds / 86400.0) > 100


def test_a_code_6_state_with_a_physical_neighbour_is_still_dropped():
    """SGP4 returns a finite position with code 6, so at the first instant of
    the grazing pair's gap the position-rate probe alone (1 s earlier, code 0,
    close) would pass the state; the fine grid drops it for its code, and so
    does ``_fine_grid_physical`` (a code 6 gap of a second or two inside an
    interval would otherwise be joined)."""
    tested = 0
    for tle in _store_from(GRAZE_GAP_INSIDE_TLES).get_all():
        sat = build_satrec(tle)
        seconds = np.arange(0.0, 7200.0)
        jd, fr = cj._instant_jd_pairs(START, seconds)
        err, r, _v = sat.sgp4_array(jd, fr)
        sixes = np.flatnonzero(np.asarray(err) == 6)
        if sixes.size == 0:
            continue
        first = sixes[:1].astype(np.float64)
        pos = np.asarray(r, dtype=np.float64)[sixes[:1]]
        assert bool(np.all(np.isfinite(pos)))
        jd0, fr0 = datetime_to_jd(START)
        assert bool(cj._physical_mask(sat, jd0, fr0, first, pos)[0])
        assert not bool(cj._fine_grid_physical(sat, START, first)[0])
        tested += 1
    assert tested >= 1


def test_interior_check_times_each_instant_as_the_fine_grid_does():
    """The 1 s grid drops +1443 s alone (A's code 1, timed by
    datetime_to_jd), which the start-anchored time would have passed;
    ``_intervals_physical`` flags interval 24, which holds it."""
    a, b = _store_from(ULP_DIP_PAIR_TLES).get_all()
    begin = ULP_DIP_WINDOW - timedelta(seconds=1.0)
    dense = cj._sample_pair(a, b, begin, 3600.0, 1.0)
    assert np.setdiff1d(np.arange(3601), dense.offsets).tolist() == [1443]
    sat = build_satrec(a)
    jd, fr = datetime_to_jd(begin + timedelta(seconds=1443.0))
    jd0, fr0 = datetime_to_jd(begin)
    shortcut = sat.sgp4_array(np.array([jd0]), np.array([fr0 + 1443.0 / 86400.0]))[0]
    assert sat.sgp4(jd, fr)[0] == 1
    assert int(shortcut[0]) == 0
    clear = cj._intervals_physical(a, b, begin, np.ones(60, dtype=bool), 60, 1.0)
    assert np.flatnonzero(~clear).tolist() == [24]


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_long_span_splits_stretches_at_a_millisecond_code_1_dip(step_s: float, method: str):
    """The round-9 code timed the interior check from the span start, passed
    the profile interval holding +1443 s and joined it, so 0.2903 km
    CRITICAL at +50585.70 s, 44 min after the first approach, was lost."""
    events = find_conjunctions(
        _store_from(ULP_DIP_PAIR_TLES), hours=1.0, threshold_km=THRESHOLD_KM,
        start=ULP_DIP_WINDOW, compute_probability=False,
        method=method, coarse_step_seconds=step_s,
    )
    _assert_found(_found(events), [
        (47936.235, 0.2819, RiskLevel.CRITICAL),
        (50585.697, 0.2903, RiskLevel.CRITICAL),
    ])


def test_every_probe_in_a_long_span_is_timed_from_its_start(monkeypatch: pytest.MonkeyPatch):
    """Profile samples, the interior check and re-sampled runs all time their
    position-rate probes from the span start, as the fine-only path does, so
    each keeps exactly the instants the fine-only path keeps. Against 3.2 km
    the shallow pair re-samples a run from interval 29, away from the start."""
    a, b = _store_from(SHALLOW_PAIR_TLES).get_all()
    begin = START + timedelta(seconds=SHALLOW_START_S - 1.0)
    anchors: set[tuple[float, float]] = set()
    real = cj._physical_mask

    def spy(satrec, jd, fr, seconds, positions):
        anchors.add((jd, fr))
        return real(satrec, jd, fr, seconds, positions)

    monkeypatch.setattr(cj, "_physical_mask", spy)
    samples, _joined = cj._long_span_samples(a, b, begin, 3602.0, 3.2, 60.0, 1.0)
    assert {1783, 1784} <= set(samples.offsets.tolist())
    assert anchors == {datetime_to_jd(begin)}


# The round-7 safety review's pair at a large threshold: from +8640 s the 5,731 s
# window ends with the pair 103.96 km apart and closing; it reaches 0.0836 km at
# +15951.29 s, 1,580 s after the window. The whole window is one stretch within
# 200 km whose lowest point inside the window is 1.3861 km at +10149.9 s.
WIDE_THRESHOLD_PAIR_TLES = """\
P-A
1 95001U          26001.00000000  .00000000  00000-0  10000-3 0    01
2 95001  70.0000   5.0000 0002000   0.0000   0.0000 14.89340181    00
P-B
1 95002U          26001.00000000  .00000000  00000-0  10000-3 0    02
2 95002  70.0000   5.0000 0062000   0.0000   0.7200 14.89290181    00
"""


@pytest.mark.parametrize("method", ["allpairs", "spatial"])
@pytest.mark.parametrize("step_s", [60.0, 120.0, 300.0])
def test_long_span_follows_an_approach_closing_at_the_window_end_at_a_wide_threshold(
    step_s: float, method: str
):
    """The last profile interval has no sample beyond it, so its acceleration
    estimate is the gate's fallback and its allowance about 90.5 km. The
    round-7 code still left it at the profile step against 200 km, so the last
    sample fell short of the span end, the approach closing there was not
    followed and 1.3861 km MODERATE was reported instead of 0.0836 km CRITICAL.
    The first and last intervals are now always re-sampled."""
    events = find_conjunctions(
        _store_from(WIDE_THRESHOLD_PAIR_TLES), hours=5731.0 / 3600.0, threshold_km=200.0,
        start=START + timedelta(seconds=8640.0), compute_probability=False,
        method=method, coarse_step_seconds=step_s,
    )
    _assert_found(_found(events), [(15951.29, 0.0836, RiskLevel.CRITICAL)])


def test_first_and_last_profile_intervals_are_always_re_sampled():
    """Against 200 km, 92 of the wide-threshold window's 96 profile intervals
    are left at the profile step; the two end intervals are sampled every 1 s
    up to the span end (and the two around the 1.3861 km low)."""
    a, b = _store_from(WIDE_THRESHOLD_PAIR_TLES).get_all()
    span = 5733.0
    samples, joined = cj._long_span_samples(
        a, b, START + timedelta(seconds=8639.0), span, 200.0, 60.0, 1.0
    )
    offsets = samples.offsets
    assert int(offsets[-1]) == int(span)
    assert bool(np.all(np.diff(offsets[:61]) == 1))
    assert bool(np.all(np.diff(offsets[offsets >= 5700]) == 1))
    assert int(np.count_nonzero(np.diff(offsets) == 60)) == 92
    assert bool(np.all(joined))
