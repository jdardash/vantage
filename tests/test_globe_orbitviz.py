"""Tests for the globe orbit-visuals module (static/js/orbitviz.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_peaks.py does it. The
fake `fetch` answers from a table, so nothing here touches the network. Skips cleanly
when Node is not installed.

The server bodies the module reads (/api/orbitviz/attributes, /debris, /history) are
built here by sda.routes.orbitviz itself, so the JavaScript decoder is tested against
the real encoder. The frame maths (sidereal angle, Sun, geodetic position) is checked
against sda.passes.

Each scenario is the body of an async function returning a JSON-able value, run in a
fresh vm context; the module-scoped `ran` fixture holds every result.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sgp4.earth_gravity import wgs72

from sda.passes import Observer, eci_to_earth_fixed, gmst_rad, solar_position_eci_km
from sda.routes.orbitviz import DEBRIS_EVENTS, build_attributes, build_debris, build_history
from sda.satcat import owner_name, parse_satcat

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "src" / "sda" / "templates" / "static" / "js" / "orbitviz.js"
SAMPLE = json.loads((ROOT / "tests" / "data" / "orbitviz" / "satcat_sample.json")
                    .read_text(encoding="utf-8"))

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MU, RE, J2 = wgs72.mu, wgs72.radiusearthkm, wgs72.j2
DEG = math.pi / 180


def _ms(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp() * 1000)


SIM_MS = _ms(2026, 3, 22, 12)
TIMES = [SIM_MS, _ms(2000, 1, 1, 12), _ms(2026, 6, 21, 0), _ms(2031, 12, 3, 17, 45)]

# SATCAT rows (Spacebook layout) for objects in the scenario catalog, plus one decayed
# object that /attributes must leave out.
SATCAT_ROWS = [
    {"id": "g5", "noradId": 5, "name": "VANGUARD 1", "intlId": "1958-002B",
     "launchDate": "1958-03-17T00:00:00", "objectType": "PAYLOAD", "owner": "US",
     "launchSite": "AFETR", "rcsSize": "MEDIUM"},
    {"id": "g2", "noradId": 2, "name": "SPUTNIK 1", "intlId": "1957-001B",
     "launchDate": "1957-10-04T00:00:00", "decayDate": "1958-01-03T00:00:00",
     "objectType": "PAYLOAD", "owner": "CIS", "launchSite": "TYMSC", "rcsSize": ""},
    {"id": "g22566", "noradId": 22566, "name": "SL-16 R/B", "intlId": "1993-016B",
     "launchDate": "1993-03-26T00:00:00", "objectType": "ROCKET BODY", "owner": "CIS",
     "launchSite": "TYMSC", "rcsSize": "LARGE"},
    {"id": "g25544", "noradId": 25544, "name": "ISS (ZARYA)", "intlId": "1998-067A",
     "launchDate": "1998-11-20T00:00:00", "objectType": "PAYLOAD", "owner": "ISS",
     "launchSite": "TYMSC", "rcsSize": "LARGE"},
    {"id": "g26824", "noradId": 26824, "name": "INTELSAT 901", "intlId": "2001-024A",
     "launchDate": "2001-06-09T00:00:00", "objectType": "PAYLOAD", "owner": "ITSO",
     "launchSite": "FRGUI", "rcsSize": "LARGE"},
    {"id": "g28163", "noradId": 28163, "name": "MOLNIYA 1-93", "intlId": "2004-005A",
     "launchDate": "2004-02-18T00:00:00", "objectType": "UNKNOWN", "owner": "CIS",
     "launchSite": "PKMTR", "rcsSize": ""},
    {"id": "g29733", "noradId": 29733, "name": "FENGYUN 1C DEB", "intlId": "1999-025AAA",
     "launchDate": "1999-05-10T00:00:00", "objectType": "DEBRIS", "owner": "PRC",
     "launchSite": "TSC", "rcsSize": "SMALL"},
    {"id": "g44700", "noradId": 44700, "name": "STARLINK-1001", "intlId": "2019-074A",
     "launchDate": "2019-11-11T00:00:00", "objectType": "PAYLOAD", "owner": "US",
     "launchSite": "AFETR", "rcsSize": "LARGE"},
    {"id": "g99999", "noradId": 99999, "name": "NO ELEMENTS", "intlId": "2022-001A",
     "launchDate": "2022-01-13T00:00:00", "objectType": "PAYLOAD", "owner": "JPN",
     "launchSite": "TNSTA", "rcsSize": "SMALL"},
]
ATTRS = build_attributes(parse_satcat(SATCAT_ROWS).values(), on_orbit=True)
SAMPLE_ENTRIES = list(parse_satcat(SAMPLE).values())
DEBRIS = {**build_debris(SAMPLE_ENTRIES, top=5, min_pieces=2), "source": "spacebook"}
HISTORY = build_history(SAMPLE_ENTRIES, 2026)

PRELUDE_TEMPLATE = r"""
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
El.prototype.replaceChildren = function () {
  var self = this;
  this.children.slice().forEach(function (c) { self.removeChild(c); });
  this._text = '';
  for (var i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, fn)
  { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.getContext = function () {
  if (this.tagName !== 'CANVAS') return null;
  var calls = this._calls = this._calls || {};
  function f(name) { return function () { calls[name] = (calls[name] || 0) + 1; }; }
  return { clearRect: f('clearRect'), fillRect: f('fillRect'), fillText: f('fillText'),
    beginPath: f('beginPath'), moveTo: f('moveTo'), lineTo: f('lineTo'), stroke: f('stroke'),
    arc: f('arc'), fill: f('fill'), drawImage: f('drawImage') };
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
      toggle: function (c) { if (this.contains(c)) { this.remove(c); return false; } this.add(c);
        return true; }
    };
  }
});
El.prototype.has = function (cls) { return this.classList.contains(cls); };
El.prototype.fire = function (type, ev) {
  ev = ev || {}; ev.type = type;
  ev.stopImmediatePropagation = function () { ev.immediate = true; };
  var fns = this.listeners[type] || [];
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
function input(id, value) { var e = new El('input'); e.id = id; e.value = value; return e; }

var DOC_LISTENERS = {};
var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  },
  addEventListener: function (t, fn) { (DOC_LISTENERS[t] = DOC_LISTENERS[t] || []).push(fn); }
};
function dispatch(type) {
  (DOC_LISTENERS[type] || []).forEach(function (fn) { fn({ type: type }); });
}

var STORE = {};
var localStorage = {
  getItem: function (k) { return k in STORE ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); }
};

// ---- fake Cesium ---------------------------------------------------------------
var COUNT = { color: 0 };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; COUNT.color++; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
function Mat3() { for (var i = 0; i < 9; i++) this[i] = 0; }
Mat3.fromRotationZ = function (angle, r) {
  r = r || new Mat3();
  var c = Math.cos(angle), s = Math.sin(angle);
  r[0] = c; r[1] = s; r[2] = 0; r[3] = -s; r[4] = c; r[5] = 0; r[6] = 0; r[7] = 0; r[8] = 1;
  r.angle = angle;
  return r;
};
function Mat4() { this.rot = null; this.trans = null; this.angle = null; }
Mat4.IDENTITY = new Mat4();
Mat4.clone = function () { return new Mat4(); };
Mat4.fromRotationTranslation = function (rot, t, r) {
  r = r || new Mat4();
  r.rot = []; for (var i = 0; i < 9; i++) r.rot.push(rot[i]);
  r.angle = rot.angle === undefined ? null : rot.angle;
  r.trans = { x: t.x, y: t.y, z: t.z };
  return r;
};
function Cart3(x, y, z) { this.x = x || 0; this.y = y || 0; this.z = z || 0; }
Cart3.ZERO = new Cart3(0, 0, 0);
Cart3.fromDegrees = function (lon, lat, h, ell, r) {
  r = r || new Cart3(); r.lon = lon; r.lat = lat; r.h = h; return r;
};
function Coll() { this.items = []; this.show = true; this.modelMatrix = new Mat4(); }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Prim(o) { this.opts = o; this.modelMatrix = o.modelMatrix; this.show = true; }
function Opts(o) { this.opts = o; }
var Cesium = {
  Color: { fromCssColorString: function (c) { return new Col(c); }, WHITE: new Col('#fff') },
  PolylineCollection: Coll, Matrix3: Mat3, Matrix4: Mat4, Cartesian3: Cart3,
  Material: { fromType: function (t, u) { return { type: t, uniforms: { color: u.color } }; } },
  CylinderGeometry: Opts, GeometryInstance: Opts, PerInstanceColorAppearance: Opts,
  ColorGeometryInstanceAttribute: { fromColor: function (c) { return { color: c }; } },
  Primitive: Prim,
  SceneMode: { SCENE3D: 3, SCENE2D: 2 },
  JulianDate: { computeTaiMinusUtc: function () { return 37; } },
  buildModuleUrl: function (p) { return 'cesium/' + p; }
};
Cesium.PerInstanceColorAppearance.VERTEX_FORMAT = 'per-instance';
var window = this;
this.Cesium = Cesium;

// ---- fake fetch ----------------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
var FETCH_DELAY = 0;
var PENDING = {};
var MAX_PENDING_IDS = 0;
function route(prefix, body, status) {
  ROUTES.unshift({ prefix: prefix, body: body, status: status || 200 });
}
function idOf(url, opts) {
  var hit = /norad_id=(\d+)|history\/(\d+)/.exec(url);
  if (hit) return hit[1] || hit[2];
  if (opts && opts.body) {
    var b = JSON.parse(opts.body);
    if (b.norad_id) return String(b.norad_id);
  }
  return '';
}
function fetch(url, opts) {
  REQUESTS.push({ url: url, method: (opts && opts.method) || 'GET',
    body: opts && opts.body ? JSON.parse(opts.body) : null });
  var reply = { status: 404, body: { detail: 'Not Found' } };
  for (var i = 0; i < ROUTES.length; i++) {
    var r = ROUTES[i];
    if (url.indexOf(r.prefix) === 0) {
      reply = typeof r.body === 'function' ? r.body(url, opts)
        : { status: r.status, body: r.body };
      break;
    }
  }
  var id = idOf(url, opts);
  if (id) {
    PENDING[id] = (PENDING[id] || 0) + 1;
    MAX_PENDING_IDS = Math.max(MAX_PENDING_IDS, Object.keys(PENDING).length);
  }
  return new Promise(function (resolve) {
    setTimeout(function () {
      if (id && --PENDING[id] === 0) delete PENDING[id];
      resolve({ ok: reply.status < 400, status: reply.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(reply.body))); } });
    }, FETCH_DELAY);
  });
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
async function settle() { for (var i = 0; i < 12; i++) await tick(); }

