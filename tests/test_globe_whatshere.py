"""Tests for the "What's here?" card (static/js/whatshere.js), run under Node."""

from __future__ import annotations

import math
import re

import pytest

from tests import globe_ux_harness as h

pytestmark = pytest.mark.skipif(h.NODE is None, reason="node is not installed")

EXTRA = r"""
var METEO = 'https://api.open-meteo.com/v1/forecast';
var WX = { latitude: 39.74, longitude: -104.99, elevation: 1609.4, utc_offset_seconds: -25200,
  timezone: 'America/Denver', timezone_abbreviation: 'GMT-7',
  current: { time: '2026-11-19T11:00', temperature_2m: 8.4, apparent_temperature: 5.9,
    relative_humidity_2m: 31, precipitation: 0, weather_code: 2, cloud_cover: 40,
    wind_speed_10m: 4.2, wind_direction_10m: 315, is_day: 1 } };
var SATS = [
  { noradId: 25544, name: 'ISS (ZARYA)', lastLat: 39.7, lastLon: -105.0, lastAlt: 420 },
  { noradId: 20580, name: 'HST', lastLat: 41.5, lastLon: -102.0, lastAlt: 530 },
  { noradId: 48274, name: 'CSS (TIANHE)', lastLat: -30, lastLon: 120, lastAlt: 390 },
  { noradId: 1, name: 'NO POSITION' }
];
function serveAll() {
  route(METEO, WX);
  route('/api/geocode/reverse', { display_name: 'Denver, Colorado, United States' });
  route('/api/cameras/nearest', { count: 2, cameras: [
    { rid: 7, name: 'Denver Union Station', city: 'Denver', lat: 39.753, lon: -105.0,
      distance_km: 1.62 },
    { rid: 9, name: 'I-25 at Colfax', city: '', lat: 39.74, lon: -105.01, distance_km: 12.4 }] });
  route('/api/peaks/nearest', { count: 1, peaks: [
    { id: 311, name: 'Mount Evans', lat: 39.588, lon: -105.643, elev_m: 4348, prom_m: 2771,
      distance_km: 47.9 }] });
}
function card() { return byClass(document.body, 'wh-card')[0] || null; }
function rowText(label) {
  var rows = byClass(card(), 'wh-row').filter(function (r) {
    return r.children[0].textContent === label; });
  return rows.length ? rows[0].children[1].textContent : null;
}
function sec(title) {
  var s = byClass(card(), 'wh-sec').filter(function (x) {
    return x.children[0].textContent === title; })[0];
  return s.children[1];
}
function links(node) { return byClass(node, 'wh-link').map(function (b) {
  return b.textContent; }); }
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> h.Ran:
    return h.run(tmp_path_factory.mktemp("whatshere"), ["whatshere.js"], SCENARIOS,
                 extra_prelude=EXTRA)


scenario("helpers", r"""
  var W = window.SDAWhatsHere;
  var R = W.EARTH_RADIUS_KM;
  var horizonDeg = Math.acos(R / (R + 420)) * 180 / Math.PI;
  var o = W.overhead(SATS, 39.74, -104.99, 10, 3);
  return {
    wmo: [0, 2, 45, 63, 95, 5].map(W.wmoText),
    compass: [0, 350, 22.5, 225, -90, 359.9, null].map(W.compass),
    offsets: [-25200, 19800, 0, 20700, null].map(W.formatOffset),
    times: [W.localTime(Date.UTC(2026, 10, 19, 18, 0), -25200),
            W.localTime(Date.UTC(2026, 10, 19, 20, 15), 19800)],
    zenith: W.elevationDeg(10, 20, 10, 20, 420), horizon: W.elevationDeg(0, 0, 0, horizonDeg, 420),
    antipode: W.elevationDeg(0, 0, 0, 180, 420),
    overhead: { count: o.count, top: o.top.map(function (t) { return [t.noradId,
      Math.round(t.el)]; }) },
    key: W.weatherKey(39.74049, -104.99), url: W.weatherUrl(39.74, -104.99),
    wx: W.describeWeather(WX),
    calm: W.describeWeather({ current: { weather_code: 0, temperature_2m: 20,
      apparent_temperature: 20.4,
      wind_speed_10m: 0, wind_direction_10m: 90, is_day: 0 } }),
    empty: [W.describeWeather({}), W.describeWeather({ current: {} })],
    constants: [W.LONG_PRESS_MS, W.LONG_PRESS_SLOP_PX, W.MIN_ELEVATION_DEG]
  };
