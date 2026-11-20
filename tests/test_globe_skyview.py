"""Tests for the sky view module (static/js/skyview.js) and its star data.

The module runs under Node's `vm` against a DOM stub, a fake Cesium that does
real vector maths on a spherical Earth, and a fake astronomy-engine that returns
a rotation matrix recorded from the real library (astronomy-engine 2.1.19,
Rotation_EQJ_HOR for 30.27 N 97.74 W at 2026-11-20T04:00Z). Comparing the
module's frame handling against that recorded output checks the one thing that
is easy to get wrong: astronomy-engine's horizontal frame is x north, y west,
z up, and its rotation matrices are applied column-wise. Skips without Node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "static"
MODULE = STATIC / "js" / "skyview.js"
SKY_JSON = STATIC / "data" / "sky.json"

# Recorded from astronomy-engine 2.1.19 (see the module docstring).
ROT = [
    [-0.467992222914, 0.360073042424, 0.807050607714],
    [-0.181480734763, -0.932923660586, 0.310995476541],
    [0.864897694645, -0.000920672896, 0.501947338034],
]
SIRIUS = {"ra": 101.287, "dec": -16.716, "az": 109.36, "el": -0.204}
VEGA = {"ra": 279.2347, "dec": 38.7837, "az": 309.252, "el": 10.142}

PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
}
El.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this; this.children.push(c); return c;
};
El.prototype.removeChild = function (c) {
  var at = this.children.indexOf(c);
  if (at < 0) throw new Error('removeChild: not a child');
  this.children.splice(at, 1); c.parentNode = null; return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, fn)
  { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function (t, fn) {
  this.listeners[t] = (this.listeners[t] || []).filter(function (f) { return f !== fn; });
};
Object.defineProperty(El.prototype, 'textContent', {
  get: function () {
    return this._text + this.children.map(function (c) { return c.textContent; }).join('');
  },
  set: function (v) { this.children = []; this._text = String(v); }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { throw new Error('innerHTML read'); },
  set: function () { throw new Error('innerHTML used'); }
});
Object.defineProperty(El.prototype, 'classList', {
  get: function () {
    var el = this;
    function list() { return el.className.split(/\s+/).filter(Boolean); }
    function set(names) { el.className = names.join(' '); }
    return {
      contains: function (c) { return list().indexOf(c) >= 0; },
      add: function (c) { if (list().indexOf(c) < 0) set(list().concat(c)); },
      remove: function (c) { set(list().filter(function (x) { return x !== c; })); },
      toggle: function (c, force) {
        var on = force === undefined ? !this.contains(c) : !!force;
        if (on) this.add(c); else this.remove(c);
        return on;
      }
    };
  }
});
El.prototype.has = function (cls) { return this.classList.contains(cls); };
El.prototype.fire = function (type, ev) {
  ev = ev || {}; ev.type = type;
  ev.stopImmediatePropagation = function () { ev.immediate = true; };
  var fns = (this.listeners[type] || []).slice();
  for (var i = 0; i < fns.length; i++) { fns[i].call(this, ev); if (ev.immediate) break; }
  return ev;
};
function all(el, out) {
  out = out || []; out.push(el);
  el.children.forEach(function (c) { all(c, out); });
  return out;
}
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }
function buttonNamed(root, text) {
  return byTag(root, 'BUTTON').filter(function (b) { return b.textContent === text; })[0];
}

var document = new El('document');
document.head = new El('head');
document.body = new El('body');
document.createElement = function (tag) { return new El(tag); };
document.getElementById = function (id) {
  var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
  return hit[0] || null;
};
var window = this;
var winListeners = {};
function addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); }
function removeEventListener(t, fn) {
  winListeners[t] = (winListeners[t] || []).filter(function (f) { return f !== fn; });
}

// ---- fake Cesium: real vector maths on a spherical Earth -------------------
var RE = 6371000;
function V(x, y, z) { this.x = x; this.y = y; this.z = z; }
var Cartesian3 = V;
V.ZERO = new V(0, 0, 0);
V.fromDegrees = function (lon, lat, h) {
  var r = RE + (h || 0), la = lat * Math.PI / 180, lo = lon * Math.PI / 180;
  return new V(r * Math.cos(la) * Math.cos(lo), r * Math.cos(la) * Math.sin(lo),
               r * Math.sin(la));
};
V.clone = function (a) { return new V(a.x, a.y, a.z); };
V.subtract = function (a, b, out) { out.x = a.x - b.x; out.y = a.y - b.y; out.z = a.z - b.z;
  return out; };
V.magnitude = function (a) { return Math.sqrt(a.x * a.x + a.y * a.y + a.z * a.z); };
function M3(a, b, c, d, e, f, g, h, i) { this.r = [[a, b, c], [d, e, f], [g, h, i]]; }
M3.multiply = function (A, B) {
  var o = new M3(0, 0, 0, 0, 0, 0, 0, 0, 0);
  for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) {
    o.r[i][j] = A.r[i][0] * B.r[0][j] + A.r[i][1] * B.r[1][j] + A.r[i][2] * B.r[2][j];
  }
  return o;
};
M3.multiplyByVector = function (A, v, out) {
  out = out || new V(0, 0, 0);
  var x = A.r[0][0] * v.x + A.r[0][1] * v.y + A.r[0][2] * v.z;
  var y = A.r[1][0] * v.x + A.r[1][1] * v.y + A.r[1][2] * v.z;
  var z = A.r[2][0] * v.x + A.r[2][1] * v.y + A.r[2][2] * v.z;
  out.x = x; out.y = y; out.z = z; return out;
};
M3.transpose = function (A) {
  var r = A.r;
  return new M3(r[0][0], r[1][0], r[2][0], r[0][1], r[1][1], r[2][1], r[0][2], r[1][2], r[2][2]);
};
var M4 = { IDENTITY: { identity: true },
  fromRotationTranslation: function (R, t) { return { rot: R, t: t }; },
  getMatrix3: function (m4) { return m4.rot; } };
function Coll() { this.items = []; this.show = true; this.modelMatrix = null; }
Coll.prototype.add = function (o) { o.show = o.show === undefined ? true : o.show;
  this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
function Handler() { this.actions = {}; Handler.last = this; }
Handler.prototype.setInputAction = function (fn, type) { this.actions[type] = fn; };
Handler.prototype.destroy = function () { this.destroyed = true; };
var Cesium = {
  Cartesian3: V, Cartesian2: function (x, y) { this.x = x; this.y = y; },
  Cartographic: {
    fromDegrees: function (lon, lat) { return { longitude: lon * Math.PI / 180,
      latitude: lat * Math.PI / 180, height: 0 }; },
    fromCartesian: function (p) {
      var r = V.magnitude(p);
      return { latitude: Math.asin(p.z / r), longitude: Math.atan2(p.y, p.x), height: r - RE };
    }
  },
  Matrix3: M3, Matrix4: M4,
  Transforms: { eastNorthUpToFixedFrame: function (p) {
    var c = Cesium.Cartographic.fromCartesian(p), la = c.latitude, lo = c.longitude;
    var e = [-Math.sin(lo), Math.cos(lo), 0];
    var n = [-Math.sin(la) * Math.cos(lo), -Math.sin(la) * Math.sin(lo), Math.cos(la)];
    var u = [Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)];
    return { rot: new M3(e[0], n[0], u[0], e[1], n[1], u[1], e[2], n[2], u[2]), t: p };
  } },
  PointPrimitiveCollection: Coll, LabelCollection: Coll, PolylineCollection: Coll,
  Material: { fromType: function (t, u) { return { type: t, uniforms: u }; } },
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000') },
  LabelStyle: { FILL_AND_OUTLINE: 2 },
  HorizontalOrigin: { LEFT: 1, CENTER: 0 }, VerticalOrigin: { CENTER: 0, BOTTOM: 1 },
  JulianDate: { toDate: function (d) { return d; } },
  ScreenSpaceEventHandler: Handler, ScreenSpaceEventType: { LEFT_CLICK: 'LEFT_CLICK' }
};

function makeViewer() {
  var pre = [];
  var canvas = new El('canvas'); canvas.clientWidth = 1600; canvas.clientHeight = 900;
  return {
    canvas: canvas,
    clock: { currentTime: new Date('2026-11-20T04:00:00Z') },
    trackedEntity: 'tracked',
    scene: {
      primitives: { list: [], add: function (c) { this.list.push(c); return c; } },
      preRender: { addEventListener: function (fn) { pre.push(fn);
        return function () { pre.splice(pre.indexOf(fn), 1); }; } },
      screenSpaceCameraController: { enableInputs: true, enableCollisionDetection: true },
      skyAtmosphere: { show: true }, sun: { show: false }, moon: { show: false },
      globe: { ellipsoid: {}, getHeight: function () { return -36990; },
               enableLighting: false, dynamicAtmosphereLighting: false,
               dynamicAtmosphereLightingFromSun: false }
    },
    camera: {
      positionWC: V.fromDegrees(0, 0, 2e7), directionWC: new V(-1, 0, 0), upWC: new V(0, 0, 1),
      frustum: { fov: 1.0 },
      positionCartographic: { latitude: 0.5, longitude: -1.7, height: 2e7 },
      lookAtTransform: function () {},
      flyTo: function (o) {
        this.flights = (this.flights || 0) + 1; if (o.complete) o.complete();
      },
      pickEllipsoid: function () { return V.fromDegrees(-97.74, 30.27, 0); }
    },
    _pre: pre
  };
}

var ROT = __ROT__;
var Astronomy = {
  Body: { Sun: 'Sun', Moon: 'Moon', Mercury: 'Mercury', Venus: 'Venus', Mars: 'Mars',
          Jupiter: 'Jupiter', Saturn: 'Saturn', Uranus: 'Uranus', Neptune: 'Neptune' },
  Observer: function (lat, lon, h) { this.latitude = lat; this.longitude = lon; this.height = h; },
  Rotation_EQJ_HOR: function () { return { rot: ROT }; },
  Equator: function (body) {
    if (body === 'Sun') return { ra: 15.6, dec: -19.8 };
    if (body === 'Jupiter') return { ra: 148.3293 / 15, dec: 13.6286 };
    return { ra: 3, dec: 10 };
  },
  Illumination: function () { return { mag: -2.135, phase_fraction: 0.7591 }; },
  MoonPhase: function () { return 121.13; }
};
// Only the identify scenario installs these two, so every other scenario also
// proves a card survives an astronomy-engine call that is missing.
var AstronomyExtras = {
  Constellation: function (ra, dec) { return { name: dec > 30 ? 'Lyra' : 'Pisces' }; },
  SearchRiseSet: function (body, obs, dir) {
    return dir > 0 ? { date: new Date('2026-11-20T06:30:00Z') } : null;
  }
};

var SKY = { stars: [[101.287, -16.716, -1.46, 0.0], [279.2347, 38.7837, 0.03, 0.0],
                    [10, 10, 5.4, 1.5]],
            named: [[101.287, -16.716, -1.46, 'Sirius'], [279.2347, 38.7837, 0.03, 'Vega']],
            lines: [[10, 10, 20, 20, 30, 25]],
            constellations: [[279, 37, 'Lyra', 1], [90, 0, 'Faint', 3]] };
var REQUESTS = [];
function fetch(url) {
  REQUESTS.push(url);
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve(JSON.parse(JSON.stringify(SKY))); } });
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 6; i++) await tick(); }
function makeCtx(satEngine) {
  var feed = [];
  return { feed: feed, ctx: { viewer: makeViewer(), api: '', mount: new El('div'),
    satEngine: satEngine || { byNoradId: {}, selectedId: null },
    addIntelItem: function (t) { feed.push(t); } } };
}
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

(async function () {
  var results = {};
  for (var name of Object.keys(scenarios)) {
    try {
      var box = { setTimeout: setTimeout, clearTimeout: clearTimeout, Promise: Promise,
                  JSON: JSON, Math: Math, Date: Date };
      vm.createContext(box);
      vm.runInContext(prelude, box, { filename: 'prelude' });
      vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'skyview.js' });
      var fn = vm.runInContext('(async function () {\n' + scenarios[name] + '\n})', box,
                               { filename: name });
      var value = await fn();
      if (value === undefined) value = null;
      results[name] = { ok: true, value: JSON.parse(JSON.stringify(value)) };
    } catch (e) {
      results[name] = { ok: false, error: String((e && e.stack) || e) };
    }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(0);
})();
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


class Ran:
    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, name: str):
        result = self._raw[name]
        assert result["ok"], result["error"]
        return result["value"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Ran:
    if NODE is None:
        pytest.skip("node is not installed")
    work = tmp_path_factory.mktemp("skyview")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    prelude = PRELUDE.replace("__ROT__", json.dumps(ROT))
    (work / "prelude.js").write_text(prelude, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- frames and geometry ---------------------------------------------------------

scenario("frames", r"""
  var S = window.SDASkyView;
  function enuOf(ra, dec) {
    var rows = S.eqjToEnuRows(ROT), v = S.eqjVector(ra, dec);
    return rows.map(function (r) { return r[0] * v[0] + r[1] * v[1] + r[2] * v[2]; });
  }
  var viaRotate = S.horToEnu(S.rotate(ROT, S.eqjVector(101.287, -16.716)));
  return {
    sirius: S.azElOf(enuOf(101.287, -16.716)),
    vega: S.azElOf(enuOf(279.2347, 38.7837)),
    jupiter: S.azElOf(enuOf(148.3293, 13.6286)),
    agree: viaRotate.map(function (x, i) { return Math.abs(x - enuOf(101.287, -16.716)[i]); }),
  };
