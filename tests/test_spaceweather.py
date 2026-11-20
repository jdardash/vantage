"""Tests for storm-aware drag and space weather (sda.spaceweather, sda.decay, sda.routes.space).

Offline and deterministic. The sample payloads below are trimmed from the live
NOAA SWPC products as fetched on 2026-11-19; the scales payload is the whole
product from that day. Every route test replaces ``spaceweather._fetch_json``
with a fake keyed by URL, so nothing here touches the network (tests/conftest.py
would refuse it anyway).
"""

from __future__ import annotations

import copy
import math
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import spaceweather as sw
from sda.decay import (
    AP_REFERENCE,
    STORM_DAY_AP,
    estimate_decay,
    geomagnetic_density_factor,
    geomagnetic_factor_top_km,
    jacchia_ap_temperature_delta_k,
    jacchia_night_temperature_k,
    orbit_perigee_km,
)
from sda.models import TLERecord
from sda.routes import space

# --------------------------------------------------------------------------
# Recorded samples (2026-11-19)
# --------------------------------------------------------------------------

FORECAST = {
    "type": "SWPC-Forecast",
    "issued": "2026-11-19T00:00:00Z",
    "product": "45 Day Forecast",
    "units": {"ap": "nT", "f107": "sfu"},
    "data": [
        {"time": "2026-11-19T00:00:00Z", "metric": "ap", "value": 8},
        {"time": "2026-11-19T00:00:00Z", "metric": "f107", "value": 115},
        {"time": "2026-11-20T00:00:00Z", "metric": "ap", "value": 12},
        {"time": "2026-11-20T00:00:00Z", "metric": "f107", "value": 115},
        {"time": "2026-11-21T00:00:00Z", "metric": "ap", "value": 8},
        {"time": "2026-11-21T00:00:00Z", "metric": "f107", "value": 105},
        {"time": "2027-01-02T00:00:00Z", "metric": "ap", "value": 8},
        {"time": "2027-01-02T00:00:00Z", "metric": "f107", "value": 135},
    ],
}

KP = [
    {"time_tag": "2026-11-12T00:00:00", "Kp": 1.0, "a_running": 4, "station_count": 8},
    {"time_tag": "2026-11-12T03:00:00", "Kp": 1.67, "a_running": 6, "station_count": 8},
    {"time_tag": "2026-11-12T06:00:00", "Kp": 1.67, "a_running": 6, "station_count": 8},
    {"time_tag": "2026-11-19T15:00:00", "Kp": 2.67, "a_running": 12, "station_count": 8},
    {"time_tag": "2026-11-19T18:00:00", "Kp": 3.0, "a_running": 15, "station_count": 8},
]

SCALES = {
    "0": {"DateStamp": "2026-11-19", "TimeStamp": "22:25:00",
          "R": {"Scale": "0", "Text": "none", "MinorProb": None, "MajorProb": None},
          "S": {"Scale": "0", "Text": "none", "Prob": None},
          "G": {"Scale": "0", "Text": "none"}},
    "1": {"DateStamp": "2026-11-19", "TimeStamp": "22:25:00",
          "R": {"Scale": None, "Text": None, "MinorProb": "15", "MajorProb": "5"},
          "S": {"Scale": None, "Text": None, "Prob": "1"},
          "G": {"Scale": "0", "Text": "none"}},
    "2": {"DateStamp": "2026-11-20", "TimeStamp": "00:00:00",
          "R": {"Scale": None, "Text": None, "MinorProb": "15", "MajorProb": "5"},
          "S": {"Scale": None, "Text": None, "Prob": "1"},
          "G": {"Scale": "0", "Text": "none"}},
    "3": {"DateStamp": "2026-11-21", "TimeStamp": "00:00:00",
          "R": {"Scale": None, "Text": None, "MinorProb": "15", "MajorProb": "5"},
          "S": {"Scale": None, "Text": None, "Prob": "1"},
          "G": {"Scale": "0", "Text": "none"}},
    "-1": {"DateStamp": "2026-11-18", "TimeStamp": "22:25:00",
           "R": {"Scale": "1", "Text": "minor", "MinorProb": None, "MajorProb": None},
           "S": {"Scale": "0", "Text": "none", "Prob": None},
           "G": {"Scale": "1", "Text": "minor"}},
}

F107 = [
    {"time_tag": "2026-11-19T20:00:00", "frequency": 2800, "flux": 107.0,
     "reporting_schedule": "Noon", "avg_begin_date": "2026-08-22T20:00:00",
     "ninety_day_mean": 111.0, "rec_count": 90},
    {"time_tag": "2026-11-19T17:00:00", "frequency": 2800, "flux": 110.0,
     "reporting_schedule": "Morning", "avg_begin_date": None, "ninety_day_mean": None,
     "rec_count": None},
    {"time_tag": "2026-11-18T22:00:00", "frequency": 2800, "flux": 115.0,
     "reporting_schedule": "Afternoon", "avg_begin_date": None, "ninety_day_mean": None,
     "rec_count": None},
    {"time_tag": "2026-10-09T20:00:00", "frequency": 2800, "flux": 121.0,
     "reporting_schedule": "Noon", "avg_begin_date": "2026-07-12T20:00:00",
     "ninety_day_mean": 115.0, "rec_count": 90},
]

ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)

# A low object (about 220 km) decaying a few km a day, so it re-enters inside
# a 45-day window. Built from the ISS lines with the mean motion and its
# derivative changed; build_satrec reads it the same way.
LOW_TLE = TLERecord(
    norad_id=99001,
    name="LOW-DECAY-TEST",
    line1="1 99001U 24001A   24045.51782528  .01000000  00000+0  22596-3 0  9997",
    line2="2 99001  51.6412 210.9280 0004885 231.2372 247.0342 16.20000000440014",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)

START = date(2026, 11, 19)


def flat_forecast(n: int, f107: float, ap: float, start: date = START) -> list[sw.ForecastDay]:
    return [sw.ForecastDay(day=start + timedelta(days=i), ap=ap, f107=f107) for i in range(n)]


def scales_with(day: date, g: int, offset: int = 2) -> tuple[sw.ScaleDay, ...]:
    return (sw.ScaleDay(offset=offset, day=day, time="00:00:00", g=g, s=None, r=None,
                        r_minor_prob=None, r_major_prob=None, s_prob=None),)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def test_parse_45_day_forecast_recorded_sample():
    fc = sw.parse_45_day_forecast(FORECAST)
    assert fc is not None
    assert fc.issued == "2026-11-19T00:00:00Z"
    assert [d.day for d in fc.days] == [date(2026, 11, 19), date(2026, 11, 20),
                                        date(2026, 11, 21), date(2027, 1, 2)]
    assert fc.days[1] == sw.ForecastDay(day=date(2026, 11, 20), ap=12.0, f107=115.0)
    assert fc.days[-1].f107 == 135.0


