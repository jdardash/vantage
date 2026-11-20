"""Tests for the globe country intelligence module (static/js/intel.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_peaks.py does it. The
fake `fetch` answers from a table, so nothing here touches the network. Skips cleanly
when Node is not installed.

Each scenario is the body of an async function returning a JSON-able value, run in a
fresh vm context; the module-scoped `ran` fixture holds every result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "intel.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

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
function one(el, cls) { return byClass(el, cls)[0]; }

var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  }
};
var countEl = new El('span'); countEl.id = 'count-intel'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var HANDLERS = [];
function Handler(canvas) { this.canvas = canvas; this.actions = {}; HANDLERS.push(this); }
Handler.prototype.setInputAction = function (fn, type) { this.actions[type] = fn; };
var Cesium = {
  PointPrimitiveCollection: Coll, LabelCollection: Coll, PolylineCollection: Coll,
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000') },
  Cartesian3: {
    fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; },
    fromDegreesArray: function (a) { return { deg: a.slice() }; }
  },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0, LEFT: 1 },
  VerticalOrigin: { CENTER: 0 },
  Material: { fromType: function (t, u) { return { type: t, uniforms: u }; } },
  Primitive: function (o) { this.opts = o; },
  GeometryInstance: function (o) { this.geometry = o.geometry; this.id = o.id;
    this.attributes = o.attributes; },
  PolygonGeometry: function (o) { this.hierarchy = o.polygonHierarchy; this.height = o.height; },
  PolygonHierarchy: function (positions, holes) { this.positions = positions;
    this.holes = holes || []; },
  PerInstanceColorAppearance: function (o) { this.opts = o; },
  ColorGeometryInstanceAttribute: { fromColor: function (c) { return { color: c }; } },
  ScreenSpaceEventHandler: Handler,
  ScreenSpaceEventType: { LEFT_CLICK: 'LEFT_CLICK' },
  Cartographic: { fromCartesian: function (p) {
    return { latitude: p.lat * Math.PI / 180, longitude: p.lon * Math.PI / 180, height: 0 };
  } }
};
var window = this;
this.Cesium = Cesium;

// ---- fake fetch ----------------------------------------------------------
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
  return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({});
    } });
}

// ---- server answers --------------------------------------------------------
var CHE = { iso3: 'CHE', iso2: 'CH', name: 'Switzerland', lat: 46.72, lon: 7.46 };
var COD = { iso3: 'COD', iso2: 'CD', name: 'Dem. Rep. Congo', lat: -1.86, lon: 23.46 };
var KEN = { iso3: 'KEN', iso2: 'KE', name: 'Kenya', lat: 0.55, lon: 37.91 };
var INSTABILITY = {
  countries: [
    { iso3: 'QAT', name: 'Qatar', lat: 25.24, lon: 51.14, score: 47.1, level: 'elevated',
      terms: { outbreaks: 5, internet_outages: 20 } },
    { iso3: 'COD', name: 'Dem. Rep. Congo', lat: -1.86, lon: 23.46, score: 13.2, level: 'low',
      terms: { outbreaks: 10 } },
    { iso3: 'CHE', name: 'Switzerland', lat: 46.72, lon: 7.46, score: 0, level: 'low',
      terms: {} }
  ],
  sources: {
    fires: { ok: false, count: 0,
      note: 'FIRMS_MAP_KEY is not set on the server; fires are left out' },
    weather: { ok: true, count: 3, note: 'NWS alerts cover the United States only' }
  }
};
var OUTBREAKS = { outbreaks: [
  { id: '2026-DON627', title: 'Chikungunya virus disease - Switzerland',
    disease: 'Chikungunya virus disease', published: '2026-11-17T19:00:00Z',
    url: 'https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON627',
    countries: [CHE] },
  { id: '2026-DON625', title: 'Ebola disease, Democratic Republic of the Congo and Kenya',
    disease: 'Ebola disease', published: '2026-11-06T20:42:12Z', url: '', countries: [COD, KEN] },
  { id: '2026-DON622', title: 'Ebola disease - Democratic Republic of the Congo',
    disease: 'Ebola disease', published: '2026-10-23T15:03:45Z', url: '', countries: [COD] },
  { id: '2026-DON624', title: 'Yellow fever - WHO African Region', disease: 'Yellow fever',
    published: '2026-11-06T19:00:00Z', url: '', countries: [] }
] };
var CYBER = { countries: [
  { iso3: 'USA', name: 'United States of America', lat: 39.54, lon: -97.48, servers: 9,
    online: 1, malware: { QakBot: 6, Emotet: 3 } },
  { iso3: 'GBR', name: 'United Kingdom', lat: 54.4, lon: -2.12, servers: 1, online: 0,
    malware: { QakBot: 1 } }
] };
var OUTAGES = { countries: [
  { iso3: 'FIN', name: 'Finland', lat: 63.25, lon: 27.28, critical: 1, warning: 0 },
  { iso3: 'QAT', name: 'Qatar', lat: 25.24, lon: 51.14, critical: 0, warning: 2 }
] };
var CELL = { id: '12:25', lat: 25.25, lon: 51.25, kinds: ['quake', 'fire', 'outbreak'],
  labels: ['earthquakes', 'fires', 'disease outbreak (country-wide)'],
  counts: { quake: 1, fire: 1, outbreak: 0 }, events: 3, score: 81, countries: ['QAT'],
  samples: { quake: ['M 4.8 - Qatar'], fire: ['fire 12.5 MW'] } };
var CONVERGENCE = { cells: [CELL], count: 1, cell_deg: 2 };
var BORDERS = { countries: [
  { iso3: 'CHE', name: 'Switzerland', polygons: [[[6, 46, 10, 46, 10, 47.8, 6, 47.8]]] },
  { iso3: 'QAT', name: 'Qatar', polygons: [
    [[50.8, 24.5, 51.6, 24.5, 51.6, 26.1, 50.8, 26.1], [51, 25, 51.1, 25, 51.1, 25.1],
     [1, 2]],
    [[1, 2]] ] },
  { iso3: 'FRA', name: 'France', polygons: [[[-1, 43, 7, 43, 7, 50, -1, 50]]] },
  { iso3: 'ZZZ', name: 'Nowhere', polygons: [[[0, 0, 1, 1]]] }
] };
var COUNTRIES = { countries: [CHE, { iso3: 'FRA', iso2: 'FR', name: 'France' },
  { iso3: 'SWE', iso2: 'SE', name: 'Sweden' }] };
var CARD_CHE = {
  iso3: 'CHE', iso2: 'CH', name: 'Switzerland', continent: 'Europe',
  subregion: 'Western Europe', region: 'Europe & Central Asia', income_group: 'High income',
  capital: { name: 'Bern', lat: 46.948, lon: 7.44821 },
  indicators: [
    { key: 'population', label: 'Population', value: 9092436, year: 2025 },
    { key: 'gdp_usd', label: 'GDP (current US$)', value: 1043529899250.92, year: 2025 },
    { key: 'gdp_per_capita_usd', label: 'GDP per capita (current US$)', value: 114769.01,
      year: 2025 },
    { key: 'inflation_pct', label: 'Inflation, consumer prices (annual %)', value: 0.154,
      year: 2025 }
  ],
  indicators_source: 'World Bank Open Data (CC BY 4.0), latest non-empty year',
  local_time: { zone: 'Europe/Zurich', time: '06:00', date: '2026-11-20', weekday: 'Fri',
    utc_offset: '+01:00' },
  exchanges: [{ mic: 'XSWX', name: 'SIX Swiss Exchange', open: false, state: 'closed',
    local_time: '06:00', next: 'opens 09:00 local', sessions: '09:00-17:30', days: 'Mon-Fri' }],
  exchanges_note: 'Regular session hours from a static table.',
  events_24h: { quakes: 0, fires: 0, weather_alerts: 0, eonet: 0, gdelt_events: 4,
    gdelt_unrest: 1, outage_sources: 0, outbreaks_30d: 1 },
  instability: { score: 5, level: 'low', terms: { outbreaks: 5, fires: 0 },
    formula: ['score = sum of the terms', 'outbreaks = 10 x min(1, O / 2)'],
    note: "This application's own heuristic." },
  outbreaks: [
    { published: '2026-11-17T19:00:00Z', title: 'Chikungunya virus disease - Switzerland',
      url: 'https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON627' },
    { published: '2026-11-01T00:00:00Z', title: 'Bad link', url: 'javascript:alert(1)' }
  ],
  outages: null,
  cyber: { servers: 2, online: 1, malware: { QakBot: 2 } },
  view: { lat: 46.72, lon: 7.46, height_m: 600000, bbox: [5.9, 45.8, 10.5, 47.8] },
  cameras: { lat: 46.948, lon: 7.44821, radius_km: 150 },
  attribution: ['Borders: Natural Earth', 'Indicators: World Bank (CC BY 4.0)']
};
var CARD_QAT = {
  iso3: 'QAT', iso2: 'QA', name: 'Qatar', continent: 'Asia', subregion: 'Western Asia',
  region: '', income_group: '', capital: null,
  indicators: [{ key: 'population', label: 'Population', value: null, year: null }],
  local_time: null, exchanges: [], events_24h: {},
  instability: { score: 47.1, level: 'elevated', terms: {}, formula: [], note: '' },
  outbreaks: [], cyber: null,
  outages: { critical: 0, warning: 2, datasources: ['bgp', 'gtr'], ongoing: false,
    url: 'https://ioda.inetintel.cc.gatech.edu/country/QA' },
  view: { lat: 25.24, lon: 51.14, height_m: 400000 }, cameras: {}, attribution: []
};

function standardRoutes() {
  route('/api/intel/instability', INSTABILITY);
  route('/api/intel/outbreaks', OUTBREAKS);
  route('/api/intel/cyber', CYBER);
  route('/api/intel/outages', OUTAGES);
  route('/api/intel/convergence', CONVERGENCE);
  route('/api/intel/borders', BORDERS);
  route('/api/intel/countries', COUNTRIES);
  route('/api/intel/country-at', CHE);
  route('/api/intel/country/', CARD_CHE);
  route('/api/intel/country/QAT', CARD_QAT);
}

function makeViewer() {
  var prims = [];
  var removed = [];
  return {
    scene: {
      primitives: {
        add: function (c) { prims.push(c); return c; },
        remove: function (c) { var at = prims.indexOf(c); if (at >= 0) prims.splice(at, 1);
          removed.push(c); return true; }
      },
      globe: { ellipsoid: {} }, canvas: { id: 'canvas' }, _picked: null,
      pick: function () { return this._picked || undefined; }
    },
    camera: {
      _hit: undefined,
      flyTo: function (o) { this.flown = o; },
      pickEllipsoid: function () { return this._hit === undefined ?
        { lon: 7.44, lat: 46.95, h: 0 } : this._hit; }
    },
    _prims: prims, _removed: removed
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    satEngine: { byNoradId: {}, selectedId: null },
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

// The module adds three collections on enable: rings, points, labels.
function layers(k) {
  var p = k.ctx.viewer._prims;
  return { rings: p[0], points: p[1], labels: p[2], choro: p[3] };
}

async function enabled(opts) {
  standardRoutes();
  var k = makeCtx(opts);
  window.SDAIntel.init(k.ctx);
  window.SDAIntel.setEnabled(true);
  await settle();
  return k;
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              setInterval: setInterval, clearInterval: clearInterval, Promise: Promise,
              JSON: JSON, Math: Math, Date: Date, encodeURIComponent: encodeURIComponent };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'intel.js' });
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
      // stop the refresh timer the scenario left running
      try { vm.runInContext('window.SDAIntel && window.SDAIntel.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("intel")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- pure helpers --------------------------------------------------------------

scenario("helpers", r"""
  var I = window.SDAIntel;
  var ring = I.ringPositions(0, 0, 111.19, 4);
  var wrap = I.ringPositions(10, 179.9, 200, 16);
  var lons = wrap.filter(function (v, i) { return i % 2 === 0; });
  return {
    q: I.query([['lat', 46.95], ['b', ''], ['c', null], ['n', 12], ['s', 'a b&c']]),
    qEmpty: I.query([['a', undefined]]),
    levels: [0, 19.9, 20, 45, 60, 79.9, 80, 100, null, undefined, NaN].map(function (s) {
      return I.levelFor(s).level; }),
    usd: [1043529899250.92, 415.2e9, 12.5e6, 114769.01, -2e9, null].map(I.formatUsd),
    people: [9092436, 1.4e9, 5000, undefined].map(I.formatPeople),
    ind: [I.formatIndicator({ key: 'inflation_pct', value: 0.154 }),
          I.formatIndicator({ key: 'population', value: 2832067 }),
          I.formatIndicator({ key: 'gdp_usd', value: 2.1e11 }),
          I.formatIndicator({ key: 'gdp_usd', value: null }), I.formatIndicator(null)],
    urls: ['https://www.who.int/a', 'http://x.org/b', 'javascript:alert(1)', 'https://a b',
           '//evil.example', '', null, 42].map(I.safeUrl),
    ring: ring, ringLen: ring.length,
    wrapLonsOk: lons.every(function (x) { return x >= -180 && x <= 180; }),
    wrapCrosses: lons.some(function (x) { return x < 0; }),
    radius: [I.cellRadiusKm(2), I.cellRadiusKm(0.5), I.cellRadiusKm(null)],
    kinds: [I.kindsText(['quake', 'fire', 'outbreak', 'weather', 'gdelt', 'odd']),
            I.kindsText(null)],
    terms: I.termList({ a: 0, b: 5, c: 2.5, d: null, e: -1 }),
    exchange: [I.exchangeLine({ name: 'Saudi Exchange (Tadawul)', open: true, state: 'open',
                 local_time: '11:00', next: 'closes 15:00 local', sessions: '10:00-15:00',
                 days: 'Sun-Thu' }),
               I.exchangeLine({ name: 'Tokyo Stock Exchange', open: false,
                 state: 'midday break', local_time: '12:00', next: 'opens 12:30 local',
                 sessions: '09:00-11:30, 12:30-15:30', days: 'Mon-Fri' }),
               I.exchangeLine(null)],
    events: I.eventsLine({ quakes: 2, gdelt_events: 40, gdelt_unrest: 6, outbreaks_30d: 1 }),
    time: [I.localTimeLine({ zone: 'Asia/Kolkata', time: '10:30', date: '2026-11-20',
             weekday: 'Fri', utc_offset: '+05:30' }), I.localTimeLine(null)],
    malware: [I.malwareText({ Emotet: 1, QakBot: 2 }), I.malwareText(null)],
    layers: I.LAYERS.map(function (l) { return [l.key, l.on]; })
  };
