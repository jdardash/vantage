"""Tests for the globe 3D models and ride-along bar (static/js/models.js) and its assets.

The JavaScript runs under Node's `vm` with the DOM stub and vector fake Cesium from
tests/globe_vm.py; nothing touches the network. The GLB checks parse every model in
static/models and hold them to a size budget, and the generator is checked to be
deterministic against the files in the repository.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "src" / "sda" / "templates" / "static" / "models"
MODULE = JS_DIR / "models.js"

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = r"""
var NOW = 1000;
SDAModels._setClock(function () { return NOW; });
var v = makeViewer();
var ctx = { viewer: v, mount: new El('div'), api: '', isStatic: false,
  satEngine: { byNoradId: {}, selectedId: null, active: true } };
function flight(id, lon, lat, alt, props) {
  return v.entities.add({ id: id, name: props.name || id,
    position: new Cesium.ConstantPositionProperty(Cesium.Cartesian3.fromDegrees(lon, lat, alt)),
    point: { show: true }, properties: props });
}
function step(ms) { NOW += (ms === undefined ? 600 : ms); SDAModels._frame(); }
function heading(e) { return Math.round(e.orientation.getValue().hpr.heading * 180 / Math.PI); }
function at(e) { return e.position.getValue(v.clock.currentTime); }
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = SETUP + body


scenario("classify_aircraft", r"""
var c = SDAModels.classifyAircraft;
return {
  b738: c({ typeCode: 'B738' }), b744: c({ typeCode: 'b744' }), a359: c({ typeCode: 'A359' }),
  c172: c({ typeCode: 'C172' }), ec35: c({ typeCode: 'EC35' }), mq9: c({ typeCode: 'MQ9' }),
  f16: c({ typeCode: 'F16' }), glf5: c({ typeCode: 'GLF5' }), at72: c({ typeCode: 'AT72' }),
  disc: c({ typeCode: 'DISC' }),
  emitter: c({ typeCode: '', category: 'A7' }), opensky: c({ category: 8 }),
  mil: c({ typeCode: 'MIL' }), slow: c({ velocity: 40, altM: 600 }),
  forced: c({ typeCode: 'B738', modelClass: 'helicopter' }), none: c({})
};
""")

scenario("classify_ship", r"""
var c = SDAModels.classifyShip;
function k(o) { return c(o).cls; }
return [k({ shipType: 30 }), k({ shipType: 35 }), k({ shipType: 36 }), k({ shipType: 37 }),
  k({ shipType: 52 }), k({ shipType: 60 }), k({ shipType: 70, name: 'MAERSK KENSINGTON' }),
  k({ shipType: 70, name: 'BULK HERO' }), k({ shipType: 84 }), k({ shipType: 41 }),
  k({ name: 'LNG RIVERS' }), k({ name: 'NOBODY' }), k({ shipType: '80' })];
""")

scenario("geometry", r"""
var dr = SDAModels.drOffset(10, 90, 60, 30);
return {
  h1: SDAModels.headingFor(90, 180), h2: SDAModels.headingFor(10, -90),
  h3: SDAModels.headingFor(350, 180),
  drEast: Math.round(dr.east), drNorth: Math.round(dr.north), drT: dr.t,
  scaleLen: SDAModels.shipScale(SDAModels.SHIP_MODELS.cargo, 95),
  scaleNone: SDAModels.shipScale(SDAModels.SHIP_MODELS.highspeed, null),
  rideAir: SDAModels.rideRangeFor('aircraft', 40, 0),
  rideShip: SDAModels.rideRangeFor('vessel', 300, 0),
  fmt: SDAModels.formatRide({ mode: 'chase', kind: 'aircraft', name: 'AAL100',
    label: 'airliner B738', altM: 11000, speedMs: 230, track: 5 }),
  fmtLow: SDAModels.formatRide({ mode: 'orbit', kind: 'aircraft', name: 'N1', label: '',
    altM: 900, speedMs: 50, track: null }),
  fmtSat: SDAModels.formatRide({ mode: 'cockpit', kind: 'satellite', name: 'ISS', label: 'ISS',
    altM: 420000, speedMs: 7660, track: null })
};
""")

