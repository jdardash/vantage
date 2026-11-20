"""Tests for the globe weather module (static/js/weather.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_aurora.py does it. The
fake `fetch` answers from a table, `setInterval` and `setTimeout` only record their
callbacks, and `performance.now` is a number the scenario moves, so nothing here
touches the network or waits. The fake scene projects with a plain
equirectangular map and puts the camera over 0 N 0 E, so the horizon test and the
wind trail drawing can be checked by coordinates. Skips cleanly when Node is not
installed.

Each scenario is the body of an async function returning a JSON-able value, run in a
fresh vm context; the module-scoped `ran` fixture holds every result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "weather.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

RV_BASE = 1795140000  # first radar frame of the fixture index, unix seconds


def hhmm(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, tz=UTC).strftime("%H:%M") + " UTC"


PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.checked = false; this.type = '';
  this.clientWidth = 0; this.clientHeight = 0;
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
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) {
    return c.textContent; }).join(''); },
  set: function (v) { this.children = []; this._text = String(v); }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { throw new Error('innerHTML read'); },
  set: function () { throw new Error('innerHTML used'); }
});
El.prototype.fire = function (type, ev) {
  ev = ev || {}; ev.type = type;
  (this.listeners[type] || []).forEach(function (fn) { fn.call(this, ev); }, this);
  return ev;
};
function all(el, out) { out = out || []; out.push(el);
  el.children.forEach(function (c) { all(c, out); }); return out; }
function byClass(el, cls) { return all(el).filter(function (x) {
  return x.className.split(/\s+/).indexOf(cls) >= 0; }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }
function cls(k, name) { return byClass(k.ctx.mount, name)[0]; }
function text(k, name) { return cls(k, name).textContent; }

// a 2D context that records what was drawn
function Ctx2d(canvas) {
  this.canvas = canvas; this.ops = []; this.strokes = 0; this.segments = []; this.path = [];
  this.globalCompositeOperation = 'source-over'; this.fillStyle = ''; this.strokeStyle = '';
  this.lineWidth = 1;
}
Ctx2d.prototype.setTransform = function (a, b, c, d) { this.ops.push(['setTransform', a, d]); };
Ctx2d.prototype.clearRect = function (x, y, w, h) { this.ops.push(['clearRect', w, h]); };
Ctx2d.prototype.fillRect = function () {
  this.ops.push(['fillRect', this.globalCompositeOperation, this.fillStyle]); };
Ctx2d.prototype.beginPath = function () { this.path = []; };
Ctx2d.prototype.moveTo = function (x, y) { this.path.push([x, y]); };
Ctx2d.prototype.lineTo = function (x, y) { this.path[this.path.length - 1].push(x, y); };
Ctx2d.prototype.stroke = function () {
  this.strokes++;
  var color = this.strokeStyle;
  this.path.forEach(function (seg) { this.segments.push({ color: color, seg: seg }); }, this);
};
var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) {
    var e = new El(tag);
    if (e.tagName === 'CANVAS') {
      e.width = 300; e.height = 150;
      e.getContext = function (kind) {
        return kind === '2d' ? (this.ctx2d = this.ctx2d || new Ctx2d(this)) : null; };
    }
    return e;
  },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  }
};
['count-weather', 'count-radar', 'count-wind', 'count-cyclones'].forEach(function (id) {
  var s = new El('span'); s.id = id; document.body.appendChild(s); });
function count(id) { return document.getElementById('count-' + id).textContent; }

// ---- clock and timers: recorded, never waited for -----------------------------
var NOW = 1000; var STEP = 0;
var performance = { now: function () { NOW += STEP; return NOW; } };
var TIMEOUTS = [];
function setTimeout(fn, ms) { TIMEOUTS.push({ fn: fn, ms: ms, live: true });
  return TIMEOUTS.length; }
function clearTimeout(id) { if (TIMEOUTS[id - 1]) TIMEOUTS[id - 1].live = false; }
function pending() { return TIMEOUTS.filter(function (t) { return t.live; }); }
function runTimeout(t) { t.live = false; t.fn(); }
var INTERVALS = [];
function setInterval(fn, ms) { INTERVALS.push({ fn: fn, ms: ms, live: true });
  return INTERVALS.length; }
function clearInterval(id) { if (INTERVALS[id - 1]) INTERVALS[id - 1].live = false; }
function liveIntervals() { return INTERVALS.filter(function (t) { return t.live; }); }
function tick() { return new Promise(function (r) { realSetTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }

// ---- fake Cesium: a sphere, an equirectangular screen, a camera over 0 N 0 E ----
var R = 6371000, W = 1000, H = 500;
function Cart3(x, y, z) { this.x = x; this.y = y; this.z = z; }
function fromDegrees(lon, lat, h, ell, out) {
  var r = R + (h || 0), a = lon * Math.PI / 180, b = lat * Math.PI / 180;
  out = out || new Cart3(0, 0, 0);
  out.x = r * Math.cos(b) * Math.cos(a); out.y = r * Math.cos(b) * Math.sin(a);
  out.z = r * Math.sin(b);
  return out;
}
function lonLatOf(c) {
  var r = Math.sqrt(c.x * c.x + c.y * c.y + c.z * c.z);
  return [Math.atan2(c.y, c.x) * 180 / Math.PI, Math.asin(c.z / r) * 180 / Math.PI];
}
function facing(lon, lat) {
  return Math.cos(lat * Math.PI / 180) * Math.cos(lon * Math.PI / 180) > 1 / 3; }
var camera = {
  positionWC: new Cart3(3 * R, 0, 0), directionWC: new Cart3(-1, 0, 0),
  positionCartographic: { height: 2 * R }, flights: [],
  flyTo: function (o) { this.flights.push(o); },
  pickEllipsoid: function (pos) {
    var lon = pos.x / W * 360 - 180, lat = 90 - pos.y / H * 180;
    return facing(lon, lat) ? fromDegrees(lon, lat, 0) : undefined;
  }
};
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function sub(kind) {
  var F = function () { Coll.call(this); this.kind = kind; };
  F.prototype = Object.create(Coll.prototype);
  return F;
}
function Opts(o) { for (var k in o) this[k] = o[k]; }
function FakeColor(css, a) { this.css = css; this.alpha = a; }
FakeColor.prototype.withAlpha = function (a) { return new FakeColor(this.css, a); };
function Provider(o) { Opts.call(this, o); this.requests = []; }
Provider.prototype.requestImage = function (x, y, level) {
  this.requests.push([x, y, level]);
  return x < 0 ? undefined : { tile: [x, y, level] };  // x < 0 plays Cesium's own throttle
};
function Mercator() { this.kind = 'mercator'; }
function ImageryLayer(p, o) { this.imageryProvider = p; this.alpha = o.alpha; this.show = o.show; }
var imagery = { list: [], removed: 0,
  add: function (l) { this.list.push(l); return l; },
  remove: function (l, destroy) { var i = this.list.indexOf(l); if (i >= 0) this.list.splice(i, 1);
    this.removed++; l.destroyed = !!destroy; return true; },
  contains: function (l) { return this.list.indexOf(l) >= 0; } };
var postRender = { fns: [],
  addEventListener: function (f) { this.fns.push(f); },
  removeEventListener: function (f) { var i = this.fns.indexOf(f);
    if (i >= 0) this.fns.splice(i, 1); } };
var primitivesAdded = [];
var container = new El('div'); container.clientWidth = W; container.clientHeight = H;
var scene = {
  mode: 3, camera: camera, postRender: postRender,
  globe: { tilesLoaded: true, ellipsoid: { name: 'ellipsoid' } },
  canvas: { clientWidth: W, clientHeight: H },
  primitives: { add: function (c) { primitivesAdded.push(c); return c; } },
  cartesianToCanvasCoordinates: function (c, out) {
    var ll = lonLatOf(c); out = out || {};
    out.x = (ll[0] + 180) / 360 * W; out.y = (90 - ll[1]) / 180 * H; return out; }
};
var viewer = { scene: scene, camera: camera, container: container, imageryLayers: imagery };
var Cesium = {
  UrlTemplateImageryProvider: Provider, WebMercatorTilingScheme: Mercator,
  ImageryLayer: ImageryLayer,
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  Cartesian3: { fromDegrees: fromDegrees, fromDegreesArray: function (a) {
    var out = []; for (var i = 0; i < a.length; i += 2) out.push(fromDegrees(a[i], a[i + 1], 0));
    return out; } },
  Cartographic: { fromCartesian: function (c) { var ll = lonLatOf(c);
    return { longitude: ll[0] * Math.PI / 180, latitude: ll[1] * Math.PI / 180, height: 0 }; } },
  SceneMode: { SCENE3D: 3 },
  PrimitiveCollection: sub('root'), PointPrimitiveCollection: sub('points'),
  LabelCollection: sub('labels'), PolylineCollection: sub('lines'),
  Primitive: function (o) { Opts.call(this, o); this.kind = 'primitive'; },
  GeometryInstance: function (o) { Opts.call(this, o); },
  PolygonGeometry: function (o) { Opts.call(this, o); },
  PolygonHierarchy: function (p) { this.positions = p; },
  PerInstanceColorAppearance: function (o) { Opts.call(this, o); },
  ColorGeometryInstanceAttribute: { fromColor: function (c) { return { color: c }; } },
  Color: { fromCssColorString: function (s) { return new FakeColor(s, 1); },
           BLACK: new FakeColor('#000000', 1), WHITE: new FakeColor('#ffffff', 1) },
  Material: { fromType: function (t, u) { return { type: t, uniforms: u }; } },
  LabelStyle: { FILL_AND_OUTLINE: 2 }, VerticalOrigin: { BOTTOM: 1 }
};
var window = this;
this.Cesium = Cesium;

// ---- fake fetch ------------------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
function route(prefix, body, status) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200 }); }
function fetch(url) {
  REQUESTS.push(url);
  for (var i = 0; i < ROUTES.length; i++) {
    if (url.indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      return Promise.resolve({ ok: r.status < 400, status: r.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r.body))); } });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () {
    return Promise.resolve({}); } });
}

// ---- fixtures ------------------------------------------------------------------
var RV_BASE = 1795140000;
var RV_URL = 'https://api.rainviewer.com/public/weather-maps.json';
function rvIndex(n, extra) {
  var past = [];
  for (var i = 0; i < n; i++) {
    past.push({ time: RV_BASE + i * 600, path: '/v2/radar/f' + i + 'ab' });
  }
  return Object.assign({ version: '2.0', generated: RV_BASE + n * 600,
    host: 'https://tilecache.rainviewer.com', radar: { past: past, nowcast: [] },
    satellite: { infrared: [] } }, extra || {});
}
function windGrid(u, v, extra) {
  var n = 72 * 31, U = [], V = [];
  for (var i = 0; i < n; i++) { U.push(u); V.push(v); }
  return Object.assign({ nx: 72, ny: 31, lo1: -180, la1: 75, dx: 5, dy: 5, u: U, v: V,
    units: 'm/s', valid_time: '2026-11-19T15:30:00Z', fetched_at: '2026-11-19T14:05:00Z',
    complete: true, resolution_deg: 5, filled: n, total: n, refreshing: false,
    max_speed: Math.round(Math.sqrt(u * u + v * v) * 100) / 100,
    source: 'Open-Meteo', stale: false }, extra || {});
}
var STORMS = { storms: [
  { id: 'al142026', name: 'Milton', source: 'NHC', agency: 'NHC',
    category: 'Hurricane (category 4)', intensity: 'C4', wind_kt: 120,
    wind_basis: 'current advisory', pressure_hpa: 929, lon: -86.8, lat: 23.4,
    movement_dir_deg: 90, movement_speed_kt: 9, advisory: '15',
    issued_at: '2026-11-19T15:00:00Z', updated_at: '2026-11-19T15:00:00Z',
    url: 'https://www.nhc.noaa.gov/text/MIATCPAT4.shtml', alert_level: null, country: null,
    track: [[[-86.8, 23.4], [-84, 25], [-80, 27]]],
    cone: [[[-87, 23], [-80, 26], [-79, 28], [-86, 24], [-87, 23]]],
    forecast_points: [{ lon: -86.8, lat: 23.4, tau_h: 0, wind_kt: 120 },
                      { lon: -84, lat: 25, tau_h: 12, wind_kt: 100 },
                      { lon: -80, lat: 27, tau_h: 24, wind_kt: 60 }],
    geometry_advisory: '15', geometry_status: 'current' },
  { id: 'gdacs-1002', name: 'KONG-REY-26', source: 'GDACS', agency: 'JTWC',
    category: 'Hurricane/typhoon strength (category 4)', intensity: 'C4', wind_kt: 115,
    wind_basis: 'event peak', pressure_hpa: null, lon: 125, lat: 15, movement_dir_deg: null,
    movement_speed_kt: null, advisory: null, issued_at: null,
    updated_at: '2026-11-19T12:00:00Z',
    url: 'https://www.gdacs.org/report.aspx?eventid=1002&eventtype=TC', alert_level: 'Orange',
    country: 'Philippines', track: [], cone: [], forecast_points: [],
    geometry_advisory: null, geometry_status: 'none' }
], count: 2, sources: { nhc: 'ok', gdacs: 'ok' }, stale: false };

function lcg(seed) {
  var s = seed >>> 0;
  return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}
function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: viewer, api: opts.api || '', isStatic: !!opts.isStatic,
    mount: opts.noMount ? undefined : new El('div'),
    addIntelItem: function (t) { feed.push(t); }, random: lcg(opts.seed || 7)
  };
  return { ctx: ctx, feed: feed };
}
function routeAll() {
  route(RV_URL, rvIndex(13));
  route('/api/weather/wind', windGrid(10, 5));
  route('/api/weather/cyclones', STORMS);
}
function windCanvas() { return byClass(container, 'weather-wind-canvas')[0] || null; }
var A = null;
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, realSetTimeout: setTimeout, Promise: Promise, JSON: JSON, Math: Math,
              Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'weather.js' });
  vm.runInContext('A = window.SDAWeather;', box);
  return box;
}

(async function () {
  var results = {};
  for (var name of Object.keys(scenarios)) {
    try {
      var box = load();
      var fn = vm.runInContext('(async function () {\n' + scenarios[name] + '\n})', box,
                               { filename: name });
      var value = await fn();
      if (value === undefined) value = null;
      results[name] = { ok: true, value: JSON.parse(JSON.stringify(value)) };
      try { vm.runInContext('window.SDAWeather && window.SDAWeather.setEnabled(false)', box); }
      catch (e) {}
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
    work = tmp_path_factory.mktemp("weather")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- radar helpers -------------------------------------------------------------------

scenario("radar_helpers", r"""
  var idx = rvIndex(3);
  idx.radar.past.push({ time: RV_BASE - 600, path: '/v2/radar/older' });  // out of order
  idx.radar.past.push({ time: RV_BASE + 50, path: '/v2/radar/f0ab' });    // same path again
  idx.radar.past.push({ time: RV_BASE + 99, path: '/v2/radar/../../x' });
  idx.radar.past.push({ time: 'x', path: '/v2/radar/bad' });
  idx.radar.past.push(null);
  idx.radar.nowcast = [{ time: RV_BASE + 1800, path: '/v2/radar/now1' }];
  var p = A.parseRainviewer(idx);
  function hostOf(h) { var r = A.parseRainviewer(Object.assign(rvIndex(2), { host: h }));
    return r && r.host; }
  return {
    host: p.host, generated: p.generated,
    times: p.frames.map(function (f) { return f.time - RV_BASE; }),
    paths: p.frames.map(function (f) { return f.path; }),
    kinds: p.frames.map(function (f) { return f.kind; }),
    hosts: ['https://tilecache.rainviewer.com/', 'https://a.b.rainviewer.com',
            'http://tilecache.rainviewer.com', 'https://rainviewer.com.evil.example',
            'https://evil.example', 42].map(hostOf),
    empty: A.parseRainviewer(rvIndex(0)),
    junk: [null, 'x', {}, { host: 'https://tilecache.rainviewer.com' },
           { host: 'https://tilecache.rainviewer.com', radar: { past: 'x' } }]
      .map(A.parseRainviewer),
    many: A.parseRainviewer(rvIndex(60)).frames.map(function (f) { return f.time - RV_BASE; }),
    url: A.radarTileUrl('https://tilecache.rainviewer.com', '/v2/radar/abc'),
    consts: [A.RADAR_MAX_LEVEL, A.RADAR_TILE_PX, A.RADAR_COLOR, A.RADAR_BUDGET,
             A.RADAR_INDEX_REFRESH_MS],
    labels: [A.frameLabel(RV_BASE), A.frameLabel(RV_BASE, RV_BASE * 1000 + 20 * 60000),
             A.frameLabel(RV_BASE, RV_BASE * 1000), A.frameLabel(RV_BASE + 600, RV_BASE * 1000),
             A.frameLabel(NaN), A.frameLabel('1795140000')],
    pct: [0, 55, 100, -3, 140, 33.6, '40', '', 'x', null, NaN].map(function (v) {
      return A.clampPct(v, 70); })
  };