""")


def test_helpers(ran):
    h = ran["helpers"]
    assert h["q"] == "?lat=46.95&n=12&s=a%20b%26c" and h["qEmpty"] == ""
    assert h["levels"] == ["low", "low", "guarded", "elevated", "high", "high", "severe",
                           "severe", "low", "low", "low"]
    assert h["usd"] == ["$1.04 trillion", "$415.2 billion", "$12.5 million", "$114,769",
                        "$-2.0 billion", "n/a"]
    assert h["people"] == ["9.09 million", "1.40 billion", "5,000", "n/a"]
    assert h["ind"] == ["0.2%", "2.83 million", "$210.0 billion", "n/a", "n/a"]
    assert h["urls"] == ["https://www.who.int/a", "http://x.org/b", "", "", "", "", "", ""]
    ring = h["ring"]
    assert h["ringLen"] == 10 and ring[:2] == ring[-2:]  # closed
    assert ring[0] == 0 and ring[1] == pytest.approx(1.0, abs=0.01)  # due north first
    assert ring[2] == pytest.approx(1.0, abs=0.01) and ring[3] == pytest.approx(0, abs=0.01)
    assert h["wrapLonsOk"] and h["wrapCrosses"]
    assert h["radius"] == [pytest.approx(100.08), 40, pytest.approx(100.08)]
    assert h["kinds"] == ["QUAKE + FIRE + OUTBREAK + WX + NEWS + ODD", ""]
    assert h["terms"] == [["b", 5], ["c", 2.5]]
    assert h["exchange"] == [
        "Saudi Exchange (Tadawul): OPEN (11:00 local), closes 15:00 local [10:00-15:00 Sun-Thu]",
        "Tokyo Stock Exchange: MIDDAY BREAK (12:00 local), opens 12:30 local"
        " [09:00-11:30, 12:30-15:30 Mon-Fri]",
        ""]
    assert h["events"] == ("quakes 2 | fires 0 | NWS alerts 0 | EONET 0 | GDELT events 40"
                           " (unrest 6) | outage sources 0 | WHO outbreaks 30 d 1")
    assert h["time"] == ["10:30 Fri 2026-11-20 (UTC+05:30, Asia/Kolkata)", "unknown"]
    assert h["malware"] == ["QakBot 2, Emotet 1", ""]
    assert h["layers"] == [["choropleth", False], ["outbreaks", True], ["cyber", True],
                           ["outages", True], ["convergence", True]]


# --- enabling draws every layer ---------------------------------------------------

scenario("enable", r"""
  var k = await enabled();
  var L = layers(k);
  return {
    enabled: window.SDAIntel.isEnabled(),
    requests: REQUESTS.slice().sort(),
    prims: k.ctx.viewer._prims.length,
    rings: L.rings.items.length, ringWidth: L.rings.items[0].width,
    ringClosed: L.rings.items[0].positions.deg.slice(0, 2).join() ===
      L.rings.items[0].positions.deg.slice(-2).join(),
    ringMaterial: L.rings.items[0].material.type,
    points: L.points.items.map(function (p) { return [p.id.iso3, p.id.kind, p.color.css,
      p.position.lat, p.position.lon]; }),
    cyberSize: L.points.items.filter(function (p) { return p.id.kind === 'cyber'; })
      .map(function (p) { return p.pixelSize; }),
    labels: L.labels.items.map(function (l) { return l.text; }),
    count: document.getElementById('count-intel').textContent,
    status: one(k.ctx.mount, 'intel-status').textContent,
    notes: one(k.ctx.mount, 'intel-notes').textContent,
    feed: k.feed,
    top: byClass(k.ctx.mount, 'intel-item').map(function (b) { return b.textContent; }),
    handlers: HANDLERS.length, click: typeof HANDLERS[0].actions.LEFT_CLICK,
    canvas: HANDLERS[0].canvas.id
  };