scenario("pick_models", r"""
var b = { max: 3, addM: 100000 };
var cands = [
  { id: 'far-target', d: 900000, has: false, target: true },
  { id: 'a', d: 50000, has: false, target: false },
  { id: 'b', d: 20000, has: false, target: false },
  { id: 'kept', d: 120000, has: true, target: false },
  { id: 'new-out', d: 120000, has: false, target: false },
  { id: 'c', d: 60000, has: false, target: false }
];
return SDAModels.pickModels(cands, b).map(function (c) { return c.id; });
""")

scenario("panel_and_hud_built", r"""
var ok = SDAModels.init(ctx);
var hud = document.getElementById('sda-ride');
return { ok: ok, title: byTag(ctx.mount, 'H3')[0].textContent,
  boxes: byTag(ctx.mount, 'INPUT').length, select: byTag(ctx.mount, 'SELECT')[0].value,
  hud: !!hud, hudOn: hud.has('on'), style: !!document.getElementById('sda-models-style'),
  buttons: byTag(hud, 'BUTTON').map(function (b) { return b.textContent; }) };
""")

scenario("init_refuses_without_mount", r"""
return [SDAModels.init({ viewer: v }), SDAModels.init(null)];
""")

scenario("aircraft_gets_model_and_reverts", r"""
SDAModels.init(ctx);
setCamera(v, -97.74, 30.27, 20000);
var e = flight('flight-abc123', -97.74, 30.30, 3000, { type: 'aircraft', aircraft_type: 'B738',
  heading: 90, velocity: 200, name: 'AAL100' });
var reported = at(e);
SDAModels._frame();
var on = { uri: e.model.uri, scale: e.model.scale, point: e.point.show, heading: heading(e),
  smooth: !!e.position.callback };
setCamera(v, -97.74, 30.27, 20000000);
step();
var back = at(e);
return { on: on, off: { model: e.model === undefined, point: e.point.show,
  constant: !!e.position.constant, moved: Cesium.Cartesian3.distance(back, reported) },
  counts: SDAModels.counts() };
""")

scenario("estimated_never_modelled", r"""
SDAModels.init(ctx);
setCamera(v, -97.74, 30.27, 20000);
var e = flight('flight-dead01', -97.74, 30.30, 3000, { type: 'aircraft', aircraft_type: 'B738',
  heading: 90, velocity: 200, state: 'estimated' });
var f = flight('flight-dead02', -97.74, 30.31, 3000, { type: 'aircraft', estimated: true });
SDAModels._frame();
return [e.model === undefined, e.point.show, f.model === undefined];
""")

scenario("dead_reckoning_cap_and_blend", r"""
SDAModels.init(ctx);
setCamera(v, -97.74, 30.27, 20000);
var e = flight('flight-abc124', -97.74, 30.30, 3000, { type: 'aircraft', aircraft_type: 'B738',
  heading: 90, velocity: 200 });
var p0 = at(e);
SDAModels._frame();
NOW += 10000;
var d10 = Cesium.Cartesian3.distance(at(e), p0);
NOW += 100000;
var dCap = Cesium.Cartesian3.distance(at(e), p0);
// A new report lands 0.004 deg north of where the model is drawn now.
var drawn = at(e);
var carto = Cesium.Cartographic.fromCartesian(drawn);
var fix = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude + 0.004 * Math.PI / 180,
  carto.height);
e.position = new Cesium.ConstantPositionProperty(fix);
SDAModels._frame();
var jump = Cesium.Cartesian3.distance(at(e), drawn);
NOW += 2500;
var settled = Cesium.Cartesian3.distance(at(e), fix);
return { d10: Math.round(d10), dCap: Math.round(dCap), jump: Math.round(jump),
  settled: Math.round(settled), smooth: !!e.position.callback };
""")

scenario("smooth_off_holds_reports", r"""
SDAModels.init(ctx);
setCamera(v, -97.74, 30.27, 20000);
var e = flight('flight-abc125', -97.74, 30.30, 3000, { type: 'aircraft', heading: 0,
  velocity: 150 });
SDAModels._frame();
var was = !!e.position.callback;
SDAModels.setSmooth(false);
NOW += 20000;
return { was: was, now: !!e.position.callback, constant: !!e.position.constant };
""")

