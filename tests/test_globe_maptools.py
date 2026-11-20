# ruff: noqa: E501  (the scenarios are JavaScript in raw strings, run in Node)
"""Tests for the globe map tools module (static/js/maptools.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_aurora.py does it. The
fake `fetch` answers from a table and nothing touches the network. Skips cleanly when
Node is not installed.

Reference values: geodesics and polygon areas from GeographicLib 2.1 (Karney) on the
display ellipsoid, UTM from PROJ 9 via pyproj 3.8, Vincenty's own Flinders Peak to
Buninyong example, and MGRS / UTM pairs from the gods-eye-view grid reference tests.

Each scenario is the body of an async function returning a JSON-able value, run in a
fresh vm context; the module-scoped `ran` fixture holds every result.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "maptools.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
var window = this;
var D2R = Math.PI / 180;
var downloads = [];
var clipLog = [];
var fetchLog = [];
var routes = [];

function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.type = ''; this.title = ''; this.checked = false; this.disabled = false;
  this.width = 0; this.height = 0; this.focused = false; this.ctxCalls = []; this.texts = [];
}
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); },
  set: function (v) {
    this.children.forEach(function (c) { c.parentNode = null; });
    this.children = []; this._text = v == null ? '' : String(v);
  }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { return ''; },
  set: function () { throw new Error('innerHTML is not allowed'); }
});
Object.defineProperty(El.prototype, 'firstChild', {
  get: function () { return this.children[0] || null; }
});
El.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this; this.children.push(c); return c;
};
El.prototype.removeChild = function (c) {
  var i = this.children.indexOf(c);
  if (i >= 0) this.children.splice(i, 1);
  c.parentNode = null; return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); };
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function (t, fn) {
  var l = this.listeners[t] || []; var i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
};
El.prototype.fire = function (t, ev) {
  var e = ev || {};
  if (!e.target) e.target = this;
  if (!e.preventDefault) e.preventDefault = function () { e.defaultPrevented = true; };
  if (!e.stopPropagation) e.stopPropagation = function () { e.stopped = true; };
  (this.listeners[t] || []).slice().forEach(function (fn) { fn.call(e.target, e); });
  return e;
};
El.prototype.click = function () {
  if (this.tagName === 'A') downloads.push({ href: this.href, name: this.download });
  return this.fire('click');
};
El.prototype.focus = function () { this.focused = true; };
El.prototype.getContext = function () {
  var self = this;
  var rec = function (name) { return function () { self.ctxCalls.push(name); }; };
  return {
    clearRect: rec('clearRect'), beginPath: rec('beginPath'), moveTo: rec('moveTo'), lineTo: rec('lineTo'),
    closePath: rec('closePath'), fill: rec('fill'), stroke: rec('stroke'),
    fillText: function (t) { self.ctxCalls.push('fillText'); self.texts.push(String(t)); },
    fillRect: rec('fillRect'), drawImage: rec('drawImage')
  };
};
El.prototype.toDataURL = function (mime) { return 'data:' + mime + ';base64,RkFLRQ=='; };

function findAll(root, pred, out) {
  var list = out || [];
  (root.children || []).forEach(function (c) { if (pred(c)) list.push(c); findAll(c, pred, list); });
  return list;
}
function byText(root, text, tag) {
  return findAll(root, function (c) { return c.tagName === (tag || 'BUTTON') && c.textContent === text; })[0] || null;
}
function byClass(root, cls) {
  return findAll(root, function (c) { return (' ' + c.className + ' ').indexOf(' ' + cls + ' ') >= 0; });
}

var document = {
  head: new El('head'), body: new El('body'), documentElement: new El('html'),
  listeners: {},
  createElement: function (t) { return new El(t); },
  getElementById: function (id) {
    var hit = findAll(this.head, function (c) { return c.id === id; }).concat(
      findAll(this.body, function (c) { return c.id === id; }));
    return hit[0] || null;
  },
  addEventListener: function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    var l = this.listeners[t] || []; var i = l.indexOf(fn); if (i >= 0) l.splice(i, 1);
  },
  fire: function (t, ev) {
    var e = ev || {};
    if (!e.target) e.target = document.body;
    e.preventDefault = function () { e.defaultPrevented = true; };
    e.stopPropagation = function () { e.stopped = true; };
    (this.listeners[t] || []).slice().forEach(function (fn) { fn(e); });
    return e;
  }
};
document.documentElement.requestFullscreen = function () { this.full = true; return Promise.resolve(); };

function makeStorage() {
  var data = {};
  return {
    data: data,
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem: function (k, v) { data[k] = String(v); },
    removeItem: function (k) { delete data[k]; }
  };
}
var localStorage = makeStorage();
var navigator = { clipboard: { writeText: function (t) { clipLog.push(t); return Promise.resolve(); } } };
var innerWidth = 1280;
var innerHeight = 800;
var blobs = [];
function Blob(parts, opts) { this.parts = parts; this.type = opts && opts.type; }
var URL = {
  createObjectURL: function (b) { blobs.push(b); return 'blob:fake/' + blobs.length; },
  revokeObjectURL: function () {}
};

function route(prefix, body, status) { routes.unshift({ prefix: prefix, body: body, status: status || 200 }); }
function fetch(url) {
  fetchLog.push(url);
  for (var i = 0; i < routes.length; i++) {
    if (url.indexOf(routes[i].prefix) === 0) {
      var r = routes[i];
      return Promise.resolve({ ok: r.status < 400, status: r.status, json: function () { return Promise.resolve(r.body); } });
    }
  }
  return Promise.reject(new Error('no route for ' + url));
}

var terrainCalls = [];
var terrainHeight = function () { return 0; };
var calls = [];

var Cesium = (function () {
  function Evt() { this.fns = []; }
  Evt.prototype.addEventListener = function (fn) {
    var self = this; this.fns.push(fn);
    return function () { var i = self.fns.indexOf(fn); if (i >= 0) self.fns.splice(i, 1); };
  };
  Evt.prototype.raise = function () {
    var a = arguments; this.fns.slice().forEach(function (f) { f.apply(null, a); });
  };
  function Entities() { this.values = []; }
  Entities.prototype.add = function (spec) {
    if (spec.id && this.values.some(function (e) { return e.id === spec.id; })) throw new Error('duplicate id ' + spec.id);
    var e = {}; Object.keys(spec).forEach(function (k) { e[k] = spec[k]; });
    this.values.push(e); return e;
  };
  Entities.prototype.remove = function (e) {
    var i = this.values.indexOf(e); if (i >= 0) { this.values.splice(i, 1); return true; } return false;
  };
  Entities.prototype.removeAll = function () { this.values = []; };
  function CustomDataSource(name) { this.name = name; this.entities = new Entities(); }
  function SSEH(canvas) { this.canvas = canvas; this.actions = {}; this.destroyed = false; }
  SSEH.prototype.setInputAction = function (fn, type) { this.actions[type] = fn; };
  SSEH.prototype.getInputAction = function (type) { return this.actions[type]; };
  SSEH.prototype.removeInputAction = function (type) { delete this.actions[type]; };
  SSEH.prototype.destroy = function () { this.destroyed = true; };
  function Color(css, alpha) { this.css = css; this.alpha = alpha == null ? 1 : alpha; }
  Color.prototype.withAlpha = function (a) { return new Color(this.css, a); };
  Color.fromCssColorString = function (s) { return new Color(s, 1); };
  Color.BLACK = new Color('#000000', 1);
  function EllipsoidTerrainProvider() { this.kind = 'ellipsoid'; }
  return {
    Evt: Evt,
    CustomDataSource: CustomDataSource,
    ScreenSpaceEventHandler: SSEH,
    ScreenSpaceEventType: { LEFT_DOWN: 0, LEFT_UP: 1, LEFT_CLICK: 2, LEFT_DOUBLE_CLICK: 3, RIGHT_CLICK: 7, MOUSE_MOVE: 15 },
    Cartesian2: function (x, y) { this.x = x; this.y = y; },
    Cartesian3: {
      fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; },
      fromDegreesArray: function (flat) {
        var out = []; for (var i = 0; i + 1 < flat.length; i += 2) out.push({ lon: flat[i], lat: flat[i + 1], h: 0 }); return out;
      },
      distance: function () { return 5000000; }
    },
    Cartographic: {
      fromCartesian: function (c) {
        return c && isFinite(c.lon) ? { longitude: c.lon * D2R, latitude: c.lat * D2R, height: c.h || 0 } : undefined;
      },
      fromDegrees: function (lon, lat, h) { return { longitude: lon * D2R, latitude: lat * D2R, height: h }; }
    },
    Color: Color,
    CallbackProperty: function (fn) { this.getValue = function () { return fn(); }; },
    PolygonHierarchy: function (positions) { this.positions = positions; },
    PolylineDashMaterialProperty: function (o) { this.options = o; },
    BoundingSphere: function (c, r) { this.center = c; this.radius = r; },
    HeadingPitchRange: function (h, p, r) { this.heading = h; this.pitch = p; this.range = r; },
    Rectangle: { fromDegrees: function (w, s, e, n) { return { w: w, s: s, e: e, n: n }; } },
    ArcType: { NONE: 0, GEODESIC: 1, RHUMB: 2 },
    HeightReference: { NONE: 0, CLAMP_TO_GROUND: 1 },
    LabelStyle: { FILL: 0, OUTLINE: 1, FILL_AND_OUTLINE: 2 },
    VerticalOrigin: { CENTER: 0, BOTTOM: 1 },
    SceneMode: { MORPHING: 0, COLUMBUS_VIEW: 1, SCENE2D: 2, SCENE3D: 3 },
    EllipsoidTerrainProvider: EllipsoidTerrainProvider,
    ArcGISTiledElevationTerrainProvider: {
      fromUrl: function (url) { terrainCalls.push({ kind: 'arcgis', url: url }); return Promise.resolve({ kind: 'arcgis', url: url }); }
    },
    createWorldTerrainAsync: function () { terrainCalls.push({ kind: 'ion' }); return Promise.resolve({ kind: 'ion' }); },
    Ion: { defaultAccessToken: undefined },
    sampleTerrainMostDetailed: function (provider, cartos) {
      cartos.forEach(function (c) { c.height = terrainHeight(c.latitude / D2R, c.longitude / D2R); });
      return Promise.resolve(cartos);
    },
    sampleTerrain: function (provider, level, cartos) { return Cesium.sampleTerrainMostDetailed(provider, cartos); },
    JulianDate: { toDate: function (t) { return new Date(t); } }
  };
})();

// Screen to globe: x 0..1000 maps to lon -10..10, y 0..600 to lat 6..-6 (0.02 degree a pixel).
function makeViewer() {
  var canvas = new El('canvas');
  canvas.width = 1000; canvas.height = 600; canvas.clientWidth = 1000; canvas.clientHeight = 600;
  var camera = {
    positionCartographic: { longitude: 0, latitude: 0, height: 5000000 },
    heading: 0.5, pitch: -1.2, roll: 0, positionWC: { lon: 0, lat: 0, h: 5000000 },
    getPickRay: function (pos) { return { pos: pos }; },
    pickEllipsoid: function (pos) {
      if (!pos || pos.x < 0 || pos.y < 0) return undefined;
      return { lon: -10 + pos.x * 0.02, lat: 6 - pos.y * 0.02, h: 0 };
    },
    zoomIn: function (a) { calls.push(['zoomIn', a]); },
    zoomOut: function (a) { calls.push(['zoomOut', a]); },
    flyTo: function (o) { calls.push(['flyTo', o]); },
    flyHome: function (d) { calls.push(['flyHome', d]); },
    flyToBoundingSphere: function (s, o) { calls.push(['flyToBoundingSphere', o.offset]); },
    setView: function (o) { calls.push(['setView', o]); },
    computeViewRectangle: function () { return { west: -10 * D2R, south: -6 * D2R, east: 10 * D2R, north: 6 * D2R }; },
    moveStart: new Cesium.Evt(), moveEnd: new Cesium.Evt()
  };
  var scene = {
    canvas: canvas, camera: camera, mode: 3, verticalExaggeration: 1,
    globe: { show: true, ellipsoid: {}, pick: function () { return undefined; } },
    postRender: new Cesium.Evt(), morphComplete: new Cesium.Evt(),
    morphTo2D: function () { this.mode = 2; calls.push(['morph', '2d']); },
    morphTo3D: function () { this.mode = 3; calls.push(['morph', '3d']); },
    morphToColumbusView: function () { this.mode = 1; calls.push(['morph', 'cv']); },
    requestRender: function () {}
  };
  var layers = [
    { show: true, alpha: 1, imageryProvider: { url: 'https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/x' } },
    { show: true, alpha: 0.6, sdaLabel: 'NASA overlay', imageryProvider: {} }
  ];
  var own = new Cesium.ScreenSpaceEventHandler(canvas);
  own.setInputAction(function () { calls.push(['trackEntity']); }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
  return {
    scene: scene, camera: camera,
    dataSources: {
      list: [],
      add: function (d) { this.list.push(d); return Promise.resolve(d); },
      remove: function (d) { var i = this.list.indexOf(d); if (i >= 0) this.list.splice(i, 1); return true; }
    },
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    imageryLayers: {
      list: layers,
      get length() { return this.list.length; },
      get: function (i) { return this.list[i]; },
      layerAdded: new Cesium.Evt(), layerRemoved: new Cesium.Evt(),
      layerMoved: new Cesium.Evt(), layerShownOrHidden: new Cesium.Evt()
    },
    screenSpaceEventHandler: own,
    clock: { currentTime: Date.parse('2026-06-21T08:24:00Z') }
  };
}

var intel = [];
var simDate = new Date('2026-06-21T08:24:00Z');
function makeCtx(opts) {
  var o = opts || {};
  var mount = new El('div');
  mount.id = 'ext-maptools';
  document.body.appendChild(mount);
  return {
    viewer: o.viewer || makeViewer(), mount: o.noMount ? undefined : mount, api: o.api || '',
    isStatic: !!o.isStatic, addIntelItem: function (t) { intel.push(t); },
    simNow: function () { return simDate; }
  };
}
function start(opts) {
  var ctx = makeCtx(opts);
  var ok = window.SDAMapTools.init(ctx);
  if (!ok) throw new Error('init failed: ' + window.SDAMapTools.lastError());
  return { ctx: ctx, mt: window.SDAMapTools.instance() };
}
function at(x, y) { return { x: x, y: y }; }
function lonX(lon) { return (lon + 10) / 0.02; }
function latY(lat) { return (6 - lat) / 0.02; }
function act(mt, type, pos) {
  var T = Cesium.ScreenSpaceEventType;
  var fn = mt.handler.actions[T[type]];
  if (type === 'MOUSE_MOVE') return fn({ endPosition: pos });
  return fn({ position: pos });
}
async function flush(n) { for (var i = 0; i < (n || 20); i++) await Promise.resolve(); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { setTimeout: setTimeout, clearTimeout: clearTimeout, Promise: Promise, JSON: JSON,
              Math: Math, Date: Date, encodeURIComponent: encodeURIComponent };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'maptools.js' });
  return box;
}

(async function () {
  var results = {};
  for (var name of Object.keys(scenarios)) {
    var t0 = Date.now();
    try {
      var box = load();
      var fn = vm.runInContext('(async function () {\n' + scenarios[name] + '\n})', box,
                               { filename: name });
      var value = await fn();
      if (value === undefined) value = null;
      results[name] = { ok: true, value: JSON.parse(JSON.stringify(value)), ms: Date.now() - t0 };
      try { vm.runInContext('window.SDAMapTools && window.SDAMapTools.destroy()', box); } catch (e) {}
    } catch (e) {
      results[name] = { ok: false, error: String((e && e.stack) || e), ms: Date.now() - t0 };
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
        assert result["ms"] < 5000, f"{name} took {result['ms']} ms"
        return result["value"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Ran:
    work = tmp_path_factory.mktemp("maptools")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- geodesics -----------------------------------------------------------------

scenario("geodesy", r"""
  var M = window.SDAMapTools;
  var lp = M.vincentyInverse(51.5074, -0.1278, 48.8566, 2.3522);
  var fl = [-(37 + 57 / 60 + 3.72030 / 3600), 144 + 25 / 60 + 29.52440 / 3600];
  var bu = [-(37 + 39 / 60 + 10.15610 / 3600), 143 + 55 / 60 + 35.38390 / 3600];
  var fb = M.vincentyInverse(fl[0], fl[1], bu[0], bu[1]);
  var direct = M.vincentyDirect(fl[0], fl[1], 306 + 52 / 60 + 5.37 / 3600, 54972.271);
  var anti = M.vincentyInverse(0.5, 0, -0.5, 179.7);
  return {
    haversine: M.haversineDistance(51.5074, -0.1278, 48.8566, 2.3522),
    vincenty: lp.distance, az1: lp.azimuth1, converged: lp.converged,
    flinders: fb.distance, flAz1: M.toDMS(fb.azimuth1, 'lat', 2), flAz2: M.toDMS(fb.azimuth2, 'lat', 2),
    direct: [direct.lat - bu[0], direct.lon - bu[1]],
    anti: anti.distance, antiConverged: anti.converged,
    same: M.vincentyInverse(10, 20, 10, 20).distance,
    path: M.pathLength([[-0.1278, 51.5074], [2.3522, 48.8566], { lat: 51.5074, lon: -0.1278 }]),
    bearing: M.initialBearing(51.5074, -0.1278, 48.8566, 2.3522),
    ring: M.ringPoints(0, 0, 10000, 36).map(function (p) { return M.geodesicDistance(0, 0, p[1], p[0]); })
  };