""")


def test_frame_matches_astronomy_engine(ran):
    r = ran["frames"]
    for got, want in ((r["sirius"], SIRIUS), (r["vega"], VEGA)):
        assert abs(got["az"] - want["az"]) < 0.05
        assert abs(got["el"] - want["el"]) < 0.05
    # Jupiter's recorded horizon position (aberration and parallax included): 57.23, -22.99
    assert abs(r["jupiter"]["az"] - 57.23) < 0.1 and abs(r["jupiter"]["el"] + 22.99) < 0.1
    assert max(r["agree"]) < 1e-12


scenario("basis", r"""
  var S = window.SDASkyView;
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
                                 a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  var worst = 0;
  [[0, 0], [90, 30], [200, 60], [315, 89.9], [10, 90], [123, -15]].forEach(function (a) {
    var d = S.enuDirection(a[0], a[1]), b = S.levelBasis(a[0], a[1]);
    var c = cross(d, b.up);
    worst = Math.max(worst, Math.abs(dot(d, b.up)), Math.abs(dot(d, b.right)),
      Math.abs(dot(b.up, b.right)), Math.abs(c[0] - b.right[0]), Math.abs(c[1] - b.right[1]),
      Math.abs(c[2] - b.right[2]), Math.abs(dot(b.up, b.up) - 1));
  });
  var round = S.azElOf(S.enuDirection(250, 40));
  return {
    worst: worst, round: round,
    fov: [S.fovFromFovy(1, 16 / 9), S.fovFromFovy(1, 0.5)],
    compass: [0, 11, 12, 45, 90, 180, 269, 359.9].map(S.compassPoint)
  };