def test_parse_45_day_forecast_skips_junk_rows():
    data = copy.deepcopy(FORECAST)
    data["data"] = [
        "not a row", None, [1, 2],
        {"time": "garbage", "metric": "ap", "value": 9},
        {"time": "2026-11-19T00:00:00Z", "metric": "kp", "value": 3},
        {"time": "2026-11-19T00:00:00Z", "metric": "ap", "value": -1},       # fill value
        {"time": "2026-11-19T00:00:00Z", "metric": "ap", "value": True},     # a flag
        {"time": "2026-11-19T00:00:00Z", "metric": "ap", "value": "nan"},
        {"time": "2026-11-19T00:00:00Z", "metric": "f107", "value": 99999},
        {"time": "2026-11-19T00:00:00Z", "metric": "AP", "value": "14"},     # numeric string
        {"time": "2026-11-19T00:00:00Z", "metric": "ap", "value": 50},       # duplicate, ignored
        {"time": "2026-11-20 00:00:00", "metric": "f107", "value": 101.5},
        {"time": 20261121, "metric": "ap", "value": 9},
    ]
    fc = sw.parse_45_day_forecast(data)
    assert fc is not None
    assert fc.days == (
        sw.ForecastDay(day=date(2026, 11, 19), ap=14.0, f107=None),
        sw.ForecastDay(day=date(2026, 11, 20), ap=None, f107=101.5),
    )


@pytest.mark.parametrize("payload", [None, [], {}, {"data": "x"}, {"data": []},
                                     {"data": [{"time": "x"}]}, "text"])
def test_parse_45_day_forecast_unusable_is_none(payload):
    assert sw.parse_45_day_forecast(payload) is None


def test_parse_kp_recorded_sample_and_header_form():
    kp = sw.parse_kp(KP)
    assert kp is not None and len(kp) == 5
    assert kp[-1] == sw.KpReading(time=datetime(2026, 11, 19, 18, tzinfo=UTC), kp=3.0)
    assert sw.latest_kp(kp) == kp[-1]
    # the older header-row-plus-values form reads the same
    old = [["time_tag", "Kp", "a_running", "station_count"],
           ["2026-11-19 15:00:00.000", "2.67", "12", "8"],
           ["2026-11-19 18:00:00.000", "3.00", "15", "8"]]
    kp_old = sw.parse_kp(old)
    assert kp_old is not None and [r.kp for r in kp_old] == [2.67, 3.0]


def test_parse_kp_skips_junk():
    rows = [{"time_tag": "2026-11-19T18:00:00", "Kp": -1}, {"time_tag": "bad", "Kp": 4},
            {"time_tag": "2026-11-19T21:00:00", "Kp": 12}, {"Kp": 5}, "x",
            {"time_tag": "2026-11-19T21:00:00", "kp_index": 5.33}]
    kp = sw.parse_kp(rows)
    assert kp == (sw.KpReading(time=datetime(2026, 11, 19, 21, tzinfo=UTC), kp=5.33),)
    assert sw.parse_kp([]) is None and sw.parse_kp({"Kp": 3}) is None


def test_parse_scales_recorded_sample():
    scales = sw.parse_scales(SCALES)
    assert scales is not None
    assert [s.offset for s in scales] == [-1, 0, 1, 2, 3]
    yesterday, now = scales[0], scales[1]
    assert yesterday.g == 1 and yesterday.r == 1 and yesterday.observed
    assert now.day == date(2026, 11, 19) and now.g == 0 and now.s == 0 and now.r == 0
    ahead = sw.scales_ahead(scales)
    assert [s.day for s in ahead] == [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)]
    assert all(s.g == 0 and s.s is None and s.r_minor_prob == 15 and s.r_major_prob == 5
               and s.s_prob == 1 and not s.observed for s in ahead)
    assert sw.scale_now(scales) == now


def test_parse_scales_skips_junk():
    data = copy.deepcopy(SCALES)
    data["x"] = {"DateStamp": "2026-11-22"}
    data["4"] = "nope"
    data["5"] = {"DateStamp": "never", "G": {"Scale": "3"}}
    data["2"]["G"] = {"Scale": "9"}      # out of range
    data["3"]["G"] = {"Scale": "2.5"}    # not a level
    data["1"]["G"] = "G1"                # not a dict
    scales = sw.parse_scales(data)
    assert scales is not None
    assert [s.offset for s in scales] == [-1, 0, 1, 2, 3]
    assert [s.g for s in sw.scales_ahead(scales)] == [None, None, None]
    assert sw.parse_scales([]) is None and sw.parse_scales({"0": {}}) is None


def test_parse_f107_recorded_sample():
    f = sw.parse_f107(F107)
    assert f == sw.F107Observed(time=datetime(2026, 11, 19, 20, tzinfo=UTC), flux=107.0,
                                ninety_day_mean=111.0)


def test_parse_f107_skips_junk():
    rows = [{"time_tag": "2026-11-20T20:00:00", "frequency": 2800, "flux": -99999},
            {"time_tag": "2026-11-20T21:00:00", "frequency": 1415, "flux": 120.0},
            {"time_tag": "nope", "flux": 130.0},
            {"time_tag": "2026-11-19T17:00:00", "frequency": 2800, "flux": "110"}]
    f = sw.parse_f107(rows)
    assert f == sw.F107Observed(time=datetime(2026, 11, 19, 17, tzinfo=UTC), flux=110.0,
                                ninety_day_mean=None)
    assert sw.parse_f107([{"flux": 100}]) is None and sw.parse_f107(None) is None


def test_parse_45_day_forecast_drops_days_outside_the_window():
    data = copy.deepcopy(FORECAST)
    data["data"] += [
        {"time": "2099-01-01T00:00:00Z", "metric": "ap", "value": 400},    # far future
        {"time": "2027-01-18T00:00:00Z", "metric": "ap", "value": 300},    # issued + 60
        {"time": "2027-01-19T00:00:00Z", "metric": "ap", "value": 300},    # issued + 61
        {"time": "2026-11-17T00:00:00Z", "metric": "f107", "value": 120},  # issued - 2
        {"time": "2026-11-16T00:00:00Z", "metric": "f107", "value": 120},  # issued - 3
        {"time": "1999-01-01T00:00:00Z", "metric": "ap", "value": 400},
    ]
    fc = sw.parse_45_day_forecast(data)
    assert fc is not None
    assert [d.day for d in fc.days] == [date(2026, 11, 17), date(2026, 11, 19), date(2026, 11, 20),
                                        date(2026, 11, 21), date(2027, 1, 2), date(2027, 1, 18)]
    # no readable issue date: the rows cannot be placed in time, so no forecast
    for issued in ("sometime", None, 20261119):
        data["issued"] = issued
        assert sw.parse_45_day_forecast(data) is None