scenario("budget_caps_and_target_first", r"""
SDAModels.init(ctx);
SDAModels.setBudget('low');
setCamera(v, 10, 50, 30000);
var list = [];
for (var i = 0; i < 30; i++) {
  list.push(flight('flight-b' + i, 10 + i * 0.01, 50, 1000, { type: 'aircraft', heading: 0,
    velocity: 100 }));
}
var far = flight('flight-far', 16, 50, 1000, { type: 'aircraft', heading: 0, velocity: 100 });
window.selectedEntity = far;
SDAModels._frame();
var modelled = list.filter(function (e) { return !!e.model; }).length;
return { modelled: modelled, nearest: !!list[0].model, farthest: !!list[29].model,
  target: !!far.model, silhouette: far.model && far.model.silhouetteSize };
""")

scenario("hysteresis_keeps_then_drops", r"""
SDAModels.init(ctx);
setCamera(v, 0, 0, 20000);
var e = flight('flight-h1', 0, 0.9, 1000, { type: 'aircraft', heading: 0, velocity: 0 });
SDAModels._frame();
var first = !!e.model;
setCamera(v, 0, -0.6, 20000);   // about 167 km: past add, inside keep
step();
var kept = !!e.model;
setCamera(v, 0, -1.0, 20000);   // about 211 km: past keep
step();
return [first, kept, !!e.model];
""")

scenario("ship_in_data_source", r"""
SDAModels.init(ctx);
var ds = new Cesium.CustomDataSource('ais');
v.dataSources.add(ds);
setCamera(v, 103.8, 1.2, 15000);
var e = ds.entities.add({ id: 'ais-1', name: 'EVER GIVEN',
  position: new Cesium.ConstantPositionProperty(Cesium.Cartesian3.fromDegrees(103.81, 1.21, 0)),
  point: { show: true },
  properties: { feed: 'ais', detail_rows: [['MMSI', '353136000'], ['SPEED', '12.5 kn'],
    ['COURSE', '45'], ['HEADING', '511']] } });
SDAModels._frame();
var on = { uri: e.model.uri, heading: heading(e), smooth: !!e.position.callback };
ds.show = false;
step();
return { on: on, hiddenSourceDropped: e.model === undefined };
""")

scenario("military_tint", r"""
SDAModels.init(ctx);
setCamera(v, 0, 0, 20000);
var e = flight('mil-ae1234', 0, 0.1, 5000, { type: 'military', aircraft_type: 'C17',
  heading: 0, velocity: 200 });
SDAModels._frame();
return { uri: e.model.uri, scale: e.model.scale, color: e.model.color && e.model.color.css };
""")

scenario("space_models", r"""
var eng = ctx.satEngine;
eng.byNoradId[25544] = { name: 'ISS (ZARYA)',
  point: { show: true, position: Cesium.Cartesian3.fromDegrees(0, 0, 420000) } };
eng.byNoradId[20580] = { name: 'HST',
  point: { show: true, position: Cesium.Cartesian3.fromDegrees(90, 0, 530000) } };
eng.byNoradId[44713] = { name: 'STARLINK-1007',
  point: { show: true, position: Cesium.Cartesian3.fromDegrees(-90, 0, 550000) } };
SDAModels.init(ctx);
setCamera(v, 0, 0, 1500000);
SDAModels._frame();
var ds = v.dataSources.list.filter(function (d) { return d.name === 'sdamodels-space'; })[0];
var iss = ds.entities.getById('sdamodels-sat-25544');
var first = { iss: !!iss, uri: iss.model.uri, type: iss.type, norad: iss.noradId,
  show: iss.show, hubble: !!ds.entities.getById('sdamodels-sat-20580'),
  pos: Math.round(Cesium.Cartesian3.distance(iss.position.getValue(),
    eng.byNoradId[25544].point.position)) };
eng.selectedId = 44713;
step();
var generic = ds.entities.getById('sdamodels-sat-44713');
var withSel = { generic: !!generic, uri: generic && generic.model.uri };
window.camState = { mode: 'cockpit', targetKind: 'satellite', targetId: 25544 };
step();
var cockpitHidesIss = iss.show === false;
window.camState = { mode: 'free' };
eng.selectedId = null;
setCamera(v, 0, 0, 30000000);
step();
return { first: first, withSel: withSel, cockpitHidesIss: cockpitHidesIss,
  genericDropped: !ds.entities.getById('sdamodels-sat-44713'),
  issHiddenFar: iss.show === false, counts: SDAModels.counts() };
""")