""")


def test_rainviewer_index_parsing(ran):
    r = ran["radar_helpers"]
    assert r["host"] == "https://tilecache.rainviewer.com"
    assert r["generated"] == RV_BASE + 1800
    # sorted oldest first, duplicate and unsafe paths dropped, nowcast kept and labelled
    assert r["times"] == [-600, 0, 600, 1200, 1800]
    assert r["paths"] == ["/v2/radar/older", "/v2/radar/f0ab", "/v2/radar/f1ab",
                          "/v2/radar/f2ab", "/v2/radar/now1"]
    assert r["kinds"] == ["past"] * 4 + ["nowcast"]
    # only https hosts under rainviewer.com, trailing slash trimmed
    assert r["hosts"] == ["https://tilecache.rainviewer.com", "https://a.b.rainviewer.com",
                          None, None, None, None]
    assert r["empty"] is None and r["junk"] == [None] * 5
    # at most 48 frames, the newest ones
    assert len(r["many"]) == 48 and r["many"][-1] == 59 * 600 and r["many"][0] == 12 * 600


def test_radar_tile_url_and_limits(ran):
    r = ran["radar_helpers"]
    # 512 px tiles, colour scheme 2 (the only one served), smoothed with snow
    assert r["url"] == ("https://tilecache.rainviewer.com/v2/radar/abc/512/{z}/{x}/{y}/2/"
                        "1_1.png")
    # zoom 7 is RainViewer's deepest level; 90 of its 100 requests a minute
    assert r["consts"] == [7, 512, 2, 90, 300000]


def test_frame_labels_and_percentages(ran):
    r = ran["radar_helpers"]
    base = hhmm(RV_BASE)
    assert r["labels"] == [base, base + " (-20 min)", base + " (now)",
                           hhmm(RV_BASE + 600) + " (+10 min)", "", ""]
    assert r["pct"] == [0, 55, 100, 0, 100, 34, 40, 70, 70, 70, 70]


scenario("budget", r"""
  var b = A.makeBudget(3, 1000);
  var takes = [b.take(0), b.take(10), b.take(20), b.take(30)];
  var used = b.used(30);
  var later = [b.take(999), b.take(1000), b.take(1001)];
  b.refund();
  return { takes: takes, used: used, later: later, afterRefund: b.used(1001),
           limit: b.limit };