// ---- server bodies built by sda.routes.orbitviz ----------------------------------
var ATTRS = __ATTRS__;
var DEBRIS = __DEBRIS__;
var HISTORY = __HISTORY__;
var TIMES = __TIMES__;

// ---- a small catalog in the satEngine record shape ---------------------------------
var SIM = __SIM__;
var PAGE = {};
var CONJ = [];
function pad(s, n, right) { s = String(s); while (s.length < n) s = right ? s + ' ' : ' ' + s;
  return s; }
function zpad(v, n) { return pad(v, n).replace(/ /g, '0'); }
function line1(id, intl, epoch) {
  return '1 ' + zpad(id, 5) + 'U ' + pad(intl, 8, true) + ' ' + epoch +
    '  .00000000  00000+0  00000-0 0  9990';
}
function line2(id, inc, raan, ecc, n) {
  return '2 ' + zpad(id, 5) + ' ' + pad(inc.toFixed(4), 8) + ' ' + pad(raan.toFixed(4), 8) +
    ' ' + zpad(Math.round(ecc * 1e7), 7) + ' ' + pad('0.0000', 8) + ' ' + pad('0.0000', 8) +
    ' ' + pad(n.toFixed(8), 11) + '    10';
}
function rec(id, name, intl, epoch, inc, raan, ecc, n) {
  var base = new Col('page-' + id);
  PAGE[id] = base;
  return { noradId: id, name: name, regime: 'LEO', color: base,
    line1: line1(id, intl, epoch), line2: line2(id, inc, raan, ecc, n), satrec: null,
    point: { color: base, outlineColor: 'page-outline' }, label: {},
    lastLat: 0, lastLon: 0, lastAlt: 500, located: false, hidden: false, hideMask: 0 };
}
function catalog() {
  var list = [rec(25544, 'ISS (ZARYA)', '98067A', '26081.50000000', 51.64, 10.7, 0.0006, 15.49)];
  for (var k = 0; k < 10; k++) {
    list.push(rec(44700 + k, 'STARLINK-' + (1001 + k), '19074' + String.fromCharCode(65 + k),
      '26081.50000000', 53.0, (k < 5 ? 10 : 40) + 0.2 * (k % 5), 0.0001, 15.06));
  }
  list.push(rec(29733, 'FENGYUN 1C DEB', '99025AAA', '26075.50000000', 98.8, 100, 0.002, 14.2));
  list.push(rec(29734, 'FENGYUN 1C DEB', '99025AAB', '26075.50000000', 98.7, 101, 0.003, 14.25));
  list.push(rec(26824, 'INTELSAT 901', '01024A', '26041.50000000', 0.02, 80, 0.0002, 1.0027));
  list.push(rec(22566, 'SL-16 R/B', '93016B', '26079.50000000', 71.0, 200, 0.001, 14.1));
  list.push(rec(36585, 'GPS BIIF-1 (PRN 25)', '10022A', '26081.00000000', 55.0, 300, 0.01,
    2.0056));
  list.push(rec(28163, 'MOLNIYA 1-93', '04005A', '26060.50000000', 62.8, 20, 0.7, 2.006));
  list.push(rec(5, 'VANGUARD 1', '58002B', '26081.50000000', 34.2, 50, 0.18, 10.85));
  var none = rec(99999, 'NO ELEMENTS', '', '', 0, 0, 0, 0);
  none.line1 = ''; none.line2 = '';
  list.push(none);
  return list;
}