def test_g_from_kp_thresholds():
    # Kp in thirds: 5- (4.67) is G1, 9- (8.67) is still G4, 9o is G5
    kps = (0, 4.33, 4.666, 4.67, 5.0, 5.33, 5.67, 6, 6.67, 7.33, 7.67, 8.67, 9)
    assert [sw.g_from_kp(k) for k in kps] == [0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5]
    assert sw.g_from_kp(None) is None and sw.g_label(3) == "G3" and sw.g_label(None) is None
    assert sw.g_from_kp(math.nan) is None and sw.g_from_kp(math.inf) is None


def test_parse_time_forms():
    want = datetime(2026, 11, 19, 18, tzinfo=UTC)
    for text in ("2026-11-19T18:00:00", "2026-11-19 18:00:00", "2026-11-19T18:00:00Z",
                 "2026-11-19T20:00:00+02:00", " 2026-11-19T18:00:00.000 "):
        assert sw.parse_time(text) == want, text
    assert sw.parse_time("2026-11-19") == datetime(2026, 11, 19, tzinfo=UTC)
    for bad in (None, 5, "", "x" * 80, "2026-13-40"):
        assert sw.parse_time(bad) is None


# --------------------------------------------------------------------------
# Storm check thresholds
# --------------------------------------------------------------------------

TCA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def storm(tca: datetime, a: float | None, b: float | None, **kw: object) -> sw.StormCheck:
    return sw.check_conjunction_storm(tca, a, b, **kw)  # type: ignore[arg-type]


def test_storm_warning_needs_low_perigee_and_g1():
    g1 = scales_with(TCA.date(), 1)
    g0 = scales_with(TCA.date(), 0)
    assert storm(TCA, 599.9, 900.0, scales=g1).status == "warning"
    assert storm(TCA, 900.0, 599.9, scales=g1).status == "warning"   # either object
    at_limit = storm(TCA, 600.0, 900.0, scales=g1)                   # strictly under 600
    assert at_limit.status == "clear" and at_limit.low_perigee is False
    assert at_limit.warning is None
    assert storm(TCA, 650.0, 700.0, scales=g1).status == "clear"
    quiet = storm(TCA, 400.0, 400.0, scales=g0)
    assert quiet.status == "clear" and quiet.warning is None and quiet.reason is None
    nothing = storm(TCA, 400.0, 400.0)
    assert nothing.status == "unknown" and nothing.warning is None
    assert nothing.reason is not None and "unknown, not quiet" in nothing.reason


def test_unknown_perigee_is_never_taken_as_high():
    g3 = scales_with(TCA.date(), 3)
    one_high = storm(TCA, 700.0, None, scales=g3)
    assert one_high.low_perigee is None and one_high.status == "unknown"
    assert one_high.warning is None
    assert one_high.reason is not None and "G3" in one_high.reason
    assert "perigee is unknown" in one_high.reason
    one_low = storm(TCA, None, 300.0, scales=g3)     # a known low perigee is enough
    assert one_low.low_perigee is True and one_low.status == "warning"
    both_high = storm(TCA, 700.0, 650.0, scales=g3)
    assert both_high.low_perigee is False and both_high.status == "clear"
    # unknown perigee on a quiet day is clear: no storm to warn about
    assert storm(TCA, 700.0, None, scales=scales_with(TCA.date(), 0)).status == "clear"


def test_storm_warning_text_is_advisory():
    check = storm(TCA, 420.0, 800.0, scales=scales_with(TCA.date(), 2))
    assert check.level == 2 and check.basis == "noaa_scales_forecast" and check.covered
    assert check.min_perigee_km == 420.0 and check.low_perigee is True
    text = check.warning
    assert text is not None and check.status == "warning"
    assert "G2" in text and "2026-11-20" in text and "420 km" in text and "600 km" in text
    assert "In-track position error grows during storms" in text
    assert "less reliable" in text and "does not change the Pc or the risk level" in text
    assert all(ord(ch) < 128 for ch in text)


def test_storm_only_counts_the_tca_day():
    other_day = scales_with(TCA.date() + timedelta(days=1), 3)
    check = storm(TCA, 300.0, None, scales=other_day)
    assert check.warning is None and check.covered is False and check.level is None
    assert check.status == "unknown"


def test_storm_observed_scale_entry_is_observed_basis():
    observed = scales_with(TCA.date(), 1, offset=0)
    check = storm(TCA, 300.0, None, scales=observed)
    assert check.basis == "noaa_scales_observed"
    assert check.warning is not None and check.warning.startswith("NOAA SWPC reports a G1")


@pytest.mark.parametrize(("offset_h", "near"), [
    (-6.0, True),            # interval ended exactly 3 h before TCA
    (-6.0 - 1 / 60, False),  # a minute further back
    (-1.5, True),            # interval holding TCA
    (3.0, True),             # interval starts 3 h after TCA
    (3.0 + 1 / 60, False),
])
def test_kp_near_tca_window(offset_h, near):
    reading = sw.KpReading(time=TCA + timedelta(hours=offset_h), kp=5.0)
    check = storm(TCA, 450.0, None, kp=[reading])
    assert (check.kp_near_tca == 5.0) is near
    assert (check.warning is not None) is near


def test_kp_threshold_and_basis():
    at = TCA - timedelta(hours=1)
    below = storm(TCA, 450.0, 700.0, kp=[sw.KpReading(at, 4.33)])
    assert below.warning is None and below.level == 0 and below.covered
    assert below.status == "clear"
    minus = storm(TCA, 450.0, 700.0, kp=[sw.KpReading(at, 4.67)])   # Kp 5- is G1
    assert minus.level == 1 and minus.status == "warning" and minus.warning is not None
    assert minus.warning.startswith("Observed Kp 4.67 (G1) within 3 hours of TCA")
    at_g1 = storm(TCA, 450.0, None, kp=[sw.KpReading(at, 5.0)])
    assert at_g1.basis == "observed_kp" and at_g1.warning is not None
    assert at_g1.warning.startswith("Observed Kp 5.00 (G1) within 3 hours of TCA")
    # Kp stronger than the day's scale entry wins; an equal one defers to the scale
    both = storm(TCA, 450.0, None, scales=scales_with(TCA.date(), 1),
                 kp=[sw.KpReading(at, 7.0)])
    assert both.level == 3 and both.basis == "observed_kp"
    tie = storm(TCA, 450.0, None, scales=scales_with(TCA.date(), 1),
                kp=[sw.KpReading(at, 5.33)])
    assert tie.level == 1 and tie.basis == "noaa_scales_forecast"


def test_storm_check_tolerates_bad_perigees_and_naive_tca():
    naive = datetime(2026, 11, 20, 12, 0)
    check = storm(naive, math.nan, None, scales=scales_with(TCA.date(), 1))
    assert check.tca == TCA and check.min_perigee_km is None and check.low_perigee is None
    assert check.warning is None and check.covered and check.status == "unknown"
    inf = storm(TCA, math.inf, 700.0, scales=scales_with(TCA.date(), 1))
    assert inf.low_perigee is None and inf.status == "unknown"


