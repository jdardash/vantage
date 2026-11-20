"""Safety-invariant regression tests.

Locks the risk-classification thresholds and the known probability-dilution
property of the 2D Pc method so neither can drift silently. Changes to these
tests require safety-reviewer approval (see CLAUDE.md).
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np

from sda import conjunction as cj
from sda.conjunction import classify_risk, coarse_gate_allowance_km
from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import RiskLevel
from sda.probability import compute_collision_probability, sigma_from_tle_age


class TestClassifyRiskBoundaries:
    """Exact boundary behavior of the safety-critical thresholds."""

    def test_below_half_km_is_critical(self):
        assert classify_risk(0.4999, 7.5) == RiskLevel.CRITICAL

    def test_exactly_half_km_is_high(self):
        # Boundary is exclusive: < 0.5 km
        assert classify_risk(0.5, 7.5) == RiskLevel.HIGH

    def test_below_one_km_is_high(self):
        assert classify_risk(0.9999, 7.5) == RiskLevel.HIGH

    def test_exactly_one_km_slow_is_moderate(self):
        assert classify_risk(1.0, 7.5) == RiskLevel.MODERATE

    def test_under_five_km_fast_is_high(self):
        assert classify_risk(4.9999, 10.0001) == RiskLevel.HIGH

    def test_under_five_km_at_ten_km_s_is_moderate(self):
        # Velocity boundary is exclusive: > 10 km/s
        assert classify_risk(4.9999, 10.0) == RiskLevel.MODERATE

    def test_exactly_five_km_is_low(self):
        assert classify_risk(5.0, 15.0) == RiskLevel.LOW

    def test_below_ten_km_is_low(self):
        assert classify_risk(9.9999, 7.5) == RiskLevel.LOW

    def test_exactly_ten_km_is_negligible(self):
        assert classify_risk(10.0, 7.5) == RiskLevel.NEGLIGIBLE

    def test_zero_miss_is_critical(self):
        assert classify_risk(0.0, 0.0) == RiskLevel.CRITICAL


class TestProbabilityDilution:
    """The 2D Pc dilution property is intentional and must stay documented.

    For a miss distance much smaller than the combined sigma, Pc scales as
    ~r_hb^2 / (2 sigma^2): growing sigma (stale TLEs) DECREASES reported Pc.
    Risk level is therefore keyed to miss distance, never to Pc.
    """

    @staticmethod
    def _pc(sigma_km: float) -> float:
        result = compute_collision_probability(
            miss_vector_km=np.array([0.02, 0.0, 0.0]),  # 20 m miss
            rel_velocity_km_s=np.array([0.0, 10.0, 0.0]),
            sigma_primary_km=sigma_km,
            sigma_secondary_km=sigma_km,
        )
        return result["probability"]

    def test_pc_decreases_as_sigma_grows_for_close_miss(self):
        pc_fresh = self._pc(0.05)
        pc_stale = self._pc(1.0)
        assert pc_fresh > pc_stale > 0.0

    def test_dilution_is_monotonic_across_decades(self):
        pcs = [self._pc(s) for s in (0.05, 0.2, 0.5, 1.0, 2.0)]
        assert all(a > b for a, b in pairwise(pcs))


class TestSigmaFromTleAge:
    """Age-scaled covariance must grow monotonically with TLE age."""

    def test_fresh_tle_uses_base_sigma(self):
        assert sigma_from_tle_age(0.0) == 0.050

    def test_negative_age_clamps_to_base(self):
        assert sigma_from_tle_age(-5.0) == 0.050

    def test_monotonic_growth(self):
        ages = [0.0, 6.0, 24.0, 72.0, 168.0, 336.0]
        sigmas = [sigma_from_tle_age(a) for a in ages]
        assert all(a <= b for a, b in pairwise(sigmas))

    def test_three_day_tle_roughly_half_km(self):
        # Vallado-style empirical model: ~10x base at 3 days
        assert 0.4 <= sigma_from_tle_age(72.0) <= 0.6


class TestCoarseGateSoundness:
    """The coarse gate may only grow: shrinking it reopens missed approaches.

    The gate keeps a pair on a coarse interval when the chord closest approach
    is within threshold + allowance (sda.conjunction.coarse_gate_allowance_km).
    Each term of the allowance is a bound; these lock them at or above the
    physical values they were derived from (WGS72 constants).
    """

    WGS72_J2 = 1.082616e-3

    def test_gravity_gradient_term_covers_point_mass_tides(self):
        assert cj._GRAVITY_GRADIENT_MAX_S2 >= 2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**3

    def test_non_point_mass_floor_covers_j2_per_object(self):
        j2_surface = 3.0 * self.WGS72_J2 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM**2
        assert j2_surface <= cj._NON_POINT_MASS_FLOOR_KM_S2
        # The default pair term is both objects at the floor.
        assert coarse_gate_allowance_km(10.0, 60.0, 0.0) >= coarse_gate_allowance_km(
            10.0, 60.0, 0.0, 2.0 * j2_surface
        )

    def test_non_point_mass_estimate_factor_not_reduced(self):
        # A velocity kink moves the path off the chord by up to 4x dt^2/8 times
        # the two-step weighted estimate (derivation at the constant).
        assert cj._NON_POINT_MASS_ESTIMATE_FACTOR >= 4.0

    def test_non_point_mass_fallback_covers_measured_high_drag_peak(self):
        # 2.33e-2 km/s^2: worst of 213 SGP4 element sets (B* to 1.0, perigee
        # down to 150 km), doubled.
        assert cj._NON_POINT_MASS_FALLBACK_KM_S2 >= 2.0 * 2.33e-2

    def test_coarse_step_cap_keeps_arc_over_chord_small(self):
        # The path bound uses the chord displacement; arc/chord at the fastest
        # valid angular rate must stay well inside the safety factor.
        assert cj._MAX_COARSE_STEP_SECONDS <= 300.0
        rate = np.sqrt(2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM) / EARTH_RADIUS_KM
        theta = rate * cj._MAX_COARSE_STEP_SECONDS
        assert theta / (2.0 * np.sin(theta / 2.0)) <= 1.02

    def test_safety_factor_not_reduced(self):
        assert cj._GATE_SAFETY_FACTOR >= 2.0

    def test_runaway_speed_cut_is_above_escape_speed(self):
        escape = np.sqrt(2.0 * EARTH_MU_KM3_S2 / EARTH_RADIUS_KM)
        assert escape < cj._MAX_PHYSICAL_SPEED_KM_S

    def test_leo_allowance_at_default_step(self):
        # Two LEO objects, 60 s step, 10 km threshold: 2.74 km.
        assert cj.DEFAULT_COARSE_STEP_SECONDS == 60.0
        assert coarse_gate_allowance_km(10.0, 60.0, 948.0) >= 2.73

    def test_pair_gate_never_below_threshold(self):
        disp = np.array([0.0, 10.0, 474.0, 1.0e4])
        gate = cj._pair_gate_km(10.0, 60.0, disp, disp)
        assert np.all(gate >= 10.0 + coarse_gate_allowance_km(10.0, 60.0, 2.0 * disp))

    def test_validity_scan_not_coarsened(self):
        # Intervals whose two samples both failed are searched for valid
        # states at this spacing; a valid stretch shorter than it is unscreened.
        assert cj._VALIDITY_SCAN_SECONDS <= 1.0

    def test_anchor_reach_covers_both_objects_at_the_speed_bound(self):
        # Each object can sit up to one interval at V from its anchor.
        for threshold_km, dt in ((10.0, 60.0), (1.0, 300.0), (50.0, 37.0)):
            assert cj._anchor_reach_km(threshold_km, dt) >= (
                threshold_km + 2.0 * cj._MAX_PHYSICAL_SPEED_KM_S * dt
            )

    def test_physical_probe_not_coarsened(self):
        # States are physical only if their position moves at most V over this
        # probe; a longer probe lets faster debris motion average out.
        assert cj._PHYSICAL_PROBE_SECONDS <= 1.0

    def test_gap_edge_resolution_within_ten_metres(self):
        # Polishing beside an SGP4 failure bisects the validity edge over one
        # refinement step; at 2 x 7.8 km/s the unsearched sliver stays < 10 m.
        sliver_s = cj._REFINE_STEP_SECONDS / 2.0**cj._GAP_EDGE_BISECTIONS
        assert 2.0 * 7.8 * sliver_s < 0.01

    def test_long_span_profile_step_not_coarsened(self):
        # The profile bound's allowance grows with the step squared and the
        # 0.43-of-allowance margin was measured at 30 s and 60 s only; past
        # 1,141 s the bound's 1 - c * GRADIENT denominator reaches zero.
        assert cj._LONG_SPAN_PROFILE_SECONDS <= 60.0

    def test_fine_span_limit_locked(self):
        # Spans up to the old +/- 5 min window (every fast encounter) are
        # sampled every refine step; only longer ones are profiled first. The
        # recall sweeps were run at this value, so a change comes back to review.
        assert cj._FINE_SPAN_MAX_SECONDS == 600.0

    def test_refine_step_is_whole_seconds(self):
        # A long span's interior gap check matches the fine grid bit for bit
        # only on whole-second offsets (_instant_jd_pairs, _intervals_physical);
        # a fractional refine step comes back to review.
        assert cj._REFINE_STEP_SECONDS == 1.0