""")


def test_sliding_window_budget(ran):
    b = ran["budget"]
    assert b["takes"] == [True, True, True, False]
    assert b["used"] == 3
    # the stamp from t=0 leaves the window at 1000, the one from 10 at 1010
    assert b["later"] == [False, True, False]
    assert b["afterRefund"] == 2 and b["limit"] == 3


# --- wind helpers ------------------------------------------------------------------------

scenario("wind_helpers", r"""
  var U = [], V = [];
  for (var r = 0; r < 31; r++) for (var c = 0; c < 72; c++) { U.push(c); V.push(r); }
  var g = A.cleanWindGrid(Object.assign(windGrid(0, 0), { u: U, v: V }));
  function s(lon, lat) { var o = A.sampleWind(g, lon, lat); return [o.u, o.v]; }
  var junk = windGrid(1, 1); junk.u[0] = 'x'; junk.u[1] = 1e6; junk.v[2] = NaN;
  var jg = A.cleanWindGrid(junk);
  var bad = [
    Object.assign(windGrid(1, 1), { dx: 4 }), Object.assign(windGrid(1, 1), { ny: 1 }),
    Object.assign(windGrid(1, 1), { la1: 95 }), Object.assign(windGrid(1, 1), { nx: 71.5 }),
    Object.assign(windGrid(1, 1), { nx: '72' }), Object.assign(windGrid(1, 1), { u: [1, 2] }),
    Object.assign(windGrid(1, 1), { v: 'x' }), Object.assign(windGrid(1, 1), { dy: -5 }),
    null, 'grid'
  ].map(A.cleanWindGrid);

  var east = A.cleanWindGrid(windGrid(10, 0));
  var north = A.cleanWindGrid(windGrid(0, 10));
  var p0 = { lon: 0, lat: 0 }, s0 = A.stepParticle(p0, east, 1113.2, null, 75);
  var p60 = { lon: 0, lat: 60 }; A.stepParticle(p60, east, 1113.2, null, 75);
  var pw = { lon: 179.95, lat: 0 }; A.stepParticle(pw, east, 1113.2, null, 75);
  var pn = { lon: 0, lat: 74.95 }, sn = A.stepParticle(pn, north, 11132, null, 75);
  var pp = { lon: 0, lat: 89.5 }, sp = A.stepParticle(pp, east, 1, null, 90);
  var rnd = lcg(3), lats = [];
  for (var i = 0; i < 2000; i++) lats.push(A.seedGlobal(rnd, 75).lat);
  var north30 = lats.filter(function (x) { return x > 30; }).length;
  return {
    meta: [g.nx, g.ny, g.latLimit, g.maxSpeed, g.u instanceof Float32Array],
    samples: [s(-177.5, 75), s(177.5, 75), s(182.5, 75), s(-180, 72.5), s(0, 89), s(0, -89),
              s(0, 0)],
    nan: s(NaN, 0),
    junk: [jg.u[0], jg.v[0], jg.u[1], jg.v[1], jg.u[2], jg.v[2], jg.u[3]],
    bad: bad,
    step: { p0: [p0.lon, p0.lat, p0.plon, p0.plat, s0], p60: p60.lon, pw: pw.lon,
            pn: [pn.lat, sn], pole: sp },
    seed: [Math.min.apply(null, lats), Math.max.apply(null, lats), north30],
    rgb: [A.windRgb(0), A.windRgb(1.5), A.windRgb(30), A.windRgb(99), A.windRgb(NaN)],
    color: [A.windColor(0), A.windColor(30, 0.5)],
    buckets: [0, 2, 15, 29.9, 30, 100, NaN, -4].map(A.bucketFor),
    bucketColors: A.bucketColors(),
    legend: A.legendGradient(),
    fade: [A.fadeKeep(40), A.fadeKeep(16), A.fadeKeep(NaN), A.fadeKeep(1e6), A.fadeKeep(0)],
    sim: [A.simSecondsPerFrame(2e7, 16.67), A.simSecondsPerFrame(1e3, 16.67),
          A.simSecondsPerFrame(2e7, 33.34), A.simSecondsPerFrame(NaN, NaN)],
    counts: [A.particleCount(1000, 500), A.particleCount(4000, 3000), A.particleCount(100, 100),
             A.particleCount(0, 0), A.particleCount(NaN, 5)],
    norm: [A.normLon(190), A.normLon(-190), A.normLon(540), A.normLon(0)]
  };
