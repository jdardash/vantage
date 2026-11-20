"""Tests for the live hotspot tour (static/js/tour.js).

Runs under Node's `vm` with the DOM stub and fake Cesium from tests/globe_vm.py. The fake
camera records flights and lookAt calls; flights finish only when a scenario calls
complete() or cancel(), and the dwell timers only when it calls SDATour._fire(), so each
step of the tour is driven by hand (wall-clock waits raced on this shared machine). The page
globals the module reads (camState, setCameraMode) live in the prelude, not in a scenario
body. The feeds are the shapes the page's own endpoints return (routes/worldview.py,
routes/weather.py, routes/maritime.py), served by the fake fetch.
"""

from __future__ import annotations

import math

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

MODULE = JS_DIR / "tour.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

EXTRA = r"""
Cesium.BoundingSphere = function (c, r) { this.center = c; this.radius = r; };
Cesium.HeadingPitchRange = function (h, p, r) {
  this.heading = h; this.pitch = p; this.range = r;
};
Cesium.Matrix4.IDENTITY = { identity: true };
var NOW = Date.UTC(2026, 10, 19, 12, 0, 0);
var camState = { mode: 'free', targetId: null };
var MODES = [];
var SELECTED = [];
function setCameraMode(m) { MODES.push(m); camState.mode = m; }
function iso(off) { return new Date(NOW + off).toISOString(); }
function utcText(off) {
  var s = new Date(NOW + off).toISOString();
  return s.slice(0, 10) + ' ' + s.slice(11, 16) + ' UTC';
}
var FEEDS = {
  storms: { storms: [
    { name: 'Weak', category: 'Tropical storm', wind_kt: 45, lon: -60, lat: 15 },
    { name: 'Melissa', category: 'Hurricane (category 4)', wind_kt: 125, pressure_hpa: 940,
      movement_dir_deg: 300, movement_speed_kt: 9, lon: -75.5, lat: 17.2, agency: 'NHC',
      wind_basis: 'current advisory' },
    { name: 'NoWind', wind_kt: null, lon: 120, lat: 15 }] },
  quakes: { earthquakes: [
    { magnitude: 7.9, place: 'Old', time: utcText(-30 * 3600000), lon: 140, lat: 35,
      depth_km: 10 },
    { magnitude: 6.1, place: '45 km SW of Somewhere', time: utcText(-3 * 3600000),
      lon: 142.3, lat: 38.1, depth_km: 33 },
    { magnitude: 4.0, place: 'Small', time: utcText(-3600000), lon: 0, lat: 0, depth_km: 5 }] },
  launches: { launches: [
    { name: 'Falcon 9 | Starlink', net: iso(5 * 3600000), provider: 'SpaceX', pad: 'SLC-40',
      lon: -80.6, lat: 28.5, status: 'Go' },
    { name: 'Old', net: iso(-7 * 3600000), lon: 0, lat: 0 },
    { name: 'Electron | Mission', net: iso(40 * 60000), provider: 'Rocket Lab', pad: 'LC-1B',
      lon: 177.9, lat: -39.3, status: 'Go' }] },
  chokepoints: { chokepoints: [
    { id: 'hormuz', name: 'Strait of Hormuz', center: [56.4, 26.5], inside_now: 41,
      transits_today: 12 },
    { id: 'malacca', name: 'Strait of Malacca', center: [100.3, 3.0], inside_now: 87,
      transits_today: 0 },
    { id: 'bab', name: 'Bab el-Mandeb', center: [43.4, 12.6], inside_now: null }] }
};
function routeAll() {
  route('/api/weather/cyclones', FEEDS.storms);
  route('/api/earthquakes', FEEDS.quakes);
  route('/api/launches', FEEDS.launches);
  route('/api/maritime/chokepoints', FEEDS.chokepoints);
}
function tourSetup(opts) {
  opts = opts || {};
  var viewer = makeViewer();
  var cam = viewer.camera;
  cam.flights = []; cam.looks = []; cam.released = 0;
  cam.flyToBoundingSphere = function (bs, o) {
    this.flights.push({ range: o.offset.range, pitch: o.offset.pitch,
      heading: o.offset.heading, duration: o.duration });
    this.pending = o;
  };
  cam.lookAt = function (c, hpr) { this.looks.push({ heading: hpr.heading, range: hpr.range }); };
  cam.lookAtTransform = function (m) { if (m && m.identity) this.released += 1; };
  var eng = { byNoradId: {} };
  if (opts.iss !== false) {
    eng.byNoradId[25544] = { point: { show: true,
      position: Cesium.Cartesian3.fromDegrees(10, 20, 421000) }, lastAlt: 421.4, lastVel: 7.66 };
  }
  var mount = document.createElement('div');
  document.body.appendChild(mount);
  SDATour._setClock(function () { return NOW; });
  SDATour._setTiming(Object.assign({ dwellMs: 1e7, flyS: 0, graceMs: 0, emptyMs: 1e7 },
    opts.timing || {}));
  var ok = SDATour.init({ viewer: viewer, satEngine: eng, api: '', isStatic: !!opts.isStatic,
    mount: mount,
    selectSatellite: opts.noSelect ? undefined : function (id) { SELECTED.push(id); } });
  return { ok: ok, viewer: viewer, cam: cam, mount: mount };
}
function caption() {
  var c = document.getElementById('sda-tour-caption');
  return c ? { on: c.has('on'), text: c.children.map(function (n) { return n.textContent; }) }
    : null;
}
"""