""")


def test_helpers(ran):
    r = ran["helpers"]
    assert r["wmo"] == ["Clear sky", "Partly cloudy", "Fog", "Rain", "Thunderstorm",
      "Weather code 5"]
    assert r["compass"] == ["N", "N", "NNE", "SW", "W", "N", ""]
    assert r["offsets"] == ["UTC-07:00", "UTC+05:30", "UTC+00:00", "UTC+05:45", ""]
    assert r["times"] == ["11:00 Thu", "01:45 Fri"]
    assert r["zenith"] == pytest.approx(90.0)
    assert abs(r["horizon"]) < 1e-9
    assert r["antipode"] < -80
    # ISS nearly straight up; HST about 340 km away at 530 km is still above 10 deg;
    # the Chinese station is on the far side; the record with no position is skipped.
    assert r["overhead"]["count"] == 2
    assert [t[0] for t in r["overhead"]["top"]] == [25544, 20580]
    assert r["overhead"]["top"][0][1] >= 89
    assert r["key"] == "39.740,-104.990"
    assert r["url"] == (
        "https://api.open-meteo.com/v1/forecast?latitude=39.7400&longitude=-104.9900"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
        "weather_code,cloud_cover,wind_speed_10m,wind_direction_10m,is_day"
        "&wind_speed_unit=ms&timezone=auto")
    assert r["wx"] == {"summary": "Partly cloudy, 8.4 C (feels 5.9 C)",
                       "detail": "wind 4.2 m/s from NW, humidity 31%, cloud 40%", "isDay": True}
    assert r["calm"] == {"summary": "Clear sky, 20.0 C", "detail": "wind 0.0 m/s", "isDay": False}
    assert r["empty"] == [None, None]
    assert r["constants"] == [600, 10, 10]


def test_overhead_matches_an_independent_calculation():
    """The spherical elevation formula against a vector computation in Python."""
    radius = 6371.0088

    def unit(lat, lon):
        la, lo = math.radians(lat), math.radians(lon)
        return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))

    obs = [radius * c for c in unit(39.74, -104.99)]
    sat = [(radius + 530) * c for c in unit(41.5, -102.0)]
    rel = [s - o for s, o in zip(sat, obs, strict=True)]
    up = unit(39.74, -104.99)
    dot = sum(a * b for a, b in zip(rel, up, strict=True))
    expected = math.degrees(math.asin(dot / math.sqrt(sum(a * a for a in rel))))
    out = h.run_inline(["whatshere.js"],
                       "return window.SDAWhatsHere.elevationDeg("
                       "39.74, -104.99, 41.5, -102.0, 530);")
    assert out == pytest.approx(expected, abs=1e-9)


scenario("long_press", r"""
  var W = window.SDAWhatsHere;
  var t = new El('div');
  var presses = [];
  var detach = W.attachLongPress(t, function (p) { presses.push(p); }, { holdMs: 30 });
  function ev(type, id, x, y, kind) { t.fire(type, { pointerType: kind || 'touch', pointerId: id,
    clientX: x, clientY: y }); }
  ev('pointerdown', 1, 100, 100); ev('pointermove', 1, 106, 105); await wait(50);
  ev('pointerup', 1, 106, 105);
  var held = presses.length;
  ev('pointerdown', 2, 100, 100, 'mouse'); await wait(50); ev('pointerup', 2, 100, 100, 'mouse');
  var mouse = presses.length;
  ev('pointerdown', 3, 100, 100); ev('pointermove', 3, 112, 100); await wait(50);
    ev('pointerup', 3);
  var drift = presses.length;
  ev('pointerdown', 4, 100, 100); ev('pointerdown', 5, 200, 200); await wait(50);
  ev('pointerup', 4); ev('pointerup', 5);
  var pinch = presses.length;
  ev('pointerdown', 6, 100, 100); await wait(5); ev('pointerup', 6); await wait(50);
  var tap = presses.length;
  detach();
  ev('pointerdown', 7, 100, 100); await wait(50);
  return { first: presses[0], held: held, mouse: mouse, drift: drift, pinch: pinch, tap: tap,
    after: presses.length, listeners: Object.keys(t.listeners).map(function (k) {
      return t.listeners[k].length; }).reduce(function (a, b) { return a + b; }, 0) };