""")


def test_wind_grid_checks_and_bilinear_sampling(ran):
    w = ran["wind_helpers"]
    # the fastest point is column 71, row 30: sqrt(71^2 + 30^2)
    assert w["meta"] == [72, 31, 75, pytest.approx(77.08, abs=0.01), True]
    samples = w["samples"]
    assert samples[0] == [0.5, 0]       # half way between the first two columns
    assert samples[1] == [35.5, 0]      # 177.5 E sits between column 71 and column 0
    assert samples[2] == samples[0]     # 182.5 E is 177.5 W
    assert samples[3] == [0, 0.5]       # half way down to the second row
    assert samples[4] == [36, 0]        # north of the grid: clamped to the first row
    assert samples[5] == [36, 30]       # south of it: the last row
    assert samples[6] == [36, 15]
    assert w["nan"] == [None, None]
    assert w["junk"] == [0, 0, 0, 0, 0, 0, 1]  # a junk component makes the point calm
    assert w["bad"] == [None] * 10


def test_particle_step(ran):
    s = ran["wind_helpers"]["step"]
    lon, lat, plon, plat, speed = s["p0"]
    assert lon == pytest.approx(0.1) and lat == 0 and plon == 0 and plat == 0
    assert speed == 10
    assert s["p60"] == pytest.approx(0.2)    # a degree of longitude is half as long at 60
    assert s["pw"] == pytest.approx(-179.95)  # wraps at the antimeridian
    assert s["pn"][0] == pytest.approx(75.95) and s["pn"][1] is None  # left the grid
    assert s["pole"] is None


def test_seeding_stays_on_the_grid_and_is_area_uniform(ran):
    lo, hi, north30 = ran["wind_helpers"]["seed"]
    assert -75 <= lo < -70 and 70 < hi <= 75
    # area-uniform: the band north of 30 holds (sin 75 - sin 30) / (2 sin 75) = 24 %
    assert 0.20 < north30 / 2000 < 0.28


def test_speed_colours_buckets_and_fade(ran):
    w = ran["wind_helpers"]
    assert w["rgb"][0] == [110, 130, 200]
    assert w["rgb"][1] == [90, 145, 215]         # half way to the 3 m/s stop
    assert w["rgb"][2] == w["rgb"][3] == [245, 210, 255]
    assert w["rgb"][4] == w["rgb"][0]
    assert w["color"] == ["rgb(110,130,200)", "rgba(245,210,255,0.5)"]
    assert w["buckets"] == [0, 1, 8, 15, 15, 15, 0, 0]
    assert len(w["bucketColors"]) == 16 and len(set(w["bucketColors"])) == 16
    assert all(c.startswith("rgba(") for c in w["bucketColors"])
    assert w["legend"].startswith("linear-gradient(90deg, rgb(110,130,200) 0%")
    assert w["legend"].endswith("rgb(245,210,255) 100%)")
    # earth.nullschool keeps 97 % per 40 ms frame; time-based here
    assert w["fade"][0] == 0.97
    assert w["fade"][1] == pytest.approx(0.97 ** 0.4, abs=1e-4)
    assert w["fade"][2] == pytest.approx(0.97 ** (16.7 / 40), abs=1e-4)
    assert w["fade"][3] == pytest.approx(0.97 ** (250 / 40), abs=1e-4)
    assert w["fade"][4] == 0.999  # never keeps everything, or trails would never clear


def test_time_step_and_particle_count(ran):
    w = ran["wind_helpers"]
    assert w["sim"][0] == pytest.approx(2100, rel=1e-3)
    assert w["sim"][1] == pytest.approx(5.25, rel=1e-3)  # clamped to 50 km of height
    assert w["sim"][2] == pytest.approx(4200, rel=1e-3)
    assert w["sim"][3] == pytest.approx(2100 * 16.7 / 16.67, rel=1e-3)
    assert w["counts"] == [1000, 4000, 400, 400, 400]
    assert w["norm"] == [-170, 170, -180, 0]


# --- cyclone helpers ---------------------------------------------------------------------

scenario("cyclone_helpers", r"""
  var dirty = { storms: [
    STORMS.storms[0],
    { id: 'x1', name: '<b>Evil</b>\u0007', lon: 10, lat: 10, wind_kt: 50, intensity: 'NOPE',
      url: 'javascript:alert(1)',
      track: [[[0, 0], [1, 1]], [[0, 0]], 'x', [[0, 0], [500, 1]]],
      cone: [[[0, 0], [1, 0], [1, 1], [0, 0]], [[0, 0], [1, 1]]],
      forecast_points: [{ lon: 1, lat: 1, tau_h: 12, wind_kt: 40 }, { lon: 999, lat: 0 }, null] },
    { id: 'x2', name: 'NoPlace', lon: 'a', lat: 3 },
    { id: '', name: 'NoId', lon: 1, lat: 1 },
    { id: 'al142026', name: 'Duplicate', lon: 1, lat: 1 },
    { id: 'x3', name: 'Calm', lon: 1, lat: 1, wind_kt: -5, pressure_hpa: 5000,
      url: 'https://ok.example/a b' },
    null, 'storm'
  ] };
  var c = A.cleanStorms(dirty);
  var clean = A.cleanStorms(STORMS);
  var x1 = c[1];
  return {
    ids: c.map(function (s) { return s.id; }),
    x1: { name: x1.name, intensity: x1.intensity, url: x1.url, track: x1.track.length,
          cone: x1.cone.length, fps: x1.forecast_points.length, source: x1.source,
          category: x1.category },
    x3: { wind: c[2].wind_kt, intensity: c[2].intensity, pressure: c[2].pressure_hpa,
          url: c[2].url },
    none: [A.cleanStorms({}), A.cleanStorms(null), A.cleanStorms({ storms: 'x' })],
    lines: clean.map(A.stormLine), labels: clean.map(A.stormLabel),
    quiet: A.cycloneLine([], { stale: false }),
    busy: A.cycloneLine(clean, { stale: true }),
    levels: [null, 33, 34, 64, 83, 96, 113, 137].map(A.intensityFor),
    windLine: A.windLine({ resolution: 10, complete: false, filled: 576, total: 2232,
                           maxSpeed: 23.456, validTime: '2026-11-19T15:30:00Z', stale: true }),
    windLineDone: A.windLine({ resolution: 5, complete: true, filled: 2232, total: 2232,
                               maxSpeed: 20, validTime: 'x', stale: false }),
    urls: [A.windUrl(''), A.windUrl('http://h'), A.cyclonesUrl(''), A.cyclonesUrl('http://h')],
    colors: A.INTENSITY_COLORS
  };