""")


def test_great_circle_and_vincenty_london_paris(ran):
    r = ran["geodesy"]
    assert abs(r["haversine"] / 1000 - 343.5) < 0.5
    # GeographicLib: 343923.1200909893 m
    assert abs(r["vincenty"] - 343923.12) < 1.0
    assert r["converged"] is True
    assert 147.5 < r["az1"] < 148.5
    assert abs(r["path"] - 2 * 343923.12) < 2.0
    assert abs(r["bearing"] - r["az1"]) < 1e-9


def test_vincenty_published_flinders_buninyong(ran):
    r = ran["geodesy"]
    assert abs(r["flinders"] - 54972.271) < 0.005
    assert r["flAz1"] == "306\u00b052'05.37\"N"
    # Forward azimuth at Buninyong; the published reverse azimuth is 127 10 25.07.
    assert r["flAz2"] == "307\u00b010'25.07\"N"
    assert abs(r["direct"][0]) < 1e-7 and abs(r["direct"][1]) < 1e-7


def test_near_antipodal_falls_back_to_sphere(ran):
    r = ran["geodesy"]
    # GeographicLib: 19995624.889961265 m
    assert r["antiConverged"] is False
    assert abs(r["anti"] - 19995624.89) / 19995624.89 < 0.005
    assert r["same"] == 0
    assert all(abs(d - 10000) < 0.01 for d in r["ring"])


scenario("areas", r"""
  var M = window.SDAMapTools;
  return {
    square: M.polygonArea([[0, 0], [1, 0], [1, 1], [0, 1]]),
    closed: M.polygonArea([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]),
    reversed: M.polygonArea([[0, 1], [1, 1], [1, 0], [0, 0]]),
    anti: M.polygonArea([[179.5, 10], [-179.5, 10], [-179.5, 11], [179.5, 11]]),
    big: M.polygonArea([[-10, 40], [20, 40], [20, 60], [-10, 60]]),
    pole: M.polygonArea([[0, 80], [90, 80], [180, 80], [-90, 80]]),
    degenerate: [M.polygonArea([[0, 0], [1, 1]]), M.polygonArea(null), M.polygonArea([[0, 0], [1, 1], [0, 95]])]
  };
""")


def test_polygon_area_against_geographiclib(ran):
    r = ran["areas"]
    ref = 12308778361.469452  # 1 degree square at the equator
    assert abs(r["square"] - ref) / ref < 5e-4
    assert r["closed"] == pytest.approx(r["square"], rel=1e-12)
    assert r["reversed"] == pytest.approx(r["square"], rel=1e-9)
    assert abs(r["anti"] - 12108467312.577087) / 12108467312.577087 < 1e-3
    assert abs(r["big"] - 4679193994572.729) / 4679193994572.729 < 1e-3
    # A ring around the pole gets the polar cap, not the rest of the world.
    assert abs(r["pole"] - 2507270031169.875) / 2507270031169.875 < 1e-3
    assert r["degenerate"] == [0, 0, 0]


# --- coordinate formats ----------------------------------------------------------

scenario("dms", r"""
  var M = window.SDAMapTools;
  return {
    london: [M.toDMS(51.5074, 'lat'), M.toDMS(-0.1278, 'lon')],
    eiffel: [M.toDMS(48.8584, 'lat'), M.toDMS(2.2945, 'lon')],
    carry: M.toDMS(10.999999, 'lat', 1),
    south0: M.toDMS(-33.85678, 'lat', 0),
    junk: M.toDMS(NaN, 'lat'),
    hemi: M.formatHemi(-33.8568, 151.2153, 4),
    decimal: M.formatDecimal(51.5074, -0.1278, 5)
  };
""")


def test_dms_formatting(ran):
    r = ran["dms"]
    assert r["london"] == ["51\u00b030'26.6\"N", "0\u00b007'40.1\"W"]
    assert r["eiffel"] == ["48\u00b051'30.2\"N", "2\u00b017'40.2\"E"]
    assert r["carry"] == "11\u00b000'00.0\"N"
    assert r["south0"] == "33\u00b051'24\"S"
    assert r["junk"] == ""
    assert r["hemi"] == "33.8568\u00b0S 151.2153\u00b0E"
    assert r["decimal"] == "51.50740, -0.12780"


scenario("utm_mgrs", r"""
  var M = window.SDAMapTools;
  var rows = [
    [40.7488215, -73.9854319, '18T WL 85650 11369'],
    [-33.8567842, 151.2152996, '56H LH 34900 52290'],
    [78.2232003, 15.6266906, '33X WG 14278 83355'],
    [60.392994, 5.3241531, '32V KN 97477 00830']
  ];
  var utm = [[51.5074, -0.1278], [-33.8568, 151.2153], [60.39299, 5.32415], [78.2232, 15.6267], [-54.8, -68.3], [0.5, 179.9]]
    .map(function (p) { var u = M.latLonToUtm(p[0], p[1]); return [u.zone, u.band, u.easting, u.northing]; });
  var trips = [[51.5074, -0.1278], [-33.8568, 151.2153], [0.0001, 9.9e-5], [-79.5, 100.2], [83.9, -40.1],
               [63.9, 11.9], [56.1, 3.2], [72.5, 8.9], [12.3456, -179.99], [-45, 170]].map(function (p) {
    var g = M.toMGRS(p[0], p[1], 5);
    var back = M.parseMGRS(g);
    return { g: g, err: back ? M.geodesicDistance(p[0], p[1], back.lat, back.lon) : null };
  });
  var times = { lat: 40.74882, lon: -73.98543 };
  function off(text) { var p = M.parseMGRS(text); return p ? M.geodesicDistance(p.lat, p.lon, times.lat, times.lon) : null; }
  return {
    forward: rows.map(function (r) { return [M.toMGRS(r[0], r[1], 5), r[2]]; }),
    utm: utm,
    utmText: M.formatUTM(51.5074, -0.1278),
    trips: trips,
    forms: ['18TWL8565011369', '18T WL 8565011369', '18T WL 85650 11369', '18twl8565011369'].map(off),
    coarse: [off('18TWL856113'), off('18T WL 856 113'), off('18TWL85651136')],
    declined: ['18TWL8511', '18T WL 85 11', '18TWL', '1 JAN 2023 1200', '18T SL 50000 50000',
               '32X NM 12345 54321', '18t wl 85650 11369', '61T WL 85650 11369'].map(M.parseMGRS),
    norway: !!M.parseMGRS('32V NM 12345 54321'), svalbard: !!M.parseMGRS('33X VG 12345 67890'),
    digits: [M.toMGRS(40.7488215, -73.9854319, 1), M.toMGRS(40.7488215, -73.9854319, 3)],
    polar: [M.toMGRS(85, 0), M.toMGRS(-81, 0), M.latLonToUtm(85, 0)],
    zones: [M.utmZone(60, 5), M.utmZone(60, 2), M.utmZone(78, 10), M.utmZone(10, 180), M.bandLetter(84), M.bandLetter(-80)],
    utmParse: [M.parseUTM('18T 585628 4511322'), M.parseUTM('33N 500000 4500000'), M.parseUTM('18T 50000 4511322')]
  };