""")


def test_camera_basis_is_orthonormal_up_to_the_zenith(ran):
    r = ran["basis"]
    assert r["worst"] < 1e-12
    assert abs(r["round"]["az"] - 250) < 1e-9 and abs(r["round"]["el"] - 40) < 1e-9
    assert r["fov"][0] > 1 and r["fov"][1] == 1
    assert r["compass"] == ["N", "N", "NNE", "NE", "E", "S", "W", "N"]


scenario("sky_rules", r"""
  var S = window.SDASkyView;
  return {
    limits: [-30, -15, -8, -3, 10].map(function (a) { return S.limitingMag(a, false); }),
    noDay: S.limitingMag(40, true),
    states: [-30, -15, -8, -3, 10].map(S.skyState),
    phases: [0, 45, 90, 135, 180, 225, 270, 315, 359].map(S.moonPhaseName),
    sizes: [-1.46, 0, 3, 5.5].map(S.starSize),
    colors: [-0.2, 0.1, 0.5, 0.8, 1.2, 1.8].map(S.starColor),
    near: S.nearestTarget([{ name: 'Far', enu: [1, 0, 0], priority: 0 },
                           { name: 'Close', enu: [0, 1, 0.02], priority: 1 }], [0, 1, 0], 4),
    none: S.nearestTarget([{ name: 'Far', enu: [1, 0, 0] }], [0, 1, 0], 4),
    // A star 0.1 deg off and a satellite 1 deg off, reach 3.3 deg: the star.
    onStar: S.nearestTarget([off('Sat', 1, -1), off('Star', 0.1, 1)], [0, 1, 0], 3.3).name,
    // A satellite 0.3 deg off and a star 0.4 deg the other way: a near tie the
    // satellite takes.
    tie: S.nearestTarget([off('Sat', 0.3, -1), off('Star', -0.4, 1)], [0, 1, 0], 3.3).name
  };
  // A target `deg` degrees from straight ahead ([0, 1, 0]).
  function off(name, deg, priority) {
    var r = deg * Math.PI / 180;
    return { name: name, enu: [Math.sin(r), Math.cos(r), 0], priority: priority };
  }