def test_non_finite_kp_is_skipped_in_any_order():
    at = TCA - timedelta(hours=1)
    for readings in ([math.nan, 6.0], [6.0, math.nan]):
        check = storm(TCA, 450.0, 700.0, kp=[sw.KpReading(at, k) for k in readings])
        assert check.kp_near_tca == 6.0 and check.level == 2 and check.status == "warning"
    only_nan = storm(TCA, 450.0, 700.0, kp=[sw.KpReading(at, math.nan)])
    assert only_nan.kp_near_tca is None and only_nan.status == "unknown"
    junk = [sw.KpReading(at, None), sw.KpReading(at, "6"), sw.KpReading(at, 12.0)]  # type: ignore[arg-type]
    assert storm(TCA, 450.0, 700.0, kp=junk).kp_near_tca == 6.0


def test_storm_check_on_recorded_scales():
    scales = sw.parse_scales(SCALES)
    assert scales is not None
    yesterday = datetime(2026, 11, 18, 9, tzinfo=UTC)
    assert storm(yesterday, 420.0, 550.0, scales=scales).status == "warning"
    assert storm(TCA, 420.0, 550.0, scales=scales).status == "clear"


def test_storm_check_has_no_quiet_none_ambiguity():
    assert not hasattr(sw, "storm_warning")  # a bare None meant both quiet and unknown


# --------------------------------------------------------------------------
# Decay with a forecast
# --------------------------------------------------------------------------


def at_epoch(tle: TLERecord, day: date, norad_id: int | None = None) -> TLERecord:
    """The same elements with their epoch moved to midnight UTC on *day*."""
    field = f"{day.year % 100:02d}{day.timetuple().tm_yday:03d}.00000000"
    line1, line2 = tle.line1[:18] + field + tle.line1[32:], tle.line2
    if norad_id is not None:
        line1 = f"{line1[:2]}{norad_id:05d}{line1[7:]}"
        line2 = f"{line2[:2]}{norad_id:05d}{line2[7:]}"
    return tle.model_copy(update={
        "line1": line1, "line2": line2, "norad_id": norad_id or tle.norad_id,
        "epoch": datetime(day.year, day.month, day.day, tzinfo=UTC),
    })


ISS_NOW = at_epoch(ISS_TLE, START)
LOW_NOW = at_epoch(LOW_TLE, START)


def test_at_epoch_helper_moves_only_the_epoch():
    from sda.propagator import build_satrec

    a, b = build_satrec(ISS_TLE), build_satrec(ISS_NOW)
    assert b.epochyr == 26 and b.epochdays == pytest.approx(323.0)
    assert (a.no_kozai, a.ecco, a.ndot, a.bstar) == (b.no_kozai, b.ecco, b.ndot, b.bstar)


def test_flat_quiet_forecast_reproduces_the_single_value_estimate():
    single = estimate_decay(ISS_NOW, f107_solar_flux=115.0)
    flat = estimate_decay(ISS_NOW, f107_solar_flux=115.0,
                          forecast=flat_forecast(45, 115.0, AP_REFERENCE), start=START)
    assert single.space_weather_forecast is None
    assert flat.decay_rate_km_per_day == single.decay_rate_km_per_day
    assert abs(flat.estimated_lifetime_days - single.estimated_lifetime_days) <= 0.1
    use = flat.space_weather_forecast
    assert use is not None
    assert use.days_used == 45 and use.first_day == "2026-11-19" and use.last_day == "2027-01-02"
    assert use.storm_days == 0 and use.peak_ap == AP_REFERENCE and not use.reentry_within_window
    assert use.f107_after_window == 115.0 and use.ap_after_window == AP_REFERENCE
    assert use.tle_epoch == use.counted_from == "2026-11-19T00:00:00+00:00"
    assert use.tle_age_days == 0.0
    assert use.reentry_before_counted_from is False
    assert "Jacchia 1971" in use.ap_scaling
    assert all(ord(ch) < 128 for ch in flat.solar_activity_note)


def test_forecast_vs_single_value_follows_the_forecast_f107():
    single = estimate_decay(ISS_NOW, f107_solar_flux=150.0)
    high = estimate_decay(ISS_NOW, f107_solar_flux=150.0,
                          forecast=flat_forecast(45, 220.0, AP_REFERENCE), start=START)
    low = estimate_decay(ISS_NOW, f107_solar_flux=150.0,
                         forecast=flat_forecast(45, 80.0, AP_REFERENCE), start=START)
    assert high.estimated_lifetime_days < single.estimated_lifetime_days
    assert low.estimated_lifetime_days > single.estimated_lifetime_days
    assert high.decay_rate_km_per_day > single.decay_rate_km_per_day > low.decay_rate_km_per_day


def test_ap_storm_raises_decay():
    quiet = flat_forecast(45, 120.0, AP_REFERENCE)
    stormy = list(quiet)
    for i in range(3):
        stormy[i] = sw.ForecastDay(day=stormy[i].day, ap=150.0, f107=120.0)
    q = estimate_decay(LOW_NOW, 120.0, forecast=quiet, start=START)
    s = estimate_decay(LOW_NOW, 120.0, forecast=stormy, start=START)
    assert s.decay_rate_km_per_day > q.decay_rate_km_per_day * 1.2
    assert s.estimated_lifetime_days < q.estimated_lifetime_days
    assert s.space_weather_forecast is not None
    assert s.space_weather_forecast.storm_days == 3
    assert s.space_weather_forecast.peak_ap == 150.0
    assert "storm drag on 3 days" in s.solar_activity_note


def test_low_object_reenters_inside_the_window():
    est = estimate_decay(LOW_NOW, 120.0, forecast=flat_forecast(45, 120.0, 8.0), start=START)
    use = est.space_weather_forecast
    assert use is not None and use.reentry_within_window
    assert est.estimated_lifetime_days < 45 and use.days_used <= 45
    assert est.reentry_risk in ("HIGH", "MODERATE")
    assert "reentry inside the window" in est.solar_activity_note
    # agrees with the single-value estimate for a flat quiet forecast
    single = estimate_decay(LOW_NOW, 120.0)
    assert abs(est.estimated_lifetime_days - single.estimated_lifetime_days) <= 0.1


def test_lifetime_is_counted_from_start_after_spending_the_days_since_epoch():
    old = at_epoch(LOW_TLE, START - timedelta(days=10))
    single = estimate_decay(old, 120.0)             # counted from the epoch, as before
    est = estimate_decay(old, 120.0, forecast=flat_forecast(45, 120.0, 8.0), start=START)
    use = est.space_weather_forecast
    assert use is not None
    assert use.tle_epoch == "2026-11-09T00:00:00+00:00"
    assert use.counted_from == "2026-11-19T00:00:00+00:00"
    assert use.tle_age_days == 10.0 and use.reentry_before_counted_from is False
    assert use.first_day == "2026-11-19"            # the forecast itself starts at START
    assert abs(est.estimated_lifetime_days - (single.estimated_lifetime_days - 10)) <= 0.1
    assert est.decay_rate_km_per_day == single.decay_rate_km_per_day
    assert "10.0 days after the TLE epoch" in est.solar_activity_note