SCENARIOS = {
    "picks": r"""
var s = SDATour.pickStorm(FEEDS.storms.storms);
var gd = SDATour.pickStorm([{ name: 'Fung-wong', wind_kt: 150, wind_basis: 'event peak',
  lon: 125, lat: 14, source: 'GDACS' }]);
var q = SDATour.pickQuake(FEEDS.quakes.earthquakes, NOW);
var l = SDATour.pickLaunch(FEEDS.launches.launches, NOW);
var flown = SDATour.pickLaunch([{ name: 'Just flew', net: iso(-20 * 60000), lon: 1, lat: 1 },
  { name: 'Later', net: iso(3 * 3600000), lon: 2, lat: 2 }], NOW);
var c = SDATour.pickChokepoint(FEEDS.chokepoints.chokepoints);
return {
  storm: [s.title, s.line, s.source], gdacs: [gd.line, gd.source],
  quake: [q.title, q.line, q.lon],
  launch: [l.title, l.kicker, l.line], flown: [flown.title, flown.kicker, flown.line],
  choke: [c.title, c.line, c.lon],
  nokey: SDATour.pickChokepoint([{ id: 'a', center: [1, 1], inside_now: null },
    { id: 'b', center: [1, 1], inside_now: 0 }]),
  empty: [SDATour.pickStorm(null), SDATour.pickQuake([], NOW), SDATour.pickLaunch(undefined, NOW),
    SDATour.pickStorm([{ name: 'Bad', wind_kt: 90, lon: 500, lat: 0 }])],
  parse: [SDATour.parseTime('2026-11-19 10:30 UTC') === Date.UTC(2026, 10, 19, 10, 30),
    SDATour.parseTime('2026-11-19T10:30:00Z') === Date.UTC(2026, 10, 19, 10, 30),
    isNaN(SDATour.parseTime(null))],
  spans: [SDATour.formatSpan(59 * 60000), SDATour.formatSpan(125 * 60000),
    SDATour.formatSpan(-3 * 86400000)],
  usgs: SDATour.usgsRows({ features: [
    { properties: { mag: 5.2, place: 'X', time: NOW - 60000 },
      geometry: { coordinates: [10, 20, 7] } },
    { properties: { mag: null } }] }),
  iss: SDATour.issStop({ lastVel: 7.66 }, 420.6).line,
  order: SDATour.STOPS
};
""",
    "full_tour": r"""
routeAll();
var t = tourSetup();
var started = SDATour.start();
var gathering = caption().text[1];
await settle();
var st1 = SDATour.state();
var first = { cap: caption(), modes: MODES.slice(), selected: SELECTED.slice(),
  touring: document.body.has('sda-touring'), stopBtn: !!buttonNamed(t.mount, 'STOP TOUR') };
SDATour._fire(); await settle();
var fl = t.cam.flights[0];
var storm = { flights: t.cam.flights.length, range: fl.range, pitch: fl.pitch,
  duration: fl.duration, modes: MODES.slice(), released: t.cam.released,
  kicker: caption().text[0], title: caption().text[1] };
t.cam.pending.complete();
SDATour._setClock(function () { return NOW + 100; });
SDATour._orbitTick();
var look = t.cam.looks[0];
var orbit = { looks: t.cam.looks.length, dh: look.heading - fl.heading, range: look.range,
  orbiting: SDATour.state().orbiting };
SDATour._fire(); await settle();
var third = { flights: t.cam.flights.length, title: caption().text[1],
  released: t.cam.released, orbiting: SDATour.state().orbiting };
return { started: started, again: SDATour.start(), gathering: gathering, st1: st1,
  first: first, storm: storm, orbit: orbit, third: third, requests: REQUESTS.slice().sort() };
""",
    "input_stops": r"""
routeAll();
var t = tourSetup({ timing: { graceMs: 400 } });
SDATour.start();
await settle();
fireDoc('keydown', { key: 'a' });
var early = SDATour.isRunning();
SDATour._setClock(function () { return NOW + 500; });
var btn = buttonNamed(t.mount, 'STOP TOUR');
fireDoc('pointerdown', { target: btn });
var onButton = SDATour.isRunning();
fireDoc('pointerdown', { target: document.body });
var r = { early: early, onButton: onButton, running: SDATour.isRunning(),
  last: SDATour.state().lastStopped, cap: caption().on,
  touring: document.body.has('sda-touring'), cancelled: t.cam.cancelled || 0,
  lastMode: MODES[MODES.length - 1], startBtn: !!buttonNamed(t.mount, 'START TOUR'),
  listeners: ['keydown', 'pointerdown', 'mousedown', 'wheel', 'touchstart'].map(function (k) {
    return (docListeners[k] || []).length; }) };
SDATour._fire(); await settle();
r.after = { flights: t.cam.flights.length, running: SDATour.isRunning() };
return r;
""",
    "key_swallowed": r"""
routeAll();
tourSetup();
SDATour.start();
await settle();
var ev = fireDoc('keydown', { key: 's' });
return { prevented: !!ev.prevented, running: SDATour.isRunning() };
""",
    "button_toggles": r"""
routeAll();
var t = tourSetup();
var b = buttonNamed(t.mount, 'START TOUR');
b.fire('click');
var on = SDATour.isRunning();
await settle();
b.fire('click');
return { on: on, text: b.textContent, running: SDATour.isRunning(),
  last: SDATour.state().lastStopped,
  status: byClass(t.mount, 'sdatour-status')[0].textContent };
""",
    "static_demo": r"""
route('https://earthquake.usgs.gov/', { features: [
  { properties: { mag: 5.8, place: 'Off the coast', time: NOW - 2 * 3600000 },
    geometry: { coordinates: [150, -5, 20] } }] });
routeAll();
var t = tourSetup({ isStatic: true });
var intro = byClass(t.mount, 'sdatour-status')[0].textContent;
SDATour.start();
await settle();
return { stops: SDATour.state().stops, requests: REQUESTS.slice(), intro: intro };
""",
    "nothing_live": r"""
route('/api/', {}, 500);
var t = tourSetup({ iss: false });
SDATour.start();
await settle();
var cap = caption().text.slice(0, 3);
SDATour._fire(); await settle();
return { cap: cap, running: SDATour.isRunning(), last: SDATour.state().lastStopped,
  status: byClass(t.mount, 'sdatour-status')[0].textContent, flights: t.cam.flights.length,
  touring: document.body.has('sda-touring') };
""",
    "skips_missing": r"""
route('/api/weather/cyclones', null, 0);
route('/api/earthquakes', {}, 500);
route('/api/launches', FEEDS.launches);
route('/api/maritime/chokepoints', { chokepoints: [{ id: 'x', center: [1, 1],
  inside_now: null }] });
var t = tourSetup({ noSelect: true });
SDATour.start();
await settle();
return { stops: SDATour.state().stops, flights: t.cam.flights.length,
  kicker: caption().text[0], title: caption().text[1], line: caption().text[2],
  range: t.cam.flights[0].range, modes: MODES.slice() };
""",
    "loops": r"""
route('/api/earthquakes', FEEDS.quakes);
var t = tourSetup({ iss: false });
SDATour.start();
await settle();
t.cam.pending.complete();
SDATour._fire(); await settle();
var s = SDATour.state();
return { cycle: s.cycle, index: s.index, stops: s.stops, flights: t.cam.flights.length,
  quakeRequests: REQUESTS.filter(function (u) {
    return u.indexOf('/api/earthquakes') === 0; }).length,
  headings: t.cam.flights.map(function (f) { return Math.round(f.heading * 180 / Math.PI); }) };
""",
    "stop_before_gather": r"""
routeAll();
var t = tourSetup();
SDATour.start();
SDATour.stop();
await settle(); SDATour._fire(); await settle();
return { running: SDATour.isRunning(), flights: t.cam.flights.length,
  selected: SELECTED.length, modes: MODES.slice(), cap: caption().on,
  last: SDATour.state().lastStopped };
""",
    "cancel_moves_on": r"""
routeAll();
var t = tourSetup({ iss: false });
SDATour.start();
await settle();
t.cam.pending.cancel();
SDATour._fire(); await settle();
return { flights: t.cam.flights.length, title: caption().text[1],
  orbiting: SDATour.state().orbiting };
""",
    "init_and_disable": r"""
var bad = SDATour.init({ viewer: makeViewer() });
var noViewer = SDATour.init({ mount: document.createElement('div') });
routeAll();
var t = tourSetup();
SDATour.start();
await settle();
SDATour.setEnabled(false);
return { bad: bad, noViewer: noViewer, ok: t.ok, running: SDATour.isRunning(),
  panel: byTag(t.mount, 'H3')[0].textContent,
  style: !!document.getElementById('sda-tour-style'),
  hint: caption().text[4] };
""",
}


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_scenarios(tmp_path_factory.mktemp("tour"), ["tour.js"], SCENARIOS,
                         extra_prelude=EXTRA)