""")


def test_sky_rules(ran):
    r = ran["sky_rules"]
    assert r["limits"] == [5.5, 4.5, 3.0, 1.5, -1.5] and r["noDay"] == 5.5
    assert r["states"] == ["Night", "Astronomical twilight", "Nautical twilight",
                           "Civil twilight", "Day"]
    assert r["phases"] == ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
                           "Full moon", "Waning gibbous", "Last quarter", "Waning crescent",
                           "New moon"]
    sizes = r["sizes"]
    assert sizes == sorted(sizes, reverse=True) and sizes[-1] >= 1.4
    assert len(set(r["colors"])) == 6
    assert r["near"]["name"] == "Close" and r["none"] is None
    # A click dead on a star names the star even with a satellite a degree away;
    # the satellite only wins a near tie.
    assert r["onStar"] == "Star" and r["tie"] == "Sat"


# --- the module on a fake globe ------------------------------------------------------

scenario("panel", r"""
  var k = makeCtx();
  var ok = window.SDASkyView.init(k.ctx);
  var labels = byTag(k.ctx.mount, 'BUTTON').map(function (b) { return b.textContent; });
  buttonNamed(k.ctx.mount, 'UNDER SAT').fire('click');
  var noSat = byClass(k.ctx.mount, 'sky-status')[0].textContent;
  var bad = await window.SDASkyView.enter({ lat: NaN, lon: 0 });
  return { ok: ok, labels: labels, noSat: noSat, bad: bad,
    status: byClass(k.ctx.mount, 'sky-status')[0].textContent,
    hud: byClass(document.body, 'sky-hud')[0].style.display || '',
    requests: REQUESTS.length, active: window.SDASkyView.isActive() };