scenario("ride_bar_follows_camera_modes", r"""
var calls = [];
window.camState = { mode: 'free', targetKind: null, targetId: null };
window.setCameraMode = function (mode) {
  calls.push(mode);
  window.camState.mode = mode;
  if (mode !== 'free') { window.camState.targetKind = 'entity';
    window.camState.targetId = window.selectedEntity.id; }
};
SDAModels.init(ctx);
setCamera(v, -97.74, 30.27, 20000);
var e = flight('flight-abc126', -97.74, 30.30, 11000, { type: 'aircraft', aircraft_type: 'B738',
  heading: 5, velocity: 230, name: 'AAL100' });
window.selectedEntity = e;
SDAModels._frame();
var hud = document.getElementById('sda-ride');
var before = hud.has('on');
window.setCameraMode('chase');
step();
var text = byClass(hud, 'sdaride-text')[0].textContent;
var chaseActive = buttonNamed(hud, 'CHASE').has('active');
buttonNamed(hud, 'FIRST PERSON').fire('click');
step();
var hidden = e.model.show === false;
buttonNamed(hud, 'EXIT').fire('click');
step();
return { before: before, text: text, chaseActive: chaseActive, hidden: hidden,
  shownAgain: e.model.show, calls: calls, after: hud.has('on') };
""")

scenario("ride_reacquires_redrawn_ship", r"""
var picked = [];
window.camState = { mode: 'chase', targetKind: 'entity', targetId: 'ais-0' };
window.selectEntity = function (en) { picked.push(en.id); window.selectedEntity = en; };
var ds = new Cesium.CustomDataSource('ais');
v.dataSources.add(ds);
function ship(id, mmsi) {
  return ds.entities.add({ id: id, name: 'MSC ANNA',
    position: new Cesium.ConstantPositionProperty(Cesium.Cartesian3.fromDegrees(4, 52, 0)),
    point: { show: true }, properties: { feed: 'ais', mmsi: mmsi, sog_kn: 10, cog_deg: 90 } });
}
var first = ship('ais-0', '255806000');
window.selectedEntity = first;
SDAModels.init(ctx);
setCamera(v, 4, 52, 10000);
SDAModels._frame();
ds.entities.removeAll();
ship('ais-0b', '111111111');
var again = ship('ais-7', '255806000');
step();
return { picked: picked, targetId: window.camState.targetId,
  selectedIsNew: window.selectedEntity === again };
""")

scenario("sources_rows_get_models", r"""
var modelledKeys = null;
SDAModels.addSource('maritime', {
  rows: function () {
    return [
      { key: '235000001', kind: 'vessel', lat: 51.0, lon: 1.5, cog_deg: 30, sog_kn: 14,
        ship_type: 80, name: 'NORDIC TANKER', pick: function () { window.pickedRow = 1; } },
      { key: '235000002', kind: 'vessel', lat: 51.01, lon: 1.5, estimate: true, sog_kn: 9 },
      { key: '235000003', kind: 'vessel', lat: 'x', lon: 1.5 }
    ];
  },
  modelled: function (keys) { modelledKeys = keys.slice(); }
});
SDAModels.addSource('aviation', {
  rows: function () {
    return [
      { key: 'ABC127', kind: 'aircraft', lat: 51.02, lon: 1.5, alt_m: 2000, track: 180,
        gs_kt: 250, type_code: 'A320', state: 'reported' },
      { key: 'def999', kind: 'aircraft', lat: 51.03, lon: 1.5, alt_m: 2500, track: 90,
        gs_kt: 300, type_code: 'DH8D', state: 'reported' }
    ];
  }
});
SDAModels.init(ctx);
// The page already draws ABC127 as flight-abc127, so it is not modelled twice.
flight('flight-abc127', 1.5, 51.02, 2000, { type: 'aircraft', heading: 180, velocity: 128 });
setCamera(v, 1.5, 51.0, 20000);
SDAModels._frame();
var ds = v.dataSources.list.filter(function (d) { return d.name === 'sdamodels-feeds'; })[0];
var ids = ds.entities.values.map(function (e) { return e.id; }).sort();
var tanker = ds.entities.getById('sdamodels-maritime:235000001');
var dash8 = ds.entities.getById('sdamodels-aviation:def999');
tanker.sdaPick();
var out = { ids: ids, tanker: tanker.model.uri, dash8: dash8.model.uri,
  picked: window.pickedRow, modelledKeys: modelledKeys,
  tankerSmooth: !!tanker.position.callback };
SDAModels.removeSource('maritime');
step();
out.afterRemove = ds.entities.values.map(function (e) { return e.id; });
SDAModels.setEnabled(false);
out.afterOff = ds.entities.values.length;
return out;
""")


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_scenarios(tmp_path_factory.mktemp("models"), ["models.js"], SCENARIOS)