""")


def test_enable_draws_markers_rings_and_the_ranking(ran):
    r = ran["enable"]
    assert r["enabled"] is True
    assert r["requests"] == ["/api/intel/convergence?cell_deg=2", "/api/intel/cyber",
                             "/api/intel/instability", "/api/intel/outages",
                             "/api/intel/outbreaks"]  # no borders until the choropleth is on
    assert r["prims"] == 3
    assert r["rings"] == 1 and r["ringWidth"] == 3 and r["ringClosed"]
    assert r["ringMaterial"] == "Color"
    assert [p[:3] for p in r["points"]] == [
        ["CHE", "outbreak", "#ff4fd8"], ["COD", "outbreak", "#ff4fd8"],
        ["KEN", "outbreak", "#ff4fd8"], ["USA", "cyber", "#4fd1ff"],
        ["GBR", "cyber", "#4fd1ff"], ["FIN", "outage", "#ff3b30"],
        ["QAT", "outage", "#ffb000"]]
    che = r["points"][0]
    assert che[3] == pytest.approx(46.72 + 0.6) and che[4] == pytest.approx(7.46)
    assert r["cyberSize"] == [12, 8]
    assert r["labels"] == ["Chikungunya virus disease 2026-11-17", "Ebola disease 2026-11-06",
                           "Ebola disease 2026-11-06", "OUTAGE 1 crit", "OUTAGE 2 warn",
                           "QUAKE + FIRE + OUTBREAK"]
    assert r["count"] == "8"
    assert r["status"] == ("4 outbreak reports, 2 countries with outage alerts,"
                           " convergence cells: 1.")
    assert r["notes"] == "fires: FIRMS_MAP_KEY is not set on the server; fires are left out"
    assert r["feed"] == ["Country intel: convergence cells 1, countries with outage alerts 2"]
    assert r["top"] == ["Qatar  47.1 elevated", "Dem. Rep. Congo  13.2 low"]
    assert r["handlers"] == 1 and r["click"] == "function" and r["canvas"] == "canvas"


# --- the country card ----------------------------------------------------------------

scenario("card", r"""
  var k = await enabled();
  layers(k).points.items[0].id.sdaPick();
  await settle();
  var card = one(k.ctx.mount, 'intel-card');
  var formula = one(card, 'intel-formula');
  var before = formula.style.display;
  buttonNamed(card, 'FORMULA').fire('click');
  var after = formula.style.display;
  buttonNamed(card, 'FLY TO').fire('click');
  var hrefs = all(card).filter(function (e) { return e.getAttribute('href') !== null; })
    .map(function (a) { return [a.tagName, a.textContent, a.getAttribute('href'),
      a.getAttribute('target'), a.getAttribute('rel')]; });
  var score = one(card, 'intel-score');
  return {
    request: REQUESTS[REQUESTS.length - 1],
    title: byTag(card, 'H4')[0].textContent,
    lines: card.children.map(function (c) { return c.textContent; }),
    before: before, after: after, formula: formula.textContent,
    hrefs: hrefs, flown: k.ctx.viewer.camera.flown.destination,
    score: score.textContent, scoreColor: score.style.color,
    feed: k.feed[k.feed.length - 1],
    status: one(k.ctx.mount, 'intel-status').textContent
  };