""")


def test_panel_mounts_and_refuses_bad_input(ran):
    r = ran["panel"]
    assert r["ok"] is True
    assert r["labels"] == ["LOOK UP HERE", "PICK A SPOT", "MY LOCATION", "UNDER SAT"]
    assert r["noSat"] == "Select a satellite on the globe first."
    assert r["bad"] is False and r["status"] == "No position to stand on."
    assert r["hud"] == "" and r["requests"] == 0 and r["active"] is False


scenario("enter_exit", r"""
  var iss = { name: 'ISS (ZARYA)', point: { show: true,
    position: Cesium.Cartesian3.fromDegrees(-97.5, 30.5, 420000) } };
  var low = { name: 'LOW', point: { show: true,
    position: Cesium.Cartesian3.fromDegrees(-60, 10, 500000) } };
  var pageLabels = { show: true };
  var k = makeCtx({ byNoradId: { 25544: iss, 1: low }, selectedId: null, labels: pageLabels });
  var v = k.ctx.viewer;
  window.SDASkyView.init(k.ctx);
  window.Astronomy = Astronomy;
  buttonNamed(k.ctx.mount, 'LOOK UP HERE').fire('click');
  await settle();
  var active = window.SDASkyView.isActive();
  var ssc = v.scene.screenSpaceCameraController;
  var during = { inputs: ssc.enableInputs, collision: ssc.enableCollisionDetection,
    moon: v.scene.moon.show, sun: v.scene.sun.show, tracked: v.trackedEntity || null };
  var g = v.scene.globe;
  var lit = [g.enableLighting, g.dynamicAtmosphereLighting, g.dynamicAtmosphereLightingFromSun,
             pageLabels.show];
  v._pre.forEach(function (fn) { fn(); });
  var nightGlow = v.scene.skyAtmosphere.show;
  var prims = v.scene.primitives.list;
  var stars = prims[0], labels = prims[3], satLabels = prims[6];
  var shownSats = satLabels.items.filter(function (l) { return l.show; })
    .map(function (l) { return l.text; });
  var bodyLabels = labels.items.map(function (l) { return l.text; });
  var hud = byClass(document.body, 'sky-hud')[0];
  var read = byClass(hud, 'sky-read')[0].textContent;
  var sunRead = byClass(hud, 'sky-dim')[0].textContent;
  var starsShown = stars.items.map(function (p) { return p.show; });
  var cameraDir = v.camera.direction;
  // a drag to the right turns the view left (towards smaller azimuth)
  v.canvas.fire('pointerdown', { clientX: 800, clientY: 450 });
  (winListeners.pointermove || []).forEach(function (fn) { fn({ clientX: 900, clientY: 450 }); });
  (winListeners.pointerup || []).forEach(function (fn) { fn({}); });
  v._pre.forEach(function (fn) { fn(); });
  var afterDrag = byClass(hud, 'sky-read')[0].textContent;
  var esc = document.fire('keydown', { key: 'Escape', target: document.body });
  return {
    active: active, during: during, shownSats: shownSats, bodyLabels: bodyLabels,
    read: read, afterDrag: afterDrag, sunRead: sunRead, starsShown: starsShown,
    hudShown: hud.style.display, exited: !window.SDASkyView.isActive(),
    restored: { inputs: ssc.enableInputs, collision: ssc.enableCollisionDetection,
      moon: v.scene.moon.show, sun: v.scene.sun.show, hud: hud.style.display },
    lit: lit, nightGlow: nightGlow,
    unlit: [g.enableLighting, g.dynamicAtmosphereLighting, g.dynamicAtmosphereLightingFromSun,
            pageLabels.show, v.scene.skyAtmosphere.show],
    model: !!stars.modelMatrix, eyeHeight: Cesium.Cartographic.fromCartesian(
      v.camera.position).height, flights: v.camera.flights, feed: k.feed, requests: REQUESTS,
    stoppedEsc: !!esc.immediate,
    cameraDir: [cameraDir.x, cameraDir.y, cameraDir.z]
  };