""")


def test_utm_matches_proj(ran):
    r = ran["utm_mgrs"]
    # PROJ 9 (pyproj 3.8), EPSG:326xx / 327xx
    ref = [
        (30, "U", 699316.234, 5710163.758),
        (56, "H", 334900.570, 6252288.753),
        (32, "V", 297477.307, 6700830.063),
        (33, "X", 514278.715, 8683355.469),
        (19, "F", 545000.053, 3927239.381),
        (60, "N", 822823.963, 55336.378),
    ]
    for got, want in zip(r["utm"], ref, strict=True):
        assert got[0] == want[0] and got[1] == want[1]
        assert abs(got[2] - want[2]) < 0.005 and abs(got[3] - want[3]) < 0.005
    assert r["utmText"] == "30U 699316 5710163"


def test_mgrs_forward_and_round_trip(ran):
    r = ran["utm_mgrs"]
    for got, want in r["forward"]:
        assert got == want
    for trip in r["trips"]:
        assert trip["g"], trip
        assert trip["err"] is not None and trip["err"] < 1.5, trip
    assert all(d is not None and d < 3 for d in r["forms"])
    assert r["coarse"][0] < 71 and r["coarse"][1] < 71 and r["coarse"][2] < 8
    assert r["declined"] == [None] * 8
    assert r["norway"] and r["svalbard"]
    assert r["digits"] == ["18T WL 8 1", "18T WL 856 113"]
    assert r["polar"] == ["", "", None]
    assert r["zones"] == [32, 31, 33, 60, "X", "C"]
    utm_hit, hemi, bad = r["utmParse"]
    assert abs(utm_hit["lat"] - 40.7488) < 0.001 and abs(utm_hit["lon"] + 73.9857) < 0.001
    assert 40 < hemi["lat"] < 41 and 14 < hemi["lon"] < 16
    assert bad is None


scenario("coords", r"""
  var M = window.SDAMapTools;
  var good = [
    '51.5074, -0.1278', '51.5074 -0.1278', '51.5074N 0.1278W', 'N 51.5074, W 0.1278', 'W 0.1278 N 51.5074',
    "51\u00b030'26.6\"N 0\u00b007'40.1\"W", "51\u00b030.443'N, 0\u00b07.668'W", '51 30 26.6 N 0 07 40.1 W',
    'geo:51.5074,-0.1278', 'geo:51.5074,-0.1278,35;u=10',
    'https://www.google.com/maps/@51.5074,-0.1278,15z', 'https://www.openstreetmap.org/#map=15/51.5074/-0.1278',
    'https://maps.google.com/?q=51.5074,-0.1278', 'https://www.openstreetmap.org/?mlat=51.5074&mlon=-0.1278',
    '30U XC 99316 10163', '30U 699316 5710163', '9C3XGV4C+XV'
  ].map(function (t) { var p = M.parseCoordinate(t); return p ? [t, p.lat, p.lon, p.kind, p.label] : [t, null]; });
  var bad = ['London', '12junk, 34oops', '91, 0', '0, 181', '-40N, 70W', '40N 50N', '', '1e3, 5',
    '1 JAN 2023 1200', '1.2.3, 4', "40\u00b075'N 70W", 'N40 S50', 'geo:91,0', null].map(M.parseCoordinate);
  return { good: good, bad: bad, plus: M.parsePlusCode('849VCWC8+R9') };
""")


def test_coordinate_box_reads_every_format(ran):
    r = ran["coords"]
    kinds = {}
    for text, lat, lon, *rest in r["good"]:
        assert lat is not None, text
        tol = 0.0006 if "+" in text else 2e-4
        assert abs(lat - 51.5074) < tol and abs(lon + 0.1278) < tol, (text, lat, lon)
        kinds[text] = rest[0]
    assert kinds["51.5074, -0.1278"] == "decimal"
    assert kinds["51 30 26.6 N 0 07 40.1 W"] == "dms"
    assert kinds["geo:51.5074,-0.1278"] == "geo"
    assert kinds["30U XC 99316 10163"] == "mgrs"
    assert kinds["30U 699316 5710163"] == "utm"
    assert kinds["9C3XGV4C+XV"] == "pluscode"
    assert kinds["https://www.google.com/maps/@51.5074,-0.1278,15z"] == "url"
    assert r["good"][0][4] == "51.50740\u00b0N 0.12780\u00b0W"
    assert abs(r["plus"]["lat"] - 37.4220625) < 1e-6 and abs(r["plus"]["lon"] + 122.0840625) < 1e-6


def test_coordinate_box_declines_junk(ran):
    assert ran["coords"]["bad"] == [None] * 14


# --- exchange formats ------------------------------------------------------------

scenario("roundtrip", r"""
  var M = window.SDAMapTools;
  var list = [
    { id: 'a1', kind: 'pin', name: '<Alpha & "Bravo">', color: '#ff4040', points: [[2.2945, 48.8584]], created: 1760000000000 },
    { id: 'a2', kind: 'line', name: 'Leg 1', color: '#00e676', tool: 'measure', points: [[-0.1278, 51.5074], [2.3522, 48.8566]], created: 1760000000000 },
    { id: 'a3', kind: 'polygon', name: 'Box', color: '#00d4ff', points: [[0, 0], [1, 0], [1, 1], [0, 1]], created: 1760000000000 },
    { id: 'a4', kind: 'circle', name: 'Rings', color: '#ffb000', radii: [5000, 1000], points: [[-73.9857, 40.7484]], created: 1760000000000 }
  ];
  function strip(ds) { return ds.map(function (d) { return [d.kind, d.name, d.color, d.points, d.radii, d.tool]; }); }
  var gj = M.drawingsToGeoJSON(list);
  var gjBack = M.geoJSONToDrawings(JSON.stringify(gj));
  var kml = M.drawingsToKML(list);
  var kmlBack = M.kmlToDrawings(kml);
  return {
    want: strip(list.map(M.sanitizeDrawing)),
    geojson: strip(gjBack.drawings), gjSkipped: gjBack.skipped,
    types: gj.features.map(function (f) { return f.geometry.type; }),
    circleRings: gj.features[3].geometry.geometries.length,
    kml: strip(kmlBack.drawings), kmlSkipped: kmlBack.skipped,
    kmlHasColor: kml.indexOf('<color>ff4040ff</color>') >= 0 && kml.indexOf('<color>ff00b0ff</color>') >= 0,
    kmlEscaped: kml.indexOf('&lt;Alpha &amp; &quot;Bravo&quot;&gt;') >= 0,
    detected: [M.detectFormat(JSON.stringify(gj), 'x.geojson'), M.detectFormat(kml, 'doc.kml'), M.detectFormat(kml, ''),
               M.detectFormat('<gpx version="1.1"></gpx>', ''), M.detectFormat('hello', 'a.txt'), M.detectFormat('', 'a.kmz')]
  };
""")


def test_geojson_and_kml_round_trip(ran):
    r = ran["roundtrip"]
    want = r["want"]
    assert want[3][4] == [1000, 5000]  # radii sorted
    assert r["geojson"] == want and r["gjSkipped"] == 0
    assert r["kml"] == want and r["kmlSkipped"] == 0
    assert r["types"] == ["Point", "LineString", "Polygon", "GeometryCollection"]
    assert r["circleRings"] == 3
    assert r["kmlHasColor"] and r["kmlEscaped"]
    assert r["detected"] == ["geojson", "kml", "kml", "gpx", "", "kmz"]


scenario("third_party", r"""
  var M = window.SDAMapTools;
  var kml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<!DOCTYPE kml [ <!ENTITY x "y"> ]>',
    '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>',
    '<!-- a comment with <Placemark> inside -->',
    '<Style id="red"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>',
    "<StyleMap id='pair'><Pair><key>normal</key><styleUrl>#red</styleUrl></Pair>",
    '<Pair><key>highlight</key><styleUrl>#blue</styleUrl></Pair></StyleMap>',
    '<Folder><Placemark><name><![CDATA[Trail <one>]]></name><styleUrl>#pair</styleUrl>',
    '<LineString><coordinates>-122.1,37.4,10 -122.2,37.5,12\n -122.3,37.6</coordinates></LineString></Placemark>',
    '<Placemark><name>Lake &amp; shore</name><Style><PolyStyle><color>8000ff00</color></PolyStyle></Style>',
    '<Polygon><outerBoundaryIs><LinearRing><coordinates>1,1 2,1 2,2 1,2 1,1</coordinates></LinearRing></outerBoundaryIs>',
    '<innerBoundaryIs><LinearRing><coordinates>1.2,1.2 1.4,1.2 1.4,1.4 1.2,1.2</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>',
    '<Placemark><name>Drive</name><gx:Track><when>2026-01-01T00:00:00Z</when><gx:coord>10 20 0</gx:coord><gx:coord>10.1 20.1 0</gx:coord></gx:Track></Placemark>',
    '<Placemark><name>Pair</name><MultiGeometry><Point><coordinates>5,6</coordinates></Point><Point><coordinates>7,8</coordinates></Point></MultiGeometry></Placemark>',
    '<Placemark><name>Nowhere</name></Placemark>',
    '<Placemark><name>Off the map</name><Point><coordinates>200,95</coordinates></Point></Placemark>',
    '</Folder></Document></kml>'
  ].join('\n');
  var res = M.kmlToDrawings(kml);
  var errors = [];
  ['{"type": "Feature"', '<kml><Document>', 'just text'].forEach(function (t, i) {
    try { M.importDrawings(t, ['a.geojson', 'b.kmz', 'c.txt'][i]); errors.push(null); } catch (e) { errors.push(e.message); }
  });
  var gj = M.geoJSONToDrawings({ type: 'FeatureCollection', features: [
    { type: 'Feature', properties: { title: 'Two', 'marker-color': '#0f0' }, geometry: { type: 'MultiPoint', coordinates: [[1, 2], [3, 4]] } },
    { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: [[[0, 0], [1, 1]], [[2, 2], [3, 3]]] } },
    { type: 'Feature', properties: {}, geometry: { type: 'MultiPolygon', coordinates: [[[[0, 0], [1, 0], [1, 1], [0, 0]]]] } },
    { type: 'Feature', properties: {}, geometry: { type: 'GeometryCollection', geometries: [{ type: 'Point', coordinates: [9, 9] }] } },
    { type: 'Feature', properties: {}, geometry: { type: 'Circle', coordinates: [0, 0] } },
    { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [[0, 0]] } },
    { type: 'Nope' }
  ] });
  var bare = M.geoJSONToDrawings({ type: 'Point', coordinates: [5, 5] });
  return {
    drawings: res.drawings.map(function (d) { return [d.kind, d.name, d.color, d.points]; }),
    skipped: res.skipped, errors: errors,
    gj: gj.drawings.map(function (d) { return [d.kind, d.name, d.color, d.points.length]; }), gjSkipped: gj.skipped,
    bare: bare.drawings.length
  };
