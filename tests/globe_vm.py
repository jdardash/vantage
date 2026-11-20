"""Shared Node `vm` harness for the globe presentation modules.

Used by test_globe_models, test_globe_visuals, test_globe_clouds, test_globe_tour and
test_globe_presentation. It follows tests/test_globe_peaks.py: plain browser JavaScript
runs under Node's `vm` against a hand-written DOM stub and a fake Cesium, with a fake
`fetch` answering from a table, so nothing touches the network. Each scenario is the
body of an async function returning a JSON-able value, run in a fresh context.

Not collected by pytest (no test_ prefix).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

NODE = shutil.which("node")
JS_DIR = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "static" / "js"

DOM = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
}
El.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this; this.children.push(c); return c;
};
El.prototype.insertBefore = function (c, ref) {
  if (c.parentNode) c.parentNode.removeChild(c);
  var at = ref ? this.children.indexOf(ref) : -1;
  c.parentNode = this;
  if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
  return c;
};
El.prototype.removeChild = function (c) {
  var at = this.children.indexOf(c);
  if (at < 0) throw new Error('removeChild: not a child');
  this.children.splice(at, 1); c.parentNode = null; return c;
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.replaceChildren = function () {
  var self = this;
  this.children.slice().forEach(function (c) { self.removeChild(c); });
  this._text = '';
  for (var i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, fn)
  { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function (t, fn) {
  var l = this.listeners[t] || []; var at = l.indexOf(fn); if (at >= 0) l.splice(at, 1);
};
El.prototype.focus = function () {};
El.prototype.contains = function (o) { return all(this).indexOf(o) >= 0; };
El.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200 };
};
Object.defineProperty(El.prototype, 'textContent', {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent;
    }).join(''); },
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
  ev = ev || {}; ev.type = type; ev.target = ev.target || this;
  ev.preventDefault = function () { ev.prevented = true; };
  ev.stopPropagation = function () {};
  ev.stopImmediatePropagation = function () { ev.immediate = true; };
  var fns = (this.listeners[type] || []).slice();
  for (var i = 0; i < fns.length; i++) { fns[i].call(this, ev); if (ev.immediate) break; }
  return ev;
};
function all(el, out) { out = out || []; out.push(el); el.children.forEach(function (c) { all(c,
  out); }); return out; }
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }
function buttonNamed(el, text) {
  return byTag(el, 'BUTTON').filter(function (b) { return b.textContent === text; })[0];
}

var docListeners = {};
var document = {
  head: new El('head'), body: new El('body'), documentElement: new El('html'),
  hidden: false,
  createElement: function (tag) { return new El(tag); },
  createTextNode: function (t) { var n = new El('#text'); n._text = String(t); return n; },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  },
  querySelector: function () { return null; },
  addEventListener: function (t, fn, opt) { (docListeners[t] = docListeners[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    var l = docListeners[t] || []; var at = l.indexOf(fn); if (at >= 0) l.splice(at, 1);
  }
};
function fireDoc(type, ev) {
  ev = ev || {}; ev.type = type;
  ev.preventDefault = function () { ev.prevented = true; };
  ev.stopPropagation = function () {};
  (docListeners[type] || []).slice().forEach(function (fn) { fn(ev); });
  return ev;
}
var window = this;
var winListeners = {};
this.addEventListener = function (t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); };
this.removeEventListener = function (t, fn) {
  var l = winListeners[t] || []; var at = l.indexOf(fn); if (at >= 0) l.splice(at, 1);
};
function fireWin(type, ev) {
  ev = ev || {}; ev.type = type;
  (winListeners[type] || []).slice().forEach(function (fn) { fn(ev); });
  return ev;
}
this.innerWidth = 1400; this.innerHeight = 900;
this.matchMedia = function (q) {
  var m = /max-width:\s*(\d+)px/.exec(q);
  return { matches: m ? window.innerWidth <= Number(m[1]) : false,
    addEventListener: function () {}, addListener: function () {} };
};
this.devicePixelRatio = 1;
this.requestAnimationFrame = function (fn) {
  return setTimeout(function () { fn(Date.now()); }, 0);
};
this.cancelAnimationFrame = function (id) { clearTimeout(id); };

// ---- fake fetch ------------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
function route(prefix, body, status) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200 }); }
function fetch(url) {
  REQUESTS.push(String(url));
  for (var i = 0; i < ROUTES.length; i++) {
    if (String(url).indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      if (r.status === 0) return Promise.reject(new Error('offline'));
      return Promise.resolve({ ok: r.status < 400, status: r.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r.body))); },
        text: function () { return Promise.resolve(typeof r.body === 'string' ? r.body
          : JSON.stringify(r.body)); } });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({});
    }, text: function () { return Promise.resolve(''); } });
}
this.fetch = fetch;

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }
"""