""")


scenario("identify", r"""
  var S = window.SDASkyView;
  var iss = { name: 'ISS (ZARYA)', point: { show: true,
    position: Cesium.Cartesian3.fromDegrees(-97.5, 30.5, 420000) } };
  var picked = [];
  var k = makeCtx({ byNoradId: { 25544: iss }, selectedId: null });
  k.ctx.selectSatellite = function (id) { picked.push(id); };
  var v = k.ctx.viewer;
  S.init(k.ctx);
  Object.keys(AstronomyExtras).forEach(function (n) { Astronomy[n] = AstronomyExtras[n]; });
  window.Astronomy = Astronomy;
  buttonNamed(k.ctx.mount, 'LOOK UP HERE').fire('click');
  await settle();
  var hud = byClass(document.body, 'sky-hud')[0];
  var card = byClass(document.body, 'sky-card')[0];
  function frame() { v._pre.forEach(function (fn) { fn(); }); }
  function aim() {
    frame();
    var t = byClass(hud, 'sky-read')[0].textContent;
    var mm = /AZ ([0-9.]+).*EL (-?[0-9.]+)\s+FOV ([0-9.]+)/.exec(t);
    return { az: +mm[1], el: +mm[2], fovy: +mm[3] };
  }
  function enuOf(ra, dec) {
    var rows = S.eqjToEnuRows(ROT), e = S.eqjVector(ra, dec);
    return rows.map(function (r) { return r[0] * e[0] + r[1] * e[1] + r[2] * e[2]; });
  }
  // turn so a direction sits in view: drag right turns left, drag down looks up
  function turnTo(az, el) {
    var a = aim(), kpx = a.fovy / 900;
    var daz = ((az - a.az + 540) % 360) - 180;
    v.canvas.fire('pointerdown', { clientX: 800, clientY: 450 });
    (winListeners.pointermove || []).forEach(function (fn) {
      fn({ clientX: 800 - daz / kpx, clientY: 450 + (el - a.el) / kpx }); });
    (winListeners.pointerup || []).forEach(function (fn) { fn({}); });
  }
  // the pixel a direction lands on, by inverting the pinhole in screenDirection
  function pixelOf(d) {
    var a = aim();
    var f = S.enuDirection(a.az, a.el), b = S.levelBasis(a.az, a.el);
    function dot(x, y) { return x[0] * y[0] + x[1] * y[1] + x[2] * y[2]; }
    var kk = Math.tan(a.fovy * Math.PI / 360) / 450, z = dot(d, f);
    return { x: 800 + dot(d, b.right) / z / kk, y: 450 - dot(d, b.up) / z / kk };
  }
  // a real click: press and release on the same pixel
  function click(px) {
    v.canvas.fire('pointerdown', { clientX: px.x, clientY: px.y });
    (winListeners.pointerup || []).forEach(function (fn) {
      fn({ clientX: px.x, clientY: px.y }); });
    return card.style.display === 'block' ? card.textContent : null;
  }
  function rows() {
    return byClass(card, 'sky-card-row').map(function (r) {
      return [r.children[0].textContent, r.children[1].textContent]; });
  }

  var vega = enuOf(279.2347, 38.7837);
  var vAe = S.azElOf(vega);
  turnTo(vAe.az, vAe.el);
  var vegaText = click(pixelOf(vega));
  var vegaTitle = byClass(card, 'sky-card-title')[0].textContent;
  var vegaKind = byClass(card, 'sky-card-kind')[0].textContent;
  var vegaRows = rows();
  var round = S.screenDirection(aim().az, aim().el, aim().fovy, pixelOf(vega).x - 800,
                                pixelOf(vega).y - 450, 900);

  var moon = enuOf(45, 10);
  var mAe = S.azElOf(moon);
  turnTo(mAe.az, Math.max(-20, mAe.el));
  click(pixelOf(moon));
  var moonTitle = byClass(card, 'sky-card-title')[0].textContent;
  var moonKind = byClass(card, 'sky-card-kind')[0].textContent;
  var moonFact = byClass(card, 'sky-card-fact')[0].textContent;
  var moonRows = rows();

  // the ISS passes almost straight overhead: look up and sweep the view for it
  var hits = [];
  turnTo(0, 89);
  for (var y2 = 0; y2 <= 900 && !hits.length; y2 += 15) {
    for (var x2 = 0; x2 <= 1600; x2 += 15) {
      var got2 = S.identifyAt(x2, y2);
      if (got2 && got2.kind === 'Satellite overhead') { hits.push(got2); break; }
    }
  }
  var act = byClass(card, 'sky-card-act')[0];
  if (act) act.fire('click');

  var empty = S.identifyAt(-5000, -5000);
  var emptyShown = card.style.display;
  click(pixelOf(vega));
  var escOnce = document.fire('keydown', { key: 'Escape', target: document.body });
  var afterEsc = { card: card.style.display, active: S.isActive() };
  document.fire('keydown', { key: 'Escape', target: document.body });
  return {
    vegaText: vegaText, vegaTitle: vegaTitle, vegaKind: vegaKind, vegaRows: vegaRows,
    roundTrip: round.map(function (x, i) { return Math.abs(x - vega[i]); }),
    moonTitle: moonTitle, moonKind: moonKind, moonFact: moonFact, moonRows: moonRows,
    sat: hits[0] || null, picked: picked, empty: empty, emptyShown: emptyShown,
    afterEsc: afterEsc, stopped: !!escOnce.immediate, exited: !S.isActive(),
    cardAfterExit: card.style.display,
    when: [S.whenText(new Date('2026-11-20T06:30:00Z'), new Date('2026-11-20T04:00:00Z')),
           S.whenText(new Date('2026-11-20T03:00:00Z'), new Date('2026-11-20T04:00:00Z')),
           S.whenText(null, new Date())],
    raDec: S.raDecOf(S.eqjVector(279.2347, 38.7837))
  };