""")


def test_long_press_is_touch_only_still_and_single_finger(ran):
    r = ran["long_press"]
    assert r["first"] == {"x": 100, "y": 100}
    assert r["held"] == 1  # 7.8 px of drift is inside the 10 px allowance
    assert r["mouse"] == 1 and r["drift"] == 1 and r["pinch"] == 1 and r["tap"] == 1
    assert r["after"] == 1 and r["listeners"] == 0


scenario("right_click_card", r"""
  serveAll();
  var opened = []; var summit = [];
  window.SDACameras = { open: function (rid) { opened.push(rid); } };
  window.SDASummit = { open: function (p) { summit.push(p); } };
  var k = makeCtx({ satEngine: { sats: SATS, byNoradId: {} },
    pick: function (pos) { return { lon: -104.99, lat: 39.74, h: 0, at: [pos.x, pos.y] }; } });
  var ok = window.SDAWhatsHere.init(k.ctx);
  fireInput(5, { position: new Cesium.Cartesian2(10, 20) });
  await settle();
  var out = {
    ok: ok, open: window.SDAWhatsHere.isOpen(), place: byClass(card(), 'wh-place')[0].textContent,
    coords: rowText('Coords'), elevation: rowText('Elevation'), time: rowText('Local time'),
    weather: rowText('Weather'), sats: sec('SATELLITES OVERHEAD').children[0].textContent,
    satLinks: links(sec('SATELLITES OVERHEAD')), cams: links(sec('NEAREST CAMERAS')),
    peaks: links(sec('NEAREST PEAKS')),
    requests: REQUESTS.slice().sort(), feed: k.feed.slice(), foot: byClass(card(),
      'wh-foot')[0].textContent
  };
  byClass(sec('NEAREST CAMERAS'), 'wh-link')[0].fire('click');
  var camFly = k.ctx.viewer.camera.flown.destination;
  byClass(sec('NEAREST PEAKS'), 'wh-link')[0].fire('click');
  byClass(sec('SATELLITES OVERHEAD'), 'wh-link')[1].fire('click');
  out.opened = opened; out.summit = summit; out.selected = k.ctx.selected;
  out.camFly = [camFly.lon, camFly.lat, camFly.h];
  byText(card(), 'X')[0].fire('click');
  out.closed = card() === null;
  return out;
""")


def test_right_click_fills_the_card(ran):
    r = ran["right_click_card"]
    assert r["ok"] is True and r["open"] is True
    assert r["place"] == "Denver, Colorado, United States"
    assert r["coords"] == "39.74000, -104.99000"
    assert r["elevation"] == "1609 m (90 m elevation model)"
    assert re.fullmatch(r"\d\d:\d\d (Sun|Mon|Tue|Wed|Thu|Fri|Sat)  \(America/Denver, UTC-07:00\)",
                        r["time"])
    assert r["weather"] == ("Partly cloudy, 8.4 C (feels 5.9 C); wind 4.2 m/s from NW, "
                            "humidity 31%, cloud 40%")
    assert r["sats"] == "2 of 4 satellites are 10 deg or more above the horizon (globe clock)."
    assert r["satLinks"][0].startswith("ISS (ZARYA)  ") and r["satLinks"][1].startswith("HST  ")
    assert r["cams"] == ["Denver Union Station, Denver  1.6 km", "I-25 at Colfax  12 km"]
    assert r["peaks"] == ["Mount Evans 4348 m  48 km"]
    assert r["requests"] == sorted([
        "/api/cameras/nearest?lat=39.74&lon=-104.99&n=5&radius_km=50",
        "/api/geocode/reverse?lat=39.740000&lon=-104.990000",
        "/api/peaks/nearest?lat=39.74&lon=-104.99&n=5&radius_km=50",
        "https://api.open-meteo.com/v1/forecast?latitude=39.7400&longitude=-104.9900"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
        "weather_code,cloud_cover,wind_speed_10m,wind_direction_10m,is_day"
        "&wind_speed_unit=ms&timezone=auto",
    ])
    assert r["feed"] == ["What's here: 39.740, -104.990"]
    assert "Open-Meteo (CC BY 4.0)" in r["foot"] and "Nominatim (ODbL)" in r["foot"]
    assert r["opened"] == [7] and r["camFly"] == [-105.0, 39.753, 3000]
    assert r["summit"] == [{"id": 311, "name": "Mount Evans", "lat": 39.588, "lon": -105.643,
                            "elev_m": 4348, "prom_m": 2771}]
    assert r["selected"] == [20580]
    assert r["closed"] is True


scenario("weather_cache_and_failure", r"""
  serveAll();
  var k = makeCtx({});
  window.SDAWhatsHere.init(k.ctx);
  window.SDAWhatsHere.open({ lat: 39.74, lon: -104.99 }); await settle();
  window.SDAWhatsHere.open({ lat: 39.7401, lon: -104.9902 }); await settle();
  var sameCell = requested(METEO).length;
  window.SDAWhatsHere.open({ lat: 39.76, lon: -104.99 }); await settle();
  var otherCell = requested(METEO).length;
  route(METEO, {}, 500);
  window.SDAWhatsHere.open({ lat: 10, lon: 77.0 }); await settle();
  var failed = { elevation: rowText('Elevation'), time: rowText('Local time'),
    weather: rowText('Weather'),
    sats: sec('SATELLITES OVERHEAD').textContent };
  window.SDAWhatsHere.open({ lat: 10, lon: 77.0 }); await settle();
  return { sameCell: sameCell, otherCell: otherCell, failed: failed,
    retried: requested(METEO).length, cards: byClass(document.body, 'wh-card').length };