# A fake Cesium with real vector arithmetic on a spherical Earth, enough for
# distances, east-north-up frames and dead reckoning to be checked numerically.
CESIUM = r"""
var R_EARTH = 6371000;
function Cartesian3(x, y, z) { this.x = x || 0; this.y = y || 0; this.z = z || 0; }
function res(r) { return r || new Cartesian3(); }
Cartesian3.fromDegrees = function (lon, lat, h) {
  return Cartesian3.fromRadians(lon * Math.PI / 180, lat * Math.PI / 180, h);
};
Cartesian3.fromRadians = function (lon, lat, h) {
  var r = R_EARTH + (h || 0);
  return new Cartesian3(r * Math.cos(lat) * Math.cos(lon), r * Math.cos(lat) * Math.sin(lon),
    r * Math.sin(lat));
};
Cartesian3.clone = function (a, r) { if (!a) return undefined; r = res(r); r.x = a.x; r.y = a.y;
  r.z = a.z; return r; };
Cartesian3.add = function (a, b, r) { r = res(r); var x = a.x + b.x, y = a.y + b.y,
  z = a.z + b.z; r.x = x; r.y = y; r.z = z; return r; };
Cartesian3.subtract = function (a, b, r) { r = res(r); var x = a.x - b.x, y = a.y - b.y,
  z = a.z - b.z; r.x = x; r.y = y; r.z = z; return r; };
Cartesian3.multiplyByScalar = function (a, s, r) { r = res(r); r.x = a.x * s; r.y = a.y * s;
  r.z = a.z * s; return r; };
Cartesian3.magnitude = function (a) { return Math.sqrt(a.x * a.x + a.y * a.y + a.z * a.z); };
Cartesian3.normalize = function (a, r) { var m = Cartesian3.magnitude(a);
  return Cartesian3.multiplyByScalar(a, 1 / m, r); };
Cartesian3.dot = function (a, b) { return a.x * b.x + a.y * b.y + a.z * b.z; };
Cartesian3.cross = function (a, b, r) { r = res(r); var x = a.y * b.z - a.z * b.y,
  y = a.z * b.x - a.x * b.z, z = a.x * b.y - a.y * b.x; r.x = x; r.y = y; r.z = z; return r; };
Cartesian3.distance = function (a, b) { return Cartesian3.magnitude(Cartesian3.subtract(a, b)); };
Cartesian3.equals = function (a, b) { return !!a && !!b && a.x === b.x && a.y === b.y &&
  a.z === b.z; };
Cartesian3.ZERO = new Cartesian3(0, 0, 0);
function Cartesian2(x, y) { this.x = x || 0; this.y = y || 0; }

function Col(css, a) { this.css = css; this.alpha = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };

function ConstantProperty(v) { this.v = v; this.constant = true; }
ConstantProperty.prototype.getValue = function () { return this.v; };
function ConstantPositionProperty(v) { this.v = v; this.constant = true; }
ConstantPositionProperty.prototype.getValue = function () { return this.v; };
function CallbackProperty(fn, c) { this.fn = fn; this.isConstant = !!c; this.callback = true; }
CallbackProperty.prototype.getValue = function (t, r) { return this.fn(t, r); };
function CallbackPositionProperty(fn, c) { this.fn = fn; this.isConstant = !!c;
  this.callback = true; }
CallbackPositionProperty.prototype.getValue = function (t, r) { return this.fn(t, r); };

function ModelGraphics(o) { var self = this; Object.keys(o || {}).forEach(function (k) {
  self[k] = o[k]; }); this.show = true; }

var ENTITY_SEQ = 0;
function Entity(o) {
  o = o || {};
  var self = this;
  Object.keys(o).forEach(function (k) { self[k] = o[k]; });
  if (this.id === undefined) this.id = 'e' + (++ENTITY_SEQ);
  if (this.show === undefined) this.show = true;
  this.isShowing = true;
  if (o.model && !(o.model instanceof ModelGraphics)) this.model = new ModelGraphics(o.model);
}
function EntityCollection(owner) { this.values = []; this.owner = owner || null; }
EntityCollection.prototype.add = function (o) {
  var e = o instanceof Entity ? o : new Entity(o);
  e.entityCollection = this; this.values.push(e); return e;
};
EntityCollection.prototype.remove = function (e) {
  var at = this.values.indexOf(e); if (at < 0) return false;
  this.values.splice(at, 1); return true;
};
EntityCollection.prototype.contains = function (e) { return this.values.indexOf(e) >= 0; };
EntityCollection.prototype.getById = function (id) {
  return this.values.filter(function (e) { return e.id === id; })[0];
};
EntityCollection.prototype.removeAll = function () { this.values = []; };
function CustomDataSource(name) { this.name = name; this.show = true;
  this.entities = new EntityCollection(this); }

function Event() { this.fns = []; }
Event.prototype.addEventListener = function (fn) { var s = this; s.fns.push(fn);
  return function () { var at = s.fns.indexOf(fn); if (at >= 0) s.fns.splice(at, 1); }; };
Event.prototype.removeEventListener = function (fn) { var at = this.fns.indexOf(fn);
  if (at >= 0) { this.fns.splice(at, 1); return true; } return false; };
Event.prototype.raiseEvent = function () { var a = arguments;
  this.fns.slice().forEach(function (fn) { fn.apply(null, a); }); };

function HeadingPitchRoll(h, p, r) {
  this.heading = h || 0; this.pitch = p || 0; this.roll = r || 0;
}

var Cesium = {
  Cartesian3: Cartesian3, Cartesian2: Cartesian2,
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff'), TRANSPARENT: new Col('transparent', 0) },
  ConstantProperty: ConstantProperty, ConstantPositionProperty: ConstantPositionProperty,
  CallbackProperty: CallbackProperty, CallbackPositionProperty: CallbackPositionProperty,
  ModelGraphics: ModelGraphics, Entity: Entity, EntityCollection: EntityCollection,
  CustomDataSource: CustomDataSource, Event: Event, HeadingPitchRoll: HeadingPitchRoll,
  ShadowMode: { DISABLED: 0, ENABLED: 1 }, ColorBlendMode: { HIGHLIGHT: 0, REPLACE: 1, MIX: 2 },
  Math: { toRadians: function (d) { return d * Math.PI / 180; },
    toDegrees: function (r) { return r * 180 / Math.PI; },
    clamp: function (v, a, b) { return Math.max(a, Math.min(b, v)); } },
  Matrix3: { fromColumnMajorArray: function (a) { return { cols: a.slice() }; } },
  Quaternion: { fromRotationMatrix: function (m) { return { fromMatrix: m }; } },
  Matrix4: {
    multiplyByPoint: function (m, p, r) {
      r = res(r);
      var x = m.o.x + m.e.x * p.x + m.n.x * p.y + m.u.x * p.z;
      var y = m.o.y + m.e.y * p.x + m.n.y * p.y + m.u.y * p.z;
      var z = m.o.z + m.e.z * p.x + m.n.z * p.y + m.u.z * p.z;
      r.x = x; r.y = y; r.z = z; return r;
    }
  },
  Transforms: {
    eastNorthUpToFixedFrame: function (p) {
      var u = Cartesian3.normalize(p);
      var e = Cartesian3.normalize(Cartesian3.cross(new Cartesian3(0, 0, 1), u));
      var n = Cartesian3.cross(u, e);
      return { o: Cartesian3.clone(p), e: e, n: n, u: u };
    },
    headingPitchRollQuaternion: function (p, hpr) { return { hpr: hpr }; }
  },
  Cartographic: { fromCartesian: function (p) {
    var r = Cartesian3.magnitude(p);
    return { latitude: Math.asin(p.z / r), longitude: Math.atan2(p.y, p.x), height: r - R_EARTH };
  } },
  JulianDate: {
    fromDate: function (d) { return { ms: d.getTime() }; },
    toDate: function (j) { return new Date(j.ms); },
    now: function () { return { ms: Date.now() }; },
    clone: function (j) { return { ms: j.ms }; }
  },
  defined: function (v) { return v !== undefined && v !== null; }
};
this.Cesium = Cesium;

function DataSources() { this.list = []; }
DataSources.prototype.add = function (ds) { this.list.push(ds); return Promise.resolve(ds); };
DataSources.prototype.remove = function (ds) { var at = this.list.indexOf(ds);
  if (at >= 0) this.list.splice(at, 1); return at >= 0; };
DataSources.prototype.get = function (i) { return this.list[i]; };
DataSources.prototype.contains = function (ds) { return this.list.indexOf(ds) >= 0; };
Object.defineProperty(DataSources.prototype, 'length', { get: function () {
  return this.list.length; } });

// A camera at lon/lat/height (degrees, metres) looking straight down.
function setCamera(viewer, lon, lat, h) {
  viewer.camera.positionWC = Cartesian3.fromDegrees(lon, lat, h);
  viewer.camera.position = viewer.camera.positionWC;
  viewer.camera.positionCartographic = { longitude: lon * Math.PI / 180,
    latitude: lat * Math.PI / 180, height: h };
}

function makeViewer() {
  var v = {
    entities: new EntityCollection(null),
    dataSources: new DataSources(),
    clock: { currentTime: { ms: 0 }, onTick: new Event(), shouldAnimate: true, multiplier: 1 },
    camera: { flyTo: function (o) { this.flown = o; if (o.complete) o.complete(); },
      cancelFlight: function () { this.cancelled = (this.cancelled || 0) + 1; },
      moveStart: new Event(), moveEnd: new Event(), changed: new Event(),
      setView: function (o) { this.view = o; }, lookAt: function () {} },
    scene: { preUpdate: new Event(), postUpdate: new Event(), preRender: new Event(),
      postRender: new Event(), renderError: new Event(),
      requestRender: function () { this.renders = (this.renders || 0) + 1; },
      primitives: { list: [], add: function (p) { this.list.push(p); return p; },
        remove: function (p) { var at = this.list.indexOf(p); if (at >= 0) this.list.splice(at, 1);
          return at >= 0; } },
      globe: { enableLighting: false, showGroundAtmosphere: false,
        dynamicAtmosphereLighting: false, dynamicAtmosphereLightingFromSun: false,
        atmosphereLightIntensity: 10, nightFadeOutDistance: 1e7, nightFadeInDistance: 5e7,
        lightingFadeOutDistance: 1e7, lightingFadeInDistance: 2e7, show: true },
      fog: { enabled: false, density: 0.0002, minimumBrightness: 0.03 },
      skyAtmosphere: { show: true }, sun: { show: true }, moon: { show: true },
      postProcessStages: { fxaa: { enabled: false }, bloom: { enabled: false, uniforms: {} },
        list: [], add: function (s) { this.list.push(s); return s; },
        remove: function (s) { var at = this.list.indexOf(s);
          if (at >= 0) this.list.splice(at, 1); return at >= 0; },
        contains: function (s) { return this.list.indexOf(s) >= 0; } },
      highDynamicRange: false, msaaSamples: 1, debugShowFramesPerSecond: false,
      canvas: null, mode: 3 },
    canvas: null,
    resolutionScale: 1.0,
    useBrowserRecommendedResolution: true,
    imageryLayers: null,
    render: function () {}
  };
  v.canvas = new El('canvas');
  v.canvas.width = 1400; v.canvas.height = 900; v.canvas.clientWidth = 1400;
  v.canvas.clientHeight = 900;
  v.scene.canvas = v.canvas;
  setCamera(v, 0, 0, 20000000);
  return v;
}
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modules = JSON.parse(process.argv[3]);
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
var teardown = process.argv[5] || '';

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              setInterval: setInterval, clearInterval: clearInterval, Promise: Promise,
              JSON: JSON, Math: Math, Date: Date, console: console };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  modules.forEach(function (p) {
    vm.runInContext(fs.readFileSync(p, 'utf8'), box, { filename: p });
  });
  return box;
}

(async function () {
  var results = {};
  for (var name of Object.keys(scenarios)) {
    var box = null;
    try {
      box = load();
      var fn = vm.runInContext('(async function () {\n' + scenarios[name] + '\n})', box,
                               { filename: name });
      var value = await fn();
      if (value === undefined) value = null;
      results[name] = { ok: true, value: JSON.parse(JSON.stringify(value)) };
    } catch (e) {
      results[name] = { ok: false, error: String((e && e.stack) || e) };
    }
    if (box && teardown) { try { vm.runInContext(teardown, box); } catch (e) {} }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(0);
})();
"""


def run_scenarios(
    tmp: Path,
    modules: list[str],
    scenarios: dict[str, str],
    extra_prelude: str = "",
    teardown: str = "",
) -> dict:
    """Run every scenario in a fresh vm context with the named static/js modules loaded."""
    tmp.mkdir(parents=True, exist_ok=True)
    prelude = tmp / "prelude.js"
    prelude.write_text(DOM + CESIUM + extra_prelude, encoding="utf-8")
    driver = tmp / "driver.js"
    driver.write_text(DRIVER, encoding="utf-8")
    cases = tmp / "scenarios.json"
    cases.write_text(json.dumps(scenarios), encoding="utf-8")
    paths = json.dumps([str(JS_DIR / m) for m in modules])
    out = subprocess.run(
        [NODE, str(driver), str(prelude), paths, str(cases), teardown],
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def value(results: dict, name: str):
    """The scenario's return value, or a failure carrying the JavaScript stack."""
    r = results[name]
    assert r["ok"], r.get("error")
    return r["value"]