""")


def test_third_party_kml_and_geojson(ran):
    r = ran["third_party"]
    assert r["drawings"] == [
        ["line", "Trail <one>", "#ff0000", [[-122.1, 37.4], [-122.2, 37.5], [-122.3, 37.6]]],
        ["polygon", "Lake & shore", "#00ff00", [[1, 1], [2, 1], [2, 2], [1, 2]]],
        ["line", "Drive", "#ffb000", [[10, 20], [10.1, 20.1]]],
        ["pin", "Pair", "#ffb000", [[5, 6]]],
        ["pin", "Pair", "#ffb000", [[7, 8]]],
    ]
    assert r["skipped"] == 2
    assert "not valid JSON" in r["errors"][0]
    assert "KMZ" in r["errors"][1]
    assert "Not a GeoJSON, KML or GPX" in r["errors"][2]
    assert r["gj"] == [
        ["pin", "Two", "#00ff00", 1], ["pin", "Two", "#00ff00", 1],
        ["line", "", "#ffb000", 2], ["line", "", "#ffb000", 2],
        ["polygon", "", "#ffb000", 3], ["pin", "", "#ffb000", 1],
    ]
    assert r["gjSkipped"] == 3
    assert r["bare"] == 1


scenario("gpx", r"""
  var M = window.SDAMapTools;
  var gpx = [
    '<?xml version="1.0"?>',
    '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">',
    '<wpt lat="46.5" lon="7.9"><name>Summit</name><ele>4158</ele></wpt>',
    '<wpt lat="95" lon="7.9"><name>Bad</name></wpt>',
    '<wpt lat="" lon="7.9"/>',
    '<rte><name>Approach</name><rtept lat="46.4" lon="7.8"/><rtept lat="46.45" lon="7.85"/></rte>',
    '<trk><name>Day 1</name><trkseg><trkpt lat="46.0" lon="7.0"><ele>1000</ele></trkpt><trkpt lat="46.1" lon="7.1"></trkpt></trkseg>',
    '<trkseg><trkpt lat="46.2" lon="7.2"/><trkpt lat="46.3" lon="7.3"/><trkpt lat="x" lon="7.4"/></trkseg>',
    '<trkseg><trkpt lat="46.9" lon="7.9"/></trkseg></trk>',
    '</gpx>'
  ].join('\n');
  var res = M.importDrawings(gpx, 'walk.gpx');
  var err = null;
  try { M.gpxToDrawings('<kml></kml>'); } catch (e) { err = e.message; }
  return { format: res.format, skipped: res.skipped, err: err,
           drawings: res.drawings.map(function (d) { return [d.kind, d.name, d.points]; }) };
""")


def test_gpx_import(ran):
    r = ran["gpx"]
    assert r["format"] == "gpx"
    assert r["drawings"] == [
        ["pin", "Summit", [[7.9, 46.5]]],
        ["line", "Approach", [[7.8, 46.4], [7.85, 46.45]]],
        ["line", "Day 1 (1)", [[7, 46], [7.1, 46.1]]],
        ["line", "Day 1 (2)", [[7.2, 46.2], [7.3, 46.3]]],
    ]
    # Two waypoints out of range or blank, and a one-point segment.
    assert r["skipped"] == 3
    assert "no <gpx>" in r["err"]


# --- stores ---------------------------------------------------------------------

scenario("stores", r"""
  var M = window.SDAMapTools;
  var st = makeStorage();
  var a = M.createBookmarkStore(st, 'bm');
  var v = { lat: 51.5, lon: -0.12, height: 12000, heading: 1, pitch: -0.8, roll: 0 };
  var one = a.add('London', v);
  var two = a.add('', { lat: -33.86, lon: 151.21, height: 5000 });
  var refused = a.add('bad', { lat: 95, lon: 0, height: 10 });
  var b = M.createBookmarkStore(st, 'bm');
  var afterReload = b.list().map(function (x) { return [x.name, x.view.lat, x.view.height, x.view.pitch]; });
  b.rename(one.id, 'Big Smoke');
  b.remove(two.id);
  var c = M.createBookmarkStore(st, 'bm');
  st.setItem('broken', '{not json');
  var broken = M.createBookmarkStore(st, 'broken').list().length;
  st.setItem('mixed', JSON.stringify([{ name: 'ok', view: v }, { name: 'no view' }, 7, null]));
  var mixed = M.createBookmarkStore(st, 'mixed').list().map(function (x) { return x.name; });

  var ds = M.createDrawingStore(st, 'dr');
  var p = ds.add({ kind: 'pin', points: [[1, 2]], name: 'p' });
  var l = ds.add({ kind: 'line', points: [[0, 0], [1, 1]], color: '#abc' });
  var rejects = [
    ds.add({ kind: 'line', points: [[0, 0]] }), ds.add({ kind: 'circle', points: [[0, 0]], radii: [] }),
    ds.add({ kind: 'blob', points: [[0, 0]] }), ds.add(null), ds.add({ kind: 'pin', points: [[0, 100]] })
  ];
  var dup = ds.add({ id: p.id, kind: 'pin', points: [[3, 4]] });
  ds.update(l.id, { name: 'renamed', color: '#ff0000' });
  var reloaded = M.createDrawingStore(st, 'dr').list().map(function (d) { return [d.kind, d.name, d.color]; });
  var last = ds.removeLast();
  var count = M.createDrawingStore(st, 'dr').list().length;
  ds.clear();
  return {
    first: [one.name, two.name], refused: refused, afterReload: afterReload,
    renamed: c.list().map(function (x) { return x.name; }), broken: broken, mixed: mixed,
    color: l.color, rejects: rejects, dupNewId: dup.id !== p.id, reloaded: reloaded,
    lastWasDup: last.id === dup.id, count: count, cleared: M.createDrawingStore(st, 'dr').list().length,
    mem: (function () { var s = M.memoryStorage(); s.setItem('k', 1); return [s.getItem('k'), s.getItem('x')]; })()
  };
""")


def test_bookmark_and_drawing_persistence(ran):
    r = ran["stores"]
    assert r["first"] == ["London", "33.860\u00b0S 151.210\u00b0E"]
    assert r["refused"] is None
    assert r["afterReload"] == [
        ["London", 51.5, 12000, -0.8],
        ["33.860\u00b0S 151.210\u00b0E", -33.86, 5000, pytest.approx(-1.5707963, abs=1e-6)],
    ]
    assert r["renamed"] == ["Big Smoke"]
    assert r["broken"] == 0
    assert r["mixed"] == ["ok"]
    assert r["color"] == "#aabbcc"
    assert r["rejects"] == [None] * 5
    assert r["dupNewId"] is True
    assert r["reloaded"] == [["pin", "p", "#ffb000"], ["line", "renamed", "#ff0000"], ["pin", "", "#ffb000"]]
    assert r["lastWasDup"] is True and r["count"] == 2 and r["cleared"] == 0
    assert r["mem"] == ["1", None]


# --- scale, units, sun, sight lines, grid ---------------------------------------------

scenario("units", r"""
  var M = window.SDAMapTools;
  return {
    scale: M.niceScale(12.3, 100),
    tiny: M.niceScale(0.004, 100),
    none: [M.niceScale(0, 100), M.niceScale(NaN, 100), M.niceScale(-1, 100)],
    dist: [M.formatDistance(5.25), M.formatDistance(950), M.formatDistance(343923.12), M.formatDistance(1234567),
           M.formatDistance(100, 'imperial'), M.formatDistance(343923.12, 'imperial'),
           M.formatDistance(150, 'nautical'), M.formatDistance(343923.12, 'nautical')],
    area: [M.formatArea(500), M.formatArea(250000), M.formatArea(12308778361), M.formatArea(2000, 'imperial'),
           M.formatArea(100000, 'imperial'), M.formatArea(12308778361, 'imperial'), M.formatArea(12308778361, 'nautical')],
    bearing: [M.formatBearing(359.99), M.formatBearing(45), M.formatBearing(-90), M.compassPoint(200)],
    radii: [M.parseRadii('1, 5, 10 km', 'metric'), M.parseRadii('500 m 2 km 3 mi', 'metric'), M.parseRadii('2, 3', 'imperial'),
            M.parseRadii('1 NM', 'metric'), M.parseRadii('10 kmh', 'metric'), M.parseRadii('', 'metric'),
            M.parseRadii('5, 5, 0, 30000 km', 'metric')],
    eyes: [M.parseEyeHeights('2, 10'), M.parseEyeHeights('5'), M.parseEyeHeights('a'), M.parseEyeHeights('1 2 3')]
  };
""")


def test_scale_bar_and_unit_formatting(ran):
    r = ran["units"]
    s = r["scale"]
    assert s["metric"]["label"] == "1 km" and s["metric"]["px"] == pytest.approx(81.3, abs=0.05)
    assert s["imperial"]["label"] == "2,000 ft" and s["imperial"]["px"] == pytest.approx(49.56, abs=0.05)
    assert r["tiny"]["metric"]["label"] == "0.2 m"
    assert r["none"] == [None, None, None]
    assert r["dist"] == ["5.3 m", "950 m", "343.9 km", "1,235 km", "328 ft", "213.7 mi", "150 m", "185.7 NM"]
    assert r["area"] == ["500 sq m", "25.00 ha", "12,309 sq km", "21,528 sq ft", "24.7 ac", "4,752.4 sq mi", "3,588.7 sq NM"]
    assert r["bearing"] == ["0.0\u00b0 N", "45.0\u00b0 NE", "270.0\u00b0 W", "SSW"]
    assert r["radii"] == [
        [1000, 5000, 10000], [500, 2000, 3 * 1609.344], [2 * 1609.344, 3 * 1609.344],
        [1852], None, [], [5000],
    ]
    assert r["eyes"] == [[2, 10], [5, 5], None, None]


scenario("sun", r"""
  var M = window.SDAMapTools;
  var solstice = M.subsolarPoint(new Date('2026-06-21T08:24:00Z'));
  var equinox = M.subsolarPoint(new Date('2026-03-20T14:46:00Z'));
  var noon = M.subsolarPoint(new Date('2026-03-20T12:00:00Z'));
  var ring = M.terminatorPoints(new Date('2026-06-21T08:24:00Z'), 0.833, 90);
  var dists = ring.map(function (p) { return M.angularDistance(solstice.lat, solstice.lon, p[1], p[0]); });
  var civil = M.terminatorPoints(new Date('2026-06-21T08:24:00Z'), 6, 36).map(function (p) {
    return M.angularDistance(solstice.lat, solstice.lon, p[1], p[0]);
  });
  return { solstice: solstice, equinox: equinox, noon: noon, n: ring.length,
           closed: ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1],
           minD: Math.min.apply(null, dists), maxD: Math.max.apply(null, dists),
           civil: [Math.min.apply(null, civil), Math.max.apply(null, civil)], bad: M.subsolarPoint('x') };