""")


def test_card_shows_every_section_and_links_only_https(ran):
    r = ran["card"]
    assert r["request"] == "/api/intel/country/CHE"
    assert r["title"] == "Switzerland (CHE)"
    text = "\n".join(r["lines"])
    for part in (
        "Europe | Western Europe | Europe & Central Asia | High income",
        "Capital Bern. Local time 06:00 Fri 2026-11-20 (UTC+01:00, Europe/Zurich)",
        "INSTABILITY (THIS APPLICATION'S HEURISTIC)", "outbreaks 5",
        "quakes 0 | fires 0 | NWS alerts 0 | EONET 0 | GDELT events 4 (unrest 1)",
        "Population: 9.09 million (2025)", "GDP (current US$): $1.04 trillion (2025)",
        "GDP per capita (current US$): $114,769 (2025)",
        "Inflation, consumer prices (annual %): 0.2% (2025)",
        "World Bank Open Data (CC BY 4.0)",
        "SIX Swiss Exchange: CLOSED (06:00 local), opens 09:00 local [09:00-17:30 Mon-Fri]",
        "Regular session hours from a static table.",
        "2026-11-17 Chikungunya virus disease - Switzerland", "2026-11-01 Bad link",
        "2 listed, 1 online. QakBot 2",
        "Borders: Natural Earth. Indicators: World Bank (CC BY 4.0)",
    ):
        assert part in text, part
    assert "fires 0," not in text  # zero terms are left out of the breakdown
    assert r["before"] == "none" and r["after"] == "block"
    assert r["formula"] == ("score = sum of the terms\noutbreaks = 10 x min(1, O / 2)\n"
                            "This application's own heuristic.")
    assert r["hrefs"] == [[
        "A", "Chikungunya virus disease - Switzerland",
        "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON627",
        "_blank", "noopener noreferrer"]]
    assert r["flown"] == {"lon": 7.46, "lat": 46.72, "h": 600000}
    assert r["score"] == "5.0 / 100 LOW" and r["scoreColor"] == "#2b83ba"
    assert r["feed"] == "Country: Switzerland, instability 5" and r["status"] == ""


scenario("card_gaps", r"""
  var k = await enabled();
  window.SDAIntel.openCountry('QAT');
  await settle();
  var card = one(k.ctx.mount, 'intel-card');
  var lines = card.children.map(function (c) { return c.textContent; });
  var hrefs = all(card).filter(function (e) { return e.getAttribute('href') !== null; })
    .map(function (a) { return a.getAttribute('href'); });
  route('/api/intel/country/', {}, 404);
  route('/api/intel/country/QAT', {}, 404);
  var missing = await window.SDAIntel.openCountry('ZZZ');
  return { lines: lines, hrefs: hrefs, missing: missing,
           status: one(k.ctx.mount, 'intel-status').textContent,
           title: byTag(one(k.ctx.mount, 'intel-card'), 'H4')[0].textContent };
