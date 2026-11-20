"""Extended conjunction tests: edge cases and historical validation."""

from datetime import UTC, datetime

from sda.conjunction import classify_risk, find_conjunctions
from sda.models import RiskLevel
from sda.tle_store import TLEStore


def test_risk_boundary_critical():
    """Exactly 0.5 km should be HIGH, not CRITICAL."""
    assert classify_risk(0.5, 5.0) == RiskLevel.HIGH
    assert classify_risk(0.499, 5.0) == RiskLevel.CRITICAL


def test_risk_boundary_high_velocity():
    """Velocity threshold boundary at 5 km miss distance."""
    assert classify_risk(4.9, 10.1) == RiskLevel.HIGH
    assert classify_risk(4.9, 10.0) == RiskLevel.MODERATE
    assert classify_risk(4.9, 9.9) == RiskLevel.MODERATE


def test_risk_boundary_low():
    """Exactly 10 km should be NEGLIGIBLE."""
    assert classify_risk(10.0, 5.0) == RiskLevel.NEGLIGIBLE
    assert classify_risk(9.99, 5.0) == RiskLevel.LOW


def test_risk_extreme_velocity():
    """Very high relative velocity with moderate miss distance."""
    # 15 km/s is a realistic head-on LEO collision velocity
    assert classify_risk(3.0, 15.0) == RiskLevel.HIGH


def test_risk_zero_miss_distance():
    """Zero miss distance should be CRITICAL regardless of velocity."""
    assert classify_risk(0.0, 0.0) == RiskLevel.CRITICAL
    assert classify_risk(0.0, 15.0) == RiskLevel.CRITICAL


def test_risk_large_miss_distance():
    """Very large miss distance should always be NEGLIGIBLE."""
    assert classify_risk(1000.0, 15.0) == RiskLevel.NEGLIGIBLE
    assert classify_risk(100.0, 0.0) == RiskLevel.NEGLIGIBLE


def test_find_conjunctions_single_satellite():
    """Pipeline should return empty for a single satellite."""
    store = TLEStore()
    store.load_from_text(
        "ISS (ZARYA)\n"
        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"
    )
    events = find_conjunctions(store, hours=1.0)
    assert events == []


def test_find_conjunctions_sorted_by_risk():
    """Results should be sorted CRITICAL first, then by miss distance."""
    store = TLEStore()
    store.load_from_text(
        "ISS (ZARYA)\n"
        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014\n"
        "CSS (TIANHE)\n"
        "1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991\n"
        "2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"
    )
    events = find_conjunctions(store, hours=24.0, threshold_km=50.0)
    if len(events) > 1:
        risk_order = {
            RiskLevel.CRITICAL: 0, RiskLevel.HIGH: 1,
            RiskLevel.MODERATE: 2, RiskLevel.LOW: 3,
            RiskLevel.NEGLIGIBLE: 4,
        }
        for i in range(len(events) - 1):
            a, b = events[i], events[i + 1]
            assert (risk_order[a.risk], a.miss_distance_km) <= (
                risk_order[b.risk], b.miss_distance_km
            )


def test_historical_iridium_cosmos_like():
    """Validate detection of a head-on LEO conjunction scenario.

    Simulates an Iridium-33/Cosmos-2251-like geometry:
    two satellites in crossing orbits with high relative velocity.
    The system should detect a close approach within the threshold.

    The pair used to be ISS and CSS from 2024-02-14, which never come closer
    than 9,131 km in this window (1 s brute force), so the old
    ``if len(events) > 0`` guard skipped every assertion. This pair, from the
    synthetic catalog of scripts/bench_screening.py (also in
    tests/test_screening_recall.py), crosses at 85.8 and 50.8 degrees
    inclination and meets at 14.24 km/s, missing by 0.670 km at 01:27:41Z.
    """
    store = TLEStore()
    # Two LEO satellites with different inclinations: a crossing-orbit scenario
    store.load_from_text(
        "SYNTH 70086\n"
        "1 70086U 13067B   26081.25673732  .00013092  00000+0  24096-3 0  9998\n"
        "2 70086  85.8281  83.1960 0003303  95.4652 265.6260 15.54770332    17\n"
        "SYNTH 71200\n"
        "1 71200U 25275A   26081.16296743  .00015089  00000+0  28686-3 0  9991\n"
        "2 71200  50.8343 267.1080 0006186 218.8034 310.9980 15.54638451    19"
    )

    # Wide threshold: every node crossing within 100 km is its own event
    events = find_conjunctions(
        store,
        hours=48.0,
        threshold_km=100.0,
        start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        compute_probability=True,
    )

    # The crossing planes give a head-on approach under 1 km and further
    # passes of the same pair within 100 km over 48 hours
    assert len(events) >= 2
    assert any(
        e.miss_distance_km < 1.0 and e.relative_velocity_km_s > 10.0 for e in events
    )
    if len(events) > 0:
        # Validate event structure
        for e in events:
            assert e.primary in (70086, 71200)
            assert e.secondary in (70086, 71200)
            assert e.primary != e.secondary
            assert e.miss_distance_km <= 100.0
            assert e.miss_distance_km >= 0.0
            assert e.relative_velocity_km_s >= 0.0
            assert e.risk in RiskLevel
            # If Pc was computed, it should be bounded
            if e.collision_probability is not None:
                assert 0.0 <= e.collision_probability.probability <= 1.0
