"""Shared Node harness for the globe UX module tests.

Used by tests/test_globe_viewmode.py, test_globe_camwall.py,
test_globe_omnisearch.py and test_globe_whatshere.py. Plain browser JavaScript
runs under Node's ``vm`` against a hand-written DOM stub and a fake Cesium, the
way tests/test_globe_cameras.py does it (no jsdom, no browser). The fake
``fetch`` answers from a table, so nothing touches the network.

Each scenario is the body of an async function returning a JSON-able value,
run in a fresh vm context with the listed modules loaded in order.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

NODE = shutil.which("node")
JS_DIR = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "static" / "js"

PRELUDE = r"""
var DOC_NODES = [];
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.disabled = false; this.checked = false; this.dataset = {};
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
El.prototype.replaceChild = function (fresh, old) {
  var at = this.children.indexOf(old);
  if (at < 0) throw new Error('replaceChild: not a child');
  if (fresh.parentNode) fresh.parentNode.removeChild(fresh);
  this.children[at] = fresh; fresh.parentNode = this; old.parentNode = null; return old;
};
El.prototype.replaceChildren = function () {
  var self = this;
  this.children.slice().forEach(function (c) { self.removeChild(c); });
  this._text = '';
  for (var i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
};
El.prototype.cloneNode = function (deep) {
  var n = new El(this.tagName);
  n.className = this.className; n.id = this.id; n.value = this.value; n._text = this._text;
  for (var k in this.attrs) n.attrs[k] = this.attrs[k];
  if (deep) this.children.forEach(function (c) { n.appendChild(c.cloneNode(true)); });
  return n;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.hasAttribute = function (k) { return k in this.attrs; };
El.prototype.addEventListener = function (t, fn)
  { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.removeEventListener = function (t, fn) {
  var l = this.listeners[t] || []; var at = l.indexOf(fn); if (at >= 0) l.splice(at, 1);
};
El.prototype.focus = function () { document.activeElement = this; this.focused = true; };
El.prototype.blur = function () {
  if (document.activeElement === this) document.activeElement = null;
  this.focused = false; };
El.prototype.select = function () {};
El.prototype.scrollIntoView = function () {};
El.prototype.canPlayType = function () { return ''; };
El.prototype.pause = function () { this.paused = true; };
El.prototype.play = function () { this.paused = false; return Promise.resolve(); };
El.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 800,
  height: 600 }; };
Object.defineProperty(El.prototype, 'isConnected', { get: function () {
  var n = this; while (n.parentNode) n = n.parentNode; return n === document.body ||
    n === document.head; } });
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
        if (on) this.add(c); else this.remove(c); return on; }
    };
  }
});
El.prototype.has = function (cls) { return this.classList.contains(cls); };
function fire(target, type, ev) {
  ev = ev || {}; ev.type = type; ev.target = ev.target || target;
  ev.preventDefault = function () { ev.defaultPrevented = true; };
  ev.stopPropagation = function () { ev.stopped = true; };
  ev.stopImmediatePropagation = function () { ev.immediate = true; ev.stopped = true; };
  var fns = (target.listeners[type] || []).slice();
  for (var i = 0; i < fns.length; i++) { fns[i].call(target, ev); if (ev.immediate) break; }
  return ev;
}
El.prototype.fire = function (type, ev) { return fire(this, type, ev); };
function all(el, out) { out = out || []; out.push(el); el.children.forEach(function (c) { all(c,
  out); }); return out; }
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }
function byText(el, text) { return all(el).filter(function (x) { return x.tagName === 'BUTTON' &&
  x.textContent === text; }); }

var document = {
  head: new El('head'), body: new El('body'), listeners: {}, hidden: false,
  visibilityState: 'visible', activeElement: null,
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  },
  addEventListener: function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    var l = this.listeners[t] || []; var at = l.indexOf(fn); if (at >= 0) l.splice(at, 1); }
};
function setHidden(hidden) {
  document.hidden = !!hidden; document.visibilityState = hidden ? 'hidden' : 'visible';
  fire(document, 'visibilitychange', {});
}
function key(k, target) { return fire(document, 'keydown', { key: k, target: target ||
  document.body }); }

// ---- storage -------------------------------------------------------------
function FakeStorage() { this.data = {}; }
FakeStorage.prototype.getItem = function (k) { return k in this.data ? this.data[k] : null; };
FakeStorage.prototype.setItem = function (k, v) { this.data[k] = String(v); };
FakeStorage.prototype.removeItem = function (k) { delete this.data[k]; };

// ---- fake Cesium ---------------------------------------------------------
function CEvent() { this.fns = []; }
CEvent.prototype.addEventListener = function (fn) { var self = this; this.fns.push(fn);
  return function () { var at = self.fns.indexOf(fn); if (at >= 0) self.fns.splice(at, 1); }; };
CEvent.prototype.removeEventListener = function (fn) { var at = this.fns.indexOf(fn);
  if (at >= 0) this.fns.splice(at, 1); };
CEvent.prototype.raiseEvent = function () { var a = arguments;
  this.fns.slice().forEach(function (f) { f.apply(null, a); }); };