""")


def test_card_with_missing_parts(ran):
    r = ran["card_gaps"]
    text = "\n".join(r["lines"])
    assert "Local time unknown" in text and "Capital" not in text
    assert "Population: n/a" in text
    assert "No major exchange in the table for this country." in text
    assert "No term above zero in the current feeds." in text
    assert "0 critical and 2 warning data sources (bgp, gtr), recovered. IODA dashboard" in text
    assert "WHO DISEASE OUTBREAK NEWS" not in text and "BOTNET" not in text
    assert r["hrefs"] == ["https://ioda.inetintel.cc.gatech.edu/country/QA"]
    assert r["missing"] is None and r["status"] == "No card for ZZZ."
    assert r["title"] == "Qatar (QAT)"  # the last good card stays up


# --- choropleth --------------------------------------------------------------------

scenario("choropleth", r"""
  var k = await enabled();
  var btn = buttonNamed(k.ctx.mount, 'INSTABILITY');
  var pressedBefore = btn.getAttribute('aria-pressed');
  btn.fire('click');
  await settle();
  var prim = layers(k).choro;
  var inst = prim.opts.geometryInstances;
  var out = {
    pressedBefore: pressedBefore, pressed: btn.getAttribute('aria-pressed'),
    btnClass: btn.className,
    borders: REQUESTS.filter(function (u) { return u.indexOf('/api/intel/borders') === 0; }),
    iso: inst.map(function (i) { return i.id.iso3; }),
    colors: inst.map(function (i) { return [i.attributes.color.color.css,
      i.attributes.color.color.a]; }),
    holes: inst.map(function (i) { return i.geometry.hierarchy.holes.length; }),
    outer: inst[0].geometry.hierarchy.positions.deg,
    height: inst[0].geometry.height,
    appearance: prim.opts.appearance.opts, async: prim.opts.asynchronous
  };
  inst[1].id.sdaPick();
  await settle();
  out.card = byTag(one(k.ctx.mount, 'intel-card'), 'H4')[0].textContent;
  window.SDAIntel.setSublayer('choropleth', false);
  out.removed = k.ctx.viewer._removed.length;
  out.primsOff = k.ctx.viewer._prims.length;
  window.SDAIntel.setSublayer('choropleth', true);
  await settle();
  out.primsOn = k.ctx.viewer._prims.length;
  out.bordersAsked = REQUESTS.filter(function (u) {
    return u.indexOf('/api/intel/borders') === 0; }).length;
  return out;