""")


def test_storm_cleaning(ran):
    c = ran["cyclone_helpers"]
    assert c["ids"] == ["al142026", "x1", "x3"]
    # angle brackets and control characters dropped, a bad level recomputed from the wind
    assert c["x1"] == {"name": "bEvil/b", "intensity": "TS", "url": None, "track": 1,
                       "cone": 1, "fps": 1, "source": "unknown",
                       "category": "Tropical cyclone"}
    assert c["x3"] == {"wind": None, "intensity": "UNK", "pressure": None, "url": None}
    assert c["none"] == [[], [], []]


def test_storm_text(ran):
    c = ran["cyclone_helpers"]
    assert c["lines"] == ["Milton: Hurricane (category 4), 120 kt, 929 hPa",
                          "KONG-REY-26: Hurricane/typhoon strength (category 4), 115 kt peak"]
    assert c["labels"] == ["Milton Cat 4 120 kt", "KONG-REY-26 Cat 4 115 kt"]
    assert c["quiet"] == "Cyclones: no active tropical cyclones (NHC, GDACS)"
    assert c["busy"] == ("Cyclones: 2 active: Milton (Cat 4, 120 kt), KONG-REY-26 (Cat 4, "
                         "115 kt) (a source is stale or unavailable)")
    assert c["levels"] == ["UNK", "TD", "TS", "C1", "C2", "C3", "C4", "C5"]
    assert c["windLine"] == ("Wind (Open-Meteo 10 m): 10-degree grid, still building (576 of "
                             "2232 points fetched), max 23.5 m/s, valid 15:30 UTC (stale: last "
                             "good copy)")
    assert c["windLineDone"] == "Wind (Open-Meteo 10 m): 5-degree grid, max 20.0 m/s"
    assert c["urls"] == ["/api/weather/wind", "http://h/api/weather/wind",
                         "/api/weather/cyclones", "http://h/api/weather/cyclones"]
    assert c["colors"]["C5"] == "#ff6060" and c["colors"]["TD"] == "#5ebaff"


# --- the whole layer: enable, draw, disable ---------------------------------------------

scenario("enable", r"""
  routeAll();
  var k = makeCtx();
  var ok = A.init(k.ctx);
  var boxes = byTag(k.ctx.mount, 'INPUT').filter(function (i) { return i.type === 'checkbox'; });
  var before = { requests: REQUESTS.length, title: byTag(k.ctx.mount, 'H3')[0].textContent,
                 status: text(k, 'weather-status'),
                 boxes: boxes.map(function (b) { return [b.className, b.checked]; }),
                 credit: text(k, 'weather-credit'), enabled: A.isEnabled() };
  A.setEnabled(true);
  A.setEnabled(true);  // a second "on" changes nothing
  await settle();
  var lay = imagery.list[0], prov = lay.imageryProvider;
  var radar = { layers: imagery.list.length, url: prov.url, max: prov.maximumLevel,
                min: prov.minimumLevel, tile: [prov.tileWidth, prov.tileHeight],
                scheme: prov.tilingScheme.kind, credit: prov.credit, show: lay.show,
                alpha: lay.alpha, frame: text(k, 'weather-frame'), count: count('radar'),
                status: text(k, 'weather-radar-status') };
  var canvas = windCanvas();
  var wind = { canvas: !!canvas, parent: canvas.parentNode === container,
               style: [canvas.style.position, canvas.style.pointerEvents, canvas.style.width,
                       canvas.style.height],
               listeners: postRender.fns.length, status: text(k, 'weather-wind-status') };
  NOW += 16;
  wind.drawn = A._windFrame();
  wind.size = [canvas.width, canvas.height];
  wind.count = count('wind');
  var root = primitivesAdded[0];
  var kinds = root.items.map(function (c) { return c.kind; });
  var pts = root.items[0].items, labels = root.items[1].items, lines = root.items[2].items;
  var cyc = { kinds: kinds, points: pts.map(function (p) { return [p.pixelSize, p.color.css]; }),
              labels: labels.map(function (l) { return l.text; }),
              lines: lines.map(function (l) { return [l.width, l.positions.length,
                l.material.uniforms.color.css, l.material.uniforms.color.alpha]; }),
              fill: root.items[3].appearance.translucent,
              fillColor: root.items[3].geometryInstances.attributes.color.color.alpha,
              pickable: pts.every(function (p) { return typeof p.id.sdaPick === 'function'
                && p.id.type === 'cyclone'; }),
              list: byClass(k.ctx.mount, 'weather-storm').map(function (r) {
                return r.textContent; }),
              counts: [count('cyclones'), count('weather')],
              status: text(k, 'weather-cyclones-status') };
  pts[3].id.sdaPick();
  var card = { text: text(k, 'weather-storm-card'), flights: camera.flights.length,
               link: byTag(cls(k, 'weather-storm-card'), 'A').map(function (a) {
                 return [a.attrs.href, a.attrs.target, a.attrs.rel]; }) };
  byClass(k.ctx.mount, 'weather-storm')[1].fire('click');
  card.second = text(k, 'weather-storm-card').slice(0, 11);
  card.flights2 = camera.flights.length;
  card.dest = camera.flights[1].destination ? true : false;
  var timers = { intervals: liveIntervals().map(function (t) { return t.ms; }).sort(),
                 timeouts: pending().map(function (t) { return t.ms; }) };
  var first = { ok: ok, before: before, requests: REQUESTS.slice(), radar: radar, wind: wind,
                cyc: cyc, card: card, timers: timers, feed: k.feed.slice(),
                status: text(k, 'weather-status'), state: A.state() };
  A.setEnabled(false);
  var off = { layers: imagery.list.length, removed: imagery.removed,
              destroyed: imagery.removed > 0, canvas: windCanvas() !== null,
              listeners: postRender.fns.length, rootItems: root.items.length, rootShow: root.show,
              intervals: liveIntervals().length, timeouts: pending().length,
              counts: ['radar', 'wind', 'cyclones', 'weather'].map(count),
              enabled: A.isEnabled(), status: text(k, 'weather-status'),
              list: byClass(k.ctx.mount, 'weather-storm').length,
              card: text(k, 'weather-storm-card') };
  A.setEnabled(true);
  await settle();
  var again = { layers: imagery.list.length, listeners: postRender.fns.length,
                rootItems: root.items.length, collections: primitivesAdded.length,
                requests: REQUESTS.length };
  return { first: first, off: off, again: again };