@needs_node
def test_picks(ran):
    r = value(ran, "picks")
    assert r["order"] == ["iss", "storm", "quake", "launch", "chokepoint"]
    assert r["storm"] == ["Melissa",
                          "Hurricane (category 4), 125 kt winds, 940 hPa, moving 300 deg at 9 kt",
                          "Source: NHC"]
    assert r["gdacs"] == ["150 kt peak winds", "Source: GDACS"]
    # The M7.9 is 30 h old, so the largest of the last day is the M6.1.
    assert r["quake"] == ["M6.1 earthquake", "45 km SW of Somewhere, 3 h 0 min ago, 33 km deep",
                          142.3]
    # Nearest to now wins: 40 min ahead beats 5 h ahead; 7 h ago is out of range.
    assert r["launch"] == ["Electron | Mission", "NEXT ORBITAL LAUNCH",
                           "Rocket Lab, LC-1B, T-40 min (Go)"]
    assert r["flown"] == ["Just flew", "LATEST ORBITAL LAUNCH", "launched 20 min ago"]
    assert r["choke"] == ["Strait of Malacca", "87 vessels inside now", 100.3]
    assert r["nokey"] is None
    assert r["empty"] == [None, None, None, None]
    assert r["parse"] == [True, True, True]
    assert r["spans"] == ["59 min", "2 h 5 min", "3 days"]
    assert r["usgs"] == [{"magnitude": 5.2, "place": "X", "time": "2026-11-19T11:59:00.000Z",
                          "lon": 10, "lat": 20, "depth_km": 7}]
    assert r["iss"] == "421 km up, 7.66 km/s"