var HANDLERS = [];
function SSEH(canvas) { this.canvas = canvas; this.actions = {}; HANDLERS.push(this); }
SSEH.prototype.setInputAction = function (fn, type) { this.actions[type] = fn; };
SSEH.prototype.removeInputAction = function (type) { delete this.actions[type]; };
SSEH.prototype.destroy = function () { this.destroyed = true; };
function fireInput(type, ev) { HANDLERS.forEach(function (h) {
  if (!h.destroyed && h.actions[type]) h.actions[type](ev); }); }
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var Cesium = {
  SceneMode: { MORPHING: 0, COLUMBUS_VIEW: 1, SCENE2D: 2, SCENE3D: 3 },
  ScreenSpaceEventHandler: SSEH,
  ScreenSpaceEventType: { LEFT_CLICK: 0, LEFT_DOUBLE_CLICK: 1, RIGHT_CLICK: 5, MOUSE_MOVE: 15 },
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat,
    h: h || 0 }; } },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  Cartographic: { fromCartesian: function (p) {
    return p ? { latitude: p.lat * Math.PI / 180, longitude: p.lon * Math.PI / 180,
      height: p.h || 0 } : undefined; } },
  Math: { toRadians: function (d) { return d * Math.PI / 180; } },
  JulianDate: { toDate: function (j) { return j; } }
};
var window = this;
this.Cesium = Cesium;

function makeViewer(opts) {
  opts = opts || {};
  var SM = Cesium.SceneMode;
  var scene = {
    mode: opts.mode === undefined ? SM.SCENE3D : opts.mode, morphs: [],
    morphComplete: new CEvent(), canvas: new El('canvas'),
    globe: { ellipsoid: {} },
    primitives: { add: function (c) { return c; } }
  };
  function morph(name, mode) { return function (d) { scene.morphs.push([name, d]);
    scene.mode = mode; scene.morphComplete.raiseEvent(); }; }
  scene.morphTo2D = morph('2d', SM.SCENE2D);
  scene.morphTo3D = morph('3d', SM.SCENE3D);
  scene.morphToColumbusView = morph('cv', SM.COLUMBUS_VIEW);
  var camera = {
    flights: [], positionCartographic: { height: opts.height || 2e7, latitude: 0, longitude: 0 },
    moveEnd: new CEvent(), moveStart: new CEvent(),
    flyTo: function (o) { this.flights.push(o); this.flown = o; },
    cancelFlight: function () { this.cancelled = (this.cancelled || 0) + 1; },
    pickEllipsoid: function (pos) {
      if (!pos || pos.x < 0) return undefined;
      return opts.pick ? opts.pick(pos) : { lon: -104.99, lat: 39.74, h: 0 };
    }
  };
  var entities = { values: opts.entities || [],
    getById: function (id) { return this.values.filter(function (e) { return e.id === id;
      })[0]; } };
  return { scene: scene, camera: camera, canvas: scene.canvas, entities: entities,
    clock: { currentTime: opts.now || new Date('2026-11-19T18:00:00Z') } };
}

// ---- fake fetch ----------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
function route(prefix, body, status) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200 }); }
function fetch(url, init) {
  REQUESTS.push(String(url));
  for (var i = 0; i < ROUTES.length; i++) {
    if (String(url).indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      var body = typeof r.body === 'function' ? r.body(url) : r.body;
      return Promise.resolve({ ok: r.status < 400, status: r.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); } });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({});
    } });
}
function requested(prefix) { return REQUESTS.filter(function (u) { return u.indexOf(prefix) === 0;
  }); }

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: opts.viewer || makeViewer(opts), api: opts.api || '', isStatic: !!opts.isStatic,
    mount: new El('div'),
    satEngine: opts.satEngine || { sats: [], byNoradId: {}, selectedId: null },
    selectSatellite: function (id) { (ctx.selected = ctx.selected || []).push(id); },
    addIntelItem: function (t) { feed.push(t); },
    storage: opts.storage
  };
  if (opts.simNow) ctx.simNow = opts.simNow;
  document.body.appendChild(ctx.mount);
  return { ctx: ctx, feed: feed };
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle(n) { for (var i = 0; i < (n || 8); i++) await tick(); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modules = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              setInterval: setInterval, clearInterval: clearInterval, Promise: Promise,
              JSON: JSON, Math: Math, Date: Date, Intl: Intl, console: console };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  modules.forEach(function (p) {
    vm.runInContext(fs.readFileSync(p, 'utf8'), box, { filename: p.split('/').pop() });
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
    try {
      vm.runInContext('[window.SDACamWall, window.SDACameras, window.SDAWhatsHere,' +
        ' window.SDAOmniSearch, window.SDAViewMode].forEach(function (m) {' +
        ' try { if (m && m.destroy) m.destroy(); else if (m && m.close) m.close(); }' +
        ' catch (e) {} });', box);
    } catch (e) { /* the scenario already failed */ }
  }
  process.stdout.write(JSON.stringify(results));
  process.exit(0);
})();
"""


class Ran:
    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, name: str):
        result = self._raw[name]
        assert result["ok"], result["error"]
        return result["value"]


def run(work: Path, modules: list[str], scenarios: dict[str, str], extra_prelude: str = "") -> Ran:
    """Run every scenario with *modules* (file names under static/js) loaded in order."""
    assert NODE is not None
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE + "\n" + extra_prelude, encoding="utf-8")
    (work / "modules.json").write_text(
        json.dumps([(JS_DIR / m).as_posix() for m in modules]), encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(scenarios), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), str(work / "modules.json"),
         str(work / "scenarios.json")],
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


def run_inline(modules: list[str], body: str, extra_prelude: str = ""):
    """Run one scenario body in a scratch directory and return its value."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return run(Path(tmp), modules, {"inline": body}, extra_prelude)["inline"]