""")


def test_panel_before_enable(ran):
    b = ran["enable"]["first"]["before"]
    assert ran["enable"]["first"]["ok"] is True
    assert b["requests"] == 0 and b["enabled"] is False
    assert b["title"] == "Weather" and "Turn on" in b["status"]
    assert b["boxes"] == [["weather-sub-radar", True], ["weather-sub-wind", True],
                          ["weather-sub-cyclones", True]]
    assert "RainViewer" in b["credit"] and "CC BY 4.0" in b["credit"]


def test_enable_loads_all_three(ran):
    f = ran["enable"]["first"]
    assert f["requests"] == [
        "https://api.rainviewer.com/public/weather-maps.json", "/api/weather/wind",
        "/api/weather/cyclones"]
    assert f["status"] == "Showing: radar, wind, cyclones."
    assert len(f["feed"]) >= 3
    assert any(line.startswith("Radar (RainViewer): 13 frames") for line in f["feed"])
    assert any(line.startswith("Wind (Open-Meteo 10 m): 5-degree grid") for line in f["feed"])
    assert any(line.startswith("Cyclones: 2 active: Milton") for line in f["feed"])
    st = f["state"]
    assert st["on"] == {"radar": True, "wind": True, "cyclones": True}
    assert st["enabled"] is True


def test_radar_layer_is_the_newest_frame(ran):
    r = ran["enable"]["first"]["radar"]
    assert r["layers"] == 1
    assert r["url"] == ("https://tilecache.rainviewer.com/v2/radar/f12ab/512/{z}/{x}/{y}/2/"
                        "1_1.png")
    assert r["max"] == 7 and r["min"] == 0 and r["tile"] == [512, 512]
    assert r["scheme"] == "mercator" and r["credit"] == "RainViewer"
    assert r["show"] is True and r["alpha"] == 0.7
    assert r["frame"].startswith(hhmm(RV_BASE + 12 * 600))
    assert r["count"] == "13"
    assert r["status"] == (f"Radar (RainViewer): 13 frames, {hhmm(RV_BASE)} to "
                           f"{hhmm(RV_BASE + 12 * 600)}")


def test_wind_canvas_overlays_the_globe(ran):
    w = ran["enable"]["first"]["wind"]
    assert w["canvas"] is True and w["parent"] is True
    assert w["style"] == ["absolute", "none", "100%", "100%"]
    assert w["listeners"] == 1
    assert w["drawn"] > 300
    assert w["size"] == [1000, 500]
    assert w["count"] == "1000"
    assert w["status"].startswith("Wind (Open-Meteo 10 m): 5-degree grid, max 11.2 m/s")


def test_cyclones_drawn_with_track_cone_and_labels(ran):
    c = ran["enable"]["first"]["cyc"]
    assert c["kinds"] == ["points", "labels", "lines", "primitive"]
    # Milton: three forecast points coloured by their own wind, then the centre; Kong-rey
    assert c["points"] == [[6, "#ff8f20"], [6, "#ffc140"], [6, "#00faf4"], [14, "#ff8f20"],
                           [14, "#ff8f20"]]
    assert c["labels"] == ["Milton Cat 4 120 kt", "KONG-REY-26 Cat 4 115 kt"]
    # cone outline in the storm colour, then the track in white
    assert c["lines"] == [[1.5, 5, "#ff8f20", 0.75], [2, 3, "#ffffff", 0.85]]
    assert c["fill"] is True and c["fillColor"] == 0.16
    assert c["pickable"] is True
    assert c["list"] == ["Milton: Hurricane (category 4), 120 kt, 929 hPa",
                         "KONG-REY-26: Hurricane/typhoon strength (category 4), 115 kt peak"]
    assert c["counts"] == ["2", "2"]
    assert c["status"].startswith("Cyclones: 2 active")


def test_picking_a_storm_fills_the_card_and_flies_there(ran):
    card = ran["enable"]["first"]["card"]
    assert card["text"].startswith("Milton")
    for part in ("Hurricane (category 4)", "120 kt (222 km/h), current advisory", "929 hPa",
                 "90 deg at 9 kt", "Advisory 15", "track and cone", "Official advisory"):
        assert part in card["text"]
    assert card["link"] == [["https://www.nhc.noaa.gov/text/MIATCPAT4.shtml", "_blank",
                             "noopener noreferrer"]]
    assert card["flights"] == 1
    assert card["second"] == "KONG-REY-26" and card["flights2"] == 2 and card["dest"] is True


def test_refresh_timers(ran):
    t = ran["enable"]["first"]["timers"]
    assert t["intervals"] == [300000, 900000]  # radar index, cyclones
    assert t["timeouts"] == [1800000]          # the next wind grid


def test_disable_removes_everything_and_enable_restores_it(ran):
    off = ran["enable"]["off"]
    assert off["layers"] == 0 and off["destroyed"] is True
    assert off["canvas"] is False and off["listeners"] == 0
    assert off["rootItems"] == 0 and off["rootShow"] is False
    assert off["intervals"] == 0 and off["timeouts"] == 0
    assert off["counts"] == ["0", "0", "0", "0"]
    assert off["enabled"] is False and "Turn on" in off["status"]
    assert off["list"] == 0 and off["card"] == ""
    again = ran["enable"]["again"]
    assert again["layers"] == 1 and again["listeners"] == 1
    assert again["rootItems"] == 4 and again["collections"] == 1  # the root is reused
    assert again["requests"] == 6


# --- radar animation, opacity, budget --------------------------------------------------

scenario("radar_play", r"""
  route(RV_URL, rvIndex(13));
  var k = makeCtx();
  A.init(k.ctx);
  A.setLayer('radar', true);
  await settle();
  var on = A.state().on;
  A.playRadar(true);
  var playing = { btn: text(k, 'weather-play'), st: A.state().radar,
                  layers: imagery.list.map(function (l) { return [l.show, l.alpha]; }),
                  timers: pending().map(function (t) { return t.ms; }) };
  runTimeout(pending()[0]);
  var after1 = { index: A.state().radar.index, timers: pending().map(function (t) {
    return t.ms; }) };
  for (var i = 0; i < 12; i++) runTimeout(pending()[0]);
  var atLast = { index: A.state().radar.index, timers: pending().map(function (t) {
    return t.ms; }) };
  scene.globe.tilesLoaded = false;
  runTimeout(pending()[0]);
  var waiting = { index: A.state().radar.index, timers: pending().map(function (t) {
    return t.ms; }) };
  NOW += 3001;
  runTimeout(pending()[0]);
  var forced = A.state().radar.index;
  scene.globe.tilesLoaded = true;
  var cached = imagery.list.length;
  var shownPlaying = imagery.list.filter(function (l) { return l.show; }).length;
  A.playRadar(false);
  var paused = { btn: text(k, 'weather-play'), timers: pending().length,
                 shown: imagery.list.filter(function (l) { return l.show; }).length };
  var ranges = byTag(cls(k, 'weather-radar'), 'INPUT');
  ranges[0].value = '3';
  ranges[0].fire('input');
  var current = imagery.list.filter(function (l) { return l.show; });
  var slid = { index: A.state().radar.index, label: text(k, 'weather-frame'),
               max: ranges[0].max, url: current[0].imageryProvider.url,
               val: byClass(cls(k, 'weather-radar'), 'val')[0].textContent };
  ranges[1].value = '25';
  ranges[1].fire('input');
  var op = [current[0].alpha, byClass(cls(k, 'weather-radar'), 'val')[1].textContent];
  A.setRadarOpacity('junk');
  op.push(current[0].alpha);
  A.setRadarOpacity(250);
  op.push(current[0].alpha, ranges[1].value);
  A.playRadar(true);
  cls(k, 'weather-play').fire('click');  // the button pauses again
  var clicked = [A.state().radar.playing, text(k, 'weather-play')];
  return { on: on, playing: playing, after1: after1, atLast: atLast, waiting: waiting,
           forced: forced, cached: cached, shownPlaying: shownPlaying, paused: paused,
           slid: slid, op: op, clicked: clicked };
""")


def test_radar_loop_preloads_the_next_frame(ran):
    r = ran["radar_play"]
    assert r["on"] == {"radar": True, "wind": False, "cyclones": False}
    p = r["playing"]
    assert p["btn"] == "PAUSE" and p["st"]["playing"] is True and p["st"]["index"] == 12
    # the shown frame at the chosen opacity, the next one loading invisibly
    assert p["layers"] == [[True, 0.7], [True, 0]]
    assert p["timers"] == [700]


def test_radar_loop_steps_holds_and_waits_for_tiles(ran):
    r = ran["radar_play"]
    assert r["after1"] == {"index": 0, "timers": [700]}  # wrapped to the oldest frame
    assert r["atLast"] == {"index": 12, "timers": [1600]}  # the newest frame is held longer
    assert r["waiting"] == {"index": 12, "timers": [250]}  # tiles still loading
    assert r["forced"] == 0  # after three seconds it moves on anyway
    assert r["cached"] == 13  # one layer per frame, reused on every loop
    assert r["shownPlaying"] == 2


def test_radar_pause_slider_and_opacity(ran):
    r = ran["radar_play"]
    assert r["paused"] == {"btn": "PLAY", "timers": 0, "shown": 1}
    s = r["slid"]
    assert s["index"] == 3 and s["max"] == "12" and s["val"] == "4/13"
    assert s["label"].startswith(hhmm(RV_BASE + 3 * 600))
    assert s["url"].startswith("https://tilecache.rainviewer.com/v2/radar/f3ab/")
    assert r["op"] == [0.25, "25%", 0.7, 1, "100"]
    assert r["clicked"] == [False, "PLAY"]


scenario("radar_budget", r"""
  route(RV_URL, rvIndex(2));
  A.init(makeCtx().ctx);
  A.setLayer('radar', true);
  await settle();
  var prov = imagery.list[0].imageryProvider;
  var got = 0;
  for (var i = 0; i < 100; i++) if (prov.requestImage(i, 0, 3) !== undefined) got++;
  var inner = prov.requests.length;
  var refused = prov.requestImage(5, 5, 3);
  var requests = REQUESTS.length;
  liveIntervals()[0].fn();  // the five-minute index refresh finds the budget spent
  await settle();
  var requestsAfter = REQUESTS.length;
  NOW += 60001;
  var throttled = prov.requestImage(-1, 0, 3);  // Cesium's own throttle: refunded
  var again = 0;
  for (i = 0; i < 100; i++) if (prov.requestImage(i, 1, 3) !== undefined) again++;
  return { got: got, inner: inner, refused: refused === undefined, requests: requests,
           requestsAfter: requestsAfter, throttled: throttled === undefined, again: again };