@needs_node
def test_full_tour(ran):
    r = value(ran, "full_tour")
    assert r["started"] is True and r["again"] is False
    assert r["gathering"] == "Gathering live hotspots"
    assert r["st1"]["stops"] == ["iss", "storm", "quake", "launch", "chokepoint"]
    assert r["st1"]["running"] is True
    first = r["first"]
    assert first["cap"]["on"] is True
    assert first["cap"]["text"][:3] == ["CREWED SPACECRAFT  |  1 OF 5",
                                        "International Space Station", "421 km up, 7.66 km/s"]
    assert first["modes"] == ["chase"] and first["selected"] == [25544]
    assert first["touring"] is True and first["stopBtn"] is True
    storm = r["storm"]
    # Leaving the ISS hands the camera back to free before the flight.
    assert storm["modes"] == ["chase", "free"] and storm["released"] == 2
    assert storm["flights"] == 1 and storm["range"] == 1800000 and storm["duration"] == 0
    assert math.isclose(storm["pitch"], math.radians(-55))
    assert storm["kicker"] == "STRONGEST TROPICAL CYCLONE  |  2 OF 5"
    assert storm["title"] == "Melissa"
    orbit = r["orbit"]
    assert orbit["looks"] == 1 and orbit["orbiting"] is True and orbit["range"] == 1800000
    assert math.isclose(orbit["dh"], math.radians(2.5) * 0.1, rel_tol=1e-9)
    third = r["third"]
    assert third["flights"] == 2 and third["title"] == "M6.1 earthquake"
    assert third["released"] == 3 and third["orbiting"] is False
    assert r["requests"] == ["/api/earthquakes", "/api/launches", "/api/maritime/chokepoints",
                             "/api/weather/cyclones"]