""")


def test_subsolar_point_and_terminator(ran):
    r = ran["sun"]
    assert abs(r["solstice"]["lat"] - 23.44) < 0.05
    assert abs(r["equinox"]["lat"]) < 0.05
    # Equation of time is about -7.5 minutes on 20 March: the sun crosses 0E late.
    assert abs(r["noon"]["lon"] - 1.85) < 0.3
    assert r["n"] == 91 and r["closed"]
    assert abs(r["minD"] - 90.833) < 1e-6 and abs(r["maxD"] - 90.833) < 1e-6
    assert abs(r["civil"][0] - 96) < 1e-6 and abs(r["civil"][1] - 96) < 1e-6
    assert r["bad"] is None


scenario("los", r"""
  var M = window.SDAMapTools;
  function flat(D, n, hill) {
    var out = [];
    for (var i = 0; i <= n; i++) {
      var d = D * i / n;
      out.push({ d: d, h: hill && Math.abs(d - D / 2) < D / 20 ? hill : 0 });
    }
    return out;
  }
  var pp = M.profilePoints([[0, 0], [0.1, 0], [0.1, 0.1]], 50);
  var mono = pp.every(function (p, i) { return !i || p.d > pp[i - 1].d; });
  return {
    clear10: M.lineOfSight(flat(10000, 200), 2, 2),
    blocked12: M.lineOfSight(flat(12000, 200), 2, 2),
    hill: M.lineOfSight(flat(5000, 200, 30), 10, 10),
    overHill: M.lineOfSight(flat(5000, 200, 30), 40, 40),
    noRefraction: M.lineOfSight(flat(10500, 200), 2, 2, { k: 0 }),
    tooShort: M.lineOfSight([{ d: 0, h: 0 }], 2, 2),
    pp: { n: pp.length, mono: mono, first: [pp[0].lon, pp[0].lat], last: [pp[pp.length - 1].lon, pp[pp.length - 1].lat],
          total: pp[pp.length - 1].d, path: M.pathLength([[0, 0], [0.1, 0], [0.1, 0.1]]) },
    stats: M.profileStats([{ d: 0, h: 100 }, { d: 10, h: 150 }, { d: 20, h: 120 }, { d: 30, h: 200 }])
  };
""")


def test_line_of_sight_curvature_and_terrain(ran):
    r = ran["los"]
    # Two 2 m eyes on flat ground with k = 0.13 see each other to about 10.8 km.
    assert r["clear10"]["visible"] is True
    assert r["blocked12"]["visible"] is False
    # The bulge x(D - x) / 2R' passes 2 m between 3.41 km and 8.59 km; the deepest point is mid-path.
    assert 3400 < r["blocked12"]["blockedAt"] < 3480
    assert r["blocked12"]["minClearanceAt"] == pytest.approx(6000, abs=60)
    assert r["blocked12"]["minClearance"] == pytest.approx(2 - 6000 * 6000 * 0.87 / (2 * 6371008.8), abs=0.01)
    assert r["hill"]["visible"] is False and r["overHill"]["visible"] is True
    # Without refraction the 2 m horizon is about 5.05 km each way.
    assert r["noRefraction"]["visible"] is False
    assert r["tooShort"] is None


def test_profile_sampling_and_stats(ran):
    r = ran["los"]
    pp = r["pp"]
    assert pp["n"] == 50 and pp["mono"]
    assert pp["first"] == [0, 0] and pp["last"] == [0.1, 0.1]
    assert abs(pp["total"] - pp["path"]) < 1e-6
    assert r["stats"] == {"min": 100, "max": 200, "gain": 130, "loss": 30, "distance": 30}


scenario("grid", r"""
  var M = window.SDAMapTools;
  var world = M.graticuleLines(null, 30);
  var anti = M.graticuleLines({ west: 170, south: -3, east: -170, north: 3 }, 5);
  var small = M.graticuleLines({ west: 2.31, south: 48.84, east: 2.38, north: 48.88 }, 0.01);
  return {
    steps: [M.graticuleStep(360), M.graticuleStep(90), M.graticuleStep(10), M.graticuleStep(1), M.graticuleStep(0.05), M.graticuleStep(0.001)],
    world: [world.meridians.length, world.parallels.map(function (p) { return p.label; })],
    worldLon: world.meridians.map(function (m) { return m.label; }),
    anti: anti.meridians.map(function (m) { return [m.lon, m.label]; }),
    small: [small.meridians.length, small.meridians[0].label, small.parallels[0].label],
    labels: [M.formatGraticuleLabel(-0.25, 'lat', 0.25), M.formatGraticuleLabel(0, 'lon', 1), M.formatGraticuleLabel(180, 'lon', 30)],
    // Camera 20,000 km over 0N 0E: the horizon cap is 76 degrees, 73 after the margin.
    faces: [[0, 0], [70, 0], [75, 0], [180, 0], [-30, 60], [0, -89]].map(function (q) {
      return M.labelFacesCamera(q[0], q[1], 0, 0, 2.0e7);
    }),
    facesUnknown: M.labelFacesCamera(180, 0, null, null, null)
  };
""")


def test_graticule_steps_lines_and_labels(ran):
    r = ran["grid"]
    assert r["steps"] == [30, 15, 2, 0.25, 0.01, 0.005]
    assert r["world"][0] == 12
    assert r["world"][1] == ["60\u00b0S", "30\u00b0S", "0\u00b0", "30\u00b0N", "60\u00b0N"]
    assert r["worldLon"][0] == "180\u00b0" and r["worldLon"][6] == "0\u00b0"
    assert r["anti"] == [
        [170, "170\u00b0E"], [175, "175\u00b0E"], [180, "180\u00b0"], [185, "175\u00b0W"], [190, "170\u00b0W"],
    ]
    assert r["small"] == [8, "2.31\u00b0E", "48.84\u00b0N"]
    assert r["labels"] == ["0.25\u00b0S", "0\u00b0", "180\u00b0"]


def test_graticule_labels_beyond_the_horizon_are_skipped(ran):
    # Labels draw with the depth test off; the far-side ones used to show
    # through the globe ("150E" over the mid Atlantic in a whole-globe view).
    r = ran["grid"]
    assert r["faces"] == [True, True, False, False, True, False]
    assert r["facesUnknown"] is True


# --- the module on a fake globe ----------------------------------------------------------

scenario("init_mounts", r"""
  var s = start();
  var mt = s.mt;
  var mount = s.ctx.mount;
  var buttons = findAll(mount, function (c) { return c.tagName === 'BUTTON'; });
  var untitled = buttons.filter(function (b) { return !b.title; }).map(function (b) { return b.textContent; });
  var panels = byClass(mount, 'panel').map(function (p) { return [p.children[0].textContent, p.className]; });
  var tools = byClass(mount, 'mt-grid')[0].children.map(function (b) { return b.textContent; });
  var readout = byClass(document.body, 'mt-readout').length;
  var nav = byClass(document.body, 'mt-nav')[0];
  var navButtons = nav.children.map(function (b) { return b.textContent || b.title; });
  var styles1 = findAll(document.head, function (c) { return c.id === window.SDAMapTools.STYLE_ID; }).length;
  var viewer = s.ctx.viewer;
  var again = window.SDAMapTools.init(s.ctx);
  var styles2 = findAll(document.head, function (c) { return c.id === window.SDAMapTools.STYLE_ID; }).length;
  var readout2 = byClass(document.body, 'mt-readout').length;
  var layers = byClass(mount, 'mt-layer').map(function (r) { return r.children[1].textContent; });
  var units = byText(mount, 'UNITS KM') !== null;
  var noMount = window.SDAMapTools.init(makeCtx({ noMount: true }));
  var bare = makeCtx();
  var saved = window.Cesium; window.Cesium = undefined;
  var noCesium = window.SDAMapTools.init(bare);
  window.Cesium = saved;
  return {
    panels: panels, tools: tools, untitled: untitled, buttonCount: buttons.length,
    readout: readout, readout2: readout2, navButtons: navButtons, styles: [styles1, styles2],
    dataSources: viewer.dataSources.list.map(function (d) { return d.name; }),
    again: again, noMount: noMount, noCesium: noCesium, layers: layers,
    status: byClass(mount, 'mt-status').length, units: units, mountLeft: mount.children.length,
    innerHTMLFree: true
  };
""")


def test_init_mounts_panels_toolbar_and_floating_widgets(ran):
    r = ran["init_mounts"]
    assert r["panels"] == [
        ["Map tools", "panel"], ["Map view", "panel collapsed"], ["Saved views", "panel collapsed"],
    ]
    assert r["tools"] == ["DIST", "AREA", "HGT", "PIN", "LINE", "POLY", "RING", "PROF", "LOS"]
    assert r["untitled"] == []
    assert r["buttonCount"] > 30
    assert r["readout"] == 1 and r["readout2"] == 1
    assert r["navButtons"] == ["N", "+", "-", "TOP", "HOME"]
    assert r["styles"] == [1, 1]
    assert r["dataSources"] == []  # the second init destroyed the first and its sources
    assert r["again"] is True and r["noMount"] is True and r["noCesium"] is False
    assert r["layers"] == ["NASA overlay", "Base: gibs.earthdata.nasa.gov"]
    assert r["units"] is True
    assert r["mountLeft"] == 0  # a later init elsewhere takes the module out of this mount


scenario("measure_session", r"""
  var s = start();
  var mt = s.mt;
  var T = Cesium.ScreenSpaceEventType;
  var viewerDbl = s.ctx.viewer.screenSpaceEventHandler;
  byText(s.ctx.mount, 'DIST').click();
  var during = { body: document.body.className, dbl: typeof viewerDbl.actions[T.LEFT_DOUBLE_CLICK], active: window.SDAMapTools.isActive() };
  act(mt, 'LEFT_CLICK', at(lonX(0), latY(0)));
  act(mt, 'LEFT_CLICK', at(lonX(0), latY(0)));       // second click of a double-click: ignored
  act(mt, 'LEFT_CLICK', at(lonX(1), latY(0)));
  act(mt, 'MOUSE_MOVE', at(lonX(1), latY(1)));
  var live = mt.ui.live.textContent;
  act(mt, 'LEFT_CLICK', at(lonX(2), latY(0)));
  var bs = document.fire('keydown', { key: 'Backspace' });
  var afterUndo = mt.state.session.points.length;
  act(mt, 'LEFT_CLICK', at(lonX(1), latY(1)));
  act(mt, 'RIGHT_CLICK', at(lonX(1), latY(1)));
  var after = { body: document.body.className, dbl: typeof viewerDbl.actions[T.LEFT_DOUBLE_CLICK], active: window.SDAMapTools.isActive() };
  var stored = JSON.parse(localStorage.getItem(window.SDAMapTools.STORE_KEYS.drawings));
  var ents = mt.dataSources.drawings.entities.values.map(function (e) { return e.id; });
  // Escape cancels without saving.
  mt.startTool('line');
  act(mt, 'LEFT_CLICK', at(lonX(3), latY(3)));
  document.fire('keydown', { key: 'Escape' });
  // Typing in an input is left alone.
  mt.startTool('line');
  var typed = document.fire('keydown', { key: 'Escape', target: mt.ui.label });
  var stillActive = window.SDAMapTools.isActive();
  mt.cancel();
  return {
    during: during, after: after, live: live, undoPrevented: !!bs.defaultPrevented, afterUndo: afterUndo,
    stored: stored.map(function (d) { return [d.kind, d.tool, d.points, d.name]; }),
    ents: ents, count: window.SDAMapTools.drawings().length, typedIgnored: !typed.defaultPrevented && stillActive,
    status: mt.ui.status.textContent, list: byClass(s.ctx.mount, 'mt-item').length
  };
