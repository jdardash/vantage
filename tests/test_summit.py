"""Tests for the summit conditions engine (sda.summit) and its routes (sda.routes.summit).

Offline: every upstream answer comes from a MockTransport built from payloads recorded on
2026-11-19 (Open-Meteo block headers, the NWS point for Mount Whitney, a Winter Storm
Warning, avalanche.org zone properties) with the hourly arrays generated around them, so a
verdict can be set up exactly. The first half ports the cases of trip-runbook's
tools/test_peakwatch.py for the functions copied from PeakWatch; the second half covers
what is new: any pressure level, the calibration region, US-only sources, batching, the
cache and the routes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda import summit as S
from sda.routes.summit import router

PST = timezone(timedelta(hours=-8))
DAY = date(2026, 11, 10)
LV = (700, 600, 500)

# tools/test_peakwatch.py's limits (OpenSnow keys dropped; no wind_odds_warn, so that rule
# is off unless a test adds it), plus the European AQI pair this port adds.
L = dict(wind_warn_mph=22, wind_fail_mph=30, chill_warn_f=-9, chill_fail_f=-19, snow_warn_in=0.1,
         snow_fail_in=0.4, precip_warn_in=0.02, p_warn=0.25, p_fail=0.5, aqi_warn=101,
         aqi_fail=151, eaqi_warn=60, eaqi_fail=80, fire_warn_mi=15, fire_fail_mi=5,
         fire_max_age_days=90, avy_warn_level=2, avy_fail_level=3)


def mod(wind=10, chill=10, snow=0.0, precip=0.0, temp=20, cape=0, cloud_h=0, snow72=0.0):
    return dict(wind=wind, chill=chill, snow=snow, precip=precip, temp=temp, cape=cape,
                cloud_h=cloud_h, snow72=snow72)


# --- heights (PeakWatch) ---------------------------------------------------------------


def test_at_height_interpolates_between_levels():
    assert S.at_height(12000, [(10000, 10), (14000, 30), (18000, 50)]) == pytest.approx(20)


def test_at_height_holds_wind_outside_the_band_and_ignores_missing_levels():
    assert S.at_height(9000, [(10000, 10), (14000, 30)]) == 10
    assert S.at_height(19000, [(10000, 10), (None, 99), (14000, None), (18000, 50)]) == 50
    assert S.at_height(12000, [(None, 1)]) is None


def test_at_height_moves_temperature_along_the_lapse_below_the_band():
    assert S.at_height(9000, [(10000, 20), (14000, 5)], lapse=0.003) == pytest.approx(23)


def om_block(day=DAY, hours=None, heights=(10000, 14000, 18500), winds=(10, 30, 60),
             temps=(20, 0, -20), models=("ecmwf_ifs025",), snow=0.0, clouds=None, levels=LV,
             tz=PST):
    """tools/test_peakwatch.py om_block, with unixtime stamps as this pull asks for."""
    hours = range(24) if hours is None else hours
    start = datetime.combine(day, time(0), tz)
    times = [int((start + timedelta(hours=h)).timestamp()) for h in hours]
    h: dict[str, Any] = {"time": times}
    units: dict[str, str] = {}
    for m in models:
        for i, (lv, z, w, t) in enumerate(zip(levels, heights, winds, temps, strict=True)):
            h[f"geopotential_height_{lv}hPa_{m}"] = [z] * len(times)
            units[f"geopotential_height_{lv}hPa_{m}"] = "ft"
            h[f"wind_speed_{lv}hPa_{m}"] = [w] * len(times)
            h[f"temperature_{lv}hPa_{m}"] = [t] * len(times)
            if clouds is not None:
                h[f"cloud_cover_{lv}hPa_{m}"] = [clouds[i]] * len(times)
        h[f"precipitation_{m}"] = [snow / 10] * len(times)
        h[f"snowfall_{m}"] = [snow] * len(times)
        h[f"cape_{m}"] = [100] * len(times)
    return {"hourly": h, "hourly_units": units}


def md(block, elev_ft, mode="", levels=LV, day=DAY, rejects=None):
    return S.model_day(S.summit_series(block, elev_ft, levels, mode, rejects), day, PST)


def test_model_day_reads_the_summit_height():
    m = md(om_block(), 12000)["ecmwf_ifs025"]
    assert m["wind"] == pytest.approx(20)
    assert m["temp"] == pytest.approx(10)
    assert m["chill"] < m["temp"]
    assert m["snow"] == 0


def test_model_day_exposed_summit_takes_at_least_the_600_wind():
    plain = md(om_block(), 12000)["ecmwf_ifs025"]
    exp = md(om_block(), 12000, "ca600")["ecmwf_ifs025"]
    assert plain["wind"] == pytest.approx(20) and exp["wind"] == pytest.approx(30)
    assert exp["chill"] < plain["chill"] and exp["temp"] == plain["temp"]


def test_model_day_exposed_leaves_summits_above_600_and_slower_600_alone():
    above = md(om_block(), 15000, "ca600")["ecmwf_ifs025"]
    assert above["wind"] == pytest.approx(30 + 30 * 1000 / 4500)
    slack = om_block(winds=(30, 10, 5))
    assert md(slack, 12000, "ca600")["ecmwf_ifs025"]["wind"] == pytest.approx(20)


def test_model_day_exposed_without_a_600_level_keeps_the_interpolated_wind():
    b = om_block()
    b["hourly"]["wind_speed_600hPa_ecmwf_ifs025"] = [None] * len(b["hourly"]["time"])
    assert md(b, 12000, "ca600")["ecmwf_ifs025"]["wind"] == pytest.approx(10 + 50 * 2000 / 8500)


def test_model_day_exposed_with_no_600_height_uses_the_standard_height():
    b = om_block()
    b["hourly"]["geopotential_height_600hPa_ecmwf_ifs025"] = [None] * len(b["hourly"]["time"])
    assert md(b, 12000, "ca600")["ecmwf_ifs025"]["wind"] == pytest.approx(30)
    assert md(b, 14500, "ca600")["ecmwf_ifs025"]["wind"] == pytest.approx(10 + 50 * 4500 / 8500)


def test_model_day_converts_metre_heights():
    b = om_block(heights=(3048, 4267.2, 5638.8))
    for k in b["hourly_units"]:
        b["hourly_units"][k] = "m"
    assert md(b, 12000)["ecmwf_ifs025"]["wind"] == pytest.approx(20)


def test_model_day_skips_a_model_short_of_hours():
    assert md(om_block(hours=range(0, 12)), 12000) == {}
    assert md(None, 12000) == {}


def test_model_day_sums_snow_over_the_wide_window():
    assert md(om_block(snow=0.1), 12000)["ecmwf_ifs025"]["snow"] == pytest.approx(1.2)


def test_sane_drops_and_counts_impossible_values():
    rejects: Counter[str] = Counter()
    assert S.sane(30, "wind", rejects) == 30 and S.sane(None, "wind", rejects) is None
    assert S.sane(137, "temp", rejects) is None and S.sane(-1, "snow", rejects) is None
    assert rejects == {"temp": 1, "snow": 1}
    assert S.sane_height(14000, 600) == 14000 and S.sane_height(4267, 600) is None


def test_model_day_drops_a_metre_height_labelled_feet():
    rejects: Counter[str] = Counter()
    assert md(om_block(heights=(3048, 4267.2, 5638.8)), 12000, rejects=rejects) == {}
    assert rejects["600 hPa height"] > 0


def test_model_day_counts_cloud_hours_at_summit_height():
    assert md(om_block(clouds=(100, 90, 0)), 12000)["ecmwf_ifs025"]["cloud_h"] == 4
    assert md(om_block(clouds=(100, 50, 0)), 15000)["ecmwf_ifs025"]["cloud_h"] == 0
    assert md(om_block(), 12000)["ecmwf_ifs025"]["cloud_h"] is None


def test_model_day_storm_snow_needs_the_past_72_hours():
    out = md(om_block(hours=range(-72, 24), snow=0.1), 12000)
    assert out["ecmwf_ifs025"]["snow72"] == pytest.approx(7.2)
    assert md(om_block(snow=0.1), 12000)["ecmwf_ifs025"]["snow72"] is None


def test_model_day_reads_the_afternoon_before_and_dawn():
    m = md(om_block(hours=range(-72, 24), temps=(40, 30, 20)), 12000)["ecmwf_ifs025"]
    assert m["thaw"] == pytest.approx(35) and m["dawn"] == pytest.approx(35)


def test_ens_p_means_the_member_probabilities_where_calibrated():
    b = om_block()
    h = b["hourly"]
    n = len(h["time"])
    h["wind_speed_600hPa"] = [5] * n  # member 0: under 18 km/h -> 0.02
    h["wind_speed_600hPa_member01"] = [40] * n  # 64 km/h -> 0.92
    p, k = S.ens_p(b, DAY, PST, 600, calibrated=True)
    assert k == 2 and p == pytest.approx((0.02 + 0.92) / 2)
    assert S.ens_p(None, DAY, PST, 600, True) == (None, 0)


# --- avalanche zones, alerts, fires (PeakWatch) ------------------------------------------

SQUARE = {"type": "Polygon", "coordinates": [
    [[-120, 37], [-118, 37], [-118, 39], [-120, 39], [-120, 37]],
    [[-119.5, 37.5], [-119, 37.5], [-119, 38], [-119.5, 38], [-119.5, 37.5]]]}

# avalanche.org map-layer properties as recorded on 2026-11-19 (Eastern Sierra Avalanche
# Center), on the test square instead of the real outline.
ESAC_PROPS = {
    "name": "Eastside Region", "center": "Eastern Sierra Avalanche Center",
    "timezone": "America/Los_Angeles", "center_id": "ESAC", "state": "CA", "off_season": False,
    "travel_advice": "Watch for signs of unstable snow such as recent avalanches, cracking in "
    "the snow, and audible collapsing.",
    "danger": "no rating", "danger_level": -1,
    "link": "https://www.esavalanche.org/forecasts#/eastside-region",
    "start_date": "2026-11-18T15:00:00", "end_date": "2026-11-20T16:00:00",
}


def test_in_geometry_handles_holes_and_multipolygons():
    assert S.in_geometry(38.5, -118.5, SQUARE)
    assert not S.in_geometry(37.75, -119.25, SQUARE)
    assert not S.in_geometry(36.5, -118.5, SQUARE)
    multi = {"type": "MultiPolygon",
             "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], SQUARE["coordinates"]]}
    assert S.in_geometry(38.5, -118.5, multi)
    assert not S.in_geometry(1, 1, None)


def test_avy_zone_and_days():
    f = {"geometry": SQUARE, "properties": dict(
        ESAC_PROPS, danger_level=3, start_date="2026-11-10T07:00:00",
        end_date="2026-11-12T00:00:00")}
    z = S.avy_zone(38.5, -118.5, [f])
    assert z is not None and z["center"] == "ESAC" and z["level"] == 3
    days = [DAY + timedelta(days=i) for i in range(4)]
    assert S.avy_days(z, days) == {DAY, DAY + timedelta(days=1)}
    assert S.avy_days({**z, "start": None}, days) == {DAY}
    assert S.avy_days({**z, "level": -1}, days) == set()
    assert S.avy_zone(36.0, -118.5, [f]) is None


def test_alert_level():
    fails = {"Winter Storm Warning"}
    assert S.alert_level("Winter Storm Warning", fails) == "fail"
    assert S.alert_level("Red Flag Warning", fails) == "warn"
    assert S.alert_level("Wind Advisory", fails) == "warn"
    assert S.alert_level("Special Weather Statement", fails) == "info"


def test_alerts_for_matches_zone_and_time():
    pt = {"forecastZone": "Z1", "county": "C1"}
    start, end = S.local_window(DAY, (6, 18), PST)
    a = {"properties": {"event": "Wind Advisory", "affectedZones": ["Z1"],
                        "onset": start.isoformat(), "ends": end.isoformat()}}
    other_zone = {"properties": {"event": "X", "affectedZones": ["Z9"]}}
    later = {"properties": {"event": "Y", "affectedZones": ["C1"], "onset": end.isoformat()}}
    no_times = {"properties": {"event": "Z", "affectedZones": ["C1"]}}
    got = S.alerts_for(pt, [a, other_zone, later, no_times], start, end)
    assert [e for e, _ in got] == ["Wind Advisory", "Z"]


def test_fires_near_drops_contained_and_old_fires():
    now = datetime(2026, 11, 10, tzinfo=UTC)
    ms = int((now - timedelta(days=3)).timestamp() * 1000)
    old = int((now - timedelta(days=200)).timestamp() * 1000)
    fires = [
        {"attributes": dict(IncidentName="Near", PercentContained=20, FireDiscoveryDateTime=ms),
         "geometry": {"x": -118.3, "y": 36.6}},
        {"attributes": dict(IncidentName="Out", PercentContained=100, FireDiscoveryDateTime=ms),
         "geometry": {"x": -118.3, "y": 36.6}},
        {"attributes": dict(IncidentName="Old", PercentContained=0, FireDiscoveryDateTime=old),
         "geometry": {"x": -118.3, "y": 36.6}},
        {"attributes": dict(IncidentName="Far", PercentContained=None, FireDiscoveryDateTime=ms),
         "geometry": {"x": -120.0, "y": 38.0}},
    ]
    out = S.fires_near(36.58, -118.29, fires, now, 90)
    assert [n for n, *_ in out] == ["Near", "Far"]
    assert out[0][1] < 2


def test_product_day_morning_and_evening_issues():
    assert S.product_day("2026-11-10T14:00:00+00:00", PST) == date(2026, 11, 10)
    assert S.product_day("2026-11-10T02:00:00+00:00", PST) == date(2026, 11, 10)


def test_avy_for_day_prefers_the_product_then_the_map_layer():
    zone = dict(center="ESAC", level=2)
    d0, d1, d2 = DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)
    prod = dict(live=True, type="forecast", day=d0, current=3, tomorrow=2, problems=["Wind Slab"])
    assert S.avy_for_day(zone, {d0}, prod, d0) == dict(center="ESAC", level=3,
                                                       problems=["Wind Slab"])
    assert S.avy_for_day(zone, {d0}, prod, d1)["level"] == 2
    assert S.avy_for_day(zone, {d0}, prod, d2) is None
    assert S.avy_for_day(zone, {d0}, dict(prod, tomorrow=None), d0)["level"] == 3
    assert S.avy_for_day(zone, {d0}, None, d0) == dict(center="ESAC", level=2, problems=[])
    assert S.avy_for_day(None, {d0}, prod, d0) is None
    v, flags = S.assess(L, {"a": mod(), "b": mod()},
                        avy=dict(center="ESAC", level=3, problems=["Wind Slab"]))
    assert v == "NO" and ("fail", "avalanche 3 Considerable (ESAC): Wind Slab") in flags


# --- scoring (PeakWatch assess) ----------------------------------------------------------


def test_assess_go_marg_no_on_model_wind():
    assert S.assess(L, {"a": mod(wind=10)}) == ("GO", [])
    assert S.assess(L, {"a": mod(wind=22)})[0] == "MARG"
    assert S.assess(L, {"a": mod(wind=30)})[0] == "NO"
    assert S.assess(L, {"a": mod(wind=10), "b": mod(wind=40), "c": mod(wind=12)})[0] == "MARG"
    assert S.assess(L, {"a": mod(wind=10), "b": mod(wind=29), "c": mod(wind=12)})[0] == "GO"


def test_assess_chill_snow_precip_and_probability():
    assert S.assess(L, {"a": mod(chill=-9)})[0] == "MARG"
    assert S.assess(L, {"a": mod(chill=-19)})[0] == "NO"
    assert S.assess(L, {"a": mod(snow=0.1)})[0] == "MARG"
    assert S.assess(L, {"a": mod(), "b": mod(snow=0.4)})[0] == "NO"
    assert S.assess(L, {"a": mod(precip=0.02)})[0] == "MARG"
    assert S.assess(L, {"a": mod()}, p=0.25)[0] == "MARG"
    assert S.assess(L, {"a": mod()}, p=0.5)[0] == "NO"


def test_assess_flags_one_model_alone_at_the_no_line():
    v, flags = S.assess(L, {"ecmwf_ifs025": mod(wind=34), "gfs_seamless": mod(wind=12),
                            "icon_seamless": mod(wind=14), "gem_seamless": mod(wind=15)})
    assert v == "MARG" and ("warn", "ECMWF alone has wind 34 mph (median 14)") in flags
    v, flags = S.assess(L, {"a": mod(chill=-25), "b": mod(chill=0), "c": mod(chill=2)})
    assert v == "MARG" and any("alone has chill -25 F" in t for _, t in flags)


def test_assess_cloud_cape_and_storm_snow():
    assert S.assess(L, {"a": mod(cloud_h=3), "b": mod(cloud_h=2)})[0] == "MARG"
    assert S.assess(L, {"a": mod(cloud_h=1), "b": mod(cloud_h=2)})[0] == "GO"
    assert S.assess(L, {"a": mod(cape=300)})[0] == "MARG"
    assert S.assess(L, {"a": mod(cape=900)})[0] == "NO"
    assert S.assess(L, {"a": mod(snow72=7), "b": mod(snow72=1)})[0] == "MARG"
    assert S.assess(L, {"a": mod(snow72=13)})[0] == "NO"
    v, flags = S.assess(L, {"a": mod(snow72=None)})
    assert v == "GO" and flags[0][0] == "info"


def test_assess_a_required_source_not_read_is_never_go():
    req = {"models", "nws-alerts", "avy"}
    v, flags = S.assess(L, {"a": mod(), "b": mod()},
                        gaps=[("nws-alerts", "NWS alerts not read")], required=req)
    assert v == "MARG" and ("warn", "NWS alerts not read") in flags
    v, flags = S.assess(L, {"a": mod(), "b": mod()},
                        gaps=[("fires", "NIFC wildfires not read")], required=req)
    assert v == "GO" and ("info", "NIFC wildfires not read") in flags


def test_assess_without_any_forecast_is_unknown():
    v, flags = S.assess(L, None, None, None)
    assert v == "?" and flags[-1] == ("info", "no model forecast")


def test_assess_alerts_avalanche_fire_and_air():
    fails = {"Winter Storm Warning"}
    assert S.assess(L, {"a": mod()}, alerts=[("Winter Storm Warning", "")],
                    fail_events=fails)[0] == "NO"
    assert S.assess(L, {"a": mod()}, alerts=[("Wind Advisory", "")],
                    fail_events=fails)[0] == "MARG"
    assert S.assess(L, {"a": mod()}, nws_text="Chance Showers And Thunderstorms")[0] == "MARG"
    z = dict(level=3, center="ESAC")
    assert S.assess(L, {"a": mod()}, avy=z)[0] == "NO"
    assert S.assess(L, {"a": mod()}, avy={**z, "level": 1})[0] == "GO"
    assert S.assess(L, {"a": mod()}, fires=[("F", 4.0, 100, 0)])[0] == "NO"
    assert S.assess(L, {"a": mod()}, fires=[("F", 14.0, 100, 0)])[0] == "MARG"
    assert S.assess(L, {"a": mod()}, fires=[("F", 16.0, 100, 0)])[0] == "GO"
    assert S.assess(L, {"a": mod()}, aqi=160)[0] == "NO"


def test_assess_reads_the_european_aqi_on_its_own_scale():
    v, flags = S.assess(L, {"a": mod()}, aqi=70, aqi_scale="European AQI")
    assert v == "MARG" and ("warn", "European AQI 70 (smoke, dust)") in flags
    assert S.assess(L, {"a": mod()}, aqi=85, aqi_scale="European AQI")[0] == "NO"
    assert S.assess(L, {"a": mod()}, aqi=70, aqi_scale="US AQI")[0] == "GO"


def test_assess_notes_a_melt_freeze_morning():
    v, flags = S.assess(L, {"a": dict(mod(), thaw=36, dawn=20),
                            "b": dict(mod(), thaw=34, dawn=25)})
    assert v == "GO" and any(lv == "info" and t.startswith("melt-freeze") for lv, t in flags)
    v, flags = S.assess(L, {"a": dict(mod(), thaw=30, dawn=20)})
    assert not any(t.startswith("melt-freeze") for _, t in flags)


# --- wind odds (PeakWatch, tools/wind_odds.json and tools/summit_odds.json) ---------------

ODDS = dict(limit_mph=21.75, run_age_h=6, bands=[
    dict(lead_days=[0, 1], intercept=-9.0, coef_median=0.43, coef_spread=0.0),
    dict(lead_days=[2, 3], intercept=-6.5, coef_median=0.30, coef_spread=0.0),
    dict(lead_days=[6, 7], intercept=-2.75, coef_median=0.12, coef_spread=0.0)])
SUMMIT = dict(ODDS, coef_cool=1.0, cool_months=[10, 11, 12, 1, 2, 3, 4])


def test_wind_odds_by_lead_band():
    mods = {"a": mod(wind=18), "b": mod(wind=18)}
    p0 = S.wind_odds(ODDS, mods, 20)
    assert p0 == pytest.approx(1 / (1 + math.exp(9.0 - 0.43 * 18)))
    assert S.wind_odds(ODDS, mods, 6 * 24 + 17) > p0
    assert S.wind_odds(ODDS, mods, 9 * 24) == S.wind_odds(ODDS, mods, 7 * 24)
    assert S.wind_odds(ODDS, mods, 4 * 24 + 5) is None


def test_wind_odds_needs_two_models_and_a_model():
    assert S.wind_odds(ODDS, {"a": mod(wind=18)}, 20) is None
    assert S.wind_odds(None, {"a": mod(wind=18), "b": mod(wind=18)}, 20) is None


def test_wind_odds_takes_the_higher_band_between_lead_days():
    mods = {"a": mod(wind=10), "b": mod(wind=18)}
    late = S.wind_odds(ODDS, mods, 2 * 24 - 3)
    assert late == S.wind_odds(ODDS, mods, 2 * 24 + 1) > S.wind_odds(ODDS, mods, 36)


def test_valid_odds_rejects_a_band_missing_a_number_and_a_bad_cool_term():
    good_band = dict(lead_days=[0, 7], intercept=-9.0, coef_median=0.4, coef_spread=0.0)
    assert not S.valid_odds(dict(limit_mph=21.75, bands=[dict(lead_days=[0, 7])]))
    assert not S.valid_odds(dict(limit_mph=21.75, bands=[dict(good_band, intercept="x")]))
    assert not S.valid_odds(dict(limit_mph="21.75", bands=[good_band]))
    assert S.valid_odds(dict(limit_mph=21.75, bands=[good_band]))
    for bad in (dict(SUMMIT, coef_cool="1"), dict(SUMMIT, coef_cool=float("nan")),
                dict(SUMMIT, cool_months=[13]), dict(SUMMIT, cool_months=[]),
                dict(SUMMIT, cool_months="10"),
                {k: v for k, v in SUMMIT.items() if k != "cool_months"}):
        assert not S.valid_odds(bad), bad
    assert S.valid_odds(SUMMIT)


def test_shipped_odds_load_and_rise_with_wind_and_winter():
    assert S.valid_odds(S.WIND_ODDS) and S.valid_odds(S.SUMMIT_ODDS)
    for lead_h in (20, 70, 120, 170):
        lo = S.wind_odds(S.WIND_ODDS, {"a": mod(wind=8), "b": mod(wind=10)}, lead_h)
        hi = S.wind_odds(S.WIND_ODDS, {"a": mod(wind=24), "b": mod(wind=26)}, lead_h)
        assert 0 <= lo < 0.2 < 0.5 < hi <= 1
        calm = {"a": mod(wind=6), "b": mod(wind=8)}
        slo = S.wind_odds(S.SUMMIT_ODDS, calm, lead_h, date(2026, 7, 10))
        shi = S.wind_odds(S.SUMMIT_ODDS, {"a": mod(wind=34), "b": mod(wind=36)}, lead_h,
                          date(2026, 7, 10))
        winter = S.wind_odds(S.SUMMIT_ODDS, calm, lead_h, date(2026, 1, 10))
        assert 0 <= slo < 0.25 and slo + 0.2 < shi <= 1 and winter > slo


def test_summit_odds_add_the_cool_term_only_in_cool_months():
    mods = {"a": mod(wind=18), "b": mod(wind=18)}
    warm = S.wind_odds(SUMMIT, mods, 20, date(2026, 7, 10))
    cold = S.wind_odds(SUMMIT, mods, 20, date(2026, 1, 10))
    assert warm == pytest.approx(1 / (1 + math.exp(9.0 - 0.43 * 18)))
    assert cold == pytest.approx(1 / (1 + math.exp(9.0 - 0.43 * 18 - 1.0)))
    assert S.wind_odds(SUMMIT, mods, 20) == warm
    assert S.wind_odds(ODDS, mods, 20, date(2026, 1, 10)) == warm


def test_assess_wind_odds_marg_only_under_the_wind_line():
    Lo = dict(L, wind_odds_warn=0.2)
    mods = {"a": mod(wind=15), "b": mod(wind=17)}
    v, flags = S.assess(Lo, mods, odds=(0.35, 21.75))
    assert v == "MARG" and any("wind odds 35%" in t for _, t in flags)
    assert S.assess(Lo, mods, odds=(0.1, 21.75))[0] == "GO"
    assert S.assess(L, mods, odds=(0.9, 21.75))[0] == "GO"
    v, flags = S.assess(Lo, {"a": mod(wind=25), "b": mod(wind=25)}, odds=(0.9, 21.75))
    assert v == "MARG" and not any("wind odds" in t for _, t in flags)


def test_assess_summit_odds_marg_alongside_the_wind_odds():
    Ls = dict(L, wind_odds_warn=0.15, summit_odds_warn=0.25)
    mods = {"a": mod(wind=15), "b": mod(wind=17)}
    v, flags = S.assess(Ls, mods, odds=(0.05, 21.75), summit_odds=(0.3, 21.75))
    assert v == "MARG" and any("summit wind odds 30%" in t for _, t in flags)
    assert S.assess(Ls, mods, odds=(0.05, 21.75), summit_odds=(0.2, 21.75))[0] == "GO"
    assert S.assess(Ls, mods, odds=(0.2, 21.75), summit_odds=(0.2, 21.75))[0] == "MARG"


# --- what is new: any summit, any place ------------------------------------------------


@pytest.mark.parametrize("elev_m,band", [
    (1345, (1000, 925, 850)),  # Ben Nevis
    (4421, (700, 600, 500)),  # Mount Whitney: PeakWatch's levels
    (3700, (700, 600, 500)),
    (4808, (600, 500, 400)),  # Mont Blanc
    (5895, (600, 500, 400)),  # Kilimanjaro
    (6190, (500, 400, 300)),  # Denali
    (8849, (400, 300, 250)),  # Everest
    (-400, (1000, 925, 850)),
])
def test_level_band_brackets_every_summit(elev_m, band):
    assert S.level_band(elev_m) == band
    lo, _, hi = band
    assert S.STD_HEIGHT_M[lo] <= max(elev_m - S.BAND_MARGIN_M, S.STD_HEIGHT_M[1000])
    assert S.STD_HEIGHT_M[hi] >= elev_m or hi == 250


def test_exposed_rule_outside_the_region_takes_the_next_level_when_close():
    near = md(om_block(), 12000, "next")["ecmwf_ifs025"]  # 600 hPa 2,000 ft (610 m) above
    assert near["wind"] == pytest.approx(30)
    far = md(om_block(), 15000, "next")["ecmwf_ifs025"]  # 500 hPa 3,500 ft (1,067 m) above
    assert far["wind"] == pytest.approx(30 + 30 * 1000 / 4500)
    slack = md(om_block(winds=(30, 10, 5)), 12000, "next")["ecmwf_ifs025"]
    assert slack["wind"] == pytest.approx(20)


def test_freezing_level_from_the_levels():
    pts = [(10000, 40.0), (14000, 24.0), (18500, 0.0)]
    assert S.freezing_level_ft(pts) == pytest.approx(12000)
    cold = S.freezing_level_ft([(10000, 20.0), (14000, 10.0)])
    assert cold == pytest.approx(10000 - 12 / S.LAPSE_F_PER_FT)
    assert S.freezing_level_ft([(10000, -60.0)]) == 0.0
    assert S.freezing_level_ft([(None, 40.0)]) is None


def test_calibration_region_is_california_and_nevada_only():
    inside = [(36.5785, -118.2923), (37.63, -119.03), (39.53, -119.81), (36.17, -115.14),
              (37.80, -122.27), (34.05, -118.24)]
    outside = [(45.8326, 6.8652), (63.0692, -151.007), (39.1178, -106.4454), (43.5, -121.0),
               (31.0, -115.5), (36.5, -112.0), (-3.0674, 37.3556)]
    assert all(S.in_cal_region(a, b) for a, b in inside)
    assert not any(S.in_cal_region(a, b) for a, b in outside)
    assert S.Point(36.5785, -118.2923, 4421).calibrated
    assert not S.Point(36.5785, -118.2923, 1500).calibrated  # band misses 600 hPa
    assert not S.Point(45.8326, 6.8652, 4808).calibrated


def test_sun_times_with_a_polar_guard():
    rise, set_, polar = S.sun_times(date(2026, 11, 20), 36.5785, -118.2923)
    assert polar == "" and rise is not None and set_ is not None
    assert rise.astimezone(PST).hour == 6 and set_.astimezone(PST).hour == 16
    assert S.sun_times(date(2026, 12, 21), 80.0, 0.0) == (None, None, "polar night")
    assert S.sun_times(date(2026, 6, 21), 80.0, 0.0) == (None, None, "midnight sun")


def test_zone_falls_back_to_a_fixed_offset_or_solar_time():
    z = S.zone_of({"utc_offset_seconds": 20700, "timezone": "Not/AZone"}, 86.9)
    assert z.exact is False and z.tz.utcoffset(None) == timedelta(seconds=20700)
    solar = S.zone_of(None, 86.9)
    assert solar.exact is False and solar.tz.utcoffset(None) == timedelta(hours=6)


# --- a recorded-shape upstream ------------------------------------------------------------

NOW = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)  # 08:00 in California, 17:00 in the Alps

MONT_BLANC = dict(id="mont-blanc", name="Mont Blanc", lat=45.8326, lon=6.8652, elev_m=4808)
WHITNEY = dict(id="whitney", name="Mount Whitney", lat=36.5785, lon=-118.2923, elev_m=4421)
ELBERT = dict(id="elbert", name="Mount Elbert", lat=39.1178, lon=-106.4454, elev_m=4401)
TREMBLANT = dict(id="tremblant", name="Mont Tremblant", lat=46.21, lon=-74.58, elev_m=875)
EVEREST = dict(id="everest", name="Everest", lat=27.9881, lon=86.925, elev_m=8849)

# api.weather.gov/points/36.5785,-118.2923 as recorded 2026-11-19 (properties used here).
NWS_WHITNEY = {
    "forecastZone": "https://api.weather.gov/zones/forecast/CAZ519",
    "county": "https://api.weather.gov/zones/county/CAC027",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/CAZ226",
    "forecast": "https://api.weather.gov/gridpoints/VEF/14,134/forecast",
    "gridId": "VEF", "timeZone": "America/Los_Angeles",
    "relativeLocation": {"properties": {"city": "Lone Pine", "state": "CA"}},
}
NWS_ELBERT = {
    "forecastZone": "https://api.weather.gov/zones/forecast/COZ060",
    "county": "https://api.weather.gov/zones/county/COC065",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/COZ220",
    "forecast": "https://api.weather.gov/gridpoints/PUB/30,99/forecast",
    "gridId": "PUB", "timeZone": "America/Denver",
}
# The Winter Storm Warning on Whitney's zone in the recorded CA feed (times moved to NOW).
STORM = {"properties": {
    "event": "Winter Storm Warning",
    "headline": "Winter Storm Warning issued November 19 at 1:40PM PST until November 20 at "
    "4:00AM PST by NWS Las Vegas NV",
    "affectedZones": ["https://api.weather.gov/zones/forecast/CAZ519"],
    "onset": "2026-11-20T05:00:00-08:00", "ends": "2026-11-21T04:00:00-08:00",
    "severity": "Severe"}}

# (timezone, utc_offset_seconds) as Open-Meteo answered with timezone=auto.
ZONES = [(-140, ("America/Anchorage", -32400)), (-112, ("America/Los_Angeles", -28800)),
         (-90, ("America/Denver", -25200)), (-60, ("America/Toronto", -18000)),
         (30, ("Europe/Rome", 3600)), (60, ("Africa/Dar_es_Salaam", 10800)),
         (180, ("Asia/Kathmandu", 20700))]


def zone_for(lon: float) -> tuple[str, int]:
    return next(z for top, z in ZONES if lon < top)


CALM = dict(wind=3.0, temp13k=30.0, snow=0.0, precip=0.0, cape=0.0, cloud=10.0, gust=15.0,
            ens_wind=None, us_aqi=20.0, eu_aqi=15.0, dem=None)


class Upstream:
    """Answers every upstream the engine calls, the way each one answered on 2026-11-19."""

    def __init__(self) -> None:
        self.now = NOW
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.headers: list[str] = []
        self.fail: set[str] = set()
        self.weather: dict[tuple[float, float], dict[str, Any]] = {}
        self.default = dict(CALM)
        self.nws = {S.round_point(WHITNEY["lat"], WHITNEY["lon"]): NWS_WHITNEY,
                    S.round_point(ELBERT["lat"], ELBERT["lon"]): NWS_ELBERT}
        self.alerts: dict[str, list[Any]] = {"CA": [], "CO": []}
        self.avy: list[Any] = []
        self.avy_lists: dict[str, Any] = {}
        self.avy_products: dict[str, Any] = {}
        self.fires: list[Any] = []
        self.thunder: set[int] = set()
        self.run_fc = 1795089600
        self.run_ens = 1795089600

    def kind(self, url: httpx.URL) -> str:
        u = str(url)
        if "meta.json" in u:
            return "om-ens-meta" if "ensemble" in u else "om-meta"
        if "ensemble-api" in u:
            return "om-ens"
        if "air-quality" in u:
            return "om-aq"
        if "/v1/elevation" in u:
            return "om-elev"
        if "/v1/forecast" in u:
            return "om-fc"
        if "/points/" in u:
            return "nws-point"
        if "/alerts/active" in u:
            return "nws-alerts"
        if "/gridpoints/" in u:
            return "nws-fc"
        if "map-layer" in u:
            return "avy"
        if "/public/products" in u:
            return "avy-list"
        if "/public/product/" in u:
            return "avy-prod"
        if "arcgis" in u:
            return "fires"
        return "?"

    def wx(self, lat: float, lon: float) -> dict[str, Any]:
        return {**self.default, **self.weather.get(S.round_point(lat, lon), {})}

    def cells(self, q: dict[str, str]) -> list[tuple[float, float]]:
        lats = [float(x) for x in q["latitude"].split(",")]
        lons = [float(x) for x in q["longitude"].split(",")]
        return list(zip(lats, lons, strict=True))

    def clock(self, lon: float, days_back: int, n_days: int) -> tuple[list[int], str, int]:
        name, off = zone_for(lon)
        tz = timezone(timedelta(seconds=off))
        today = self.now.astimezone(tz).date()
        start = datetime.combine(today - timedelta(days=days_back), time(0), tz)
        times = [int((start + timedelta(hours=i)).timestamp()) for i in range(24 * n_days)]
        return times, name, off

    def header(self, lat: float, lon: float, name: str, off: int) -> dict[str, Any]:
        return {"latitude": lat, "longitude": lon, "generationtime_ms": 1.0,
                "utc_offset_seconds": off, "timezone": name, "timezone_abbreviation": "",
                "elevation": 4000.0}

    def forecast(self, q: dict[str, str]) -> list[dict[str, Any]]:
        levels = sorted({int(x) for x in re.findall(r"wind_speed_(\d+)hPa", q["hourly"])},
                        reverse=True)
        out = []
        for lat, lon in self.cells(q):
            w = self.wx(lat, lon)
            times, name, off = self.clock(lon, int(q["past_days"]), int(q["past_days"]) + 8)
            n = len(times)
            h: dict[str, Any] = {"time": times}
            units: dict[str, str] = {"time": "unixtime"}
            for m in q["models"].split(","):
                wind = w["wind"][m] if isinstance(w["wind"], dict) else w["wind"]
                for lv in levels:
                    z = round(S.STD_HEIGHT_M[lv] * S.FT_PER_M)
                    h[f"geopotential_height_{lv}hPa_{m}"] = [z] * n
                    units[f"geopotential_height_{lv}hPa_{m}"] = "ft"
                    h[f"wind_speed_{lv}hPa_{m}"] = [wind] * n
                    t = w["temp13k"] - (z - 13000) * S.LAPSE_F_PER_FT
                    h[f"temperature_{lv}hPa_{m}"] = [round(t, 1)] * n
                    h[f"cloud_cover_{lv}hPa_{m}"] = [w["cloud"]] * n
                h[f"precipitation_{m}"] = [w["precip"]] * n
                h[f"snowfall_{m}"] = [w["snow"]] * n
                h[f"cape_{m}"] = [w["cape"]] * n
                h[f"wind_gusts_10m_{m}"] = [w["gust"]] * n
            out.append({**self.header(lat, lon, name, off), "hourly_units": units, "hourly": h})
        return out

    def ensemble(self, q: dict[str, str]) -> list[dict[str, Any]]:
        (lv,) = re.findall(r"wind_speed_(\d+)hPa", q["hourly"])
        out = []
        for lat, lon in self.cells(q):
            w = self.wx(lat, lon)
            ens = w["ens_wind"] if w["ens_wind"] is not None else (
                w["wind"] if not isinstance(w["wind"], dict) else max(w["wind"].values()))
            times, name, off = self.clock(lon, 0, 8)
            h: dict[str, Any] = {"time": times, f"wind_speed_{lv}hPa": [ens] * len(times)}
            for k in range(1, 51):
                h[f"wind_speed_{lv}hPa_member{k:02d}"] = [ens] * len(times)
            out.append({**self.header(lat, lon, name, off), "hourly": h})
        return out

    def air(self, q: dict[str, str]) -> list[dict[str, Any]]:
        out = []
        for lat, lon in self.cells(q):
            w = self.wx(lat, lon)
            times, name, off = self.clock(lon, 0, 5)
            out.append({**self.header(lat, lon, name, off), "hourly": {
                "time": times, "us_aqi": [w["us_aqi"]] * len(times),
                "european_aqi": [w["eu_aqi"]] * len(times)}})
        return out

    def nws_forecast(self, url: str) -> dict[str, Any]:
        lon = -118.29 if "VEF" in url else -106.45
        name, off = zone_for(lon)
        tz = timezone(timedelta(seconds=off))
        today = self.now.astimezone(tz).date()
        periods = []
        for i in range(8):
            d = today + timedelta(days=i)
            short = "Chance Snow Showers And Thunderstorms" if i in self.thunder else "Sunny"
            periods.append({"startTime": datetime.combine(d, time(6), tz).isoformat(),
                            "isDaytime": True, "shortForecast": short, "temperature": 18,
                            "temperatureUnit": "F", "windSpeed": "5 to 10 mph"})
        return {"properties": {"periods": periods}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        kind = self.kind(request.url)
        q = dict(request.url.params)
        self.requests.append((kind, q))
        self.headers.append(request.headers.get("user-agent", ""))
        if kind in self.fail:
            return httpx.Response(500, json={"error": True, "reason": "upstream down"})
        body: Any
        if kind == "om-meta":
            body = {"last_run_initialisation_time": self.run_fc}
        elif kind == "om-ens-meta":
            body = {"last_run_initialisation_time": self.run_ens}
        elif kind in ("om-fc", "om-ens", "om-aq"):
            make = {"om-fc": self.forecast, "om-ens": self.ensemble, "om-aq": self.air}[kind]
            blocks = make(q)
            body = blocks[0] if len(blocks) == 1 else blocks  # one point: one object, as live
        elif kind == "om-elev":
            body = {"elevation": [self.wx(a, b)["dem"] or 1000.0 for a, b in self.cells(q)]}
        elif kind == "nws-point":
            lat, lon = (float(x) for x in request.url.path.rsplit("/", 1)[1].split(","))
            props = self.nws.get(S.round_point(lat, lon))
            if props is None:
                return httpx.Response(404, json={"title": "Data Unavailable For Requested Point"})
            body = {"properties": props}
        elif kind == "nws-alerts":
            body = {"type": "FeatureCollection", "features": self.alerts.get(q.get("area"), [])}
        elif kind == "nws-fc":
            body = self.nws_forecast(str(request.url))
        elif kind == "avy":
            body = {"type": "FeatureCollection", "features": self.avy}
        elif kind == "avy-list":
            c = q.get("avalanche_center_id", "")
            if c not in self.avy_lists:
                return httpx.Response(500)
            body = self.avy_lists[c]
        elif kind == "avy-prod":
            body = self.avy_products.get(request.url.path.rsplit("/", 1)[1], {})
        elif kind == "fires":
            body = {"features": self.fires}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.requests if k == kind)


@pytest.fixture
def up(monkeypatch):
    S.clear_cache()
    u = Upstream()
    S.set_transport(httpx.MockTransport(u))
    monkeypatch.setattr(S, "_now", lambda: u.now)
    yield u
    S.set_transport(None)
    S.clear_cache()


def cond(p: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return S.conditions(p["lat"], p["lon"], p.get("elev_m"), p.get("name", ""), **kw)


def texts(day: dict[str, Any], level: str | None = None) -> list[str]:
    return [r["text"] for r in day["reasons"] if level is None or r["level"] == level]


def src(res: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in res["sources"] if s["id"] == sid)


# --- whole days ---------------------------------------------------------------------------


def test_a_calm_day_is_go_everywhere_outside_the_us(up):
    res = cond(MONT_BLANC)
    assert [d["verdict"] for d in res["days"]] == ["GO"] * 7
    d = res["days"][1]
    assert d["read"] == {"models": True, "om-ens": True, "om-aq": True}
    assert d["numbers"]["wind_mph"] == pytest.approx(3) and d["numbers"]["models_read"] == 4
    assert d["numbers"]["ens_p"] == 0 and d["numbers"]["ens_members"] == 51
    assert d["top_reason"].startswith("no limit crossed: wind 3 mph")
    assert res["levels_hpa"] == [600, 500, 400] and res["ens_level_hpa"] == 600
    assert res["timezone"] == "Europe/Rome" and res["summit_hours_local"] == "10:00-14:00"
    assert "A screen, not a go/no-go" in res["screen_note"]
    assert len(res["hourly"]["t"]) == 7 * 24
    assert set(res["hourly"]) >= {"wind", "wind_min", "wind_max", "gust", "temp", "chill",
                                  "cloud", "precip", "freezing_ft"}


def test_a_calm_day_is_go_in_the_us_when_every_us_source_was_read(up):
    res = cond(WHITNEY)
    d = res["days"][1]
    assert d["verdict"] == "GO", d["reasons"]
    assert d["read"] == {"models": True, "om-ens": True, "om-aq": True, "nws-alerts": True,
                         "avy": True, "fires": True, "nws-fc": True}
    assert d["numbers"]["nws_day"] == "Sunny, 18F, 5 to 10 mph"
    assert res["in_us"] is True and res["aqi_scale"] == "US AQI"
    assert res["calibration"]["applied"] is True and res["levels_hpa"] == [700, 600, 500]
    assert all(s["read"] for s in res["sources"] if s["required"])


def test_a_windy_day_is_no(up):
    up.weather[S.round_point(MONT_BLANC["lat"], MONT_BLANC["lon"])] = dict(wind=45.0)
    res = cond(MONT_BLANC)
    d = res["days"][2]
    assert d["verdict"] == "NO"
    assert d["top_reason"] == "wind 45 mph (models)"
    assert ("fail", "wind 45 mph (models)") in [(r["level"], r["text"]) for r in d["reasons"]]
    assert d["numbers"]["ens_p"] == 1.0  # every member over the line
    assert any(t.startswith("strong-wind share 1.00") for t in texts(d, "fail"))


def test_a_required_source_not_read_holds_the_day_at_marg(up):
    up.fail.add("om-ens")
    res = cond(MONT_BLANC)
    assert [d["verdict"] for d in res["days"]] == ["MARG"] * 7
    d = res["days"][0]
    assert ("warn", "ECMWF ensemble not read") in [(r["level"], r["text"]) for r in d["reasons"]]
    assert d["read"]["om-ens"] is False and d["unread"] == ["ECMWF ensemble not read"]
    s = src(res, "om-ens")
    assert s["read"] is False and s["state"] == "failed" and s["error"] == "HTTP 500"


def test_us_sources_not_read_hold_the_us_day_at_marg(up):
    up.fail.update({"nws-alerts", "avy"})
    res = cond(WHITNEY)
    d0, d3 = res["days"][0], res["days"][3]
    assert d0["verdict"] == "MARG"
    assert {"NWS alerts not read", "avalanche.org not read"} <= set(texts(d0, "warn"))
    assert d3["verdict"] == "MARG" and "avalanche.org not read" not in texts(d3)  # days 0-1 only
    up2_fail_only_fires(up)


def up2_fail_only_fires(up: Upstream) -> None:
    S.clear_cache()
    up.fail = {"fires"}
    d = cond(WHITNEY)["days"][1]
    assert d["verdict"] == "GO" and ("info", "NIFC wildfires not read") in [
        (r["level"], r["text"]) for r in d["reasons"]]


def test_an_alert_and_an_avalanche_rating_are_read_for_a_us_point(up):
    up.alerts["CA"] = [STORM]
    zone = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
        [[-119.0, 36.0], [-118.0, 36.0], [-118.0, 37.0], [-119.0, 37.0], [-119.0, 36.0]]]},
        "properties": dict(ESAC_PROPS, danger_level=3, start_date="2026-11-20T06:00:00",
                           end_date="2026-11-21T00:00:00")}
    up.avy = [zone]
    up.avy_lists["ESAC"] = []
    res = cond(WHITNEY)
    d0 = res["days"][0]
    assert d0["verdict"] == "NO"
    assert "Winter Storm Warning" in texts(d0, "fail")
    assert "avalanche 3 Considerable (ESAC)" in texts(d0, "fail")
    assert d0["numbers"]["alerts"] == ["Winter Storm Warning"]
    assert src(res, "avy")["note"] == "zone ESAC Eastside Region"


def test_thunder_in_the_nws_text_and_a_near_fire_count(up):
    up.thunder = {2}
    up.fires = [{"attributes": dict(IncidentName="Lone", PercentContained=0,
                                    FireDiscoveryDateTime=int(NOW.timestamp() * 1000)),
                 "geometry": {"x": -118.25, "y": 36.6}}]
    days = cond(WHITNEY)["days"]
    assert "wildfire Lone 3 mi" in texts(days[1], "fail")
    assert "thunder in the NWS forecast" in texts(days[2], "warn")


def test_smoke_uses_the_us_aqi_in_the_us_and_the_european_aqi_elsewhere(up):
    up.weather[S.round_point(WHITNEY["lat"], WHITNEY["lon"])] = dict(us_aqi=160.0, eu_aqi=10.0)
    up.weather[S.round_point(MONT_BLANC["lat"], MONT_BLANC["lon"])] = dict(us_aqi=10.0,
                                                                           eu_aqi=70.0)
    w = cond(WHITNEY)
    m = cond(MONT_BLANC)
    assert "US AQI 160 (smoke)" in texts(w["days"][1], "fail")
    assert "European AQI 70 (smoke, dust)" in texts(m["days"][1], "warn")
    assert m["days"][1]["numbers"]["aqi_scale"] == "European AQI"
    assert "no air-quality forecast this far out" in texts(m["days"][6], "info")


# --- calibrations stay in California and Nevada ------------------------------------------


def _breezy(up: Upstream, p: dict[str, Any]) -> None:
    winds = {"ecmwf_ifs025": 17.0, "gfs_seamless": 18.0, "icon_seamless": 18.0,
             "gem_seamless": 19.0}
    up.weather[S.round_point(p["lat"], p["lon"])] = dict(wind=winds, ens_wind=18.0)


def test_calibrations_apply_inside_california(up):
    _breezy(up, WHITNEY)
    d = cond(WHITNEY)["days"][0]
    assert d["numbers"]["wind_odds"] is not None and d["numbers"]["wind_odds"] >= 0.15
    assert any(t.startswith("wind odds") for t in texts(d, "warn"))
    assert any(t.startswith("summit-wind p 0.44 (ECMWF ens, White Mountain table, a proxy)")
               for t in texts(d, "warn"))
    assert "wind odds not calibrated here: the plain median limits decide" not in texts(d)


@pytest.mark.parametrize("peak", [MONT_BLANC, ELBERT], ids=["alps", "colorado"])
def test_calibrations_are_not_applied_outside_california(up, peak):
    _breezy(up, peak)
    res = cond(peak)
    assert res["calibration"]["applied"] is False
    assert res["calibration"]["note"] == "wind odds not calibrated here"
    for d in res["days"]:
        assert d["numbers"]["wind_odds"] is None and d["numbers"]["summit_odds"] is None
        assert "wind odds not calibrated here: the plain median limits decide" in texts(d, "info")
        assert not any("wind odds" in t for t in texts(d, "warn"))
        assert not any("White Mountain" in t for t in texts(d))
    d = res["days"][0]
    assert d["verdict"] == "GO", d["reasons"]  # 18 mph median: under the plain line
    assert d["numbers"]["ens_p"] == 0.0  # raw share of members at 21.75 mph
    assert res["exposed_rule"].startswith("the summit takes at least the wind of the next level")


# --- US-only sources -----------------------------------------------------------------------


def test_us_extras_are_skipped_outside_the_us(up):
    res = cond(MONT_BLANC)
    asked = {k for k, _ in up.requests}
    assert asked.isdisjoint({"nws-point", "nws-alerts", "nws-fc", "avy", "avy-list", "fires"})
    for sid in ("nws-alerts", "nws-fc", "avy", "fires"):
        s = src(res, sid)
        assert s["state"] == "skipped" and s["required"] is False
        assert s["note"] == "outside the US: not checked"
    assert res["in_us"] is False
    assert "no avalanche forecast checked outside the US: judge the snowpack yourself" in texts(
        res["days"][0], "info")


def test_a_point_the_nws_does_not_cover_counts_as_outside_the_us(up):
    res = cond(TREMBLANT)
    assert res["in_us"] is False and res["aqi_scale"] == "European AQI"
    assert up.count("nws-point") == 1 and up.count("nws-alerts") == 0 and up.count("avy") == 0
    assert res["days"][1]["verdict"] == "GO"
    cond(TREMBLANT)
    assert up.count("nws-point") == 1  # the 404 is an answer, cached like one


def test_an_nws_lookup_failure_inside_the_us_is_not_read_not_skipped(up):
    up.fail.add("nws-point")
    res = cond(WHITNEY)
    assert res["in_us"] is None
    d = res["days"][1]
    assert d["verdict"] == "MARG"
    assert "NWS alerts not read (the NWS point lookup failed)" in texts(d, "warn")


# --- time ----------------------------------------------------------------------------------


def test_summit_hours_follow_the_points_own_time_zone(up):
    ev = cond(EVEREST)
    assert ev["days"][0]["summit_hours"]["start"] == "2026-11-20T04:15:00+00:00"
    assert ev["days"][0]["summit_hours"]["end"] == "2026-11-20T08:15:00+00:00"
    wh = cond(WHITNEY)
    assert wh["days"][0]["summit_hours"]["start"] == "2026-11-20T18:00:00+00:00"
    assert wh["days"][0]["sunrise"].startswith("06:") and wh["days"][0]["sunset"].startswith("16:")


def test_verdicts_take_today_until_its_summit_hours_pass(up):
    out = S.verdicts([WHITNEY, MONT_BLANC])
    assert out["whitney"]["when"] == "today" and out["whitney"]["day"] == "2026-11-20"
    assert out["mont-blanc"]["when"] == "tomorrow" and out["mont-blanc"]["day"] == "2026-11-21"
    assert out["mont-blanc"]["verdict"] == "GO"
    assert set(out["whitney"]) == {"verdict", "day", "when", "top_reason"}


# --- batching, caching, stale-on-error ---------------------------------------------------


def _grid(n: int, elev_m: float = 4000.0) -> list[dict[str, Any]]:
    return [dict(id=f"p{i}", lat=45.50 + 0.01 * i, lon=6.80, elev_m=elev_m) for i in range(n)]


def test_open_meteo_is_asked_for_20_points_a_request(up):
    out = S.verdicts(_grid(21))  # 20 + 1
    assert len(out) == 21 and all(v["verdict"] == "GO" for v in out.values())
    assert up.count("om-fc") == 2 and up.count("om-ens") == 2 and up.count("om-aq") == 2
    first = next(q for k, q in up.requests if k == "om-fc")
    assert len(first["latitude"].split(",")) == 20 and first["timezone"] == "auto"
    assert first["timeformat"] == "unixtime" and first["forecast_days"] == "8"
    assert first["models"] == ",".join(S.MODELS)
    assert up.count("om-meta") == 1 and up.count("om-ens-meta") == 1


def test_points_on_different_levels_go_in_separate_requests(up):
    S.verdicts(_grid(2, 4000.0) + [dict(id="hi", lat=45.9, lon=6.9, elev_m=6000.0)])
    fcs = [q["hourly"] for k, q in up.requests if k == "om-fc"]
    assert len(fcs) == 2
    assert any("wind_speed_700hPa" in h for h in fcs)
    assert any("wind_speed_400hPa" in h for h in fcs)
    assert up.count("om-ens") == 2


def test_a_second_call_inside_the_ttl_reads_the_cache(up):
    S.verdicts(_grid(3))
    n = len(up.requests)
    again = cond(dict(lat=45.50, lon=6.80, elev_m=4000.0))
    assert len(up.requests) == n  # the drawer and the verdicts share one pull
    assert src(again, "models")["state"] == "cached"


def test_forecasts_expire_after_an_hour_and_the_ensemble_after_three(up):
    S.verdicts(_grid(2))
    up.now = NOW + timedelta(minutes=61)
    S.verdicts(_grid(2))
    assert up.count("om-fc") == 2 and up.count("om-ens") == 1 and up.count("om-aq") == 1
    up.now = NOW + timedelta(hours=3, minutes=1)
    S.verdicts(_grid(2))
    assert up.count("om-fc") == 3 and up.count("om-ens") == 2 and up.count("om-aq") == 2


def test_a_new_model_run_replaces_the_forecast_before_the_ttl(up):
    S.verdicts(_grid(2))
    up.now = NOW + timedelta(minutes=16)  # the run id itself is re-read every 15 min
    up.run_fc += 6 * 3600
    S.verdicts(_grid(2))
    assert up.count("om-fc") == 2 and up.count("om-ens") == 1
    up.now = NOW + timedelta(minutes=20)
    S.verdicts(_grid(2))
    assert up.count("om-fc") == 2


def test_a_failed_fetch_serves_the_last_copy_with_its_age_then_gives_up(up):
    cond(MONT_BLANC)
    up.fail.add("om-fc")
    up.now = NOW + timedelta(hours=2)
    res = cond(MONT_BLANC)
    s = src(res, "models")
    assert s["state"] == "stale" and s["read"] is True and s["age_min"] == 120
    assert s["error"] == "HTTP 500"
    assert res["days"][0]["verdict"] in ("GO", "MARG", "NO")
    up.now = NOW + timedelta(hours=13)
    res = cond(MONT_BLANC)
    s = src(res, "models")
    assert s["state"] == "failed" and "past the 12 h limit" in s["error"]
    assert all(d["verdict"] == "?" for d in res["days"])
    assert "0 of 4 models read" in texts(res["days"][0], "warn")


def test_a_failed_source_is_not_asked_again_for_two_minutes(up):
    up.fail.add("om-fc")
    cond(MONT_BLANC)
    cond(MONT_BLANC)
    assert up.count("om-fc") == 1
    up.now = NOW + timedelta(seconds=S.RETRY_AFTER_S + 1)
    cond(MONT_BLANC)
    assert up.count("om-fc") == 2


def test_a_missing_height_comes_from_the_elevation_model(up):
    up.weather[S.round_point(WHITNEY["lat"], WHITNEY["lon"])] = dict(dem=4355.0)
    res = cond(dict(WHITNEY, elev_m=None))
    assert res["elev_m"] == 4355 and res["elev_source"].startswith("Open-Meteo elevation")
    assert up.count("om-elev") == 1
    cond(dict(WHITNEY, elev_m=None))
    assert up.count("om-elev") == 1


def test_requests_carry_a_generic_user_agent(up):
    cond(WHITNEY)
    assert set(up.headers) == {S.USER_AGENT}
    assert "@" not in S.USER_AGENT and "summit" in S.USER_AGENT


# --- routes ------------------------------------------------------------------------------


@pytest.fixture
def client(up):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_conditions_route(client):
    r = client.get("/api/summit/conditions", params=dict(
        lat=MONT_BLANC["lat"], lon=MONT_BLANC["lon"], elev_m=4808, name="Mont Blanc"))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Mont Blanc" and len(body["days"]) == 7
    assert body["days"][0]["verdict"] in ("GO", "MARG", "NO", "?")
    assert body["sources"][0]["id"] == "models" and "fetched" in body["sources"][0]
    assert len(body["hourly"]["t"]) == 7 * 24
    r = client.get("/api/summit/conditions", params=dict(lat=1, lon=2, elev_m=500, days=2))
    assert r.status_code == 200 and len(r.json()["days"]) == 2


@pytest.mark.parametrize("params", [dict(lat=91, lon=0), dict(lat=0, lon=181),
                                    dict(lat=0, lon=0, days=8), dict(lat=0, lon=0, elev_m=9500),
                                    dict(lon=0)])
def test_get_conditions_rejects_bad_input(client, params):
    assert client.get("/api/summit/conditions", params=params).status_code == 422


def test_get_conditions_answers_502_without_any_height(client, up):
    up.fail.add("om-elev")
    r = client.get("/api/summit/conditions", params=dict(lat=10, lon=10))
    assert r.status_code == 502 and "elevation" in r.json()["detail"]


def test_post_verdicts_route(client):
    pts = [MONT_BLANC, WHITNEY]
    body = [{k: p[k] for k in ("id", "lat", "lon", "elev_m")} for p in pts]
    r = client.post("/api/summit/verdicts", json=body)
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {"mont-blanc", "whitney"}
    assert out["mont-blanc"]["when"] == "tomorrow" and out["whitney"]["when"] == "today"
    r = client.post("/api/summit/verdicts", json={"points": body})
    assert r.status_code == 200 and set(r.json()) == {"mont-blanc", "whitney"}


def test_post_verdicts_caps_the_batch_at_50(client, monkeypatch):
    # the cap is the route's; scoring 50 summits is covered by the batching tests above
    monkeypatch.setattr(S, "verdicts", lambda items: {
        p["id"]: dict(verdict="GO", day="2026-11-21", when="tomorrow", top_reason="")
        for p in items})
    many = [dict(id=f"p{i}", lat=45.0 + 0.01 * i, lon=6.8, elev_m=4000) for i in range(51)]
    assert client.post("/api/summit/verdicts", json=many).status_code == 422
    assert client.post("/api/summit/verdicts", json={"points": many}).status_code == 422
    assert client.post("/api/summit/verdicts", json=[]).status_code == 422
    assert client.post("/api/summit/verdicts", json=[{"id": "x", "lat": 95, "lon": 0}]
                       ).status_code == 422
    assert client.post("/api/summit/verdicts", json=many[:50]).status_code == 200