""")


def test_clicking_the_sky_says_what_is_there(ran):
    r = ran["identify"]
    # a star: name, kind, magnitude and constellation
    assert r["vegaTitle"] == "Vega" and r["vegaKind"] == "Star"
    rows = dict(r["vegaRows"])
    assert rows["Brightness"] == "magnitude +0.03" and rows["In"] == "Lyra"
    assert rows["Position"].startswith("alt 10.1 deg, az 309")
    assert rows["RA / Dec"] == "18.62 h / +38.8 deg"
    assert all(d < 1e-9 for d in r["roundTrip"])
    # a solar system body: what it is, how lit, how far, when it rises and sets
    assert r["moonTitle"] == "Moon" and r["moonKind"] == "Moon of Earth"
    assert "27.3 days" in r["moonFact"]
    moon = dict(r["moonRows"])
    assert moon["Phase"] == "Waxing gibbous" and moon["Lit"] == "76%"
    assert moon["Brightness"] == "magnitude -2.1"
    assert moon["Rises"] == "06:30 UTC, in 2 h 30 min" and moon["Sets"] == "not in the next 2 days"
    assert "Distance" not in moon  # the fake Equator gives no distance, so the row is left out
    # a satellite overhead opens its own card
    assert r["sat"] is not None and r["sat"]["title"] == "ISS (ZARYA)"
    assert r["sat"]["norad"] == 25544 and r["picked"] == [25544]
    # empty sky closes the card; Esc closes the card before it leaves the sky
    assert r["empty"] is None and r["emptyShown"] == "none"
    assert r["afterEsc"] == {"card": "none", "active": True} and r["stopped"]
    assert r["exited"] and r["cardAfterExit"] == "none"
    assert r["when"] == ["06:30 UTC, in 2 h 30 min", "03:00 UTC", "--"]
    assert abs(r["raDec"]["ra"] - 18.6156) < 1e-3 and abs(r["raDec"]["dec"] - 38.7837) < 1e-9


def test_enter_draws_the_sky_and_exit_restores_the_globe(ran):
    r = ran["enter_exit"]
    assert r["active"] is True
    assert r["during"] == {"inputs": False, "collision": False, "moon": True, "sun": True,
                           "tracked": None}
    assert r["requests"] == ["static/data/sky.json"]
    # the ISS 420 km up and about 30 km away is labelled; the one over the Atlantic is not
    assert len(r["shownSats"]) == 1 and r["shownSats"][0].startswith("ISS (ZARYA)  42")
    assert "Sirius" in r["bodyLabels"] and "LYRA" in r["bodyLabels"]
    assert "FAINT" not in r["bodyLabels"]
    assert "Moon 76% waxing gibbous" in r["bodyLabels"]
    assert "Jupiter -2.1" in r["bodyLabels"]
    # opens facing south (northern observer) 30 deg up, the default field of view
    assert r["read"].startswith("AZ 180.0 S  EL 30.0  FOV 75")
    assert float(r["afterDrag"].split()[1]) < 180
    assert "Night" in r["sunRead"]  # the fake Sun sits far below the horizon
    assert r["starsShown"] == [True, True, True]  # night: everything to 5.5 shows
    assert r["model"] is True
    # garbage ground height (-36990 m) is ignored: the eye stays 2 m up
    assert abs(r["eyeHeight"] - 2) < 0.01
    assert r["flights"] == 2  # the dive in and the flight back out
    assert r["exited"] is True and r["stoppedEsc"] is True
    assert r["restored"] == {"inputs": True, "collision": True, "moon": False, "sun": False,
                             "hud": "none"}
    assert any("Sky view: 30.270 N 97.740 W, Night" in line for line in r["feed"])
    # sun-driven sky lighting and no page labels while up (the sky view labels its own)
    assert r["lit"] == [True, True, True, False]
    assert r["nightGlow"] is False  # Sun far below -18 deg: no atmosphere glow at all
    # and the page's own settings come back on exit
    assert r["unlit"] == [False, False, False, True, True]


scenario("daylight", r"""
  var k = makeCtx();
  window.SDASkyView.init(k.ctx);
  Astronomy.Equator = function (body) {
    // put the Sun near the zenith over Austin at the recorded time
    if (body === 'Sun') return { ra: 0, dec: 0 };
    return { ra: 3, dec: 10 };
  };
  var rows = window.SDASkyView.eqjToEnuRows(ROT);
  // find the J2000 direction of the zenith: the third ENU row
  var z = rows[2];
  var ra = Math.atan2(z[1], z[0]) * 180 / Math.PI, dec = Math.asin(z[2]) * 180 / Math.PI;
  Astronomy.Equator = function (body) {
    if (body === 'Sun') return { ra: ((ra + 360) % 360) / 15, dec: dec };
    return { ra: 3, dec: 10 };
  };
  window.Astronomy = Astronomy;
  await window.SDASkyView.enter({ lat: 30.27, lon: -97.74 });
  var v = k.ctx.viewer;
  v._pre.forEach(function (fn) { fn(); });
  var stars = v.scene.primitives.list[0];
  var dayShown = stars.items.map(function (p) { return p.show; });
  var atmosphere = v.scene.skyAtmosphere.show;
  buttonNamed(document.body, 'DAYLIGHT').fire('click');
  var nightShown = stars.items.map(function (p) { return p.show; });
  return { dayShown: dayShown, nightShown: nightShown, atmosphereDay: atmosphere,
    atmosphereOff: v.scene.skyAtmosphere.show,
    sunRead: byClass(document.body, 'sky-dim')[0].textContent };