""")


def test_choropleth_colours_countries_and_picks_open_cards(ran):
    r = ran["choropleth"]
    assert r["pressedBefore"] == "false" and r["pressed"] == "true"
    assert r["btnClass"] == "mode-btn active"
    assert r["borders"] == ["/api/intel/borders?tol=0.2"]
    assert r["iso"] == ["CHE", "QAT", "FRA"]  # too-short rings are skipped
    assert r["colors"] == [["#2b83ba", 0.38], ["#ffe680", 0.38], ["#2b83ba", 0.12]]
    assert all(c[1] < 1 for c in r["colors"])  # translucent pass keeps markers visible
    assert r["holes"] == [0, 1, 0]
    assert r["outer"] == [6, 46, 10, 46, 10, 47.8, 6, 47.8]
    assert r["height"] == 1500
    assert r["appearance"] == {"flat": True, "translucent": True} and r["async"] is True
    assert r["card"] == "Qatar (QAT)"
    assert r["removed"] == 1 and r["primsOff"] == 3
    assert r["primsOn"] == 4 and r["bordersAsked"] == 1  # borders are kept


# --- clicks on empty globe --------------------------------------------------------------

scenario("click", r"""
  var k = await enabled();
  var click = HANDLERS[0].actions.LEFT_CLICK;
  var start = REQUESTS.length;
  k.ctx.viewer.scene._picked = { id: { type: 'satellite' } };
  var onSomething = click({ position: { x: 1, y: 2 } });
  k.ctx.viewer.scene._picked = null;
  window.SDAMapTools = { isActive: function () { return true; } };
  var measuring = click({ position: {} });
  window.SDAMapTools = { isActive: function () { return false; } };
  await click({ position: {} });
  await settle();
  var asked = REQUESTS.slice(start);
  var title = byTag(one(k.ctx.mount, 'intel-card'), 'H4')[0].textContent;
  route('/api/intel/country-at', { detail: 'No country at this point' }, 404);
  await click({ position: {} });
  await settle();
  var sea = one(k.ctx.mount, 'intel-status').textContent;
  k.ctx.viewer.camera._hit = null;
  var space = click({ position: {} });
  window.SDAIntel.setEnabled(false);
  k.ctx.viewer.camera._hit = undefined;
  var before = REQUESTS.length;
  var off = click({ position: {} });
  window.SDAIntel.setEnabled(true);
  await settle();
  return { onSomething: onSomething, measuring: measuring, asked: asked, title: title,
           sea: sea, space: space, off: off, offRequests: REQUESTS.length - before,
           handlers: HANDLERS.length };
""")


def test_empty_globe_click_asks_which_country(ran):
    r = ran["click"]
    assert r["onSomething"] is None and r["measuring"] is None
    assert r["asked"] == ["/api/intel/country-at?lat=46.95&lon=7.44", "/api/intel/country/CHE"]
    assert r["title"] == "Switzerland (CHE)"
    assert r["sea"] == "No country there."
    assert r["space"] is None and r["off"] is None
    assert r["offRequests"] == 5  # only the re-enable reload, never the click
    assert r["handlers"] == 1


# --- convergence ring ------------------------------------------------------------------

scenario("convergence", r"""
  var k = await enabled();
  layers(k).rings.items[0].id.sdaPick();
  var card = one(k.ctx.mount, 'intel-card');
  var lines = card.children.map(function (c) { return c.textContent; });
  var same = layers(k).labels.items[5].id === layers(k).rings.items[0].id;
  buttonNamed(card, 'Open QAT country card').fire('click');
  await settle();
  return { lines: lines, same: same, request: REQUESTS[REQUESTS.length - 1],
           title: byTag(one(k.ctx.mount, 'intel-card'), 'H4')[0].textContent };