def test_time_of_day_counts_at_both_ends():
    """A 23:59 start is nearly a day later than 00:01; the 7-day IMMINENT line sees it."""
    old = at_epoch(LOW_TLE, START - timedelta(days=27))
    fc = flat_forecast(45, 120.0, 8.0)
    first_minute = datetime(2026, 11, 19, 0, 1, tzinfo=UTC)
    last_minute = datetime(2026, 11, 19, 23, 59, tzinfo=UTC)
    early = estimate_decay(old, 120.0, forecast=fc, start=first_minute)
    late = estimate_decay(old, 120.0, forecast=fc, start=last_minute)
    assert 7.0 <= early.estimated_lifetime_days < 7.5 and early.reentry_risk == "HIGH"
    assert 6.0 <= late.estimated_lifetime_days < 6.5 and late.reentry_risk == "IMMINENT"
    assert abs((early.estimated_lifetime_days - late.estimated_lifetime_days) - 1438 / 1440) < 0.11
    # a mid-day epoch spends only the rest of its own day
    noon = old.model_copy(update={"epoch": datetime(2026, 10, 23, 12, tzinfo=UTC)})
    half = estimate_decay(noon, 120.0, forecast=fc, start=first_minute)
    assert abs(half.estimated_lifetime_days - (early.estimated_lifetime_days + 0.5)) <= 0.1
    assert half.space_weather_forecast is not None
    assert half.space_weather_forecast.tle_age_days == pytest.approx(26.5 + 1 / 1440, abs=1e-3)


def test_reentry_before_the_forecast_reports_the_carry_in():
    old = at_epoch(LOW_TLE, START - timedelta(days=60))
    est = estimate_decay(old, 130.0, forecast=flat_forecast(45, 120.0, 8.0), f107_mean=200.0,
                         start=START)
    use = est.space_weather_forecast
    assert use is not None and use.reentry_before_counted_from and use.days_used == 0
    assert est.solar_activity_note.startswith(
        "Reentry before the first forecast day (2026-11-19), at F10.7 130 SFU and Ap 8")
    assert est.decay_rate_km_per_day == estimate_decay(old, 130.0).decay_rate_km_per_day
    assert est.estimated_lifetime_days == 1.0 and est.reentry_risk == "IMMINENT"
    assert use.reentry_before_forecast is True


def test_reentry_before_a_future_forecast_is_named_as_such():
    """Forecast starting after the reentry, elements fresh: not stale, just not reached."""
    late = flat_forecast(10, 120.0, 8.0, start=START + timedelta(days=40))
    est = estimate_decay(LOW_NOW, 120.0, forecast=late, start=START)
    use = est.space_weather_forecast
    assert use is not None and use.reentry_before_forecast and use.days_used == 0
    assert use.reentry_before_counted_from is False and not use.reentry_within_window
    assert est.solar_activity_note.startswith("Reentry before the first forecast day (2026-12-29)")


def test_after_window_uses_the_forecast_mean_not_one_day():
    flat = flat_forecast(45, 120.0, 8.0)
    last_low = [*flat[:44], sw.ForecastDay(flat[44].day, ap=8.0, f107=50.0)]
    a = estimate_decay(ISS_NOW, 120.0, forecast=flat, start=START)
    b = estimate_decay(ISS_NOW, 120.0, forecast=last_low, start=START)
    assert b.space_weather_forecast is not None
    assert b.space_weather_forecast.f107_after_window == round((44 * 120 + 50) / 45, 1)
    assert abs(b.estimated_lifetime_days / a.estimated_lifetime_days - 1.0) < 0.05


def test_elements_old_enough_to_have_reentered_floor_at_one_day():
    est = estimate_decay(LOW_TLE, 120.0, forecast=flat_forecast(45, 120.0, 8.0), start=START)
    use = est.space_weather_forecast
    assert use is not None and use.reentry_before_counted_from is True
    assert use.tle_age_days == (START - date(2024, 2, 14)).days and use.days_used == 0
    assert est.estimated_lifetime_days == 1.0 and est.reentry_risk == "IMMINENT"
    assert "predict reentry before 2026-11-19 00:00 UTC, refresh the TLE" in (
        est.solar_activity_note)


def test_start_before_the_epoch_counts_from_the_epoch():
    est = estimate_decay(ISS_NOW, 150.0, forecast=flat_forecast(3, 150.0, 8.0),
                         start=START - timedelta(days=5))
    use = est.space_weather_forecast
    assert use is not None and use.counted_from == "2026-11-19T00:00:00+00:00"
    assert use.tle_age_days == 0.0


def test_days_before_epoch_are_skipped_and_gaps_never_carry_ap():
    fc = [sw.ForecastDay(START - timedelta(days=5), ap=300.0, f107=300.0),  # before epoch
          sw.ForecastDay(START, ap=None, f107=None),                        # carry-in F
          sw.ForecastDay(START + timedelta(days=2), ap=40.0, f107=None),
          sw.ForecastDay(START + timedelta(days=3), ap=None, f107=None)]
    est = estimate_decay(ISS_NOW, 130.0, forecast=fc, start=START)
    use = est.space_weather_forecast
    assert use is not None
    assert use.first_day == START.isoformat() and use.days_used == 4
    assert use.f107_min == use.f107_max == 130.0       # never the 300 from before epoch
    assert use.peak_ap == 40.0 and use.storm_days == 1  # day 1 and day 3 are Ap 8
    assert use.ap_after_window == AP_REFERENCE


def test_a_storm_day_is_not_stretched_over_a_gap():
    gap = [sw.ForecastDay(START, ap=200.0, f107=120.0),
           sw.ForecastDay(START + timedelta(days=40), ap=8.0, f107=120.0)]
    full = [sw.ForecastDay(START + timedelta(days=i), ap=200.0 if i == 0 else 8.0, f107=120.0)
            for i in range(41)]
    a = estimate_decay(ISS_NOW, 120.0, forecast=gap, start=START)
    b = estimate_decay(ISS_NOW, 120.0, forecast=full, start=START)
    assert a.space_weather_forecast is not None and a.space_weather_forecast.storm_days == 1
    assert a.estimated_lifetime_days == b.estimated_lifetime_days


def test_far_future_rows_cannot_stretch_the_window():
    quiet = flat_forecast(3, 120.0, 8.0)
    junk = [*quiet, sw.ForecastDay(START + timedelta(days=300), ap=400.0, f107=300.0)]
    a = estimate_decay(LOW_NOW, 120.0, forecast=quiet, start=START)
    b = estimate_decay(LOW_NOW, 120.0, forecast=junk, start=START)
    assert b.space_weather_forecast is not None
    assert b.space_weather_forecast.last_day == "2026-11-21"
    assert b.space_weather_forecast.peak_ap == 8.0
    assert a.estimated_lifetime_days == b.estimated_lifetime_days