function makeViewer() {
  var prims = [];
  var pre = [];
  return {
    scene: { mode: 3, primitives: { add: function (p) { prims.push(p); return p; } },
      preUpdate: { addEventListener: function (fn) { pre.push(fn); } } },
    _prims: prims, _pre: pre
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var list = catalog();
  var by = {};
  list.forEach(function (s) { by[s.noradId] = s; });
  var calls = { select: [], refresh: 0, intel: [] };
  var ctx = {
    viewer: makeViewer(), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    satEngine: { sats: list, byNoradId: by, selectedId: null, lastLabelRebuild: 1 },
    selectSatellite: function (id) { calls.select.push(id); },
    addIntelItem: function (t) { calls.intel.push(t); },
    getVisibleLayers: function () { return {}; },
    simNow: function () { return new Date(SIM); },
    refreshSatVisibility: function () { calls.refresh++; },
    getConjunction: function () { return null; },
    getConjunctions: function () { return CONJ; }
  };
  document.body.appendChild(ctx.mount);
  return { ctx: ctx, calls: calls, list: list, by: by };
}

function start(opts) {
  var k = makeCtx(opts);
  k.ok = window.SDAOrbitViz.init(k.ctx);
  k.m = window.SDAOrbitViz._state();
  return k;
}
function legend(k) {
  return byClass(k.m.legend, 'ov-item').map(function (b) {
    return byClass(b, 'ov-count')[0].textContent; });
}
function labels(k) {
  return byClass(k.m.legend, 'ov-item').map(function (b) {
    return byClass(b, 'ov-label')[0].textContent; });
}
function hiddenIds(k) {
  return k.list.filter(function (s) { return s.hidden; }).map(function (s) { return s.noradId; })
    .sort(function (a, b) { return a - b; });
}
"""

PRELUDE = (
    PRELUDE_TEMPLATE
    .replace("__ATTRS__", json.dumps(ATTRS))
    .replace("__DEBRIS__", json.dumps(DEBRIS))
    .replace("__HISTORY__", json.dumps(HISTORY))
    .replace("__TIMES__", json.dumps(TIMES))
    .replace("__SIM__", str(SIM_MS))
)

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              setInterval: setInterval, clearInterval: clearInterval, Promise: Promise,
              JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'orbitviz.js' });
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
      // stop the history player a scenario left running
      try { vm.runInContext('window.SDAOrbitViz && window.SDAOrbitViz.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("orbitviz")
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


def fmt_int(n: float) -> str:
    return f"{round(n):,}"


def history_text(hist: dict, year: int) -> str:
    i = year - hist["first_year"]
    kinds = [("payload", "PAYLOAD"), ("rocket body", "ROCKET BODY"), ("debris", "DEBRIS"),
             ("unknown", "UNKNOWN")]
    parts = ", ".join(f"{label} {fmt_int(hist['in_orbit'][key][i])}" for label, key in kinds)
    return f"{year}: {fmt_int(hist['total'][i])} in orbit at year end ({parts})"


def alt_for(n_rev_day: float) -> float:
    n = n_rev_day * 2 * math.pi / 86400
    return (MU / n ** 2) ** (1 / 3) - RE


def footprint_km(alt: float, min_el_deg: float) -> float:
    el = min_el_deg * DEG
    return (math.acos(RE * math.cos(el) / (RE + alt)) - el) * RE


# --- pure helpers ------------------------------------------------------------------

scenario("helpers", r"""
  var V = window.SDAOrbitViz;
  var iss1 = '1 25544U 98067A   26081.16296743  .00015089  00000+0  28686-3 0  9990';
  var iss2 = '2 25544  51.6343  10.6992 0006186 218.8034 141.2511 15.48444672558264';
  var el = V.parseLine2(iss2);
  var sso = V.parseLine2(line2(29733, 98.7, 100, 0.001, 14.2));
  var sun = { x: 1, y: 0, z: 0 };
  var e2 = V.parseLine1(iss1).epochMs;
  var hist = V.historyFromYears([{ year: 1957, type: 0 }, { year: 1958, type: 2 },
    { year: 0, type: 0 }, { year: 2030, type: 1 }], 1960);
  var planes = V.clusterPlanes([
    { id: 1, inc: 53, alt: 550, raan: 359.0 }, { id: 2, inc: 53, alt: 550, raan: 359.6 },
    { id: 3, inc: 53, alt: 550, raan: 0.2 }, { id: 4, inc: 53, alt: 550, raan: 0.8 },
    { id: 5, inc: 53, alt: 550, raan: 1.4 }, { id: 6, inc: 53, alt: 550, raan: 90 },
    { id: 7, inc: 53, alt: 550, raan: 90.5 }, { id: 8, inc: 53, alt: 550, raan: 91 },
    { id: 9, inc: 70, alt: 550, raan: 10 }, { id: 10, inc: 0.05, alt: 35786, raan: 10 },
    { id: 11, inc: 0.03, alt: 35786, raan: 200 }], { incGap: 1, altGap: 50, raanGap: 5,
    min: 2 });
  return {
    l1: V.parseLine1(iss1), l1bad: V.parseLine1('short'), el: el,
    l2bad: [V.parseLine2(''), V.parseLine2(null)],
    shape: V.orbitShape(el),
    regimes: [V.regimeIndex(el),
      V.regimeIndex(V.parseLine2(line2(1, 0.02, 80, 0.0002, 1.0027))),
      V.regimeIndex(V.parseLine2(line2(1, 55, 300, 0.01, 2.0056))),
      V.regimeIndex(V.parseLine2(line2(1, 62.8, 20, 0.7, 2.006))),
      V.regimeIndex(V.parseLine2(line2(1, 34.2, 50, 0.18, 10.85))),
      V.regimeIndex(V.parseLine2(line2(1, 1, 50, 0.01, 0.5))),
      V.regimeIndex(null)],
    ages: [0.5, 2, 5, 10, 45, -0.2, NaN].map(V.ageBucket),
    decades: [1957, 1979, 1980, 1999, 2015, 2026, 2031, 0, 1950].map(V.decadeIndex),
    types: ['ISS (ZARYA)', 'SL-16 R/B', 'FENGYUN 1C DEB', 'ISS OBJECT A', 'DEBUT',
      'COSMOS 2251 DEBRIS', null].map(V.typeFromName),
    constellations: ['STARLINK-1008', 'STARLINK-1008 DEB', 'ONEWEB-0012',
      'GPS BIIF-1 (PRN 25)', 'NAVSTAR 81 (USA 319)', 'GSAT0101 (GALILEO-PFM)',
      'COSMOS 2425 (GLONASS-M)', 'BEIDOU-3 M1', 'IRIDIUM 106', 'IRIDIUM 33 DEB', 'KUIPER-P1',
      'ISS (ZARYA)', ''].map(V.constellationOf),
    rates: [V.raanRateDegDay(el), V.raanRateDegDay(sso)],
    raanLater: V.raanAt(el, e2, e2 + 86400000), raanNoEpoch: V.raanAt(el, NaN, 0),
    split: V.splitCircular([{ raan: 359.5 }, { raan: 0.3 }, { raan: 180 }, { raan: 0.8 },
      { raan: 180.5 }], 5).map(function (g) { return g.map(function (x) { return x.raan; }); }),
    planes: planes.map(function (p) { return { inc: p.inc, alt: p.alt, raan: p.raan,
      ids: p.ids.slice().sort(function (a, b) { return a - b; }) }; }),
    ring: V.ringPoints(53, 30, 7000, 4),
    shadow: [V.shadowClass(-7000, 0, 0, sun), V.shadowClass(-7000, 6383.135, 0, sun),
      V.shadowClass(7000, 0, 0, sun), V.shadowClass(-7000, 7000, 0, sun),
      V.shadowClass(-42164, 0, 0, sun), V.shadowClass(-42164, 6300, 0, sun)],
    umbra: [V.umbraRadiusKm(0), V.umbraRadiusKm(60000), V.umbraRadiusKm(42164),
      V.umbraRadiusKm(5e6)],
    penumbra: [V.penumbraRadiusKm(60000), V.penumbraRadiusKm(42164)],
    foot: [V.footprintAngle(400, 0), V.footprintAngle(400, 10), V.footprintAngle(35786, 10),
      V.footprintAngle(0, 0), V.footprintAngle(NaN, 0)],
    circle: V.circlePoints(51.5, 0, 0.3, 8),
    polar: V.circlePoints(80, 120, 0.3, 12),
    night: [V.nightSpan(180, 0, 0), V.nightSpan(0, 0, 0), V.nightSpan(180, 23, 0),
      V.nightSpan(180, -23, 0)],
    equinox: V.subsolarPoint(Date.UTC(2026, 2, 20, 12)),
    gmst: TIMES.map(V.gmstRad),
    sunEci: TIMES.map(function (t) { return V.sunEci(t); }),
    sunFixed: TIMES.map(function (t) { return V.sunEarthFixed(t, {}); }),
    geo: [V.geodeticToEarthFixed(51.5, -0.1, 0.02), V.geodeticToEarthFixed(-33.9, 151.2, 0),
      V.geodeticToEarthFixed(90, 0, 1)],
    designator: V.launchDesignatorOf(iss1),
    fmt: [0, 999, 1000, 1234567, -1500, NaN, 12.6].map(V.fmtInt),
    utc: [V.formatUtc('2026-03-22T19:47:21Z'), V.formatUtc('nonsense'), V.formatUtc(0)],
    next: V.nextPass([{ rise_time: '2026-03-22T10:00:00Z', set_time: '2026-03-22T10:10:00Z' },
      { rise_time: '2026-03-22T21:00:00Z', set_time: '2026-03-22T21:10:00Z' },
      { rise_time: '2026-03-22T11:55:00Z', set_time: '2026-03-22T12:05:00Z' }], SIM),
    noNext: V.nextPass(null, SIM),
    newest: V.newestConjunction(25544, [[
      { primary: 25544, secondary: 1, tca: '2026-03-21T00:00:00Z' },
      { primary: 7, secondary: 25544, tca: '2026-03-23T00:00:00Z' },
      { primary: 7, secondary: 8, tca: '2026-03-24T00:00:00Z' }], null,
      [{ primary: 25544, secondary: 9, tca: 'bad' }]]),
    conjText: [V.conjunctionText(25544, { primary: 25544, secondary: 33757,
      secondary_name: 'COSMOS 2251 DEB', tca: '2026-03-22T13:00:00Z', miss_distance_km: 0.42,
      risk: 'high' }), V.conjunctionText(7, { primary: 25544, primary_name: '',
      secondary: 7, tca: '2026-03-22T13:00:00Z' }), V.conjunctionText(1, null)],
    flags: [V.flagText(null), V.flagText({ snapshots: 3, maneuvers: [], decay_anomalies: [] }),
      V.flagText({ snapshots: 1200, maneuvers: [{ end_epoch: '2026-02-01T00:00:00Z' },
        { end_epoch: '2026-03-01T06:00:00Z' }], decay_anomalies: [{}] })],
    passes: [V.passText(null), V.passText({ rise_time: '2026-03-22T19:44:00Z',
      max_elevation_deg: 66.8, visible: true }), V.passText({ rise_time: '2026-03-22T03:00:00Z',
      max_elevation_deg: 12.2, sunlit: true }), V.passText({ rise_time: '2026-03-22T03:00:00Z' })],
    watch: V.cleanWatch([{ id: 25544, name: 'ISS' }, 25544, '48274', -1, 2.5, { id: 'x' },
      null, { id: 20580, name: 7 }]),
    watchCap: V.cleanWatch(Array.from({ length: 70 }, function (_, i) { return i + 1; })).length,
    watchBad: V.cleanWatch('25544'),
    observers: [V.cleanObserver({ lat: 51.5, lon: 359, altM: 20 }), V.cleanObserver({ lat: 91,
      lon: 0 }), V.cleanObserver({ lat: '', lon: 5 }), V.cleanObserver({ lat: 10, lon: 20,
      altM: 1e6 }), V.cleanObserver(null), V.cleanObserver({ lat: '12.5', lon: '-3' })],
    hist: hist, histText: [V.historyText(hist, 1958), V.historyText(hist, 1990),
      V.historyText(null, 1958)],
    decoded: (function () {
      var d = V.decodeAttributes(ATTRS);
      return { count: d.count, ids: d.ids, years: d.years, owners: d.owners,
        at25544: d.index[25544], missing: d.index[1] === undefined };
    })(),
    decodeBad: [V.decodeAttributes(null), V.decodeAttributes({ id_deltas: [1], types: 'PP' })],
    events: V.DEBRIS_EVENTS, hideBit: V.HIDE_BIT, watchMax: V.WATCH_MAX,
    modes: V.MODES.map(function (md) { return [md.key, md.classes ? md.classes.length : 0]; })
  };
""")


def test_line_parsing_and_orbit_classes(ran):
    h = ran["helpers"]
    epoch = datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000 + 80.16296743 * 86400000
    assert h["l1"]["designator"] == "1998-067" and h["l1"]["launchYear"] == 1998
    assert h["l1"]["epochMs"] == pytest.approx(epoch, abs=1)
    assert h["l1bad"] == {"designator": "", "launchYear": 0, "epochMs": None}
    assert h["el"] == pytest.approx({"inc": 51.6343, "raan": 10.6992, "ecc": 0.0006186,
                                     "n": 15.48444672})
    assert h["l2bad"] == [None, None]
    a = alt_for(15.48444672) + RE
    assert h["shape"]["a"] == pytest.approx(a, rel=1e-12)
    assert h["shape"]["perigee"] == pytest.approx(a * (1 - 0.0006186) - RE, rel=1e-12)
    assert h["shape"]["period"] == pytest.approx(1440 / 15.48444672)
    assert h["regimes"] == [0, 2, 1, 3, 3, 4, 5]
    assert h["ages"] == [0, 1, 2, 3, 4, 0, 5]
    assert h["decades"] == [0, 0, 1, 2, 4, 5, 5, 6, 6]
    assert h["types"] == [0, 1, 2, 2, 0, 2, 0]
    assert h["constellations"] == ["STARLINK", None, "ONEWEB", "GPS", "GPS", "GALILEO",
                                   "GLONASS", "BEIDOU", "IRIDIUM", None, "KUIPER", None, None]
    assert h["designator"] == "1998-067"


def test_node_drift_matches_the_j2_rate(ran):
    h = ran["helpers"]

    def rate(inc: float, ecc: float, n_rev_day: float) -> float:
        n = n_rev_day * 2 * math.pi / 86400
        p = (MU / n ** 2) ** (1 / 3) * (1 - ecc ** 2)
        return -1.5 * n * J2 * (RE / p) ** 2 * math.cos(inc * DEG) * 86400 / DEG

    iss = rate(51.6343, 0.0006186, 15.48444672)
    assert h["rates"][0] == pytest.approx(iss, rel=1e-9) and -5.1 < iss < -4.8
    # A 98.7 deg orbit near 800 km precesses with the Sun, about +0.9856 deg/day.
    assert h["rates"][1] == pytest.approx(rate(98.7, 0.001, 14.2), rel=1e-9)
    assert abs(h["rates"][1] - 0.9856) < 0.05
    assert h["raanLater"] == pytest.approx(10.6992 + iss, abs=1e-6)
    assert h["raanNoEpoch"] == pytest.approx(10.6992)


def test_planes_cluster_by_inclination_altitude_and_node(ran):
    h = ran["helpers"]
    assert h["split"] == [[180, 180.5], [359.5, 0.3, 0.8]]
    planes = h["planes"]
    assert [p["ids"] for p in planes] == [[10, 11], [1, 2, 3, 4, 5], [6, 7, 8]]
    assert planes[0]["raan"] == 0  # near-equatorial: the node is undefined
    assert planes[1]["raan"] == pytest.approx(0.2, abs=1e-6)
    assert planes[2]["raan"] == pytest.approx(90.5, abs=1e-6)
    assert planes[1]["inc"] == pytest.approx(53) and planes[1]["alt"] == pytest.approx(550)
    ring = np.array(h["ring"])
    assert np.allclose(np.linalg.norm(ring, axis=1), 7000)
    assert ring[0] == pytest.approx([7000 * math.cos(30 * DEG), 7000 * math.sin(30 * DEG), 0])
    assert ring[1][2] == pytest.approx(7000 * math.sin(53 * DEG))


def test_shadow_geometry(ran):
    h = ran["helpers"]
    assert h["shadow"] == [2, 1, 0, 0, 2, 1]
    umbra_tan = math.tan(math.asin((696000 - RE) / 149597870.7))
    pen_tan = math.tan(math.asin((696000 + RE) / 149597870.7))
    assert h["umbra"] == pytest.approx([RE, RE - 60000 * umbra_tan, RE - 42164 * umbra_tan, 0])
    assert h["penumbra"] == pytest.approx([RE + 60000 * pen_tan, RE + 42164 * pen_tan])
    assert 6090 < h["umbra"][1] < 6110 and 6650 < h["penumbra"][0] < 6670


def test_footprint_and_circles(ran):
    h = ran["helpers"]
    expected = [footprint_km(400, 0) / RE, footprint_km(400, 10) / RE,
                footprint_km(35786, 10) / RE, 0, 0]
    assert h["foot"] == pytest.approx(expected, abs=1e-12)
    for (lat0, lon0), pts in (((51.5, 0.0), h["circle"]), ((80.0, 120.0), h["polar"])):
        assert pts[0] == pytest.approx(pts[-1])
        for lat, lon in pts:
            # great-circle distance from the centre is the requested angle
            c = (math.sin(lat0 * DEG) * math.sin(lat * DEG)
                 + math.cos(lat0 * DEG) * math.cos(lat * DEG) * math.cos((lon - lon0) * DEG))
            assert math.acos(min(1.0, c)) == pytest.approx(0.3, abs=1e-9)
            assert -90 <= lat <= 90 and -180 <= lon <= 180
    assert h["circle"][0] == pytest.approx([51.5 + 0.3 / DEG, 0.0])


def test_night_and_subsolar_point(ran):
    h = ran["helpers"]
    night = h["night"]
    assert night[0] == {"from": -90, "to": 90} and night[1] is None
    # June: midnight meridian is dark from the South Pole to 67 N (midnight sun beyond).
    assert night[2]["from"] == -90 and night[2]["to"] == pytest.approx(67, abs=0.1)
    assert night[3]["to"] == 90 and night[3]["from"] == pytest.approx(-67, abs=0.1)
    assert abs(h["equinox"]["lat"]) < 1 and abs(h["equinox"]["lon"]) < 3


def test_frame_maths_matches_sda_passes(ran):
    h = ran["helpers"]
    for k, ms in enumerate(TIMES):
        when = datetime.fromtimestamp(ms / 1000, tz=UTC)
        gmst = gmst_rad(when)
        diff = (h["gmst"][k] - gmst + math.pi) % (2 * math.pi) - math.pi
        assert abs(diff) < 1e-7, when
        sun = solar_position_eci_km(when)
        js = h["sunEci"][k]
        assert [js["x"], js["y"], js["z"]] == pytest.approx(list(sun), rel=1e-9, abs=1.0)
        fixed = eci_to_earth_fixed(sun, gmst)
        unit = fixed / np.linalg.norm(fixed)
        jf = h["sunFixed"][k]
        assert [jf["x"], jf["y"], jf["z"]] == pytest.approx(list(unit), abs=1e-7)
        assert jf["dist"] == pytest.approx(float(np.linalg.norm(sun)), rel=1e-9)
    for js, (lat, lon, alt) in zip(h["geo"], [(51.5, -0.1, 0.02), (-33.9, 151.2, 0.0),
                                              (90.0, 0.0, 1.0)], strict=True):
        ref = Observer(latitude_deg=lat, longitude_deg=lon, altitude_km=alt)
        assert [js["x"], js["y"], js["z"]] == pytest.approx(
            list(ref.earth_fixed_position_km()), abs=1e-6)


def test_text_helpers(ran):
    h = ran["helpers"]
    assert h["fmt"] == ["0", "999", "1,000", "1,234,567", "-1,500", "-", "13"]
    assert h["utc"] == ["2026-03-22 19:47 UTC", "-", "1970-01-01 00:00 UTC"]
    assert h["next"]["rise_time"] == "2026-03-22T11:55:00Z" and h["noNext"] is None
    assert h["newest"]["primary"] == 7 and h["newest"]["secondary"] == 25544
    assert h["conjText"] == [
        "Conjunction 2026-03-22 13:00 UTC with COSMOS 2251 DEB, 0.42 km, HIGH",
        "Conjunction 2026-03-22 13:00 UTC with 25544, ?",
        "No conjunction on record.",
    ]
    assert h["flags"] == [
        "No element history.",
        "No maneuver or decay flags in 3 element sets.",
        "2 maneuvers, last 2026-03-01; 1 decay anomaly (1,200 element sets).",
    ]
    assert h["passes"] == [
        "No pass above 10 deg in 24 h.",
        "Next pass 2026-03-22 19:44 UTC, max 67 deg, visible to the eye",
        "Next pass 2026-03-22 03:00 UTC, max 12 deg, sunlit",
        "Next pass 2026-03-22 03:00 UTC, max ? deg",
    ]


def test_stored_values_are_cleaned(ran):
    h = ran["helpers"]
    assert h["watch"] == [{"id": 25544, "name": "ISS"}, {"id": 48274, "name": ""},
                          {"id": 20580, "name": ""}]
    assert h["watchCap"] == h["watchMax"] == 50 and h["watchBad"] == []
    assert h["observers"] == [
        {"lat": 51.5, "lon": -1, "altM": 20}, None, None, {"lat": 10, "lon": 20, "altM": 0},
        None, {"lat": 12.5, "lon": -3, "altM": 0},
    ]


def test_local_history_counts_launches(ran):
    h = ran["helpers"]
    hist = h["hist"]
    assert hist["years"] == [1957, 1958, 1959, 1960] and hist["fallback"] is True
    assert hist["in_orbit"]["PAYLOAD"] == [1, 1, 1, 1]
    assert hist["in_orbit"]["DEBRIS"] == [0, 1, 1, 1]
    assert hist["in_orbit"]["ROCKET BODY"] == [0, 0, 0, 1]
    assert hist["total"] == [1, 2, 2, 3] and hist["no_launch_date"] == 1
    assert h["histText"] == [
        "1958: 2 drawn objects launched by then (payload 1, rocket body 0, debris 1, unknown 0)",
        "", "",
    ]


def test_attributes_decode_what_the_server_encodes(ran):
    d = ran["helpers"]["decoded"]
    entries = sorted((e for e in parse_satcat(SATCAT_ROWS).values() if e.on_orbit),
                     key=lambda e: e.norad_id)
    assert d["count"] == len(entries) == 8
    assert d["ids"] == [e.norad_id for e in entries]
    assert d["years"] == [e.launch_year for e in entries]
    assert d["at25544"] == d["ids"].index(25544) and d["missing"] is True
    assert sorted(d["owners"]) == sorted({e.owner for e in entries})
    assert ran["helpers"]["decodeBad"] == [None, None]


def test_debris_events_mirror_the_server_list(ran):
    js = ran["helpers"]["events"]
    py = [{"key": e.key, "name": e.name, "designators": list(e.designators),
           "event_year": e.event_year, "kind": e.kind} for e in DEBRIS_EVENTS]
    assert js == py
    assert ran["helpers"]["hideBit"] == 4
    assert ran["helpers"]["modes"] == [["default", 0], ["type", 4], ["owner", 9], ["regime", 6],
                                       ["age", 6], ["rcs", 5], ["decade", 7], ["sunlight", 4]]


# --- the module on a page ------------------------------------------------------------

scenario("init_guards", r"""
  var V = window.SDAOrbitViz;
  var noMount = V.init({ viewer: makeViewer() });
  var noViewer = V.init({ mount: new El('div') });
  var bad = makeCtx();
  bad.ctx.mount.appendChild = function () { throw new Error('boom'); };
  var broken = V.init(bad.ctx);
  var before = [V.isEnabled(), V.colorMode(), V.watchlist(), V.setYear(2000)];
  var k = start();
  var heads = k.ctx.mount.children.map(function (p) {
    return [p.children[0].textContent, p.has('collapsed')]; });
  var watch = k.m.panels.watch;
  var ev = watch.head.fire('click');
  return { noMount: noMount, noViewer: noViewer, broken: broken, intel: bad.calls.intel,
    before: before, ok: k.ok, heads: heads,
    style: !!document.getElementById('sda-orbitviz-style'),
    opened: !watch.root.has('collapsed'), stopped: !!ev.immediate, enabled: V.isEnabled(),
    mode: V.colorMode(), untouched: k.list.every(function (s) {
      return s.color === PAGE[s.noradId]; }),
    requests: REQUESTS.length, frameHooks: k.ctx.viewer._pre.length,
    events: Object.keys(DOC_LISTENERS).sort(),
    status: k.m.watchStatus.textContent, obs: k.m.obsStatus.textContent };
""")


def test_init_guards_and_panels(ran):
    r = ran["init_guards"]
    assert r["noMount"] is False and r["noViewer"] is False
    assert r["broken"] is False and r["intel"] == ["Orbit visuals did not start: boom"]
    assert r["before"] == [False, None, [], None]
    assert r["ok"] is True and r["style"] is True and r["enabled"] is True
    assert r["heads"] == [["Colour by", False], ["Watchlist", True], ["Visible tonight", True],
                          ["Constellations", True], ["Debris clouds", True],
                          ["Catalog history", True], ["Shadow and footprint", True]]
    assert r["opened"] is True and r["stopped"] is True
    assert r["mode"] == "default" and r["untouched"] is True
    assert r["requests"] == 0 and r["frameHooks"] == 1
    assert r["events"] == ["sda:catalog-loaded", "sda:satellite-deselected",
                           "sda:satellite-selected"]
    assert r["status"] == "Select an object, then STAR SELECTED."
    assert r["obs"] == "No observer yet."


scenario("modes_from_elements", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  var out = { defaultLegend: k.m.legend.textContent };
  k.ctx.satEngine.selectedId = 25544;
  k.by[25544].point.outlineColor = 'SELECTED';
  out.regime = V.setColorMode('regime');
  out.regimeCounts = legend(k);
  out.regimeLabels = labels(k);
  out.colors = [k.by[25544].color.css, k.by[26824].color.css, k.by[99999].color.css,
    k.by[28163].color.css];
  out.pointFollows = k.by[25544].point.color === k.by[25544].color;
  out.selectedOutline = k.by[25544].point.outlineColor;
  out.outline = [k.by[26824].point.outlineColor.css, k.by[26824].point.outlineColor.a];
  out.saved = STORE['sda.orbitviz.colorMode'];
  out.labelRebuild = k.ctx.satEngine.lastLabelRebuild;
  V.setColorMode('age'); out.age = legend(k);
  V.setColorMode('type'); out.type = legend(k);
  out.typeLoading = k.m.modeNote.textContent;
  await settle();
  out.typeNote = k.m.modeNote.textContent;
  V.setColorMode('decade'); out.decade = legend(k);
  await settle();
  out.bogus = V.setColorMode('bogus');
  out.select = k.m.modeSelect.value;
  k.m.modeSelect.value = 'regime'; k.m.modeSelect.fire('change');
  out.fromSelect = [V.colorMode(), legend(k)[0]];
  out.attrRequests = REQUESTS.filter(function (r) {
    return r.url === '/api/orbitviz/attributes'; }).length;
  return out;
""")