""")


def test_measure_session_persists_and_restores_viewer(ran):
    r = ran["measure_session"]
    assert "mt-drawing" in r["during"]["body"]
    assert r["during"]["dbl"] == "undefined" and r["during"]["active"] is True
    assert "mt-drawing" not in r["after"]["body"]
    assert r["after"]["dbl"] == "function" and r["after"]["active"] is False
    assert r["live"] == "Total 221.9 km  Leg 110.6 km  0.0\u00b0 N"
    assert r["undoPrevented"] is True and r["afterUndo"] == 2
    assert len(r["stored"]) == 1
    kind, tool, points, name = r["stored"][0]
    assert kind == "line" and tool == "measure"
    assert points == [[0, 0], [1, 0], [1, 1]]
    assert name == "221.9 km"
    assert any(e.endswith("-0") for e in r["ents"])
    assert r["count"] == 1 and r["typedIgnored"] is True
    assert r["list"] == 1


scenario("height_measure", r"""
  var s = start();
  var mt = s.mt;
  var sc = s.ctx.viewer.scene;
  // OSM buildings stand on a visible globe; the height tool reads the drawn surface.
  var meshCalls = 0;
  var surface = 480;
  sc.pickPositionSupported = true;
  sc.pickPosition = function (pos) { meshCalls++; return { lon: -10 + pos.x * 0.02, lat: 6 - pos.y * 0.02, h: surface }; };
  byText(s.ctx.mount, 'HGT').click();
  var first = mt.ui.live.textContent;
  act(mt, 'LEFT_CLICK', at(lonX(1), latY(1)));
  surface = 505.4;
  mt.state.lastMove = 0;
  act(mt, 'MOUSE_MOVE', at(lonX(1), latY(1)));
  var live = mt.ui.live.textContent;
  act(mt, 'LEFT_CLICK', at(lonX(1), latY(1)));   // the roof edge straight above the foot
  var stored = JSON.parse(localStorage.getItem(window.SDAMapTools.STORE_KEYS.drawings));
  var after = { active: window.SDAMapTools.isActive(), status: mt.ui.status.textContent };
  // The other measures keep reading the globe, not the mesh.
  var before = meshCalls;
  byText(s.ctx.mount, 'DIST').click();
  act(mt, 'LEFT_CLICK', at(lonX(2), latY(2)));
  mt.cancel();
  return { first: first, live: live, after: after, distMesh: meshCalls - before,
    stored: stored.map(function (d) { return [d.kind, d.tool, d.points.length, d.name]; }) };
""")


def test_height_measure_reads_the_mesh(ran):
    r = ran["height_measure"]
    assert r["first"] == "Click the foot, then the top."
    assert r["live"] == "Height 25 m up"
    assert r["stored"] == [["line", "measure", 2, "Height 25 m up"]]
    assert r["after"] == {"active": False, "status": "Height 25 m up"}
    assert r["distMesh"] == 0


scenario("area_pin_ring_popup", r"""
  var s = start();
  var mt = s.mt;
  mt.startTool('area');
  [[0, 0], [1, 0], [1, 1], [0, 1]].forEach(function (p) { act(mt, 'LEFT_CLICK', at(lonX(p[0]), latY(p[1]))); });
  document.fire('keydown', { key: 'Enter' });
  mt.ui.label.value = 'Camp';
  mt.startTool('pin');
  act(mt, 'LEFT_CLICK', at(lonX(2), latY(2)));
  act(mt, 'LEFT_CLICK', at(lonX(3), latY(2)));
  var pinActive = window.SDAMapTools.isActive();
  mt.ui.label.value = '';
  mt.ui.rings.value = '1, 5 km';
  mt.startTool('ring');
  act(mt, 'LEFT_CLICK', at(lonX(-2), latY(-2)));
  mt.ui.rings.value = 'lots';
  act(mt, 'LEFT_CLICK', at(lonX(-3), latY(-2)));
  var ringMsg = mt.ui.status.textContent;
  act(mt, 'RIGHT_CLICK', at(lonX(0), latY(0)));
  var ringStopped = !window.SDAMapTools.isActive();
  var summitCalls = [];
  window.SDASummit = { open: function (q) { summitCalls.push([q.lat, q.lon]); } };
  route('/api/geocode/reverse', { display_name: 'Null Island, Atlantic' });
  act(mt, 'RIGHT_CLICK', at(lonX(4), latY(-1)));
  var pop = byClass(document.body, 'mt-pop')[0];
  await mt.state.popupPlace;
  var lines = pop.children.filter(function (c) { return c.tagName === 'DIV' && c.className !== 'mt-row'; }).map(function (c) { return c.textContent; });
  byText(pop, 'COPY').click();
  await flush();
  byText(pop, 'PIN').click();
  var popGone = byClass(document.body, 'mt-pop').length === 0;
  act(mt, 'RIGHT_CLICK', at(lonX(4), latY(-1)));
  var pop2 = byClass(document.body, 'mt-pop')[0];
  await mt.state.popupPlace;
  byText(pop2, 'CONDITIONS').click();
  var condGone = byClass(document.body, 'mt-pop').length === 0;
  delete window.SDASummit;
  var list = window.SDAMapTools.drawings().map(function (d) { return [d.kind, d.name, d.radii]; });
  return { list: list, pinActive: pinActive, ringMsg: ringMsg, ringStopped: ringStopped, lines: lines,
           clip: clipLog, popGone: popGone, fetches: fetchLog, summitCalls: summitCalls, condGone: condGone };
""")


def test_area_pins_rings_and_right_click_popup(ran):
    r = ran["area_pin_ring_popup"]
    kinds = [d[0] for d in r["list"]]
    assert kinds == ["polygon", "pin", "pin", "circle", "pin"]
    assert r["list"][0][1] == "12,309 sq km"
    assert r["list"][1][1] == "Camp" and r["list"][2][1] == "Camp"
    assert r["list"][3][2] == [1000, 5000] and r["list"][3][1] == "Rings 1.00 km / 5.00 km"
    assert r["list"][4][1] == "Null Island"
    # The popup hands the point to the summit conditions drawer when it is loaded.
    assert len(r["summitCalls"]) == 1 and r["condGone"] is True
    lat, lon = r["summitCalls"][0]
    assert abs(lat - -1) < 0.05 and abs(lon - 4) < 0.05
    assert r["pinActive"] is True
    assert r["ringMsg"].startswith("Rings: write radii")
    assert r["ringStopped"] is True
    assert r["lines"][0] == "-1.000000, 4.000000"
    assert r["lines"][2] == "MGRS 31M FU 11263 89452"  # 1 S is band M
    assert r["lines"][3].startswith("UTM 31M 611263 9889452")
    assert r["lines"][4] == "Elev: terrain off"
    assert r["lines"][5] == "Place: Null Island, Atlantic"
    assert r["clip"] == ["-1.000000, 4.000000 | 1\u00b000'00.0\"S 4\u00b000'00.0\"E | 31M FU 11263 89452"]
    assert r["popGone"] is True
    assert r["fetches"] == ["/api/geocode/reverse?lat=-1.000000&lon=4.000000"]


scenario("screenshot_fullscreen", r"""
  var s = start();
  var mt = s.mt;
  var scene = s.ctx.viewer.scene;
  var credit = new El('div'); credit.id = 'basemap-credit'; credit.textContent = 'Imagery NASA GIBS';
  document.body.appendChild(credit);
  var made = [];
  var mk = document.createElement;
  document.createElement = function (t) { var e = mk.call(document, t); if (t === 'canvas') made.push(e); return e; };
  var before = scene.postRender.fns.length;
  var p = mt.action('screenshot');
  var waiting = scene.postRender.fns.length;
  scene.postRender.raise();
  var url = await p;
  var after = scene.postRender.fns.length;
  var shot = made[made.length - 1];
  var full1 = mt.action('fullscreen');
  var wasFull = !!document.documentElement.full;
  var exited = false;
  document.fullscreenElement = document.documentElement;
  document.exitFullscreen = function () { exited = true; };
  var full2 = mt.action('fullscreen');
  return {
    before: before, waiting: waiting, after: after, url: url, downloads: downloads,
    size: [shot.width, shot.height], calls: shot.ctxCalls.slice(0, 3), footer: shot.texts[0],
    status: mt.ui.status.textContent, full: [full1, wasFull, full2, exited],
    anchorsLeft: findAll(document.body, function (c) { return c.tagName === 'A'; }).length
  };
""")


def test_screenshot_copies_inside_post_render_and_fullscreen_toggles(ran):
    r = ran["screenshot_fullscreen"]
    # One listener of its own (compass and scale), one more while the shot waits for a frame.
    assert r["before"] == 1 and r["waiting"] == 2 and r["after"] == 1
    assert r["url"] == "data:image/png;base64,RkFLRQ=="
    assert len(r["downloads"]) == 1
    assert r["downloads"][0]["href"] == r["url"]
    assert re.fullmatch(r"sda-globe-\d{8}-\d{6}\.png", r["downloads"][0]["name"])
    assert r["size"] == [1000, 622]
    assert r["calls"] == ["drawImage", "fillRect", "fillText"]
    assert r["footer"].startswith("SDA WorldView   2026-06-21T08:24:00Z   camera ")
    assert r["footer"].endswith("   Imagery NASA GIBS")
    assert r["status"] == "Screenshot saved."
    assert r["full"] == [True, True, False, True]
    assert r["anchorsLeft"] == 0


scenario("reverse_geocode", r"""
  var s = start({ api: 'http://x' });
  var mt = s.mt;
  route('http://x/api/geocode/reverse', {}, 404);
  route('https://nominatim.openstreetmap.org/reverse', { display_name: 'Paris, Ile-de-France, France' });
  var a = await mt.reverseGeocode(48.8566, 2.3522);
  var missing = mt.state.geo.serverMissing;
  mt.state.geo.last = 0;
  var t0 = Date.now();
  var pair = await Promise.all([mt.reverseGeocode(51.5, -0.12), mt.reverseGeocode(40.7, -74.0)]);
  var gap = Date.now() - t0;
  var cached = await mt.reverseGeocode(48.85661, 2.35224);
  var log1 = fetchLog.slice();
  // A server that answers keeps being used.
  var s2 = start({ api: 'http://y' });
  route('http://y/api/geocode/reverse', { results: [{ name: 'Lyon', lat: 45.76, lon: 4.83 }] });
  var viaServer = await s2.mt.reverseGeocode(45.76, 4.83);
  // A server error that is not "missing" gives no name and no fallback.
  route('http://y/api/geocode/reverse', {}, 500);
  var err500 = await s2.mt.reverseGeocode(10, 10);
  var still = s2.mt.state.geo.serverMissing;
  // The static demo has no server: straight to Nominatim.
  fetchLog.length = 0;
  var s3 = start({ isStatic: true });
  var st = await s3.mt.reverseGeocode(48.8566, 2.3522);
  return { a: a, missing: missing, pair: pair, gap: gap, cached: cached, log1: log1,
           viaServer: viaServer, err500: err500, still: still, st: st, log3: fetchLog.slice() };