def test_forecast_horizon_is_forty_five_days_from_the_count_day():
    edge = [sw.ForecastDay(START + timedelta(days=d), ap=8.0, f107=120.0) for d in (44, 45)]
    est = estimate_decay(ISS_NOW, 120.0, forecast=edge, start=START)
    use = est.space_weather_forecast
    assert use is not None and use.first_day == use.last_day == "2027-01-02"


def test_f107_mean_is_used_after_the_window():
    fc = flat_forecast(10, 150.0, AP_REFERENCE)
    last = estimate_decay(ISS_NOW, 150.0, forecast=fc, start=START)
    mean = estimate_decay(ISS_NOW, 150.0, forecast=fc, f107_mean=90.0, start=START)
    assert last.space_weather_forecast is not None and mean.space_weather_forecast is not None
    assert last.space_weather_forecast.f107_after_window == 150.0
    assert mean.space_weather_forecast.f107_after_window == 90.0
    assert mean.estimated_lifetime_days > last.estimated_lifetime_days


def test_forecast_with_no_day_left_falls_back_to_the_single_value():
    stale = flat_forecast(5, 250.0, 200.0, start=START - timedelta(days=30))
    single = estimate_decay(ISS_NOW, 140.0)
    est = estimate_decay(ISS_NOW, 140.0, forecast=stale, start=START)
    use = est.space_weather_forecast
    assert use is not None and use.days_used == 0 and use.first_day is None
    assert est.estimated_lifetime_days == single.estimated_lifetime_days
    assert est.solar_activity_note.startswith(
        "Forecast covers no day from 2026-11-19 to 2027-01-02")


def test_start_accepts_a_date_or_a_datetime():
    fc = flat_forecast(3, 150.0, 60.0)
    a = estimate_decay(ISS_NOW, 150.0, forecast=fc, start=START)
    b = estimate_decay(ISS_NOW, 150.0, forecast=fc, start=datetime(2026, 11, 19, tzinfo=UTC))
    c = estimate_decay(ISS_NOW, 150.0, forecast=fc, start=datetime(2026, 11, 19))  # naive = UTC
    assert a == b == c


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_f107_is_refused(bad):
    with pytest.raises(ValueError, match="finite"):
        estimate_decay(ISS_NOW, bad)
    with pytest.raises(ValueError, match="finite"):
        estimate_decay(ISS_NOW, bad, forecast=flat_forecast(3, 120.0, 8.0), start=START)


def test_existing_signature_unchanged():
    est = estimate_decay(ISS_TLE)
    assert est.space_weather_forecast is None
    assert "moderate" in est.solar_activity_note.lower()
    assert all(ord(ch) < 128 for ch in est.solar_activity_note)
    assert estimate_decay(ISS_TLE, 70.0).estimated_lifetime_days > est.estimated_lifetime_days


def test_jacchia_ap_temperature_formula():
    assert jacchia_ap_temperature_delta_k(0.0) == 0.0
    assert jacchia_ap_temperature_delta_k(8.0) == pytest.approx(8 + 100 * (1 - math.exp(-0.64)))
    assert jacchia_ap_temperature_delta_k(400.0) == pytest.approx(500.0, abs=1e-6)
    assert jacchia_ap_temperature_delta_k(1e6) == jacchia_ap_temperature_delta_k(400.0)
    assert jacchia_night_temperature_k(150.0) == pytest.approx(379 + 3.24 * 150)
    assert jacchia_night_temperature_k(150.0, 120.0) == pytest.approx(379 + 3.24 * 120 + 1.3 * 30)


def test_geomagnetic_factor_shape():
    assert geomagnetic_density_factor(AP_REFERENCE, 400.0) == 1.0
    aps = [0, 8, 15, 30, 48, 100, 200, 400]
    at400 = [geomagnetic_density_factor(a, 400.0) for a in aps]
    assert at400 == sorted(at400) and at400[0] < 1.0
    # Ap 48 (a G1-like day) at 400 km: about 1.6x; Ap 400 about 5.6x (F10.7 150)
    assert 1.4 < geomagnetic_density_factor(48, 400.0) < 1.9
    assert 4.0 < geomagnetic_density_factor(400, 400.0) < 7.0
    # higher up, a storm matters more; at or below 120 km not at all
    assert geomagnetic_density_factor(100, 250.0) < geomagnetic_density_factor(100, 500.0)
    assert geomagnetic_density_factor(400, 120.0) == 1.0
    assert geomagnetic_density_factor(400, 90.0) == 1.0
    # held at the 600 km value above that at F10.7 150
    assert geomagnetic_density_factor(100, 900.0) == geomagnetic_density_factor(100, 600.0)
    # relatively larger at solar minimum
    assert geomagnetic_density_factor(100, 400.0, 70.0) > geomagnetic_density_factor(
        100, 400.0, 250.0)
    assert geomagnetic_density_factor(math.nan, 400.0) == 1.0
    assert geomagnetic_density_factor(100, math.inf) == 1.0
    assert STORM_DAY_AP == 30.0


def test_geomagnetic_factor_cap_follows_the_flux():
    assert [geomagnetic_factor_top_km(f) for f in (50.0, 70.0, 110.0, 150.0, 250.0)] == [
        450.0, 450.0, 525.0, 600.0, 600.0]
    # at solar minimum the factor stops growing at 450 km (F-bar 70)
    low = geomagnetic_density_factor(100, 450.0, 70.0)
    assert geomagnetic_density_factor(100, 500.0, 70.0) == low
    assert geomagnetic_density_factor(100, 800.0, 70.0) == low
    assert geomagnetic_density_factor(100, 400.0, 70.0) < low
    # the 81-day mean, not the daily value, sets the cap
    assert geomagnetic_density_factor(100, 550.0, 200.0, 70.0) == geomagnetic_density_factor(
        100, 450.0, 200.0, 70.0)


def test_orbit_perigee_matches_estimate():
    assert round(orbit_perigee_km(ISS_TLE), 1) == estimate_decay(ISS_TLE).perigee_km


# --------------------------------------------------------------------------
# Routes (fetch replaced)
# --------------------------------------------------------------------------


class FakeSWPC:
    """Stands in for spaceweather._fetch_json; answers per product, queued per call."""

    def __init__(self, **answers: object) -> None:
        self.queues = {name: list(v) if isinstance(v, tuple) else [v]
                       for name, v in answers.items()}
        self.urls = {url: name for name, url in sw.PRODUCTS.items()}
        self.calls: list[str] = []

    async def __call__(self, url: str) -> object:
        self.calls.append(url)
        queue = self.queues.get(self.urls[url], [None])
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)


def use(monkeypatch, **answers: object) -> FakeSWPC:
    fake = FakeSWPC(**answers)
    monkeypatch.setattr(sw, "_fetch_json", fake)
    return fake