@needs_node
def test_aircraft_classes(ran):
    r = value(ran, "classify_aircraft")
    expect = {"b738": "airliner", "b744": "quadjet", "a359": "widebody", "c172": "light",
              "ec35": "helicopter", "mq9": "drone", "f16": "fastjet", "glf5": "bizjet",
              "at72": "turboprop", "disc": "glider", "emitter": "helicopter",
              "opensky": "helicopter", "mil": "airliner", "slow": "light",
              "forced": "helicopter", "none": "airliner"}
    assert {k: v["cls"] for k, v in r.items()} == expect
    assert r["b738"]["basis"] == "type"
    assert r["emitter"]["basis"] == "category"
    assert r["slow"]["basis"] == "speed"
    assert r["mil"]["basis"] == "default"


@needs_node
def test_ship_classes(ran):
    assert value(ran, "classify_ship") == [
        "fishing", "military", "sailing", "pleasure", "tug", "passenger", "container",
        "cargo", "tanker", "highspeed", "tanker", "cargo", "tanker"]


@needs_node
def test_heading_dead_reckoning_and_ranges(ran):
    r = value(ran, "geometry")
    assert (r["h1"], r["h2"], r["h3"]) == (270, 280, 170)
    assert (r["drEast"], r["drNorth"], r["drT"]) == (300, 0, 30)
    assert r["scaleLen"] == pytest.approx(0.5)
    assert r["scaleNone"] == pytest.approx(1.8)
    assert r["rideAir"] == pytest.approx(144)
    assert r["rideShip"] == pytest.approx(720)
    assert r["fmt"] == "CHASE | AAL100 | airliner B738 | FL361 | 447 kt | HDG 005"
    assert r["fmtLow"] == "ORBIT | N1 | 2,953 ft | 97 kt"
    assert r["fmtSat"] == "COCKPIT | ISS | ISS | 420 km | 7.66 km/s"


@needs_node
def test_pick_order_target_hysteresis_cap(ran):
    assert value(ran, "pick_models") == ["far-target", "b", "a"]


@needs_node
def test_panel_and_ride_bar_build(ran):
    r = value(ran, "panel_and_hud_built")
    assert r["ok"] is True and r["title"] == "3D models"
    assert r["boxes"] == 2 and r["select"] == "medium"
    assert r["hud"] and not r["hudOn"] and r["style"]
    assert r["buttons"] == ["CHASE", "FIRST PERSON", "ORBIT", "EXIT"]
    assert value(ran, "init_refuses_without_mount") == [False, False]


@needs_node
def test_aircraft_model_swaps_in_and_out(ran):
    r = value(ran, "aircraft_gets_model_and_reverts")
    assert r["on"] == {"uri": "static/models/airplane.glb", "scale": 0.74, "point": False,
                       "heading": 270, "smooth": True}
    assert r["off"]["model"] and r["off"]["point"] is True and r["off"]["constant"]
    # The point goes back to its last report, not to a dead-reckoned spot.
    assert r["off"]["moved"] == pytest.approx(0, abs=1e-6)
    assert r["counts"]["aircraft"] == 0


@needs_node
def test_estimates_are_never_modelled(ran):
    assert value(ran, "estimated_never_modelled") == [True, True, True]


@needs_node
def test_dead_reckoning_is_capped_and_blended(ran):
    r = value(ran, "dead_reckoning_cap_and_blend")
    assert r["d10"] == pytest.approx(2000, abs=2)
    assert r["dCap"] == pytest.approx(9000, abs=5)     # 45 s at 200 m/s, then held
    assert r["jump"] < 1                                # a new report does not snap
    assert r["settled"] == pytest.approx(500, abs=5)    # 2.5 s along the track after the blend
    assert r["smooth"]