""")


def test_reverse_geocode_falls_back_to_nominatim_politely(ran):
    r = ran["reverse_geocode"]
    assert r["a"] == "Paris, Ile-de-France, France" and r["missing"] is True
    assert r["pair"] == ["Paris, Ile-de-France, France"] * 2
    # Nominatim's policy is one request a second; the second direct call waited.
    assert r["gap"] >= 1000
    assert r["cached"] == "Paris, Ile-de-France, France"
    assert r["log1"][0] == "http://x/api/geocode/reverse?lat=48.856600&lon=2.352200"
    assert r["log1"][1].startswith("https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=48.856600&lon=2.352200&zoom=")
    assert len(r["log1"]) == 4  # server once, then three direct calls; the cache answered the fifth
    assert all(u.startswith("https://nominatim.openstreetmap.org/") for u in r["log1"][1:])
    assert r["viaServer"] == "Lyon"
    assert r["err500"] == "" and r["still"] is False
    assert r["st"] == "Paris, Ile-de-France, France"
    assert len(r["log3"]) == 1 and r["log3"][0].startswith("https://nominatim.openstreetmap.org/reverse?")


scenario("terrain", r"""
  var s = start();
  var mt = s.mt;
  var v = s.ctx.viewer;
  var initial = [mt.state.terrainOn, v.terrainProvider.kind];
  var on1 = await mt.setTerrain(true);
  var r1 = [on1, v.terrainProvider.kind, v.terrainProvider.url, mt.state.terrainSource, mt.ui.terrain.className];
  var savedPref = JSON.parse(localStorage.getItem(window.SDAMapTools.STORE_KEYS.prefs)).terrain;
  // Elevation under the cursor is the picked height over the exaggeration.
  mt.setExaggeration(2);
  v.scene.globe.pick = function () { return { lon: 1, lat: 1, h: 200 }; };
  mt.state.lastMove = 0;
  act(mt, 'MOUSE_MOVE', at(500, 300));
  var elev = mt.ui.rElev.textContent;
  var off = await mt.setTerrain(false);
  var r2 = [off, v.terrainProvider.kind, mt.ui.status.textContent];
  Cesium.Ion.defaultAccessToken = 'token-from-server';
  var on2 = await mt.setTerrain(true);
  var r3 = [on2, v.terrainProvider.kind, mt.state.terrainSource];
  Cesium.Ion.defaultAccessToken = undefined;
  var ex = [mt.setExaggeration(3), v.scene.verticalExaggeration, mt.setExaggeration(50), mt.setExaggeration('junk'), mt.ui.exagVal.textContent];
  // A reload with the terrain preference on asks for terrain again.
  var callsBefore = terrainCalls.length;
  var s2 = start();
  await flush();
  var reloaded = [terrainCalls.length - callsBefore, s2.mt.state.terrainOn];
  // Older Cesium: no scene.verticalExaggeration, so the globe takes it.
  var old = makeViewer();
  delete old.scene.verticalExaggeration;
  var s3 = start({ viewer: old });
  s3.mt.setExaggeration(4);
  var globeEx = old.scene.globe.terrainExaggeration;
  // A failed load turns the toggle back off and says why.
  await s3.mt.setTerrain(false);
  Cesium.ArcGISTiledElevationTerrainProvider.fromUrl = function () { return Promise.reject(new Error('offline')); };
  var failed = await s3.mt.setTerrain(true);
  return {
    initial: initial, r1: r1, savedPref: savedPref, elev: elev, r2: r2, r3: r3, ex: ex, reloaded: reloaded,
    globeEx: globeEx, failed: failed, failStatus: s3.mt.ui.status.textContent, failWant: s3.mt.state.terrainWant,
    failPref: JSON.parse(localStorage.getItem(window.SDAMapTools.STORE_KEYS.prefs)).terrain
  };
""")


def test_terrain_toggle_keyless_ion_and_exaggeration(ran):
    r = ran["terrain"]
    assert r["initial"] == [False, "ellipsoid"]
    assert r["r1"][:4] == [True, "arcgis",
                           "https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer",
                           "Esri World Elevation 3D"]
    assert "active" in r["r1"][4]
    assert r["savedPref"] is True
    assert r["elev"] == "Elev 100 m / 328 ft"
    assert r["r2"] == [False, "ellipsoid", "Terrain off."]
    assert r["r3"] == [True, "ion", "Cesium World Terrain"]
    assert r["ex"] == [3, 3, 10, 1, "x1"]
    assert r["reloaded"] == [1, True]
    assert r["globeEx"] == 4
    assert r["failed"] is False
    assert r["failStatus"] == "Terrain did not load: offline"
    assert r["failWant"] is False and r["failPref"] is False


scenario("profile_los_ui", r"""
  var s = start();
  var mt = s.mt;
  function drawWith(tool, pts) {
    mt.startTool(tool);
    pts.forEach(function (p) { act(mt, 'LEFT_CLICK', at(lonX(p[0]), latY(p[1]))); });
    document.fire('keydown', { key: 'Enter' });
  }
  // Terrain off: the profile says what to do, and LOS falls back to the earth curve.
  drawWith('profile', [[0, 0], [0.1, 0]]);
  await flush();
  var offMsg = mt.ui.status.textContent;
  var chartHidden = mt.ui.chartWrap.style.display;
  mt.ui.eyes.value = '2, 2';
  drawWith('los', [[0, 0], [0.5, 0]]);
  await flush();
  var curve = window.SDAMapTools.drawings().slice(-1)[0];
  var curveInfo = mt.ui.chartInfo.textContent;
  // Terrain on, with a 500 m ridge across the middle of an 11 km path.
  await mt.setTerrain(true);
  terrainHeight = function (lat, lon) { return Math.abs(lon - 0.05) < 0.01 ? 500 : 0; };
  mt.ui.chart.ctxCalls.length = 0;
  drawWith('profile', [[0, 0], [0.1, 0]]);
  await flush(40);
  var profileInfo = mt.ui.chartInfo.textContent;
  var chartCalls = mt.ui.chart.ctxCalls.filter(function (c) { return c === 'stroke' || c === 'fill'; }).length;
  drawWith('los', [[0, 0], [0.1, 0]]);
  await flush(40);
  var blocked = window.SDAMapTools.drawings().slice(-1)[0];
  mt.ui.eyes.value = '700, 700';
  drawWith('los', [[0, 0], [0.1, 0]]);
  await flush(40);
  var clear = window.SDAMapTools.drawings().slice(-1)[0];
  var ent = mt.dataSources.drawings.entities.values.filter(function (e) { return e.id.indexOf('mt-' + clear.id + '-') === 0; })[0];
  return {
    offMsg: offMsg, chartHidden: chartHidden, curve: [curve.name, curve.color, curve.tool], curveInfo: curveInfo,
    profileInfo: profileInfo, chartCalls: chartCalls, chartShown: mt.ui.chartWrap.style.display,
    blocked: [blocked.name, blocked.color], clear: [clear.name, clear.color],
    entProps: ent && ent.properties, sampled: terrainCalls.length
  };
""")


def test_profile_and_line_of_sight_from_the_toolbar(ran):
    r = ran["profile_los_ui"]
    assert r["offMsg"] == "Elevation profile needs terrain: turn TERRAIN on in Map view, then draw it again."
    assert r["chartHidden"] == "none"
    assert r["curve"][0].startswith("LOS blocked 55.") and " (at " in r["curve"][0]
    assert r["curve"][1:] == ["#ff4040", "los"]
    assert r["curveInfo"].endswith("(terrain off: earth curve only)")
    assert r["profileInfo"] == "Min 0 m  max 500 m  climb 500 m  descent 500 m"
    assert r["chartCalls"] >= 2 and r["chartShown"] == ""
    assert r["blocked"][0].startswith("LOS blocked 11.1 km (at ") and r["blocked"][1] == "#ff4040"
    assert r["clear"] == ["LOS clear 11.1 km", "#00e676"]
    assert r["entProps"]["type"] == "annotation" and r["entProps"]["tool"] == "los"


scenario("goto", r"""
  var s = start({ api: 'http://x' });
  var mt = s.mt;
  function lastFly() {
    var f = calls.filter(function (c) { return c[0] === 'flyTo'; }).pop();
    var d = f[1].destination;
    return [Math.round(d.lat * 1e4) / 1e4, Math.round(d.lon * 1e4) / 1e4, d.h];
  }
  var dec = await mt.goTo('48.8584, 2.2945');
  var f1 = lastFly();
  var mgrs = await mt.goTo('31U DQ 48252 11954');  // PROJ: 448252.0 5411954.9
  var f2 = lastFly();
  var marks = mt.dataSources.drawings.entities.values.filter(function (e) { return e.id === 'mt-goto'; }).length;
  route('http://x/api/geocode?q=Paris', { results: [{ name: 'Paris', type: 'city', lat: 48.8566, lon: 2.3522 }] });
  var place = await mt.goTo('Paris');
  var f3 = lastFly();
  route('http://x/api/geocode?q=Nowhere', { results: [] });
  var none = await mt.goTo('Nowhere');
  var noneMsg = mt.ui.status.textContent;
  var empty = await mt.goTo('   ');
  var emptyMsg = mt.ui.status.textContent;
  var broken = await mt.goTo('Atlantis');
  var brokenMsg = mt.ui.status.textContent;
  // Enter in the box and the GO button both go.
  mt.ui.goto.value = '-33.8568, 151.2153';
  var ev = mt.ui.goto.fire('keydown', { key: 'Enter' });
  await flush();
  var f4 = lastFly();
  mt.ui.goto.value = '9C3XGV4C+XV';
  byText(s.ctx.mount, 'GO').click();
  await flush();
  var f5 = lastFly();
  var focusBefore = mt.ui.goto.focused;
  mt.action('goto');
  var log = fetchLog.slice();
  // The static demo searches Nominatim directly.
  var s2 = start({ isStatic: true });
  route('https://nominatim.openstreetmap.org/search', [{ display_name: 'Lyon, France', lat: '45.76', lon: '4.83' }]);
  var lyon = await s2.mt.goTo('Lyon');
  return {
    dec: [dec.kind, dec.label], f1: f1, mgrs: mgrs.kind, f2: f2, marks: marks, place: [place.name, place.kind], f3: f3,
    none: none, noneMsg: noneMsg, empty: empty, emptyMsg: emptyMsg, broken: broken, brokenMsg: brokenMsg,
    entered: !!ev.defaultPrevented, f4: f4, f5: f5, focus: [focusBefore, mt.ui.goto.focused], log: log,
    lyon: [lyon.name, lyon.lat, lyon.lon], lyonUrl: fetchLog[fetchLog.length - 1], intel: intel
  };