""")


def test_daylight_hides_faint_stars_until_removed(ran):
    r = ran["daylight"]
    assert r["dayShown"] == [False, False, False]  # no star beats a day sky (limit -1.5)
    assert r["atmosphereDay"] is True
    assert r["nightShown"] == [True, True, True] and r["atmosphereOff"] is False
    assert "Day" in r["sunRead"] or "mag 5.5" in r["sunRead"]


# --- the bundled data -----------------------------------------------------------------


def test_sky_data_is_complete_and_licensed():
    data = json.loads(SKY_JSON.read_text(encoding="utf-8"))
    assert "BSD-3-Clause" in data["license"] and "d3-celestial" in data["source"]
    assert len(data["stars"]) > 2500 and len(data["named"]) > 100
    assert len(data["constellations"]) >= 88 and len(data["lines"]) > 100
    for ra, dec, mag, _bv in data["stars"]:
        assert 0 <= ra < 360 and -90 <= dec <= 90 and mag <= 5.5
    names = {n[3] for n in data["named"]}
    assert {"Sirius", "Vega", "Polaris", "Betelgeuse"} <= names
    sirius = next(n for n in data["named"] if n[3] == "Sirius")
    assert abs(sirius[0] - SIRIUS["ra"]) < 0.01 and abs(sirius[1] - SIRIUS["dec"]) < 0.01


def test_module_pins_and_policy():
    text = MODULE.read_text(encoding="utf-8")
    assert "astronomy-engine@2.1.19/astronomy.browser.min.js" in text
    assert "sha384-LTVHvxfbG3XHla4Lu0C0s9U18B2sBOuzJIeOoVHm2t05SOOORvMbBMEVjXrGau9g" in text
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "skyview.js must stay ASCII"


# --- the Sun and the Moon on the globe's space view ----------------------------------

scenario("space_bodies", r"""
  var S = window.SDASkyView;
  var e = 23.4393 * Math.PI / 180, AU = 149597870700, MOON = 384400000;
  var sunI = [AU, 0, 0];
  var quarter = [0, MOON * Math.cos(e), MOON * Math.sin(e)];  // 90 deg east of the Sun
  var cam = [7000000, 0, 0];
  function dirTo(deg) { var r = deg * Math.PI / 180; return [Math.cos(r), Math.sin(r), 0]; }
  var moonAhead = [{ key: 'Moon', pos: [MOON, 0, 0] }];
  var both = [{ key: 'Sun', pos: [-AU, 0, 0] }, { key: 'Moon', pos: [MOON, 0, 0] }];
  var moonCard = S.describeSpaceBody('Moon', sunI, quarter, [0, 0, MOON]);
  var sunCard = S.describeSpaceBody('Sun', sunI, quarter, [0, -AU, 0]);
  return {
    lon: [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map(function (v) {
      return Math.round(S.eclipticLongitude(v) * 1000) / 1000; }),
    phase: Math.round(S.moonPhaseDeg(sunI, quarter) * 1000) / 1000,
    full: Math.round(S.moonPhaseDeg(sunI, [-MOON, 0, 0])),
    lit: [0, 90, 180].map(function (d) { return Math.round(S.litFraction(d) * 100); }),
    sub: S.subPoint([0, 0, 5]), subEq: S.subPoint([0, -3, 0]),
    onMoon: S.bodyUnderRay(dirTo(0), cam, moonAhead),
    nearMoon: S.bodyUnderRay(dirTo(1.0), cam, moonAhead),
    pastMoon: S.bodyUnderRay(dirTo(2.0), cam, moonAhead),
    onSun: S.bodyUnderRay(dirTo(178.0), cam, both),
    emptySky: S.bodyUnderRay(dirTo(90), cam, both),
    moonCard: moonCard, sunCard: sunCard,
    noViewer: S.spaceBodyAt(null, { x: 1, y: 1 })
  };
""")


def test_clicking_the_sun_or_moon_in_space_says_what_it_is(ran):
    """Cesium draws both on the globe view but cannot pick them; the page tests a
    click that misses the Earth against their directions (2026-11-20)."""
    r = ran["space_bodies"]
    assert r["lon"] == [0, 90, 90]
    assert r["phase"] == 90 and r["full"] == 180
    assert r["lit"] == [0, 50, 100]
    assert round(r["sub"]["lat"]) == 90 and round(r["subEq"]["lon"]) == -90
    assert r["onMoon"]["key"] == "Moon" and r["onMoon"]["angle"] < 1e-6
    assert round(r["onMoon"]["rangeKm"]) == 377400
    assert r["nearMoon"]["key"] == "Moon"  # inside the 1.2 degree reach
    assert r["pastMoon"] is None
    assert r["onSun"]["key"] == "Sun" and r["emptySky"] is None
    moon = dict(r["moonCard"]["rows"])
    assert r["moonCard"]["name"] == "The Moon" and r["moonCard"]["type"] == "Moon of Earth"
    assert moon["PHASE"] == "First quarter, 50% lit"
    assert moon["DISTANCE"] == "384,400 km from the centre of the Earth"
    assert moon["OVERHEAD AT"] == "90.0 N, 0.0 E"
    sun = dict(r["sunCard"]["rows"])
    assert r["sunCard"]["name"] == "The Sun" and r["sunCard"]["type"] == "Star"
    assert sun["DISTANCE"] == "1.000 AU, light takes 8.3 min"
    assert sun["OVERHEAD AT"] == "0.0 N, 90.0 W"
    assert "1.39 million km" in sun["WHAT"]
    assert r["noViewer"] is None


def test_the_page_asks_about_the_sun_and_moon_only_for_empty_clicks():
    page = (Path(__file__).resolve().parent.parent / "src" / "sda" / "templates"
            / "worldview.html").read_text(encoding="utf-8")
    start = page.index("handler.setInputAction(function(click) {")
    body = page[start : page.index("Cesium.ScreenSpaceEventType.LEFT_CLICK", start)]
    tail = body[body.rindex("} else {"):]
    assert "!Cesium.defined(picked)" in tail
    assert "SDASkyView.spaceBodyAt(viewer, click.position)" in tail
    assert "showInfoCard(body)" in tail and "deselectEntity()" in tail