@needs_node
def test_any_input_stops(ran):
    r = value(ran, "input_stops")
    assert r["early"] is True             # inside the grace period
    assert r["onButton"] is True          # the panel button handles its own click
    assert r["running"] is False and r["last"] == "input"
    assert r["cap"] is False and r["touring"] is False
    assert r["cancelled"] == 1 and r["lastMode"] == "free" and r["startBtn"] is True
    assert r["listeners"] == [0, 0, 0, 0, 0]
    assert r["after"] == {"flights": 0, "running": False}


@needs_node
def test_stopping_key_is_swallowed(ran):
    assert value(ran, "key_swallowed") == {"prevented": True, "running": False}


@needs_node
def test_button_toggles(ran):
    r = value(ran, "button_toggles")
    assert r["on"] is True and r["running"] is False
    assert r["text"] == "START TOUR" and r["last"] == "button"
    assert r["status"].startswith("Stopped. Flies to the ISS")


@needs_node
def test_static_demo(ran):
    r = value(ran, "static_demo")
    assert r["stops"] == ["iss", "quake"]
    assert r["requests"] == [
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"]
    assert r["intro"].startswith("Demo build")


@needs_node
def test_nothing_live(ran):
    r = value(ran, "nothing_live")
    assert r["cap"] == ["LIVE TOUR", "Nothing live to show right now", "No hotspot feed answered"]
    assert r["running"] is False and r["last"] == "nothing live" and r["flights"] == 0
    assert r["status"] == "No live hotspot answered; try again later."
    assert r["touring"] is False


@needs_node
def test_skips_missing(ran):
    r = value(ran, "skips_missing")
    # Offline, 500 and keyless chokepoints drop out; the ISS is gathered but cannot be
    # ridden without selectSatellite, so the tour goes straight to the launch.
    assert r["stops"] == ["iss", "launch"]
    assert r["flights"] == 1 and r["range"] == 60000 and r["modes"] == []
    assert r["kicker"] == "NEXT ORBITAL LAUNCH  |  2 OF 2"
    assert r["title"] == "Electron | Mission"
    assert r["line"] == "Rocket Lab, LC-1B, T-40 min (Go)"


@needs_node
def test_loops_with_fresh_data(ran):
    r = value(ran, "loops")
    assert r["cycle"] == 1 and r["index"] == 0 and r["stops"] == ["quake"]
    assert r["flights"] == 2 and r["quakeRequests"] == 2
    assert r["headings"] == [0, 47]


@needs_node
def test_stop_before_gather(ran):
    assert value(ran, "stop_before_gather") == {
        "running": False, "flights": 0, "selected": 0, "modes": [], "cap": False, "last": "api"}


@needs_node
def test_cancelled_flight_moves_on(ran):
    assert value(ran, "cancel_moves_on") == {"flights": 2, "title": "M6.1 earthquake",
                                             "orbiting": False}


@needs_node
def test_init_and_disable(ran):
    r = value(ran, "init_and_disable")
    assert r["bad"] is False and r["noViewer"] is False and r["ok"] is True
    assert r["running"] is False and r["panel"] == "Tour" and r["style"] is True
    assert r["hint"] == "Press any key or click to stop the tour"


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert text.isascii() and "innerHTML" not in text
    assert "global.SDATour" in text