""")


def test_go_to_box_coordinates_and_places(ran):
    r = ran["goto"]
    assert r["dec"] == ["decimal", "48.85840\u00b0N 2.29450\u00b0E"]
    assert r["f1"] == [48.8584, 2.2945, 15000]
    assert r["mgrs"] == "mgrs" and r["f2"][:2] == pytest.approx([48.8584, 2.2945], abs=2e-5)
    assert r["marks"] == 1  # the go-to marker is replaced, not stacked
    assert r["place"] == ["Paris", "place"] and r["f3"] == [48.8566, 2.3522, 30000]
    assert r["none"] is None and r["noneMsg"] == "No coordinate or place matched Nowhere."
    assert r["empty"] is None and r["emptyMsg"] == "Type a coordinate or a place."
    assert r["broken"] is None and r["brokenMsg"].startswith("Place search failed: no route for http://x/api/geocode?q=Atlantis")
    assert r["entered"] is True and r["f4"][:2] == [-33.8568, 151.2153]
    assert r["f5"][:2] == pytest.approx([51.5074, -0.1278], abs=2e-4)
    assert r["focus"] == [False, True]
    assert r["log"] == ["http://x/api/geocode?q=Paris", "http://x/api/geocode?q=Nowhere", "http://x/api/geocode?q=Atlantis"]
    assert r["lyon"] == ["Lyon, France", 45.76, 4.83]
    assert r["lyonUrl"] == "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=Lyon"
    assert "Go to: Paris" in r["intel"]


scenario("views_layers_toggles", r"""
  var s = start();
  var mt = s.mt;
  var v = s.ctx.viewer;
  var mount = s.ctx.mount;
  var M = window.SDAMapTools;
  // Saved views.
  mt.ui.viewName.value = 'Pacific';
  byText(mount, 'SAVE').click();
  var stored = JSON.parse(localStorage.getItem(M.STORE_KEYS.bookmarks));
  var names = byClass(mt.ui.views, 'mt-name').map(function (b) { return b.textContent; });
  byClass(mt.ui.views, 'mt-name')[0].click();
  var fly = calls.filter(function (c) { return c[0] === 'flyTo'; }).pop()[1];
  // Imagery rows: top layer first, opacity slider and show box act on the layer.
  var rows = byClass(mount, 'mt-layer');
  var rowNames = rows.map(function (r) { return r.children[1].textContent; });
  rows[0].children[2].value = '0.25';
  rows[0].children[2].fire('input');
  rows[1].children[0].checked = false;
  rows[1].children[0].fire('change');
  var layerState = v.imageryLayers.list.map(function (l) { return [l.show, l.alpha]; });
  v.imageryLayers.list.push({ show: true, alpha: 1, imageryProvider: { url: 'https://server.arcgisonline.com/x' } });
  v.imageryLayers.layerAdded.raise();
  var rowsSoon = byClass(mount, 'mt-layer').length;
  await wait(350);
  var rowsLater = byClass(mount, 'mt-layer').map(function (r) { return r.children[1].textContent; });
  // Graticule and terminator.
  var gridCount = mt.setGraticule(true);
  var gridLabels = mt.dataSources.grid.entities.values.filter(function (e) { return e.label; }).map(function (e) { return e.label.text; });
  var sunCount = mt.setTerminator(true);
  var sunIds = mt.dataSources.sun.entities.values.map(function (e) { return e.id; });
  v.camera.moveEnd.raise();
  var gridAfterMove = mt.dataSources.grid.entities.values.length;
  var gridOff = mt.action('graticule');
  // A frame: compass and scale bars.
  mt.state.lastFrame = 0;
  v.scene.postRender.raise();
  var needle = mt.ui.needle.style.transform;
  var scale = [mt.ui.scaleM.bar.style.width, mt.ui.scaleM.label.textContent, mt.ui.scaleI.bar.style.width, mt.ui.scaleI.label.textContent];
  // Scene modes.
  calls.length = 0;
  mt.setSceneMode('2d');
  var mode2 = [mt.ui.modes['2d'].className, mt.ui.modes['3d'].className];
  v.scene.mode = Cesium.SceneMode.COLUMBUS_VIEW;
  v.scene.morphComplete.raise();
  var modeCv = mt.ui.modes.cv.className;
  v.scene.mode = Cesium.SceneMode.SCENE3D;
  // Units cycle through the button.
  var units = [mt.action('units'), mt.ui.unitsBtn.textContent, mt.action('units'), mt.action('units'), mt.ui.unitsBtn.textContent];
  // Navigation.
  calls.length = 0;
  mt.action('zoomIn'); mt.action('zoomOut'); mt.action('home'); mt.action('resetNorth'); mt.action('topDown');
  byText(mt.ui.nav, '+').click();
  mt.ui.compass.click();
  v.scene.mode = Cesium.SceneMode.SCENE2D;
  mt.action('resetNorth');
  var nav = calls.map(function (c) {
    if (c[0] === 'flyToBoundingSphere') return [c[0], c[1].heading, c[1].pitch, c[1].range];
    if (c[0] === 'setView') return [c[0], c[1].orientation.heading, c[1].orientation.pitch];
    return c;
  });
  // HUD and NAV toggles.
  var hud = [mt.action('readout'), mt.ui.readout.style.display, mt.setNav(false), mt.ui.nav.style.display];
  var prefs = JSON.parse(localStorage.getItem(M.STORE_KEYS.prefs));
  // Colour swatches.
  var sw = mt.ui.swatches[3];
  sw.el.click();
  var colour = [mt.prefs.color, sw.el.className, mt.setColor('not a colour'), mt.setColor('#0f0')];
  return {
    stored: stored.map(function (b) { return [b.name, Math.round(b.view.height), b.view.heading]; }), names: names,
    fly: [fly.orientation.heading, fly.orientation.pitch, fly.destination.h], rowNames: rowNames, layerState: layerState,
    rowsSoon: rowsSoon, rowsLater: rowsLater, gridCount: gridCount, gridLabels: gridLabels, sunCount: sunCount, sunIds: sunIds,
    gridAfterMove: gridAfterMove, gridOff: gridOff, gridLeft: mt.dataSources.grid.entities.values.length,
    needle: needle, scale: scale, mode2: mode2, modeCv: modeCv, units: units, nav: nav, hud: hud,
    prefs: [prefs.grid, prefs.sun, prefs.readout, prefs.nav, prefs.units], colour: colour,
    actions: mt.actions
  };
""")


def test_saved_views_layers_grid_sun_scale_and_navigation(ran):
    r = ran["views_layers_toggles"]
    assert r["stored"] == [["Pacific", 5000000, 0.5]] and r["names"] == ["Pacific"]
    assert r["fly"] == [0.5, -1.2, 5000000]
    assert r["rowNames"] == ["NASA overlay", "Base: gibs.earthdata.nasa.gov"]
    assert r["layerState"] == [[False, 1], [True, 0.25]]
    assert r["rowsSoon"] == 2  # debounced
    assert r["rowsLater"] == ["Layer 2: server.arcgisonline.com", "NASA overlay", "Base: gibs.earthdata.nasa.gov"]
    assert r["gridCount"] > 4 and "0\u00b0" in r["gridLabels"]
    assert r["sunCount"] == 4
    assert r["sunIds"] == ["mt-sun-0", "mt-sun-1", "mt-sun-2", "mt-sun-3", "mt-sun-point"]
    assert r["gridAfterMove"] > 0 and r["gridOff"] == 0 and r["gridLeft"] == 0
    # heading 0.5 rad is 28.6 degrees; the needle turns the other way.
    assert r["needle"] == "rotate(-28.6deg)"
    # 100 px across the middle spans 2 degrees of equator, 222.6 km.
    assert r["scale"] == ["90px", "200 km", "72px", "100 mi"]
    assert r["mode2"] == ["mt-btn active", "mt-btn"] and r["modeCv"] == "mt-btn active"
    assert r["units"] == ["imperial", "UNITS MI", "nautical", "metric", "UNITS KM"]
    assert r["nav"] == [
        ["zoomIn", 2500000], ["zoomOut", 5000000], ["flyHome", 1.5],
        ["flyToBoundingSphere", 0, -1.2, 5000000],
        ["flyToBoundingSphere", 0.5, pytest.approx(-1.5707963, abs=1e-6), 5000000],
        ["zoomIn", 2500000],
        ["flyToBoundingSphere", 0, -1.2, 5000000],
        ["setView", 0, -1.2],
    ]
    assert r["hud"] == [False, "none", False, "none"]
    assert r["prefs"] == [False, True, False, False, "metric"]
    assert r["colour"] == ["#00d4ff", "mt-swatch active", "#00d4ff", "#00ff00"]
    assert sorted(r["actions"]) == sorted([
        "zoomIn", "zoomOut", "resetNorth", "topDown", "home", "screenshot", "fullscreen", "graticule",
        "terminator", "terrain", "readout", "units", "measure", "area", "pin", "goto", "mode2d", "mode3d", "modecv",
    ])


scenario("import_export_ui", r"""
  var s = start();
  var mt = s.mt;
  var M = window.SDAMapTools;
  var none = mt.exportFile('kml');
  var noneMsg = mt.ui.status.textContent;
  var gj = JSON.stringify({ type: 'FeatureCollection', features: [
    { type: 'Feature', properties: { name: 'Base' }, geometry: { type: 'Point', coordinates: [2.35, 48.85] } },
    { type: 'Feature', properties: { name: 'Route' }, geometry: { type: 'LineString', coordinates: [[2, 48], [3, 49]] } },
    { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [500, 0] } }
  ] });
  var res = mt.importText(gj, 'trip.geojson');
  var msg = mt.ui.status.textContent;
  var rows = byClass(s.ctx.mount, 'mt-item').map(function (r) { return r.children[1].textContent; });
  var head = mt.ui.listHead.textContent;
  var flew = calls.filter(function (c) { return c[0] === 'flyToBoundingSphere' || c[0] === 'flyTo'; }).length;
  var bad = mt.importText('this is not a map file', 'notes.txt');
  var badMsg = mt.ui.status.textContent;
  downloads.length = 0;
  byText(s.ctx.mount, 'KML').click();
  byText(s.ctx.mount, 'GEOJSON').click();
  var files = downloads.map(function (d) { return d.name.replace(/\d{8}-\d{6}/, 'STAMP'); });
  var blobTypes = blobs.map(function (b) { return b.type; });
  var kmlText = blobs[0].parts[0];
  // A file through the hidden input.
  var fileText = '<gpx><wpt lat="10" lon="20"><name>Camp</name></wpt></gpx>';
  mt.ui.file.files = [{ name: 'walk.gpx', size: fileText.length, text: function () { return Promise.resolve(fileText); } }];
  mt.ui.file.fire('change');
  await flush();
  var afterFile = M.drawings().map(function (d) { return d.name; });
  // Row buttons: delete one, undo last, then the two-click clear.
  var x = byClass(s.ctx.mount, 'mt-x')[0];
  x.click();
  var afterX = M.drawings().length;
  byText(s.ctx.mount, 'UNDO LAST').click();
  var afterUndo = M.drawings().length;
  var clear1 = mt.clearAll();
  var armed = mt.ui.clearBtn.textContent;
  var stillThere = M.drawings().length;
  var clear2 = mt.clearAll();
  return {
    none: none, noneMsg: noneMsg, res: res, msg: msg, rows: rows, head: head, flew: flew, bad: bad, badMsg: badMsg,
    files: files, blobTypes: blobTypes, kmlStart: kmlText.slice(0, 38), kmlHasBase: kmlText.indexOf('<name>Base</name>') >= 0,
    afterFile: afterFile, afterX: afterX, afterUndo: afterUndo, clear: [clear1, armed, stillThere, clear2, mt.ui.clearBtn.textContent],
    left: M.drawings().length, ents: mt.dataSources.drawings.entities.values.length, intel: intel
  };
""")


def test_import_export_and_list_buttons(ran):
    r = ran["import_export_ui"]
    assert r["none"] is None and r["noneMsg"] == "Nothing to export yet."
    assert r["res"] == {"added": 2, "skipped": 1, "format": "geojson"}
    assert r["msg"] == "Imported 2 from trip.geojson, skipped 1."
    assert r["rows"] == ["Route", "Base"]
    assert r["head"].startswith("Drawings: 2")
    assert r["flew"] == 1
    assert r["bad"] is None and r["badMsg"].startswith("Import failed: ")
    assert r["files"] == ["sda-annotations-STAMP.kml", "sda-annotations-STAMP.geojson"]
    assert r["blobTypes"] == ["application/vnd.google-earth.kml+xml", "application/geo+json"]
    assert r["kmlStart"] == '<?xml version="1.0" encoding="UTF-8"?>'
    assert r["kmlHasBase"] is True
    assert r["afterFile"] == ["Base", "Route", "Camp"]
    assert r["afterX"] == 2 and r["afterUndo"] == 1
    assert r["clear"] == [False, "SURE?", 1, True, "CLEAR"]
    assert r["left"] == 0 and r["ents"] == 0
    assert "Imported 2 from trip.geojson, skipped 1." in r["intel"]


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in text), "maptools.js must be ASCII (use \\u escapes)"
    assert chr(0x2014) not in text and chr(92) + "u2014" not in text
    assert ".innerHTML" not in text and "eval(" not in text and "new Function" not in text
    assert "global.SDAMapTools = " in text
    assert "MIT" in text and "gods-eye-view" in text  # donor notice kept with the copied code
    own = Path(__file__).read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in own)


scenario("page_terrain", r"""
  var s = start();
  var mt = s.mt;
  var v = s.ctx.viewer;
  var before = [mt.state.terrainOn, mt.ui.terrain.className];
  // The page sets ion terrain after the module started (checkConfig runs later).
  v.terrainProvider = { kind: 'ion-from-page' };
  v.scene.globe.pick = function () { return { lon: 0, lat: 0, h: 1234 }; };
  mt.state.lastMove = 0;
  act(mt, 'MOUSE_MOVE', at(500, 300));
  return { before: before, elev: mt.ui.rElev.textContent, after: [mt.state.terrainOn, mt.state.terrainSource, mt.ui.terrain.className] };
""")


def test_terrain_loaded_later_by_the_page_is_noticed(ran):
    r = ran["page_terrain"]
    assert r["before"] == [False, "mt-btn"]
    assert r["elev"] == "Elev 1,234 m / 4,049 ft"
    assert r["after"] == [True, "page", "mt-btn active"]