""")


def test_weather_is_cached_per_point_and_a_failure_is_not(ran):
    r = ran["weather_cache_and_failure"]
    assert r["sameCell"] == 1 and r["otherCell"] == 2
    f = r["failed"]
    assert f["elevation"] == "not available"
    assert re.fullmatch(r"about \d\d:\d\d \w{3} \(UTC\+05:00 by longitude\)", f["time"])
    assert f["weather"] == "Open-Meteo did not answer"
    assert f["sats"] == "No satellite catalog loaded yet."
    assert r["retried"] == 4  # the failed answer was dropped, so the same point asks again
    assert r["cards"] == 1


scenario("map_tools_place_and_static", r"""
  route(METEO, WX);
  window.SDAMapTools = {
    instance: function () { return { reverseGeocode: function () { return Promise.resolve('');
      } }; },
    toMGRS: function () { return '13S EE 01298 00500'; }
  };
  var k = makeCtx({ isStatic: true });
  window.SDAWhatsHere.init(k.ctx);
  window.SDAWhatsHere.open({ lat: 39.74, lon: -104.99 });
  await settle();
  var withMt = { place: byClass(card(), 'wh-place')[0].textContent, coords: rowText('Coords'),
    cams: sec('NEAREST CAMERAS').textContent, peaks: sec('NEAREST PEAKS').textContent };
  delete window.SDAMapTools;
  window.SDAWhatsHere.open({ lat: 39.74, lon: -104.99 });
  await settle();
  return { withMt: withMt, bare: byClass(card(), 'wh-place')[0].textContent,
    apiRequests: requested('/api/').length, meteo: requested(METEO).length };
""")


def test_map_tools_and_the_static_demo(ran):
    r = ran["map_tools_place_and_static"]
    assert r["withMt"]["place"] == "No named place here (open water or remote land)."
    assert r["withMt"]["coords"] == "39.74000, -104.99000  |  MGRS 13S EE 01298 00500"
    assert r["withMt"]["cams"] == "Needs the live server (camera catalog)."
    assert r["withMt"]["peaks"] == "Needs the live server (peak catalog)."
    assert r["bare"] == "Place lookup needs the live server or map tools."
    assert r["apiRequests"] == 0
    assert r["meteo"] == 1  # Open-Meteo is keyless and cached, so the static demo has weather


scenario("catalog_states", r"""
  route(METEO, WX);
  route('/api/geocode/reverse', {}, 502);
  route('/api/cameras/nearest', { count: 0, cameras: [] });
  route('/api/peaks/nearest', { detail: 'not imported' }, 503);
  var k = makeCtx({});
  window.SDAWhatsHere.init(k.ctx);
  window.SDAWhatsHere.open({ lat: 0, lon: -30 });
  await settle();
  return { place: byClass(card(), 'wh-place')[0].textContent,
    cams: sec('NEAREST CAMERAS').textContent,
    peaks: sec('NEAREST PEAKS').textContent };