""")


def test_convergence_ring_lists_its_kinds(ran):
    r = ran["convergence"]
    assert r["lines"] == [
        "Convergence: QUAKE + FIRE + OUTBREAK",
        "Score 81 / 100, 3 events within 24 h in this 2 degree cell.",
        "earthquakes: 1 (M 4.8 - Qatar)", "fires: 1 (fire 12.5 MW)",
        "disease outbreak (country-wide)", "Open QAT country card"]
    assert r["same"] is True
    assert r["request"] == "/api/intel/country/QAT" and r["title"] == "Qatar (QAT)"


# --- nearby cameras ------------------------------------------------------------------------

scenario("cameras", r"""
  var opened = [];
  var k = await enabled();
  route('/api/cameras/nearest', { cameras: [
    { rid: 'c1', name: 'Bern Bundesplatz', city: 'Bern', lat: 46.947, lon: 7.444,
      distance_km: 0.3 },
    { rid: 'c2', name: 'Thun', city: '', lat: 46.75, lon: 7.62, distance_km: 25.4 }] });
  await window.SDAIntel.openCountry('CHE');
  var card = one(k.ctx.mount, 'intel-card');
  buttonNamed(card, 'CAMERAS').fire('click');
  await settle();
  var list = byClass(card, 'intel-list')[0];
  var names = list.children.map(function (b) { return b.textContent; });
  var status = one(k.ctx.mount, 'intel-status').textContent;
  list.children[0].fire('click');  // no camera module loaded: still flies
  var flownBare = k.ctx.viewer.camera.flown.destination;
  window.SDACameras = { open: function (rid) { opened.push(rid); } };
  list.children[1].fire('click');
  var flown = k.ctx.viewer.camera.flown.destination;
  var asked = REQUESTS.filter(function (u) { return u.indexOf('/api/cameras') === 0; });
  route('/api/cameras/nearest', { cameras: [] });
  buttonNamed(card, 'CAMERAS').fire('click');
  await settle();
  var none = one(k.ctx.mount, 'intel-status').textContent;
  route('/api/cameras/nearest', {}, 404);
  buttonNamed(card, 'CAMERAS').fire('click');
  await settle();
  return { names: names, status: status, opened: opened, flownBare: flownBare, flown: flown,
           asked: asked, none: none,
           missing: one(k.ctx.mount, 'intel-status').textContent };
""")


def test_cameras_near_the_capital(ran):
    r = ran["cameras"]
    assert r["asked"] == ["/api/cameras/nearest?lat=46.948&lon=7.448&n=12&radius_km=150"]
    assert r["names"] == ["Bern Bundesplatz, Bern (0 km)", "Thun (25 km)"]
    assert r["status"] == "2 public cameras nearest Switzerland."
    assert r["flownBare"] == {"lon": 7.444, "lat": 46.947, "h": 20000}
    assert r["opened"] == ["c2"] and r["flown"] == {"lon": 7.62, "lat": 46.75, "h": 20000}
    assert r["none"] == "No public camera within 150 km."
    assert "python -m sda.cameras import" in r["missing"]


# --- search -----------------------------------------------------------------------------

scenario("search", r"""
  standardRoutes();
  var k = makeCtx();
  window.SDAIntel.init(k.ctx);
  var input = one(k.ctx.mount, 'intel-search');
  function names() { return byClass(one(k.ctx.mount, 'intel-list'), 'intel-item')
    .map(function (b) { return b.textContent; }); }
  input.value = 'sw'; input.fire('input'); await settle();
  var sw = names();
  input.value = 'fra'; input.fire('input'); await settle();
  var fra = names();
  input.value = 'f'; input.fire('input'); await settle();
  var short = names();
  input.value = 'zzzz'; input.fire('input'); await settle();
  var status = one(k.ctx.mount, 'intel-status').textContent;
  input.value = 'swi'; input.fire('input'); await settle();
  byClass(one(k.ctx.mount, 'intel-list'), 'intel-item')[0].fire('click');
  await settle();
  return { sw: sw, fra: fra, short: short, status: status,
           listAsked: REQUESTS.filter(function (u) { return u === '/api/intel/countries'; })
             .length,
           last: REQUESTS[REQUESTS.length - 1] };
""")


def test_search_finds_countries(ran):
    r = ran["search"]
    assert r["sw"] == ["Switzerland (CHE)", "Sweden (SWE)"]
    assert r["fra"] == ["France (FRA)"]
    assert r["short"] == []
    assert r["status"] == 'No country named like "zzzz".'
    assert r["listAsked"] == 1 and r["last"] == "/api/intel/country/CHE"


# --- sub-layers, disable, static, failures ------------------------------------------------

scenario("sublayers", r"""
  var k = await enabled();
  var L = layers(k);
  window.SDAIntel.setSublayer('outbreaks', false);
  var noOutbreaks = [L.points.items.length, document.getElementById('count-intel').textContent,
    buttonNamed(k.ctx.mount, 'OUTBREAKS').getAttribute('aria-pressed')];
  buttonNamed(k.ctx.mount, 'C2').fire('click');
  var noCyber = [L.points.items.length, document.getElementById('count-intel').textContent];
  buttonNamed(k.ctx.mount, 'CONVERGE').fire('click');
  var noRings = [L.rings.items.length, L.labels.items.length,
    document.getElementById('count-intel').textContent];
  buttonNamed(k.ctx.mount, 'OUTBREAKS').fire('click');
  return { noOutbreaks: noOutbreaks, noCyber: noCyber, noRings: noRings,
           back: L.points.items.length, requests: REQUESTS.length };