""")


def test_tile_requests_share_one_budget(ran):
    b = ran["radar_budget"]
    # the index fetch took one of the 90, so 89 tiles went through
    assert b["got"] == 89 and b["inner"] == 89
    assert b["refused"] is True
    assert b["requests"] == 1 and b["requestsAfter"] == 1
    # a minute later the window is clear; a request Cesium declines costs nothing
    assert b["throttled"] is True and b["again"] == 90


# --- wind drawing ------------------------------------------------------------------------

scenario("wind_render", r"""
  route('/api/weather/wind', windGrid(10, 5));
  var k = makeCtx();
  A.init(k.ctx);
  A.setLayer('wind', true);
  await settle();
  var canvas = windCanvas(), g = canvas.ctx2d;
  NOW += 16;
  var d1 = A._windFrame();
  var ops1 = g.ops.map(function (o) { return o[0]; });
  g.ops = []; g.segments = [];
  NOW += 16;
  var d2 = A._windFrame();
  var ops2 = g.ops.slice();
  var segs = g.segments.slice();
  var xs = [], colors = {};
  segs.forEach(function (s) { xs.push(s.seg[0], s.seg[2]); colors[s.color] = true; });
  var lens = segs.map(function (s) { return Math.abs(s.seg[2] - s.seg[0]) +
    Math.abs(s.seg[3] - s.seg[1]); });
  var mode = g.globalCompositeOperation;
  g.ops = [];
  camera.positionWC = new Cart3(0, 3 * R, 0);  // the camera swings round to 90 E
  NOW += 16;
  A._windFrame();
  var ops3 = g.ops.map(function (o) { return o[0]; });
  g.ops = [];
  STEP = 20;  // every clock read now costs 20 ms: frames look slow
  for (var i = 0; i < 3; i++) { NOW += 16; A._windFrame(); }
  STEP = 0;
  var slowed = [A.state().wind.count, count('wind')];
  container.clientWidth = 400; container.clientHeight = 300;
  NOW += 16;
  A._windFrame();
  var resized = [canvas.width, canvas.height, A.state().wind.count, A.state().wind.particles];
  return { d1: d1, d2: d2, ops1: ops1, ops2: ops2, xmin: Math.min.apply(null, xs),
           xmax: Math.max.apply(null, xs), colors: Object.keys(colors),
           want: A.bucketColors()[A.bucketFor(Math.sqrt(125))], fade: A.fadeKeep(16),
           maxLen: Math.max.apply(null, lens), mode: mode, ops3: ops3, slowed: slowed,
           resized: resized, lineWidth: g.lineWidth };
""")


def test_wind_frames_fade_trails_and_clear_on_camera_moves(ran):
    w = ran["wind_render"]
    assert w["ops1"][:2] == ["setTransform", "clearRect"] and "fillRect" not in w["ops1"]
    assert w["ops2"][0] == ["fillRect", "destination-in", f"rgba(0,0,0,{w['fade']})"]
    assert w["mode"] == "source-over"
    assert "clearRect" in w["ops3"] and "fillRect" not in w["ops3"]
    assert w["d1"] > 300 and w["d2"] > 300
    assert w["lineWidth"] == 1.25


def test_wind_particles_behind_the_globe_are_not_drawn(ran):
    w = ran["wind_render"]
    # camera over 0 N 0 E: the visible cap reaches about 70.5 degrees east and west,
    # which the fake screen maps to x between 304 and 696
    assert w["xmin"] > 300 and w["xmax"] < 700
    assert w["maxLen"] < 5  # short steps, no segments jumping across the screen


def test_wind_colour_bucket_and_governor(ran):
    w = ran["wind_render"]
    assert w["colors"] == [w["want"]]  # one speed everywhere, one bucket
    assert w["slowed"] == [850, "850"]  # slow frames cut 15 % of the particles
    assert w["resized"] == [400, 300, 400, 400]  # a smaller window gets fewer particles


scenario("wind_building", r"""
  route('/api/weather/wind', windGrid(5, 0, { complete: false, resolution_deg: 10,
                                              filled: 576 }));
  var k = makeCtx();
  A.init(k.ctx);
  A.setLayer('wind', true);
  await settle();
  var building = { status: text(k, 'weather-wind-status'),
                   timers: pending().map(function (t) { return t.ms; }),
                   requests: REQUESTS.length };
  route('/api/weather/wind', windGrid(6, 0));
  runTimeout(pending()[0]);
  await settle();
  var done = { status: text(k, 'weather-wind-status'),
               timers: pending().map(function (t) { return t.ms; }), requests: REQUESTS.length,
               feed: k.feed.length };
  await A.refresh();
  await settle();
  return { building: building, done: done,
           afterRefresh: pending().map(function (t) { return t.ms; }) };
""")


def test_wind_polls_while_the_server_builds_the_grid(ran):
    w = ran["wind_building"]
    assert "10-degree grid, still building (576 of 2232 points fetched)" in (
        w["building"]["status"])
    assert w["building"]["timers"] == [70000] and w["building"]["requests"] == 1
    assert w["done"]["status"].startswith("Wind (Open-Meteo 10 m): 5-degree grid, max 6.0")
    assert w["done"]["timers"] == [1800000] and w["done"]["requests"] == 2
    assert w["done"]["feed"] == 2
    assert w["afterRefresh"] == [1800000]  # a manual refresh never leaves two polls running


# --- switches, static build, failures ----------------------------------------------------

scenario("sublayers", r"""
  routeAll();
  var k = makeCtx();
  A.init(k.ctx);
  A.setLayer('wind', true);
  await settle();
  var a = { on: A.state().on, enabled: A.isEnabled(), requests: REQUESTS.slice(),
            status: text(k, 'weather-status') };
  var radarBox = cls(k, 'weather-sub-radar');
  radarBox.checked = true;
  radarBox.fire('change');
  await settle();
  var b = A.state().on;
  var windBox = cls(k, 'weather-sub-wind');
  windBox.checked = false;
  windBox.fire('change');
  var c = { on: A.state().on, want: A.state().want, listeners: postRender.fns.length,
            layerOn: [A.isLayerOn('wind'), A.isLayerOn('radar')] };
  A.setEnabled(false);
  A.setEnabled(true);
  await settle();
  var d = { on: A.state().on, windBox: windBox.checked };
  A.setLayer('bogus', true);
  return { a: a, b: b, c: c, d: d, layers: A.LAYERS };
""")


def test_sublayer_switches(ran):
    s = ran["sublayers"]
    # a sublayer switch turns the master on, and only that sublayer
    assert s["a"]["on"] == {"radar": False, "wind": True, "cyclones": False}
    assert s["a"]["enabled"] is True and s["a"]["requests"] == ["/api/weather/wind"]
    assert s["a"]["status"] == "Showing: wind."
    assert s["b"] == {"radar": True, "wind": True, "cyclones": False}
    assert s["c"]["on"]["wind"] is False and s["c"]["want"]["wind"] is False
    assert s["c"]["listeners"] == 0 and s["c"]["layerOn"] == [False, True]
    # the master switch brings back what the sub-switches want
    assert s["d"] == {"on": {"radar": True, "wind": False, "cyclones": True}, "windBox": False}
    assert s["layers"] == ["radar", "wind", "cyclones"]


scenario("static", r"""
  routeAll();
  var k = makeCtx({ isStatic: true });
  var ok = A.init(k.ctx);
  var initial = [text(k, 'weather-wind-status'), text(k, 'weather-cyclones-status')];
  A.setEnabled(true);
  A.setEnabled(false);
  A.setEnabled(true);
  await A.refresh();
  await settle();
  return { ok: ok, initial: initial, requests: REQUESTS, feed: k.feed, on: A.state().on,
           layers: imagery.list.length, canvas: windCanvas() !== null,
           collections: primitivesAdded.length, wind: text(k, 'weather-wind-status'),
           cyc: text(k, 'weather-cyclones-status') };