""")


def test_empty_and_missing_catalogs_say_so(ran):
    assert ran["catalog_states"] == {
        "place": "The place lookup did not answer.",
        "cams": "No live camera within 50 km.",
        "peaks": "The peak catalog is not imported.",
    }


scenario("toggle_and_long_press", r"""
  serveAll();
  var store = new FakeStorage();
  var k = makeCtx({ storage: store });
  window.SDAWhatsHere.init(k.ctx);
  var canvas = k.ctx.viewer.scene.canvas;
  var box = byTag(k.ctx.mount, 'INPUT')[0];
  var startChecked = box.checked;
  box.checked = false; box.fire('change');
  fireInput(5, { position: new Cesium.Cartesian2(10, 20) });
  canvas.fire('pointerdown', { pointerType: 'touch', pointerId: 1, clientX: 50, clientY: 60 });
  await wait(650);
  canvas.fire('pointerup', { pointerType: 'touch', pointerId: 1, clientX: 50, clientY: 60 });
  var offOpen = window.SDAWhatsHere.isOpen();
  var saved = store.getItem('sda.whatshere.v1.rightclick');
  window.SDAWhatsHere.destroy();
  var k2 = makeCtx({ storage: store });
  window.SDAWhatsHere.init(k2.ctx);
  var reloaded = [byTag(k2.ctx.mount, 'INPUT')[0].checked, window.SDAWhatsHere.enabled()];
  window.SDAWhatsHere.setEnabled(true);
  var canvas2 = k2.ctx.viewer.scene.canvas;
  canvas2.fire('pointerdown', { pointerType: 'touch', pointerId: 2, clientX: 50, clientY: 60 });
  await wait(650);
  canvas2.fire('pointerup', { pointerType: 'touch', pointerId: 2, clientX: 50, clientY: 60 });
  var pressOpen = window.SDAWhatsHere.isOpen();
  var esc = key('Escape');
  var escInput = (function () { window.SDAWhatsHere.open({ lat: 1, lon: 2 });
    var e = key('Escape', new El('input')); return [window.SDAWhatsHere.isOpen(),
      !!e.defaultPrevented]; })();
  window.SDAWhatsHere.close();
  var centre = window.SDAWhatsHere.openAtCentre();
  return { startChecked: startChecked, offOpen: offOpen, saved: saved, reloaded: reloaded,
    pressOpen: pressOpen, escPrevented: !!esc.defaultPrevented, escInput: escInput,
    centre: centre, point: window.SDAWhatsHere.point(),
      savedOn: store.getItem('sda.whatshere.v1.rightclick'),
    bad: [window.SDAWhatsHere.open({ lat: 95, lon: 0 }), window.SDAWhatsHere.init({
      mount: new El('div') })] };
""")


def test_right_click_switch_long_press_escape_and_centre(ran):
    r = ran["toggle_and_long_press"]
    assert r["startChecked"] is True
    assert r["offOpen"] is False and r["saved"] == "0"
    assert r["reloaded"] == [False, False]  # remembered across a reload
    assert r["pressOpen"] is True
    assert r["escPrevented"] is True
    assert r["escInput"] == [True, False]  # Escape typed in a text box is not ours
    assert r["centre"] is True
    assert r["point"]["lat"] == pytest.approx(39.74)
    assert r["point"]["lon"] == pytest.approx(-104.99)
    assert r["savedOn"] == "1"
    assert r["bad"] == [False, False]


scenario("panel_collapse", r"""
  var k = makeCtx({});
  window.SDAWhatsHere.init(k.ctx);
  var panel = byClass(k.ctx.mount, 'panel')[0];
  var head = byTag(panel, 'H3')[0];
  // The page binds its own collapse toggle after init; the panel must still toggle once.
  head.addEventListener('click', function () { panel.classList.toggle('collapsed'); });
  head.fire('click');
  return panel.has('collapsed');
""")


def test_panel_head_toggles_once_alongside_the_page_binding(ran):
    assert ran["panel_collapse"] is True