def expire() -> None:
    assert sw._state.attempted_at is not None
    sw._state.attempted_at -= sw.CACHE_TTL_S + 1.0


@pytest.fixture(autouse=True)
def _fresh_cache():
    sw.clear_cache()
    yield
    sw.clear_cache()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(space.router)
    return TestClient(app)


ALL = {"forecast": FORECAST, "kp": KP, "scales": SCALES, "f107": F107}


def test_summary_shape(client, monkeypatch):
    fake = use(monkeypatch, **ALL)
    resp = client.get("/api/space-weather/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert sorted(fake.calls) == sorted(sw.PRODUCTS.values())
    assert data["kp_now"] == 3.0 and data["kp_time"] == "2026-11-19T18:00:00+00:00"
    assert data["kp_g_scale"] == "G0"
    assert data["g_scale_now"] == "G0" and data["g_scale_next_3_days_max"] == "G0"
    assert data["s_scale_now"] == "S0" and data["r_scale_now"] == "R0"
    assert data["scales_time"] == "2026-11-19 22:25:00"
    assert data["next_3_days"][1] == {"date": "2026-11-20", "g_scale": "G0", "r_minor_prob": 15,
                                      "r_major_prob": 5, "s_prob": 1}
    assert len(data["next_3_days"]) == 3
    assert data["storm_now"] is False and data["storm_forecast"] is False
    assert data["f107_observed"] == 107.0 and data["f107_90day_mean"] == 111.0
    assert data["forecast_issued"] == "2026-11-19T00:00:00Z"
    assert data["forecast"][0] == {"date": "2026-11-19", "ap": 8.0, "f107": 115.0}
    assert len(data["forecast"]) == 4 and data["forecast_peak_ap"] == 12.0
    assert data["stale"] is False and data["stale_products"] == []
    assert data["missing_products"] == [] and data["source"] == "NOAA SWPC"


def test_summary_flags_a_forecast_storm(client, monkeypatch):
    scales = copy.deepcopy(SCALES)
    scales["3"]["G"] = {"Scale": "2", "Text": "moderate"}
    kp = KP + [{"time_tag": "2026-11-19T21:00:00", "Kp": 5.33}]
    use(monkeypatch, **{**ALL, "scales": scales, "kp": kp})
    data = client.get("/api/space-weather/summary").json()
    assert data["g_scale_next_3_days_max"] == "G2" and data["storm_forecast"] is True
    assert data["kp_g_scale"] == "G1" and data["storm_now"] is True  # from Kp, scales say G0


def test_summary_cached_within_ttl_and_refetched_after(client, monkeypatch):
    kp2 = KP + [{"time_tag": "2026-11-19T21:00:00", "Kp": 4.0}]
    fake = use(monkeypatch, **{**ALL, "kp": (KP, kp2)})
    assert client.get("/api/space-weather/summary").json()["kp_now"] == 3.0
    assert client.get("/api/space-weather/summary").json()["kp_now"] == 3.0
    assert len(fake.calls) == 4
    expire()
    assert client.get("/api/space-weather/summary").json()["kp_now"] == 4.0
    assert len(fake.calls) == 8


def test_summary_stale_on_error_per_product(client, monkeypatch):
    use(monkeypatch, **{**ALL, "kp": (KP, None), "f107": (F107, RuntimeError("down"))})
    client.get("/api/space-weather/summary")
    expire()
    data = client.get("/api/space-weather/summary").json()
    assert data["stale"] is True and data["stale_products"] == ["f107", "kp"]
    assert data["kp_now"] == 3.0 and data["f107_observed"] == 107.0  # last good copies
    assert data["missing_products"] == []


def test_summary_missing_products_and_502(client, monkeypatch):
    use(monkeypatch, forecast=None, kp={"bad": 1}, scales="x", f107=[])
    resp = client.get("/api/space-weather/summary")
    assert resp.status_code == 502
    sw.clear_cache()
    use(monkeypatch, forecast=None, kp=KP, scales=None, f107=None)
    data = client.get("/api/space-weather/summary").json()
    assert data["kp_now"] == 3.0 and data["g_scale_now"] is None and data["forecast"] == []
    assert data["missing_products"] == ["f107", "forecast", "scales"]
    assert data["stale"] is False  # missing is not stale


def test_conjunction_route_warns(client, monkeypatch):
    scales = copy.deepcopy(SCALES)
    scales["2"]["G"] = {"Scale": "1", "Text": "minor"}  # 2026-11-20
    use(monkeypatch, **{**ALL, "scales": scales})
    resp = client.get("/api/space-weather/conjunction",
                      params={"tca": "2026-11-20T12:00:00Z", "perigee_a": 420, "perigee_b": 780})
    assert resp.status_code == 200
    data = resp.json()
    assert data["low_perigee"] is True and data["covered"] is True
    assert data["status"] == "warning" and data["reason"] is None
    assert data["g_scale_tca_day"] == "G1" and data["level"] == "G1"
    assert data["perigee_limit_km"] == 600.0
    storm = data["storm"]
    assert storm["g_scale"] == "G1" and storm["basis"] == "noaa_scales_forecast"
    assert "less reliable" in storm["message"]


def test_conjunction_route_quiet_high_and_uncovered(client, monkeypatch):
    use(monkeypatch, **ALL)
    quiet = client.get("/api/space-weather/conjunction",
                       params={"tca": "2026-11-20T12:00:00", "perigee_a": 420,
                               "perigee_b": 800}).json()
    assert quiet["storm"] is None and quiet["covered"] is True and quiet["level"] == "G0"
    assert quiet["status"] == "clear"
    assert quiet["tca"] == "2026-11-20T12:00:00+00:00"  # naive TCA is UTC
    high = client.get("/api/space-weather/conjunction",
                      params={"tca": "2026-11-18T12:00:00Z", "perigee_a": 700,
                              "perigee_b": 650}).json()
    assert high["storm"] is None and high["level"] == "G1" and high["low_perigee"] is False
    assert high["status"] == "clear"
    far = client.get("/api/space-weather/conjunction",
                     params={"tca": "2026-12-20T12:00:00Z", "perigee_a": 300,
                             "perigee_b": 800}).json()
    assert far["storm"] is None and far["covered"] is False and far["level"] is None
    assert far["status"] == "unknown" and "not quiet" in far["reason"]


def test_conjunction_route_uses_catalog_perigees(client, monkeypatch):
    use(monkeypatch, **ALL)
    monkeypatch.setattr(space, "_catalog_tle",
                        lambda nid: ISS_TLE if nid == 25544 else None)
    data = client.get("/api/space-weather/conjunction",
                      params={"tca": "2026-11-18T06:00:00Z", "primary": 25544,
                              "perigee_b": 900}).json()
    assert data["perigee_a_km"] == round(orbit_perigee_km(ISS_TLE), 1)
    assert data["storm"] is not None and data["storm"]["basis"] == "noaa_scales_observed"
    missing = client.get("/api/space-weather/conjunction",
                         params={"tca": "2026-11-18T06:00:00Z", "secondary": 11})
    assert missing.status_code == 404


def test_conjunction_route_errors(client, monkeypatch):
    use(monkeypatch, forecast=FORECAST, kp=None, scales=None, f107=F107)
    url, tca = "/api/space-weather/conjunction", "2026-11-20T12:00:00Z"
    none = client.get(url, params={"tca": tca})
    assert none.status_code == 422
    one = client.get(url, params={"tca": tca, "perigee_a": 700})  # unknown B is not "high"
    assert one.status_code == 422 and "both perigees" in one.json()["detail"]
    for bad_value in ("nan", "inf"):
        resp = client.get(url, params={"tca": tca, "perigee_a": bad_value, "perigee_b": 400})
        assert resp.status_code == 422, bad_value
    down = client.get(url, params={"tca": tca, "perigee_a": 400, "perigee_b": 500})
    assert down.status_code == 502
    bad = client.get(url, params={"tca": "soon", "perigee_a": 400, "perigee_b": 500})
    assert bad.status_code == 422


def test_full_app_serves_both_the_hud_strip_and_the_summary(monkeypatch):
    from sda.api import app
    from sda.routes import worldview

    async def offline(*args: object, **kwargs: object) -> None:
        return None

    use(monkeypatch, **ALL)
    monkeypatch.setattr(worldview, "_get_json", offline)
    worldview.clear_proxy_caches()
    full = TestClient(app)
    hud = full.get("/api/space-weather").json()
    assert "alerts" in hud and "forecast" not in hud  # the HUD strip, untouched
    summary = full.get("/api/space-weather/summary").json()
    assert summary["kp_now"] == 3.0 and len(summary["forecast"]) == 4
    worldview.clear_proxy_caches()


# --------------------------------------------------------------------------
# Decay route opt-in
# --------------------------------------------------------------------------


ROUTE_ID = 99002


@pytest.fixture()
def full_client():
    """The full app with the ISS elements re-dated to today under a test catalog number."""
    from sda.api import app, store

    tle = at_epoch(ISS_TLE, datetime.now(UTC).date(), norad_id=ROUTE_ID)
    assert store.load_from_text(f"DECAY-ROUTE-TEST\n{tle.line1}\n{tle.line2}") == 1
    yield TestClient(app)
    store.remove(ROUTE_ID)


def today_forecast(n: int, ap: float) -> dict:
    today = datetime.now(UTC).date()
    rows = []
    for i in range(n):
        stamp = f"{(today + timedelta(days=i)).isoformat()}T00:00:00Z"
        rows += [{"time": stamp, "metric": "ap", "value": ap},
                 {"time": stamp, "metric": "f107", "value": 130}]
    return {"issued": f"{today.isoformat()}T00:00:00Z", "data": rows}


def test_decay_route_without_forecast_is_unchanged(full_client, monkeypatch):
    fake = use(monkeypatch, **ALL)
    data = full_client.get(f"/satellites/{ROUTE_ID}/decay", params={"f107": 130}).json()
    assert fake.calls == []  # no SWPC request unless asked
    assert "forecast_source" not in data and data["space_weather_forecast"] is None
    assert data["f107_source"] == "user"


def test_decay_route_with_forecast(full_client, monkeypatch):
    use(monkeypatch, **{**ALL, "forecast": today_forecast(45, 8)})
    quiet = full_client.get(f"/satellites/{ROUTE_ID}/decay",
                            params={"f107": 130, "forecast": True}).json()
    src = quiet["forecast_source"]
    assert src["used"] is True and src["stale"] is False
    assert src["url"] == sw.FORECAST_45_DAY_URL and src["source"] == sw.FORECAST_SOURCE
    assert src["f107_mean"] == 111.0 and "90-day" in src["f107_mean_source"]
    assert quiet["space_weather_forecast"]["days_used"] == 45
    assert 0.0 <= quiet["space_weather_forecast"]["tle_age_days"] < 1.0  # epoch is 00:00 today
    sw.clear_cache()
    use(monkeypatch, **{**ALL, "forecast": today_forecast(45, 120)})
    stormy = full_client.get(f"/satellites/{ROUTE_ID}/decay",
                             params={"f107": 130, "forecast": True}).json()
    assert stormy["decay_rate_km_per_day"] > quiet["decay_rate_km_per_day"]
    assert stormy["estimated_lifetime_days"] < quiet["estimated_lifetime_days"]
    assert stormy["space_weather_forecast"]["storm_days"] == 45


def test_decay_route_forecast_unavailable_falls_back(full_client, monkeypatch):
    use(monkeypatch, forecast=None, kp=None, scales=None, f107=None)
    plain = full_client.get(f"/satellites/{ROUTE_ID}/decay", params={"f107": 130}).json()
    data = full_client.get(f"/satellites/{ROUTE_ID}/decay",
                           params={"f107": 130, "forecast": True}).json()
    assert data["forecast_source"]["used"] is False
    assert "unavailable" in data["forecast_source"]["error"]
    assert data["space_weather_forecast"] is None
    assert data["estimated_lifetime_days"] == plain["estimated_lifetime_days"]


def test_decay_route_forecast_with_no_usable_day_is_not_used(full_client, monkeypatch):
    past = today_forecast(3, 8)
    for row in past["data"]:
        row["time"] = "2020-01-01T00:00:00Z"  # before the elements' epoch
    past["issued"] = "2020-01-01T00:00:00Z"
    use(monkeypatch, **{**ALL, "forecast": past})
    data = full_client.get(f"/satellites/{ROUTE_ID}/decay",
                           params={"f107": 130, "forecast": True}).json()
    assert data["forecast_source"]["used"] is False
    assert "covers no day" in data["forecast_source"]["error"]
    assert data["space_weather_forecast"]["days_used"] == 0


def test_decay_route_says_when_the_elements_are_too_old(monkeypatch):
    from sda.api import app, store

    old = at_epoch(LOW_TLE, datetime.now(UTC).date() - timedelta(days=60), norad_id=99003)
    assert store.load_from_text(f"OLD-LOW-TEST\n{old.line1}\n{old.line2}") == 1
    try:
        use(monkeypatch, **{**ALL, "forecast": today_forecast(45, 8)})
        data = TestClient(app).get("/satellites/99003/decay",
                                   params={"f107": 130, "forecast": True}).json()
    finally:
        store.remove(99003)
    assert data["forecast_source"]["used"] is False
    assert "before any forecast day; refresh the TLE" in data["forecast_source"]["error"]
    assert data["solar_activity_note"].startswith("Reentry before the first forecast day")
    assert data["reentry_risk"] == "IMMINENT"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_decay_route_refuses_non_finite_f107(full_client, monkeypatch, bad):
    use(monkeypatch, **ALL)
    assert full_client.get(f"/satellites/{ROUTE_ID}/decay",
                           params={"f107": bad}).status_code == 422