""")


def test_static_build_shows_radar_only(ran):
    s = ran["static"]
    assert s["ok"] is True
    assert s["initial"] == ["Wind needs the live server.", "Cyclones need the live server."]
    # RainViewer is read straight from the browser; no /api route is asked
    assert s["requests"]
    assert all(u.startswith("https://api.rainviewer.com/") for u in s["requests"])
    assert s["on"] == {"radar": True, "wind": False, "cyclones": False}
    assert s["layers"] == 1 and s["canvas"] is False and s["collections"] == 0
    assert s["feed"][:2] == ["Wind needs the live server (Open-Meteo grid).",
                             "Tropical cyclones need the live server (NHC, GDACS)."]
    assert len(s["feed"]) == 3 and s["feed"][2].startswith("Radar (RainViewer): 13 frames")
    assert "live server" in s["wind"] and "live server" in s["cyc"]


scenario("failures", r"""
  route(RV_URL, {}, 500);
  route('/api/weather/wind', { detail: 'Open-Meteo wind grid unavailable' }, 502);
  route('/api/weather/cyclones', { detail: 'NHC and GDACS cyclone feeds unavailable' }, 502);
  var k = makeCtx();
  A.init(k.ctx);
  A.setEnabled(true);
  await settle();
  var failed = { feed: k.feed.slice(), radar: text(k, 'weather-radar-status'),
                 wind: text(k, 'weather-wind-status'),
                 cyc: text(k, 'weather-cyclones-status'),
                 timeouts: pending().map(function (t) { return t.ms; }),
                 layers: imagery.list.length, canvas: windCanvas() !== null };
  await A.refresh();
  await settle();
  var repeats = k.feed.length;
  route(RV_URL, rvIndex(4));
  route('/api/weather/wind', windGrid(3, 4, { stale: true }));
  route('/api/weather/cyclones', Object.assign({}, STORMS, { stale: true }));
  await A.refresh();
  await settle();
  return { failed: failed, repeats: repeats, layers: imagery.list.length,
           canvas: windCanvas() !== null, tail: k.feed.slice(failed.feed.length),
           timeouts: pending().map(function (t) { return t.ms; }) };
""")


def test_failures_are_reported_once_then_recover(ran):
    f = ran["failures"]
    assert sorted(f["failed"]["feed"]) == [
        "Cyclones: NHC and GDACS feeds unavailable",
        "Radar: RainViewer weather maps unavailable",
        "Wind: Open-Meteo grid not available yet"]
    assert "retrying in five minutes" in f["failed"]["radar"]
    assert "still building" in f["failed"]["wind"]
    assert "retrying in fifteen minutes" in f["failed"]["cyc"]
    assert f["failed"]["timeouts"] == [70000]
    assert f["failed"]["layers"] == 0 and f["failed"]["canvas"] is False
    assert f["repeats"] == 3
    assert f["layers"] == 1 and f["canvas"] is True
    tail = f["tail"]
    assert any(t.startswith("Radar (RainViewer): 4 frames") for t in tail)
    assert any(t.endswith("(stale: last good copy)") for t in tail)
    assert any(t.endswith("(a source is stale or unavailable)") for t in tail)
    assert f["timeouts"] == [1800000]


scenario("hostile", r"""
  route('/api/weather/cyclones', { storms: [
    { id: 'x<1>', name: '<img src=x onerror=alert(1)>Bad', lon: 10, lat: 10, wind_kt: 70,
      intensity: 'C1', category: '<script>', url: 'javascript:alert(1)', track: 'x',
      cone: [[['a', 1]]], forecast_points: 'x' }], stale: false });
  route('/api/weather/wind', windGrid(1, 1, { nx: 10 }));
  route(RV_URL, rvIndex(2, { host: 'https://evil.example' }));
  var k = makeCtx();
  A.init(k.ctx);
  A.setEnabled(true);
  await settle();
  var root = primitivesAdded[0];
  var labels = root.items[1].items.map(function (l) { return l.text; });
  root.items[0].items[0].id.sdaPick();
  var card = cls(k, 'weather-storm-card');
  var out = { labels: labels, card: card.textContent, links: byTag(card, 'A').length,
              feed: k.feed.slice(), layers: imagery.list.length, canvas: windCanvas() !== null,
              wind: text(k, 'weather-wind-status') };
  route('/api/weather/cyclones', { storms: [], stale: false });
  await A.refresh();
  await settle();
  out.after = { items: root.items.map(function (c) { return c.items ? c.items.length : -1; }),
                card: card.textContent, count: count('cyclones'),
                status: text(k, 'weather-cyclones-status') };
  return out;
""")


def test_hostile_answers_are_cleaned_or_refused(ran):
    h = ran["hostile"]
    assert h["labels"] == ["img src=x onerror=alert(1)Bad Cat 1 70 kt"]
    assert h["links"] == 0
    for line in [h["card"], *h["feed"], *h["labels"]]:
        assert "<" not in line and ">" not in line
    assert "script" in h["card"]  # the text survives, the brackets do not
    assert h["layers"] == 0  # a radar host outside rainviewer.com is never used
    assert h["canvas"] is False and "did not refresh" not in h["wind"]
    assert h["after"]["items"] == [0, 0, 0]
    assert h["after"]["card"] == "" and h["after"]["count"] == "0"
    assert h["after"]["status"] == "Cyclones: no active tropical cyclones (NHC, GDACS)"


scenario("late_answer", r"""
  routeAll();
  var k = makeCtx();
  A.init(k.ctx);
  A.setEnabled(true);
  A.setEnabled(false);  // off before any answer lands
  await settle();
  return { layers: imagery.list.length, canvas: windCanvas() !== null,
           collections: primitivesAdded.length, feed: k.feed,
           counts: ['radar', 'wind', 'cyclones'].map(count), timeouts: pending().length };
""")


def test_answers_after_disable_are_dropped(ran):
    assert ran["late_answer"] == {"layers": 0, "canvas": False, "collections": 0, "feed": [],
                                  "counts": ["0", "0", "0"], "timeouts": 0}


scenario("guards", r"""
  routeAll();
  var k1 = makeCtx({ noMount: true });
  var ok = A.init(k1.ctx);
  A.setLayer('radar', true);
  await settle();
  var noMount = imagery.list.length;
  var k2 = makeCtx();
  var second = A.init(k2.ctx);  // a second init turns the first one off
  var replaced = { layers: imagery.list.length, enabled: A.isEnabled() };
  var saved = window.Cesium;
  window.Cesium = undefined;
  var noCesium = A.init(makeCtx().ctx);
  window.Cesium = saved;
  return { ok: ok, noMount: noMount, second: second, replaced: replaced, noCesium: noCesium,
           noViewer: A.init({ mount: new El('div') }), nothing: A.init(null),
           state: A.state() !== null };
""")


def test_init_guards(ran):
    g = ran["guards"]
    assert g == {"ok": True, "noMount": 1, "second": True,
                 "replaced": {"layers": 0, "enabled": False}, "noCesium": False,
                 "noViewer": False, "nothing": False, "state": True}


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "weather.js must stay ASCII"
    assert "global.SDAWeather" in text
    # the RainViewer zoom limit and colour scheme are pinned in one place
    assert "var RADAR_MAX_LEVEL = 7;" in text and "var RADAR_COLOR = 2;" in text