def test_colour_modes_from_the_element_sets(ran):
    r = ran["modes_from_elements"]
    assert r["defaultLegend"] == "19 objects drawn in the page colours."
    assert r["regime"] == "regime"
    assert r["regimeCounts"] == ["14", "1", "1", "2", "0", "1"]
    assert r["regimeLabels"] == ["LEO", "MEO", "GEO", "HEO", "Beyond GEO", "No elements"]
    assert r["colors"] == ["#3987e5", "#199e70", "#55544f", "#c98500"]
    assert r["pointFollows"] is True
    assert r["selectedOutline"] == "SELECTED"  # the selection ring is the page's
    assert r["outline"] == ["#199e70", 0.6]
    assert r["saved"] == '"regime"' and r["labelRebuild"] == 0
    assert r["age"] == ["13", "1", "2", "1", "1", "1"]
    assert r["type"] == ["16", "1", "2", "0"]
    assert r["typeLoading"].endswith("Loading SATCAT...")
    assert r["typeNote"].endswith("SATCAT did not load.")
    assert r["decade"] == ["1", "0", "4", "2", "11", "0", "1"]
    assert r["bogus"] == "default" and r["select"] == "default"
    assert r["fromSelect"] == ["regime", "14"]
    assert r["attrRequests"] == 2  # a failed load is retried on the next SATCAT mode