@needs_node
def test_smooth_motion_can_be_turned_off(ran):
    assert value(ran, "smooth_off_holds_reports") == {"was": True, "now": False,
                                                      "constant": True}


@needs_node
def test_budget_cap_and_selected_target(ran):
    r = value(ran, "budget_caps_and_target_first")
    assert r["modelled"] == 15 and r["nearest"] and not r["farthest"]
    assert r["target"] and r["silhouette"] == 2


@needs_node
def test_hysteresis(ran):
    assert value(ran, "hysteresis_keeps_then_drops") == [True, True, False]


@needs_node
def test_ship_rows_from_layers(ran):
    r = value(ran, "ship_in_data_source")
    # No AIS type, so the operator name (EVER for Evergreen) picks the container hull.
    assert r["on"] == {"uri": "static/models/ship_container.glb", "heading": 315,
                       "smooth": True}
    assert r["hiddenSourceDropped"]


@needs_node
def test_military_tint_and_type(ran):
    r = value(ran, "military_tint")
    assert r == {"uri": "static/models/airplane.glb", "scale": 1.25, "color": "#a3ab8f"}


@needs_node
def test_space_models(ran):
    r = value(ran, "space_models")
    assert r["first"] == {"iss": True, "uri": "static/models/iss.glb", "type": "satellite",
                          "norad": 25544, "show": True, "hubble": False, "pos": 0}
    assert r["withSel"] == {"generic": True, "uri": "static/models/satellite.glb"}
    assert r["cockpitHidesIss"] and r["genericDropped"] and r["issHiddenFar"]
    assert r["counts"]["space"] == 0


@needs_node
def test_ride_bar(ran):
    r = value(ran, "ride_bar_follows_camera_modes")
    assert r["before"] is False and r["after"] is False
    assert r["text"].startswith("CHASE | AAL100 | airliner B738 | FL361 | 447 kt | HDG 005")
    assert r["chaseActive"] and r["hidden"] and r["shownAgain"] is True
    assert r["calls"] == ["chase", "cockpit", "free"]


@needs_node
def test_ride_follows_a_redrawn_ship(ran):
    r = value(ran, "ride_reacquires_redrawn_ship")
    assert r == {"picked": ["ais-7"], "targetId": "ais-7", "selectedIsNew": True}


@needs_node
def test_sources(ran):
    r = value(ran, "sources_rows_get_models")
    assert r["ids"] == ["sdamodels-aviation:def999", "sdamodels-maritime:235000001"]
    assert r["tanker"] == "static/models/ship_tanker.glb"
    assert r["dash8"] == "static/models/atr72.glb"
    assert r["picked"] == 1 and r["modelledKeys"] == ["235000001"] and r["tankerSmooth"]
    assert r["afterRemove"] == ["sdamodels-aviation:def999"]
    assert r["afterOff"] == 0


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert text.isascii()
    assert "innerHTML" not in text
    assert "global.SDAModels" in text


# ---- assets ---------------------------------------------------------------------

def _glb(path: Path) -> dict:
    data = path.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF" and version == 2, path.name
    assert length == len(data), path.name
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    assert chunk_type == 0x4E4F534A, path.name          # JSON
    doc = json.loads(data[20:20 + chunk_len].decode("utf-8"))
    assert doc["asset"]["version"] == "2.0"
    assert doc["meshes"], path.name
    return doc


def test_every_model_file_parses_and_fits_the_budget():
    files = sorted(MODELS.glob("*.glb"))
    names = {f.name for f in files}
    module = MODULE.read_text(encoding="utf-8")
    for f in files:
        _glb(f)
        assert f.stat().st_size < 600_000, f.name
        assert f"'{f.name}'" in module, f"{f.name} is not used by models.js"
    assert sum(f.stat().st_size for f in files) < 2_400_000
    notice = (MODELS / "NOTICE.txt").read_text(encoding="utf-8")
    for name in names:
        assert name in notice, f"{name} has no licence line in NOTICE.txt"


def test_generated_models_match_the_generator():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_globe_models.py"), "--check"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