""")


def test_sublayer_buttons_redraw_without_refetching(ran):
    r = ran["sublayers"]
    assert r["noOutbreaks"] == [4, "5", "false"]
    assert r["noCyber"] == [2, "3"]
    assert r["noRings"] == [0, 2, "2"]
    assert r["back"] == 5 and r["requests"] == 5


scenario("disable", r"""
  var k = await enabled();
  window.SDAIntel.setSublayer('choropleth', true);
  await settle();
  var L = layers(k);
  window.SDAIntel.setEnabled(false);
  var asked = REQUESTS.length;
  var refreshed = await window.SDAIntel.refresh();
  var out = { enabled: window.SDAIntel.isEnabled(), points: L.points.items.length,
    labels: L.labels.items.length, rings: L.rings.items.length,
    shown: [L.points.show, L.labels.show, L.rings.show], prims: k.ctx.viewer._prims.length,
    count: document.getElementById('count-intel').textContent,
    status: one(k.ctx.mount, 'intel-status').textContent, refreshed: refreshed,
    refreshAsked: REQUESTS.length - asked };
  // A load still in flight when the layer is switched off draws nothing.
  window.SDAIntel.setEnabled(true);
  window.SDAIntel.setEnabled(false);
  await settle();
  out.raced = [L.points.items.length, document.getElementById('count-intel').textContent];
  return out;
""")


def test_disable_clears_everything(ran):
    r = ran["disable"]
    assert r["enabled"] is False
    assert (r["points"], r["labels"], r["rings"]) == (0, 0, 0)
    assert r["shown"] == [False, False, False] and r["prims"] == 3
    assert r["count"] == "0" and r["status"] == ""
    assert r["refreshed"] is None and r["refreshAsked"] == 0
    assert r["raced"] == [0, "0"]


scenario("static", r"""
  standardRoutes();
  var k = makeCtx({ isStatic: true });
  window.SDAIntel.init(k.ctx);
  window.SDAIntel.setEnabled(true);
  var input = one(k.ctx.mount, 'intel-search');
  input.value = 'swi'; input.fire('input');
  var card = window.SDAIntel.openCountry('CHE');
  await settle();
  return { requests: REQUESTS.length, enabled: window.SDAIntel.isEnabled(), card: card,
           summary: one(k.ctx.mount, 'intel-summary').textContent,
           status: one(k.ctx.mount, 'intel-status').textContent,
           prims: k.ctx.viewer._prims.length, handlers: HANDLERS.length };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == 0 and r["enabled"] is False and r["card"] is None
    assert r["summary"] == "Country intel needs the live server."
    assert r["status"] == "Country intel needs the live server."
    assert r["prims"] == 0 and r["handlers"] == 0


scenario("failing", r"""
  var k = makeCtx();
  window.SDAIntel.init(k.ctx);
  window.SDAIntel.setEnabled(true);
  await settle();
  var status = one(k.ctx.mount, 'intel-status').textContent;
  var count = document.getElementById('count-intel').textContent;
  await window.SDAIntel.openCountry('CHE');
  window.SDAIntel.setSublayer('choropleth', true);
  await settle();
  return { status: status, count: count, feed: k.feed,
           after: one(k.ctx.mount, 'intel-status').textContent,
           prims: k.ctx.viewer._prims.length };
""")


def test_failing_endpoints_degrade_quietly(ran):
    r = ran["failing"]
    assert r["status"] == "Country intel did not load."
    assert r["count"] == "0" and r["feed"] == []
    assert r["after"] == "Country borders did not load."
    assert r["prims"] == 3


scenario("init_guards", r"""
  var noMount = window.SDAIntel.init({ viewer: makeViewer() });
  var noViewer = window.SDAIntel.init({ mount: new El('div') });
  window.SDAIntel.setEnabled(true);  // nothing initialised: a no-op
  var openNone = window.SDAIntel.openCountry('CHE');
  var saved = window.Cesium;
  window.Cesium = undefined;
  var noCesium = window.SDAIntel.init(makeCtx().ctx);
  window.Cesium = saved;
  var k = makeCtx();
  var ok = window.SDAIntel.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  return { noMount: noMount, noViewer: noViewer, noCesium: noCesium, openNone: openNone,
           ok: ok, collapsed: k.ctx.mount.children[0].has('collapsed'),
           title: head.textContent, style: !!document.getElementById('sda-intel-style'),
           requests: REQUESTS.length,
           api: Object.keys(window.SDAIntel).filter(function (n) {
             return ['init', 'setEnabled', 'isEnabled', 'refresh', 'openCountry',
                     'setSublayer'].indexOf(n) >= 0; }).length };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert r == {"noMount": False, "noViewer": False, "noCesium": False, "openNone": None,
                 "ok": True, "collapsed": True, "title": "Country Intel", "style": True,
                 "requests": 0, "api": 6}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "intel.js must stay ASCII"