scenario("isolate_a_class", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  V.setColorMode('regime');
  byClass(k.m.legend, 'ov-item')[2].fire('click');
  var out = {};
  out.rows = byClass(k.m.legend, 'ov-item').map(function (r) {
    return r.has('active') ? 'A' : r.has('muted') ? 'M' : '-'; }).join('');
  out.focus = k.m.focusLine.textContent;
  out.geo = [k.by[26824].color.css, k.by[26824].color.a];
  out.iss = [k.by[25544].color.css, k.by[25544].color.a];
  var before = COUNT.color;
  V.repaint(); V.repaint();
  out.allocs = COUNT.color - before;
  byClass(k.m.legend, 'ov-item')[2].fire('click');
  out.after = [k.by[25544].color.a, k.m.focusLine.textContent, k.m.focusClear.style.display];
  byClass(k.m.legend, 'ov-item')[0].fire('click');
  out.leo = k.m.focusLine.textContent;
  buttonNamed(k.m.panels.colour.body, 'CLEAR').fire('click');
  out.cleared = [k.m.focus, k.by[26824].color.a];
  byClass(k.m.legend, 'ov-item')[0].fire('click');
  V.setColorMode('age');
  out.modeChangeClears = k.m.focus;
  return out;
""")


def test_legend_rows_isolate_a_class_without_allocating(ran):
    r = ran["isolate_a_class"]
    assert r["rows"] == "MMAMMM"
    assert r["focus"] == "Highlighting GEO (orbit class (perigee, apogee, period))"
    assert r["geo"] == ["#199e70", 1] and r["iss"] == ["#3987e5", 0.12]
    assert r["allocs"] == 0
    assert r["after"] == [1, "", "none"]
    assert r["leo"] == "Highlighting LEO (orbit class (perigee, apogee, period))"
    assert r["cleared"] == [None, 1] and r["modeChangeClears"] is None


scenario("satcat_modes", r"""
  var V = window.SDAOrbitViz;
  route('/api/orbitviz/attributes', ATTRS);
  var k = start();
  var out = {};
  V.setColorMode('owner');
  out.loading = legend(k);
  await settle();
  out.owner = legend(k); out.ownerLabels = labels(k);
  out.note = k.m.modeNote.textContent;
  V.setColorMode('type'); out.type = legend(k);
  V.setColorMode('rcs'); out.rcs = legend(k);
  V.setColorMode('decade'); out.decade = legend(k);
  out.requests = REQUESTS.length;
  return out;
""")


def test_colour_modes_from_satcat(ran):
    r = ran["satcat_modes"]
    assert r["loading"] == ["0"] * 8 + ["19"]
    assert r["owner"] == ["2", "2", "1", "0", "0", "1", "0", "2", "11"]
    assert r["ownerLabels"] == [owner_name("US"), owner_name("CIS"), owner_name("PRC"),
                                "United Kingdom", "France", owner_name("JPN"), "India",
                                "Other owners", "Not in SATCAT"]
    assert r["note"].endswith("SATCAT: 8 objects on orbit.")
    assert r["type"] == ["15", "1", "2", "1"]
    assert r["rcs"] == ["4", "1", "2", "1", "11"]
    assert r["decade"] == ["1", "0", "4", "2", "11", "1", "0"]
    assert r["requests"] == 1


scenario("static_build", r"""
  var V = window.SDAOrbitViz;
  var k = start({ isStatic: true });
  var out = {};
  V.setColorMode('owner');
  out.owner = legend(k); out.note = k.m.modeNote.textContent;
  out.watch = V.toggleWatch(25544);
  await settle();
  out.lines = byClass(k.m.watchList, 'ov-watch-line').map(function (x) { return x.textContent; });
  out.vis = await V.findVisible();
  out.visStatus = k.m.visStatus.textContent;
  var d = await V.loadDebris();
  out.debrisStatus = k.m.debrisStatus.textContent;
  out.fy = d.events[0].ids;
  out.fyRow = byClass(byClass(k.m.debrisList, 'ov-watch')[0], 'ov-count')[0].textContent;
  var h = await V.loadHistory();
  out.hist = [h.fallback, h.total[h.total.length - 1], h.last_year];
  out.histNote = k.m.histNote.textContent;
  for (var id = 1; id <= 60; id++) V.toggleWatch(1000 + id);
  out.count = V.watchlist().length;
  out.full = k.m.watchStatus.textContent;
  out.requests = REQUESTS.length;
  return out;
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static_build"]
    assert r["owner"] == ["0"] * 8 + ["19"]
    assert r["note"].endswith("SATCAT needs the live server.")
    assert r["watch"] is True
    assert r["lines"] == ["Passes and element history need the live server.",
                          "No conjunction on record.", ""]
    assert r["vis"] is None and r["visStatus"] == "Visible passes need the live server."
    assert r["debrisStatus"] == "Counted from the drawn catalog only (static build)."
    assert r["fy"] == [29733, 29734] and r["fyRow"] == "2 drawn"
    assert r["hist"] == [True, 18, 2026]
    assert r["histNote"].startswith("Without SATCAT")
    assert r["count"] == 50 and r["full"] == "The watchlist holds 50 objects."
    assert r["requests"] == 0


scenario("watchlist", r"""
  var V = window.SDAOrbitViz;
  STORE['sda.orbitviz.watchlist'] = JSON.stringify([{ id: 25544, name: 'ISS' }, 'junk',
    { id: 25544 }]);
  STORE['sda.orbitviz.observer'] = JSON.stringify({ lat: 51.5, lon: -0.1, altM: 20 });
  CONJ.push({ primary: 25544, secondary: 48274, secondary_name: 'CSS (TIANHE)',
    tca: '2026-03-23T01:00:00Z', miss_distance_km: 3.2, risk: 'moderate' });
  route('/passes', function (url, opts) {
    var b = JSON.parse(opts.body);
    if (b.norad_id !== 25544) return { status: 404, body: { detail: 'not found' } };
    return { status: 200, body: { passes: [
      { rise_time: '2026-03-22T10:00:00Z', set_time: '2026-03-22T10:10:00Z',
        max_elevation_deg: 30 },
      { rise_time: '2026-03-22T19:44:00Z', set_time: '2026-03-22T19:54:00Z',
        max_elevation_deg: 66.8, visible: true }] } };
  });
  route('/conjunctions/history', [{ primary: 25544, secondary: 33757,
    secondary_name: 'COSMOS 2251 DEB', tca: '2026-03-21T08:00:00Z', miss_distance_km: 0.42,
    risk: 'HIGH' }]);
  route('/tle/history/25544/maneuvers', { norad_id: 25544, snapshots: 12,
    maneuvers: [{ end_epoch: '2026-03-01T06:00:00Z' }], decay_anomalies: [], change_log: [] });
  FETCH_DELAY = 3;
  var k = start();
  var out = {};
  out.loaded = V.watchlist();
  out.obs = k.m.obsStatus.textContent;
  out.added = V.toggleWatch(29733);
  [44700, 44701, 44702].forEach(function (id) { V.toggleWatch(id); });
  await wait(40);
  out.saved = JSON.parse(STORE['sda.orbitviz.watchlist']);
  MAX_PENDING_IDS = 0;
  out.refreshed = await V.refreshWatch(true);
  out.maxInFlight = MAX_PENDING_IDS;
  var boxes = byClass(k.m.watchList, 'ov-watch');
  out.names = boxes.map(function (b) { return byClass(b, 'ov-label')[0].textContent; });
  out.lines = boxes.slice(0, 2).map(function (b) {
    return byClass(b, 'ov-watch-line').map(function (x) { return x.textContent; }); });
  var passReq = REQUESTS.filter(function (r) {
    return r.url === '/passes' && r.body.norad_id === 25544; })[0];
  out.passReq = [passReq.method, passReq.body];
  out.conjUrls = REQUESTS.filter(function (r) {
    return r.url.indexOf('/conjunctions/history') === 0; }).map(function (r) { return r.url; });
  k.ctx.satEngine.selectedId = 22566;
  dispatch('sda:satellite-selected');
  out.starBefore = k.m.starBtn.textContent;
  k.m.starBtn.fire('click');
  out.starAfter = [k.m.starBtn.textContent, V.isWatched(22566)];
  V.highlightWatch(true);
  out.highlight = [k.by[25544].color.a, k.by[26824].color.a, k.m.focusLine.textContent,
    k.m.watchHighlightBtn.has('active')];
  V.highlightWatch(false);
  out.unhighlight = [k.by[26824].color.a, k.m.focus];
  buttonNamed(byClass(k.m.watchList, 'ov-watch')[0], 'REMOVE').fire('click');
  out.afterRemove = V.watchlist().map(function (w) { return w.id; });
  byClass(byClass(k.m.watchList, 'ov-watch')[0], 'ov-item')[0].fire('click');
  out.selected = k.calls.select;
  out.unstar = V.toggleWatch(22566);
  out.final = JSON.parse(STORE['sda.orbitviz.watchlist']).map(function (w) { return w.id; });
  await wait(30);
  return out;
""")


def test_watchlist_checks_and_persists(ran):
    r = ran["watchlist"]
    assert r["loaded"] == [{"id": 25544, "name": "ISS"}]
    assert r["obs"] == "Observer 51.500, -0.100, 20 m."
    assert r["added"] is True
    assert [w["id"] for w in r["saved"]] == [25544, 29733, 44700, 44701, 44702]
    assert r["saved"][1]["name"] == "FENGYUN 1C DEB"
    assert r["refreshed"] == 5 and 2 <= r["maxInFlight"] <= 3
    assert r["names"] == ["ISS", "FENGYUN 1C DEB", "STARLINK-1001", "STARLINK-1002",
                          "STARLINK-1003"]
    assert r["lines"][0] == [
        "Next pass 2026-03-22 19:44 UTC, max 67 deg, visible to the eye",
        "Conjunction 2026-03-23 01:00 UTC with CSS (TIANHE), 3.20 km, MODERATE",
        "1 maneuver, last 2026-03-01 (12 element sets).",
    ]
    assert r["lines"][1] == ["Not in the server catalog, no passes.",
                             "No conjunction on record.", "No element history archived."]
    method, body = r["passReq"]
    assert method == "POST"
    assert body == {"observer": {"latitude_deg": 51.5, "longitude_deg": -0.1,
                                 "altitude_km": 0.02},
                    "norad_id": 25544, "start": "2026-03-22T12:00:00.000Z", "hours": 24,
                    "min_elevation_deg": 10}
    assert "/conjunctions/history?norad_id=25544&limit=50" in r["conjUrls"]
    assert r["starBefore"] == "STAR SELECTED"
    assert r["starAfter"] == ["UNSTAR SELECTED", True]
    assert r["highlight"] == [1, 0.12, "Highlighting the watchlist (6)", True]
    assert r["unhighlight"] == [1, None]
    assert r["afterRemove"] == [29733, 44700, 44701, 44702, 22566]
    assert r["selected"] == [29733]
    assert r["unstar"] is False and r["final"] == [29733, 44700, 44701, 44702]


scenario("visible_tonight", r"""
  var V = window.SDAOrbitViz;
  var VIS_BODIES = [];
  var VIS_REPLY = null;
  route('/api/orbitviz/visible', function (url, opts) {
    VIS_BODIES.push(JSON.parse(opts.body)); return VIS_REPLY; });
  var k = start();
  var body = k.m.panels.visible.body;
  var out = {};
  out.noObserver = await V.findVisible();
  out.noObserverStatus = k.m.visStatus.textContent;
  out.bad = [V.setObserver({ lat: 100, lon: 0 }), k.m.obsStatus.textContent];
  k.m.obsLat.value = '51.5'; k.m.obsLon.value = ''; k.m.obsLat.fire('change');
  out.typedMissing = k.m.obsStatus.textContent;
  k.m.obsLon.value = '-0.1'; k.m.obsAlt.value = '35'; k.m.obsLon.fire('change');
  out.typed = [k.m.observer, JSON.parse(STORE['sda.orbitviz.observer'])];
  document.body.appendChild(input('passpanel-latitude', '40.7'));
  document.body.appendChild(input('passpanel-longitude', '-74.0'));
  document.body.appendChild(input('passpanel-altitude', '10'));
  buttonNamed(body, 'FROM PASS PANEL').fire('click');
  out.fromPanel = k.m.observer;
  window.SDAPassPanel = { validateObserver: function () {
    return { ok: false, error: 'Latitude out of range' }; } };
  buttonNamed(body, 'FROM PASS PANEL').fire('click');
  out.panelRefused = k.m.obsStatus.textContent;
  delete window.SDAPassPanel;
  buttonNamed(body, 'MY LOCATION').fire('click');
  out.noGeo = k.m.obsStatus.textContent;
  window.navigator = { geolocation: { getCurrentPosition: function (ok) {
    ok({ coords: { latitude: 51.5, longitude: 0, altitude: null } }); } } };
  buttonNamed(body, 'MY LOCATION').fire('click');
  out.located = [k.m.observer, k.m.obsStatus.textContent];
  V.toggleWatch(25544);
  await settle();
  VIS_REPLY = { status: 200, body: { candidates: 3, dark_minutes: 1268, count: 2, passes: [
    { norad_id: 25544, name: 'ISS (ZARYA)', magnitude: -3.6,
      visible_start: { time: '2026-03-22T19:44:30+00:00', compass: 'WSW', elevation_deg: 10 },
      visible_peak: { time: '2026-03-22T19:47:21+00:00', compass: 'S', elevation_deg: 66.8 },
      visible_end: { time: '2026-03-22T19:50:10+00:00', compass: 'ENE', elevation_deg: 12 } },
    { norad_id: 20580, name: 'HST', magnitude: 1.9,
      visible_start: { time: '2026-03-22T20:01:00+00:00', compass: 'SW', elevation_deg: 10 },
      visible_peak: { time: '2026-03-22T20:03:00+00:00', compass: 'S', elevation_deg: 21.4 },
      visible_end: { time: '2026-03-22T20:05:00+00:00', compass: 'SE', elevation_deg: 11 } }
  ] } };
  buttonNamed(body, 'FIND PASSES').fire('click');
  await settle();
  out.body = VIS_BODIES[0];
  out.status = k.m.visStatus.textContent;
  var rows = byClass(k.m.visList, 'ov-item');
  out.rows = rows.map(function (r) { return [byClass(r, 'ov-label')[0].textContent,
    byClass(r, 'ov-count')[0].textContent, r.has('muted')]; });
  rows[0].fire('click'); rows[1].fire('click');
  out.selected = k.calls.select;
  VIS_REPLY = { status: 200, body: { candidates: 3, dark_minutes: 0, passes: [] } };
  await V.findVisible(); out.noDark = k.m.visStatus.textContent;
  VIS_REPLY = { status: 200, body: { candidates: 5, dark_minutes: 300, passes: [] } };
  await V.findVisible(); out.noBright = k.m.visStatus.textContent;
  VIS_REPLY = { status: 503, body: {} };
  await V.findVisible(); out.loading = k.m.visStatus.textContent;
  VIS_REPLY = { status: 500, body: {} };
  await V.findVisible(); out.failed = k.m.visStatus.textContent;
  return out;
""")


def test_visible_tonight(ran):
    r = ran["visible_tonight"]
    assert r["noObserver"] is None and r["noObserverStatus"] == "Set an observer first."
    assert r["bad"] == [False, "That is not a valid observer."]
    assert r["typedMissing"] == "Enter a latitude and a longitude."
    assert r["typed"] == [{"lat": 51.5, "lon": -0.1, "altM": 35}] * 2
    assert r["fromPanel"] == {"lat": 40.7, "lon": -74, "altM": 10}
    assert r["panelRefused"] == "The pass panel has no observer: Latitude out of range"
    assert r["noGeo"] == "Location is not available in this browser."
    assert r["located"] == [{"lat": 51.5, "lon": 0, "altM": 0}, "Observer 51.500, 0.000, 0 m."]
    assert r["body"] == {"observer": {"latitude_deg": 51.5, "longitude_deg": 0,
                                      "altitude_km": 0},
                         "start": "2026-03-22T12:00:00.000Z", "hours": 24,
                         "min_elevation_deg": 10, "max_magnitude": 4.5, "limit": 20,
                         "norad_ids": [25544]}
    assert r["status"] == "2 passes from 3 candidates; 21.1 h of dark sky."
    assert r["rows"] == [["-3.6  ISS (ZARYA)  19:44 to 19:50 UTC", "max 67 deg, WSW to ENE",
                          False],
                         ["+1.9  HST  20:01 to 20:05 UTC", "max 21 deg, SW to SE", True]]
    assert r["selected"] == [25544]  # HST is not drawn, so its row selects nothing
    assert r["noDark"] == "The sky does not get dark here in the next 24 hours."
    assert r["noBright"] == "No bright pass in the next 24 hours from 5 candidates."
    assert r["loading"] == "The catalog is still loading on the server; try again shortly."
    assert r["failed"] == "Visible passes did not load."


scenario("constellations", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  var body = k.m.panels.const.body;
  var out = {};
  var W = Date.now() + 1e6;
  var planes = V.buildPlanes(true);
  out.counts = V.CONSTELLATIONS.map(function (c) {
    return [c.key, planes[c.key].count, planes[c.key].planes.length]; });
  out.starlink = planes.STARLINK.planes.map(function (p) {
    return { inc: p.inc, alt: p.alt, raan: p.raan, ids: p.ids }; });
  out.rows = byClass(k.m.constList, 'ov-item').map(function (r) {
    return byClass(r, 'ov-label')[0].textContent + ' | ' +
      byClass(r, 'ov-count')[0].textContent; });
  buttonNamed(body, 'ALL RINGS').fire('click');
  var rings = k.m.rings;
  out.ring = [rings.items.length, rings.show, rings.items[0].loop,
    rings.items[0].positions.length, rings.items[0].id.type, rings.items[1].id.plane];
  var p0 = rings.items[0].positions[0];
  out.ringRadius = Math.sqrt(p0.x * p0.x + p0.y * p0.y + p0.z * p0.z) / 1000;
  out.angle = [rings.modelMatrix.angle, -V.gmstRad(SIM)];
  rings.items[1].id.sdaPick();
  out.focus = k.m.focusLine.textContent;
  out.picked = [rings.items[1].width,
    rings.items[1].material.uniforms.color === Cesium.Color.WHITE, rings.items[0].width];
  out.alpha = [k.by[44705].color.a, k.by[44700].color.a, k.by[25544].color.a];
  out.planeTitle = k.m.planeTitle.textContent;
  out.planeRows = byClass(k.m.planeList, 'ov-item').map(function (r) { return r.has('active'); });
  rings.items[1].id.sdaPick();
  out.cleared = [k.m.focus, rings.items[1].width, k.by[44700].color.a];
  byClass(k.m.planeList, 'ov-item')[0].fire('click');
  out.listFocus = k.m.focusLine.textContent;
  var later = SIM + 3600 * 1000;
  V._frame(later, W + 5000);
  out.later = [k.m.rings.modelMatrix.angle, -V.gmstRad(later)];
  var jd = SIM / 86400000 + 2440587.5;
  var day = Math.floor(jd);
  k.ctx.viewer._pre[0](k.ctx.viewer.scene, { dayNumber: day,
    secondsOfDay: (jd - day) * 86400 + 37 });
  out.pre = [k.m.rings.modelMatrix.angle, -V.gmstRad(SIM)];
  k.ctx.viewer.scene.mode = 2; V._frame(SIM, W + 9000); out.show2d = k.m.rings.show;
  k.ctx.viewer.scene.mode = 3; V._frame(SIM, W + 11000); out.show3d = k.m.rings.show;
  buttonNamed(body, 'NO RINGS').fire('click');
  out.none = [k.m.rings.items.length, k.m.rings.show, k.m.focus];
  byClass(k.m.constList, 'ov-item')[0].fire('click');
  out.toggled = [k.m.rings.items.length, k.m.ringsOn.STARLINK];
  out.prims = k.ctx.viewer._prims.length;
  return out;
""")


def test_constellation_planes_and_rings(ran):
    r = ran["constellations"]
    counts = {key: (n, planes) for key, n, planes in r["counts"]}
    assert counts["STARLINK"] == (10, 2) and counts["GPS"] == (1, 0)
    assert all(counts[k] == (0, 0) for k in ("ONEWEB", "KUIPER", "GALILEO", "GLONASS",
                                             "BEIDOU", "IRIDIUM"))
    alt = alt_for(15.06)
    a, b = r["starlink"]
    assert a["ids"] == [44700, 44701, 44702, 44703, 44704]
    assert b["ids"] == [44705, 44706, 44707, 44708, 44709]
    assert a["inc"] == pytest.approx(53) and a["alt"] == pytest.approx(alt, abs=1e-6)
    assert a["raan"] == pytest.approx(10.4, abs=1e-6)
    assert b["raan"] == pytest.approx(40.4, abs=1e-6)
    assert r["rows"][0] == "Starlink | 10 / 2 planes"
    assert r["rows"][3] == "GPS | 1 / 0 planes" and r["rows"][7] == "Iridium | 0 / 0 planes"
    assert r["ring"] == [2, True, True, 90, "orbitviz-plane", 1]
    assert r["ringRadius"] == pytest.approx(RE + alt, abs=1e-6)
    assert r["angle"][0] == pytest.approx(r["angle"][1], abs=1e-12)
    km = fmt_int(alt)
    assert r["focus"] == f"Highlighting Starlink plane 2 (5 objects, 53.0 deg, {km} km)"
    assert r["picked"] == [3, True, 1]
    assert r["alpha"] == [1, 0.12, 0.12]
    assert r["planeTitle"] == "STARLINK PLANES (2)" and r["planeRows"] == [False, True]
    assert r["cleared"] == [None, 1, 1]
    assert r["listFocus"] == f"Highlighting Starlink plane 1 (5 objects, 53.0 deg, {km} km)"
    assert r["later"][0] == pytest.approx(r["later"][1], abs=1e-12)
    # Cesium's clock is TAI; the module takes the leap seconds off before turning it.
    assert r["pre"][0] == pytest.approx(r["pre"][1], abs=1e-7)
    assert r["show2d"] is False and r["show3d"] is True
    assert r["none"] == [0, False, None]
    assert r["toggled"] == [2, True]
    assert r["prims"] == 1  # one polyline collection for every ring


scenario("debris", r"""
  var V = window.SDAOrbitViz;
  route('/api/orbitviz/debris', DEBRIS);
  var k = start();
  var out = {};
  await V.loadDebris();
  out.status = k.m.debrisStatus.textContent;
  var boxes = byClass(k.m.debrisList, 'ov-watch');
  function row(b) {
    return [byClass(b, 'ov-label')[0].textContent, byClass(b, 'ov-count')[0].textContent,
      byClass(b, 'ov-watch-line')[0].textContent, byClass(b, 'ov-item')[0].has('muted')]; }
  out.first = row(boxes[0]);
  out.second = row(boxes[1]);
  out.last = row(boxes[boxes.length - 1]);
  out.sub = byClass(k.m.debrisList, 'ov-sub').map(function (x) { return x.textContent; });
  out.boxes = boxes.length;
  byClass(boxes[0], 'ov-item')[0].fire('click');
  out.focus = k.m.focusLine.textContent;
  out.alpha = [k.by[29733].color.a, k.by[29734].color.a, k.by[25544].color.a];
  out.active = byClass(byClass(k.m.debrisList, 'ov-watch')[0], 'ov-item')[0].has('active');
  byClass(byClass(k.m.debrisList, 'ov-watch')[0], 'ov-item')[0].fire('click');
  out.off = [k.m.focus, k.m.debrisKey];
  await V.loadDebris();
  out.requests = REQUESTS.filter(function (r) { return r.url === '/api/orbitviz/debris'; })
    .length;
  return out;
""")

scenario("debris_fallback", r"""
  var V = window.SDAOrbitViz;
  route('/api/orbitviz/debris', {}, 503);
  var k = start();
  k.m.panels.debris.head.fire('click');
  await settle();
  var boxes = byClass(k.m.debrisList, 'ov-watch');
  return { status: k.m.debrisStatus.textContent, boxes: boxes.length,
    first: [byClass(boxes[0], 'ov-count')[0].textContent,
      byClass(boxes[0], 'ov-watch-line')[0].textContent],
    pair: byClass(boxes[1], 'ov-watch-line')[0].textContent };
""")


def test_debris_clouds_from_satcat(ran):
    r = ran["debris"]
    assert r["status"] == "SATCAT counts from spacebook."
    assert r["first"] == ["Fengyun-1C (2007 asat test)", "6 on orbit",
                          "1999-025, 2 drawn, 9 catalogued, 3 re-entered", False]
    assert r["second"] == ["Cosmos 2251 and Iridium 33 (2009 collision)", "6 on orbit",
                           "1993-036, 1997-051, 0 drawn, 8 catalogued, 2 re-entered", True]
    assert r["sub"] == ["OTHER LARGE CLOUDS BY LAUNCH"]
    assert r["last"][0] == "THORAD AGENA D" and r["last"][1] == "4 on orbit"
    assert r["boxes"] == len(DEBRIS_EVENTS) + 1
    assert r["focus"] == "Highlighting Fengyun-1C debris (2 drawn)"
    assert r["alpha"] == [1, 1, 0.12] and r["active"] is True
    assert r["off"] == [None, None]
    assert r["requests"] == 1


def test_debris_clouds_fall_back_to_the_drawn_catalog(ran):
    r = ran["debris_fallback"]
    assert r["status"] == "Counted from the drawn catalog only (SATCAT not available)."
    assert r["boxes"] == len(DEBRIS_EVENTS)
    assert r["first"] == ["2 drawn", "1999-025"]
    assert r["pair"] == "1993-036, 1997-051"


scenario("history", r"""
  var V = window.SDAOrbitViz;
  route('/api/orbitviz/history', HISTORY);
  var k = start();
  var out = {};
  k.by[5].hideMask = 1; k.by[5].hidden = true;  // another filter hid Vanguard
  k.ctx.satEngine.selectedId = 44700;
  await V.loadHistory();
  out.readout = k.m.histReadout.textContent;
  out.slider = [k.m.histSlider.min, k.m.histSlider.max, k.m.histSlider.value];
  out.note = k.m.histNote.textContent;
  out.drawn = (k.m.histCanvas._calls || {}).fillRect > 0;
  out.year = V.setYear(2000);
  out.hidden2000 = hiddenIds(k);
  out.masks = [k.by[44700].hideMask, k.by[44700].hidden, k.by[44701].hideMask,
    k.by[5].hideMask, k.by[25544].hideMask];
  out.readout2000 = k.m.histReadout.textContent;
  out.refresh = k.calls.refresh;
  k.m.histSlider.value = '1990'; k.m.histSlider.fire('input');
  out.hidden1990 = hiddenIds(k);
  out.clamp = V.setYear(1800);
  out.last = V.setYear(3000);
  out.after = [k.m.histYear, hiddenIds(k), k.by[5].hideMask, k.by[44701].hideMask];
  k.m.playBtn.fire('click');
  out.playing = [k.m.playBtn.textContent, k.m.histYear];
  k.m.playBtn.fire('click');
  out.paused = k.m.playBtn.textContent;
  V.setYear(1990);
  V.setEnabled(false);
  out.disabled = k.list.filter(function (s) { return s.hideMask & 4; }).length;
  V.setEnabled(true);
  out.reenabled = k.list.filter(function (s) { return s.hideMask & 4; }).length;
  buttonNamed(k.m.panels.history.body, 'ALL YEARS').fire('click');
  out.allYears = [k.m.histYear, k.list.filter(function (s) { return s.hideMask & 4; }).length];
  k.m.histCanvas.fire('mousemove', { offsetX: 29 });
  out.hover = k.m.histReadout.textContent;
  k.m.histCanvas.fire('mouseleave');
  out.leave = k.m.histReadout.textContent;
  return out;
""")


def test_catalog_history_slider(ran):
    r = ran["history"]
    assert r["readout"] == history_text(HISTORY, 2026)
    assert r["slider"] == ["1957", "2026", "2026"]
    assert r["note"].startswith(HISTORY["definition"])
    assert r["drawn"] is True
    assert r["year"] == 2000
    starlink_rest = list(range(44701, 44710))
    # Launched after 2000: Starlink, Intelsat 901, GPS, Molniya. The selected Starlink
    # stays visible; Vanguard stays hidden by the other filter's bit.
    assert r["hidden2000"] == [5, 26824, 28163, 36585, *starlink_rest]
    assert r["masks"] == [4, False, 4, 1, 0]
    assert r["readout2000"] == history_text(HISTORY, 2000) + \
        ". Globe: 6 drawn objects launched by 2000."
    assert r["refresh"] >= 1
    assert r["hidden1990"] == [5, 22566, 25544, 26824, 28163, 29733, 29734, 36585,
                               *starlink_rest]
    assert r["clamp"] == 1957 and r["last"] == 2026
    assert r["after"] == [None, [5], 1, 0]
    assert r["playing"] == ["PAUSE", 1957] and r["paused"] == "PLAY"
    assert r["disabled"] == 0 and r["reenabled"] == 17
    assert r["allYears"] == [None, 0]
    assert r["hover"] == history_text(HISTORY, 1957)
    assert r["leave"] == history_text(HISTORY, 2026)


scenario("shadow_and_footprint", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  var body = k.m.panels.shadow.body;
  var out = {};
  var sun = V.subsolarPoint(SIM);
  var antiLon = sun.lon > 0 ? sun.lon - 180 : sun.lon + 180;
  var iss = k.by[25544];
  iss.located = true; iss.lastLat = sun.lat; iss.lastLon = sun.lon; iss.lastAlt = 420;
  var rb = k.by[22566];
  rb.located = true; rb.lastLat = -sun.lat; rb.lastLon = antiLon; rb.lastAlt = 800;
  out.sun = sun;
  buttonNamed(body, 'EARTH SHADOW').fire('click');
  var sh = k.m.shadow;
  function geom(p) { return p.opts.geometryInstances.opts.geometry.opts; }
  out.cones = [geom(sh.umbra), geom(sh.pen)].map(function (g) {
    return [g.length, g.topRadius, g.bottomRadius]; });
  out.shows = [sh.umbra.show, sh.pen.show, sh.rings.show];
  out.axis = sh.axis;
  out.sunFixed = V.sunEarthFixed(SIM, {});
  out.trans = sh.umbra.modelMatrix.trans;
  out.rot = sh.umbra.modelMatrix.rot;
  out.sameFrame = sh.pen.modelMatrix.trans.z === out.trans.z &&
    sh.rings.modelMatrix.trans.z === out.trans.z;
  var r0 = sh.rings.items[0].positions[0];
  var r1 = sh.rings.items[1].positions[0];
  out.geoRings = [Math.sqrt(r0.x * r0.x + r0.y * r0.y) / 1000, r0.z / 1000,
    Math.sqrt(r1.x * r1.x + r1.y * r1.y) / 1000];
  out.btn = buttonNamed(body, 'EARTH SHADOW').has('active');
  k.ctx.viewer.scene.mode = 2; V._frame(SIM, Date.now() + 1e6);
  out.flat = sh.umbra.show;
  k.ctx.viewer.scene.mode = 3;
  buttonNamed(body, 'EARTH SHADOW').fire('click');
  out.off = [sh.umbra.show, sh.pen.show, sh.rings.show];
  buttonNamed(body, 'COLOUR BY SUNLIGHT').fire('click');
  out.mode = V.colorMode();
  out.sunCounts = legend(k);
  out.colors = [iss.color.css, rb.color.css];
  var W = Date.now() + 2e6;
  iss.lastLat = rb.lastLat; iss.lastLon = rb.lastLon;
  V._frame(SIM, W); out.tick1 = legend(k);
  iss.lastLat = sun.lat; iss.lastLon = sun.lon;
  V._frame(SIM, W + 100); out.tick2 = legend(k);
  V._frame(SIM, W + 1200); out.tick3 = legend(k);
  buttonNamed(body, 'FOOTPRINT').fire('click');
  out.footNoSel = k.m.geoStatus.textContent;
  k.ctx.satEngine.selectedId = 25544;
  dispatch('sda:satellite-selected');
  var f = k.m.foot;
  out.foot = [f.coll.show, f.horizon.arr.length, f.mask.arr.length, k.m.geoStatus.textContent];
  out.footFirst = [f.horizon.arr[0].lat, f.horizon.arr[0].lon, f.horizon.arr[0].h,
    f.mask.arr[0].lat, f.horizon.poly.positions === f.horizon.arr];
  k.m.maskSelect.value = '30'; k.m.maskSelect.fire('change');
  out.foot30 = k.m.geoStatus.textContent;
  k.ctx.satEngine.selectedId = 44700;
  dispatch('sda:satellite-selected');
  out.footNoPos = [f.coll.show, k.m.geoStatus.textContent];
  k.ctx.satEngine.selectedId = 25544;
  dispatch('sda:satellite-selected');
  var calls = 0;
  window.satellite = {
    twoline2satrec: function (l1, l2) { return { l1: l1, l2: l2 }; },
    propagate: function () { calls++; return { position: { x: 1, y: 2, z: 3 } }; },
    gstime: function () { return 0; },
    eciToGeodetic: function () { return { latitude: 0.5, longitude: (calls % 60) / 10 - 3 }; }
  };
  buttonNamed(body, 'GROUND TRACK').fire('click');
  out.strip = [k.m.strip.style.display, k.m.stripPoints, (k.m.strip._calls || {}).stroke > 0,
    calls];
  V._frame(SIM, W + 1300);
  out.stripCached = calls;
  V._frame(SIM + 60000, W + 2500);
  out.stripRedrawn = calls;
  buttonNamed(body, 'GROUND TRACK').fire('click');
  out.stripOff = k.m.strip.style.display;
  out.prims = k.ctx.viewer._prims.length;
  V.setEnabled(false);
  out.disabled = [f.coll.show, sh.umbra.show];
  return out;
""")


def test_earth_shadow_cones(ran):
    r = ran["shadow_and_footprint"]
    umbra_tan = math.tan(math.asin((696000 - RE) / 149597870.7))
    pen_tan = math.tan(math.asin((696000 + RE) / 149597870.7))
    assert r["cones"][0] == pytest.approx([6e7, (RE - 60000 * umbra_tan) * 1000, RE * 1000])
    assert r["cones"][1] == pytest.approx([6e7, (RE + 60000 * pen_tan) * 1000, RE * 1000])
    assert r["shows"] == [True, True, True] and r["btn"] is True
    sun = np.array([r["sunFixed"][c] for c in "xyz"])
    axis = np.array([r["axis"][c] for c in "xyz"])
    assert axis == pytest.approx(-sun, abs=1e-12)
    # the cone's centre sits half its length behind the Earth, on the anti-Sun axis
    trans = np.array([r["trans"][c] for c in "xyz"])
    assert trans == pytest.approx(axis * 3e7, abs=1e-3)
    rot = np.array(r["rot"]).reshape(3, 3)  # rows here are the local axes (columns in Cesium)
    assert rot @ rot.T == pytest.approx(np.eye(3), abs=1e-12)
    assert rot[2] == pytest.approx(axis, abs=1e-12)
    assert np.linalg.det(rot) == pytest.approx(1.0)
    assert r["sameFrame"] is True
    assert r["geoRings"] == pytest.approx([RE - 42164 * umbra_tan, 42164 - 30000,
                                           RE + 42164 * pen_tan])
    assert r["flat"] is False
    assert r["off"] == [False, False, False]


def test_sunlight_colours(ran):
    r = ran["shadow_and_footprint"]
    assert r["mode"] == "sunlight"
    assert r["sunCounts"] == ["1", "0", "1", "17"]
    assert r["colors"] == ["#c98500", "#3987e5"]
    assert r["tick1"] == ["0", "0", "2", "17"]   # repainted on the slow tick
    assert r["tick2"] == ["0", "0", "2", "17"]   # throttled: under a second later
    assert r["tick3"] == ["1", "0", "1", "17"]


def test_footprint_and_ground_track(ran):
    r = ran["shadow_and_footprint"]
    assert r["footNoSel"] == "Select an object to draw its footprint."
    horizon, mask = footprint_km(420, 0), footprint_km(420, 10)
    assert r["foot"] == [True, 73, 73, f"ISS (ZARYA): {fmt_int(horizon)} km to the horizon, "
                                         f"{fmt_int(mask)} km above 10 deg."]
    lat, lon, height, mask_lat, reused = r["footFirst"]
    assert lat == pytest.approx(r["sun"]["lat"] + horizon / RE / DEG, abs=1e-9)
    assert lon == pytest.approx(r["sun"]["lon"], abs=1e-9) and height == 5000
    assert mask_lat == pytest.approx(r["sun"]["lat"] + mask / RE / DEG, abs=1e-9)
    assert reused is True
    assert r["foot30"].endswith(f"{fmt_int(footprint_km(420, 30))} km above 30 deg.")
    assert r["footNoPos"] == [False, "The selected object has no position yet."]
    assert r["strip"] == ["", 241, True, 241]
    assert r["stripCached"] == 241 and r["stripRedrawn"] == 482
    assert r["stripOff"] == "none"
    assert r["prims"] == 4  # two cones, the shadow rings, the footprint collection
    assert r["disabled"] == [False, False]


scenario("catalog_reload_and_disable", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  V.setColorMode('regime');
  var extra = rec(43013, 'NOAA 20', '17073A', '26081.50000000', 98.7, 50, 0.0001, 14.19);
  k.list.push(extra); k.by[43013] = extra;
  dispatch('sda:catalog-loaded');
  var out = { counts: legend(k), color: extra.color.css, base: extra._ovBase === PAGE[43013] };
  V.setEnabled(false);
  out.restored = k.list.every(function (s) {
    return s.color === PAGE[s.noradId] && s.point.color === PAGE[s.noradId]; });
  out.legend = k.m.legend.textContent;
  out.enabled = V.isEnabled();
  out.mode = V.colorMode();
  V.setEnabled(true);
  out.again = [legend(k), k.by[25544].color.css];
  return out;
""")


def test_catalog_reload_and_disable_restore_the_page(ran):
    r = ran["catalog_reload_and_disable"]
    assert r["counts"] == ["15", "1", "1", "2", "0", "1"]
    assert r["color"] == "#3987e5" and r["base"] is True
    assert r["restored"] is True
    assert r["legend"] == "20 objects drawn in the page colours."
    assert r["enabled"] is False and r["mode"] == "regime"
    assert r["again"] == [["15", "1", "1", "2", "0", "1"], "#3987e5"]


scenario("scale", r"""
  var V = window.SDAOrbitViz;
  var k = start();
  var many = [];
  var by = {};
  for (var i = 0; i < 33000; i++) {
    var s = rec(100000 + i, i % 7 ? 'OBJ ' + i : 'OBJ DEB', '20001A', '26081.50000000',
      50 + (i % 40), i % 360, 0.001, 14 + (i % 3));
    s.located = true; s.lastLat = (i % 180) - 90; s.lastLon = (i % 360) - 180;
    many.push(s); by[s.noradId] = s;
  }
  k.ctx.satEngine.sats = many; k.ctx.satEngine.byNoradId = by;
  var t0 = Date.now();
  V.setColorMode('regime');
  var first = Date.now() - t0;
  t0 = Date.now();
  for (var j = 0; j < 5; j++) V.repaint();
  var again = (Date.now() - t0) / 5;
  var before = COUNT.color;
  V.repaint();
  var allocs = COUNT.color - before;
  V.setColorMode('sunlight');
  var W = Date.now() + 1e6;
  t0 = Date.now();
  for (j = 0; j < 60; j++) V._frame(SIM, W + j * 16);
  var perFrame = (Date.now() - t0) / 60;
  var counts = k.m.counts;
  return { first: first, again: again, allocs: allocs, perFrame: perFrame, counts: counts,
    total: k.m.total, prims: k.ctx.viewer._prims.length };
""")


def test_stays_fast_with_a_full_catalog(ran):
    r = ran["scale"]
    assert r["total"] == 33000 and sum(r["counts"]) == 33000
    assert r["counts"][3] == 0  # every object has a position
    assert r["allocs"] == 0
    assert r["prims"] == 0  # no entity or primitive per object
    # generous bounds: a shared, loaded machine
    assert r["first"] < 3000 and r["again"] < 500 and r["perFrame"] < 40


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "orbitviz.js must stay ASCII"
