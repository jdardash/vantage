"""Tests for the globe infrastructure module (static/js/infra.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_peaks.py does it. The
fake `fetch` answers from a table, so nothing here touches the network. Skips
cleanly when Node is not installed.

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
    / "src" / "sda" / "templates" / "static" / "js" / "infra.js"
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

var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  }
};
var countEl = new El('span'); countEl.id = 'count-infra'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var Cesium = {
  PolylineCollection: Coll, LabelCollection: Coll, BillboardCollection: Coll,
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: {
    fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; },
    fromDegreesArray: function (flat) {
      var out = [];
      for (var i = 0; i + 1 < flat.length; i += 2) out.push({ lon: flat[i], lat: flat[i + 1] });
      return out;
    }
  },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  NearFarScalar: function () {},
  Material: { fromType: function (type, u) { return { type: type, uniforms: u }; } },
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0, LEFT: 1 },
  VerticalOrigin: { CENTER: 0, BOTTOM: 1 }
};
var window = this;
this.Cesium = Cesium;

// ---- fake fetch ----------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
var DELAYS = {};  // url -> milliseconds before the answer arrives
// url -> held answer: it arrives only when the test calls release(), so an
// in-flight request stays in flight however slow the machine is.
var GATES = {};
function gate(url) {
  var g = {};
  g.promise = new Promise(function (r) { g.release = r; });
  GATES[url] = g;
  return g;
}
function route(prefix, body, status) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200 }); }
function fetch(url) {
  REQUESTS.push(url);
  for (var i = 0; i < ROUTES.length; i++) {
    if (url.indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      var answer = { ok: r.status < 400, status: r.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r.body))); } };
      if (GATES[url]) return GATES[url].promise.then(function () { return answer; });
      if (DELAYS[url]) return wait(DELAYS[url]).then(function () { return answer; });
      return Promise.resolve(answer);
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () { return Promise.resolve({});
    } });
}

// ---- fixtures --------------------------------------------------------------
var SUMMARY = {
  available: true, count: 9, imported_at: '2026-11-19T00:00:00Z',
  kinds: { power: { count: 4 }, nuclear: { count: 1 }, airport: { count: 2 }, port: { count: 0 },
    dam: { count: 0 }, datacenter: { count: 0 }, landing: { count: 2 }, radiation: { count: 0 } },
  cables: { available: true, count: 2, landing_points: 2, loading: false, error: null },
  attribution: 'Power: WRI GPPD (CC BY 4.0). Cables: TeleGeography (CC BY-NC-SA 3.0).'
};
var JFK = { id: 'airport-KJFK', kind: 'airport', sub: 'large', name: 'John F Kennedy Intl',
  lat: 30.27, lon: -97.74, country: 'US', brief: 'Large airport, KJFK / JFK', score: 4500 };
var LGA = { id: 'airport-KLGA', kind: 'airport', sub: 'large', name: 'LaGuardia',
  lat: 30.271, lon: -97.741, country: 'US', brief: 'Large airport, KLGA / LGA', score: 4400 };
var PLANT = { id: 'nuclear-WRI1', kind: 'nuclear', sub: 'Nuclear', name: 'Comanche Peak',
  lat: 30.27, lon: -97.74, country: 'United States', brief: '2,400 MW nuclear', score: 2400 };
var HYDRO = { id: 'power-WRI2', kind: 'power', sub: 'Hydro', name: 'Mansfield Dam',
  lat: 30.5, lon: -97.5, country: 'United States', brief: '108 MW hydro', score: 108 };
var ITEMS = { items: [JFK, LGA, PLANT, HYDRO], count: 4,
  counts: { power: 1, nuclear: 1, airport: 2 }, total: 4, capped: false };
var CABLES = { type: 'FeatureCollection', available: true, loading: false, error: null,
  features: [
    { type: 'Feature', id: 'alpha-link', properties: { id: 'alpha-link', name: 'Alpha Link',
      color: '#3366ff' }, geometry: { type: 'MultiLineString',
      coordinates: [[[0, 0], [1, 1]], [[2, 2], [3, 3]]] } },
    { type: 'Feature', id: 'beta-cable', properties: { id: 'beta-cable', name: 'Beta Cable',
      color: 'red;' }, geometry: { type: 'MultiLineString', coordinates: [[[5, 5], [6, 6]]] } }
  ] };
var ALPHA = { id: 'alpha-link', name: 'Alpha Link', color: '#3366ff', length: '1,200 km',
  rfs_year: 2019, is_planned: false, owners: 'Alpha Telecom', suppliers: 'SubCom',
  url: 'https://alpha.example/',
  map_url: 'https://www.submarinecablemap.com/submarine-cable/alpha-link',
  landing_points: [{ id: 'a', name: 'A, Isle', country: 'Isle' },
                   { id: 'b', name: 'B, Mainland', country: 'Mainland' }],
  countries: ['Isle', 'Mainland'],
  countries_detail: [
    { country: 'Isle', landings: 1, cables_total: 1, other_cables: 0 },
    { country: 'Mainland', landings: 1, cables_total: 3, other_cables: 2 }],
  attribution: 'TeleGeography submarinecablemap.com, CC BY-NC-SA 3.0' };
var JFK_DETAIL = Object.assign({}, JFK, { label: 'Airports',
  fields: { ident: 'KJFK', icao: 'KJFK', iata: 'JFK', type: 'Large airport',
    municipality: 'New York', region: 'US-NY', elevation_ft: 13, scheduled_service: true },
  links: [{ label: 'LiveATC feeds for KJFK', url: 'https://www.liveatc.net/search/?icao=KJFK' },
          { label: 'Bad', url: 'javascript:alert(1)' }],
  source: { name: 'OurAirports', licence: 'Public domain', url: 'https://ourairports.com/' } });
var LANDING_DETAIL = { id: 'landing-a', kind: 'landing', sub: 'landing', name: 'A, Isle',
  lat: 1, lon: 1, country: 'Isle', brief: '1 cable', score: 1, label: 'Cable landings',
  fields: { landing_id: 'a', cables: [{ id: 'alpha-link', name: 'Alpha Link' }],
    country_cables: 1, map_url: 'https://www.submarinecablemap.com/landing-point/a' },
  links: [{ label: 'Submarine Cable Map',
            url: 'https://www.submarinecablemap.com/landing-point/a' }],
  source: { name: 'TeleGeography', licence: 'CC BY-NC-SA 3.0', url: 'https://x.example/' } };

function standardRoutes() {
  route('/api/infra/summary', SUMMARY);
  route('/api/infra/items', ITEMS);
  route('/api/infra/cables', CABLES);
  route('/api/infra/cables/alpha-link', ALPHA);
  route('/api/infra/item/airport-KJFK', JFK_DETAIL);
  route('/api/infra/item/landing-a', LANDING_DETAIL);
}

function makeViewer(heightM) {
  var moveEnd = [];
  var prims = [];
  var d = Math.PI / 180;
  return {
    scene: { primitives: { add: function (c) { prims.push(c); return c; } },
      globe: { ellipsoid: {} } },
    camera: {
      positionCartographic: { height: heightM, latitude: 30.27 * d, longitude: -97.74 * d },
      rect: { west: -98 * d, south: 30 * d, east: -97 * d, north: 31 * d },
      computeViewRectangle: function () { return this.rect; },
      moveEnd: { addEventListener: function (fn) { moveEnd.push(fn); } },
      flyTo: function (o) { this.flown = o; }
    },
    _moveEnd: moveEnd,
    _prims: prims
  };
}

function makeCtx(heightM, opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(heightM), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    satEngine: { byNoradId: {}, selectedId: null },
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

// The module adds three collections: cable lines, markers, labels.
function layers(k) {
  var p = k.ctx.viewer._prims;
  return { lines: p[0], marks: p[1], labels: p[2] };
}
function cardText(k) { return byClass(k.ctx.mount, 'infra-card')[0].textContent; }
function cardLinks(k) {
  return byTag(byClass(k.ctx.mount, 'infra-card')[0], 'A').map(function (a) {
    return [a.textContent, a.getAttribute('href'), a.getAttribute('rel')];
  });
}
function text(k, cls) { return byClass(k.ctx.mount, cls)[0].textContent; }

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
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
              JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'infra.js' });
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
      // stop any retry timer the scenario left running
      try { vm.runInContext('window.SDAInfra && window.SDAInfra.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("infra-globe")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=90, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- pure helpers --------------------------------------------------------------

scenario("helpers", r"""
  var I = window.SDAInfra;
  var d = Math.PI / 180;
  var shapes = {}, colors = {};
  I.KINDS.forEach(function (k) { shapes[k.shape] = 1; colors[k.color] = 1; });
  return {
    kinds: I.KINDS.map(function (k) { return k.key; }),
    shapes: Object.keys(shapes).length, colors: Object.keys(colors).length,
    icons: I.KINDS.every(function (k) {
      return I.ICONS[k.key].indexOf('data:image/svg+xml,') === 0 &&
        I.CLUSTER_ICONS[k.key] !== I.ICONS[k.key]; }),
    query: I.query([['bbox', '1,2,3,4'], ['kinds', ''], ['q', 'a b&c'], ['n', null]]),
    empty: I.query([['x', undefined]]),
    rect: I.boxFromRect({ west: -98 * d, south: 30 * d, east: -97 * d, north: 31 * d }, 0.1),
    world: I.boxFromRect({ west: -Math.PI, south: -Math.PI / 2, east: Math.PI,
      north: Math.PI / 2 }, 0.1),
    dateline: I.boxFromRect({ west: 179 * d, south: 0, east: -179 * d, north: 1 * d }, 0.5),
    noRect: I.boxFromRect(null),
    around: I.boxAround(0, 179.5, 111.195),
    kindsAll: I.kindsParam({ power: true, nuclear: true, airport: true, port: true, dam: true,
      datacenter: true, landing: true, radiation: true }),
    kindsSome: I.kindsParam({ radiation: true, airport: true, bogus: true }),
    kindsNone: I.kindsParam({}),
    atc: [I.liveAtcUrl('kjfk'), I.liveAtcUrl(' EGLL '), I.liveAtcUrl('JFK'),
          I.liveAtcUrl('K J'), I.liveAtcUrl(null)],
    safe: [I.safeUrl('https://a.b/c'), I.safeUrl('http://x'), I.safeUrl('javascript:x'),
           I.safeUrl('https://a b'), I.safeUrl(7)],
    counts: [I.formatCount(7), I.formatCount(1234), I.formatCount(56789),
             I.formatCount(2500000), I.formatCount(NaN)],
    sizes: [I.clusterSize(1), I.clusterSize(10), I.clusterSize(1e9)],
    lines: I.cableLines({ geometry: { type: 'MultiLineString', coordinates: [
      [[0, 0], [1, 1]], [[2, 2], [999, 0]], [[3, 3], ['x', 1], [4, 4]]] } }),
    lineString: I.cableLines({ geometry: { type: 'LineString', coordinates: [[0, 0], [1, 1]] } }),
    point: I.cableLines({ geometry: { type: 'Point', coordinates: [0, 0] } }),
    colors2: [I.cableColor({ properties: { color: '#A1b2C3' } }),
              I.cableColor({ properties: { color: 'red;x' } }), I.cableColor(null)]
  };
""")


def test_kinds_have_distinct_shapes_colours_and_icons(ran):
    r = ran["helpers"]
    assert r["kinds"] == ["power", "nuclear", "airport", "port", "dam", "datacenter", "landing",
                          "radiation"]
    assert r["shapes"] == 8 and r["colors"] == 8
    assert r["icons"] is True


def test_view_and_url_helpers(ran):
    r = ran["helpers"]
    assert r["query"] == "?bbox=1%2C2%2C3%2C4&q=a%20b%26c"
    assert r["empty"] == ""
    assert r["rect"] == {"west": -98.1, "south": 29.9, "east": -96.9, "north": 31.1}
    assert r["world"] == {"west": -180, "south": -90, "east": 180, "north": 90}
    # a view across the antimeridian keeps west > east
    assert r["dateline"]["west"] == 178 and r["dateline"]["east"] == -178
    assert r["noRect"] is None
    assert r["around"]["west"] == 178.5 and r["around"]["east"] == -179.5
    assert r["kindsAll"] == "power,nuclear,airport,port,dam,datacenter,landing,radiation"
    assert r["kindsSome"] == "airport,radiation" and r["kindsNone"] == ""
    assert r["atc"] == ["https://www.liveatc.net/search/?icao=KJFK",
                        "https://www.liveatc.net/search/?icao=EGLL", "", "", ""]
    assert r["safe"] == ["https://a.b/c", "http://x", "", "", ""]
    assert r["counts"] == ["7", "1.2k", "57k", "2.5M", "0"]
    assert r["sizes"][0] == 16 and r["sizes"][1] == 22 and r["sizes"][2] == 34


def test_cable_geometry_helpers(ran):
    r = ran["helpers"]
    assert r["lines"] == [[[0, 0], [1, 1]], [[3, 3], [4, 4]]]
    assert r["lineString"] == [[[0, 0], [1, 1]]]
    assert r["point"] == []
    assert r["colors2"] == ["#A1b2C3", "#ff7043", "#ff7043"]


scenario("cluster", r"""
  var I = window.SDAInfra;
  var box = { west: -98, south: 30, east: -97, north: 31 };
  var near = I.cluster([JFK, LGA, PLANT, HYDRO, { id: 'x', kind: 'bogus', lat: 0, lon: 0 },
    { id: 'y', kind: 'power', lat: NaN, lon: 0 }], box, 28);
  var cross = I.cluster([
    { id: 'e', kind: 'dam', lat: 0, lon: 179.99, score: 1 },
    { id: 'w', kind: 'dam', lat: 0, lon: -179.99, score: 5 }],
    { west: 179.2, south: -1, east: -179.1, north: 1 }, 28);
  return {
    singles: near.singles.map(function (s) { return s.id; }),
    clusters: near.clusters.map(function (c) {
      return { kind: c.kind, count: c.count, best: c.best.id,
               lat: Math.round(c.lat * 10000) / 10000,
               lon: Math.round(c.lon * 10000) / 10000 }; }),
    cross: cross.clusters.map(function (c) {
      return { count: c.count, best: c.best.id, lon: Math.round(Math.abs(c.lon) * 100) / 100 };
    }),
    empty: I.cluster([], box, 28)
  };
""")


def test_cluster_groups_one_kind_per_cell(ran):
    r = ran["cluster"]
    # the two airports share a cell; the nuclear plant at the same spot stays its own marker
    assert r["singles"] == ["nuclear-WRI1", "power-WRI2"]
    assert r["clusters"] == [{"kind": "airport", "count": 2, "best": "airport-KJFK",
                              "lat": 30.2705, "lon": -97.7405}]
    # two dams either side of the antimeridian share a cell (the grid is world-aligned, so
    # the view is chosen with a cell straddling 180) and their mean sits on the line
    assert r["cross"] == [{"count": 2, "best": "w", "lon": 180.0}]
    assert r["empty"] == {"singles": [], "clusters": []}


scenario("card_models", r"""
  var I = window.SDAInfra;
  function cm(kind, fields, extra) {
    return I.cardModel(Object.assign({ kind: kind, name: 'N', country: 'CC', brief: 'b',
      fields: fields, links: [], source: { name: 'S', licence: 'L' } }, extra || {}));
  }
  var noLink = cm('airport', { icao: 'EGLL', iata: 'LHR', type: 'Large airport',
    municipality: 'London', region: 'GB-ENG', elevation_ft: 83, scheduled_service: true });
  var withLink = I.cardModel(JFK_DETAIL);
  return {
    airport: noLink.rows, airportLinks: noLink.links,
    jfkLinks: withLink.links, jfkTitle: withLink.title, jfkSub: withLink.subtitle,
    jfkSource: withLink.source,
    power: cm('nuclear', { capacity_mw: 2430, fuel: 'Nuclear', other_fuels: ['Oil'],
      commissioning_year: 1990.0, owner: 'Luminant', generation_gwh: 19000,
      generation_year: 2019, generation_estimated: true }).rows,
    port: cm('port', { harbor_size: 'Large', harbor_type: 'River', un_locode: 'NLRTM',
      channel_depth_m: 24.1, cargo_pier_depth_m: 16, max_vessel_length_m: 400,
      container: 'Yes', oil_terminal: 'Yes', lng_terminal: 'No' }).rows,
    dam: cm('dam', { height_m: 221.4, year: 1936, river: 'Colorado River',
      types: ['arch dam', 'gravity dam'] }).rows,
    dc: cm('datacenter', { operator: 'Equinix', city: 'Ashburn', state: 'VA', networks: 300,
      exchanges: 4, carriers: 0, clli: 'ASBNVA' }).rows,
    bfs: cm('radiation', { network: 'BfS ODL (Germany)', dose_rate_usvh: 0.0921,
      measured_at: '2026-11-19T10:00:00Z', status: 'operating' }).rows,
    safecast: cm('radiation', { network: 'Safecast (volunteer)', cpm: 33.4,
      tube: 'lnd_7318u', dose_rate_usvh_approx: 0.1, position_note: 'rounded to 0.01 degree'
    }).rows,
    landing: I.cardModel(LANDING_DETAIL),
    junk: [I.cardModel(null), I.cardModel('x')],
    unnamed: cm('dam', {}, { name: '' }).title
  };
""")


def test_card_models_per_kind(ran):
    r = ran["card_models"]
    assert r["airport"] == [["Codes", "ICAO EGLL / IATA LHR"], ["Type", "Large airport"],
                            ["Place", "London, GB-ENG"], ["Elevation", "83 ft"],
                            ["Scheduled service", "yes"]]
    # the LiveATC search link is added when the server left it out, never twice
    assert r["airportLinks"] == [{"label": "LiveATC feeds for EGLL",
                                  "url": "https://www.liveatc.net/search/?icao=EGLL"}]
    assert r["jfkLinks"] == [{"label": "LiveATC feeds for KJFK",
                              "url": "https://www.liveatc.net/search/?icao=KJFK"}]
    assert r["jfkTitle"] == "John F Kennedy Intl"
    assert r["jfkSub"] == "Airports | Large airport, KJFK / JFK"
    assert r["jfkSource"] == "OurAirports (Public domain)"
    assert r["power"] == [["Capacity", "2,430 MW"], ["Fuel", "Nuclear, Oil"],
                          ["Commissioned", "1990"], ["Owner", "Luminant"],
                          ["Generation", "19,000 GWh in 2019 (estimated)"], ["Country", "CC"]]
    assert r["port"] == [["Harbor", "Large, River"], ["UN/LOCODE", "NLRTM"],
                         ["Channel depth", "24.1 m"], ["Cargo pier depth", "16.0 m"],
                         ["Max vessel length", "400 m"],
                         ["Facilities", "container, oil terminal"], ["Country", "CC"]]
    assert r["dam"] == [["Height", "221.4 m"], ["Built", "1936"], ["River", "Colorado River"],
                        ["Type", "arch dam, gravity dam"], ["Country", "CC"]]
    assert r["dc"] == [["Operator", "Equinix"], ["Place", "Ashburn, VA, CC"],
                       ["Networks", "300"], ["Exchanges", "4"], ["Carriers", "0"],
                       ["CLLI", "ASBNVA"]]
    assert r["bfs"] == [["Network", "BfS ODL (Germany)"], ["Dose rate", "0.092 uSv/h"],
                        ["Measured", "2026-11-19T10:00:00Z"], ["Status", "operating"]]
    assert r["safecast"] == [["Network", "Safecast (volunteer)"],
                             ["Dose rate", "about 0.100 uSv/h"],
                             ["Count rate", "33 cpm (lnd_7318u)"],
                             ["Position", "rounded to 0.01 degree"]]
    landing = r["landing"]
    assert landing["rows"] == [["Country", "Isle"], ["Cables landing here", "1"],
                               ["Cables landing in Isle", "1"]]
    assert landing["cables"] == [{"id": "alpha-link", "name": "Alpha Link"}]
    assert r["junk"] == [None, None]
    assert r["unnamed"] == "Unnamed"


scenario("cable_model", r"""
  var I = window.SDAInfra;
  var m = I.cableCardModel(ALPHA);
  var planned = I.cableCardModel({ name: 'P', is_planned: true, countries_detail: [
    { country: 'X', landings: 2, other_cables: 1 }, { country: 7, other_cables: 1 }],
    url: 'javascript:alert(1)' });
  return { m: m, planned: planned, junk: I.cableCardModel(null) };
""")


def test_cable_card_model_names_sole_links(ran):
    r = ran["cable_model"]
    m = r["m"]
    assert m["title"] == "Alpha Link"
    assert m["subtitle"] == "Submarine cable | 2 countries"
    assert m["rows"] == [["Length", "1,200 km"], ["Ready for service", "2019"],
                         ["Owners", "Alpha Telecom"], ["Suppliers", "SubCom"],
                         ["Landing points", "2"]]
    assert m["countries"] == [
        {"country": "Isle", "text": "1 landing, no other cable lands there", "sole": True},
        {"country": "Mainland", "text": "1 landing, 2 other cables land there", "sole": False}]
    assert [link["label"] for link in m["links"]] == ["Cable website", "Submarine Cable Map"]
    p = r["planned"]
    assert p["rows"] == [["Ready for service", "planned"]]
    assert p["countries"] == [{"country": "X", "text": "2 landings, 1 other cable land there",
                               "sole": False}]
    assert p["links"] == []
    assert r["junk"] is None


# --- the layer on a fake globe ---------------------------------------------------

scenario("enable", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  var kindCounts = {};
  byClass(k.ctx.mount, 'infra-kind').forEach(function (b) {
    kindCounts[b.children[1].textContent] = b.children[2].textContent; });
  var cluster = L.marks.items.filter(function (b) { return b.id.type === 'infra-cluster'; });
  var singles = L.marks.items.filter(function (b) { return b.id.type === 'infra'; });
  return {
    requests: REQUESTS,
    marks: L.marks.items.length, cluster: cluster.map(function (c) {
      return [c.id.kind, c.id.count]; }),
    singles: singles.map(function (s) { return s.id.id; }),
    labels: L.labels.items.map(function (l) { return l.text; }).sort(),
    depthClose: L.marks.items.every(function (b) {
      return b.disableDepthTestDistance === Infinity; }),
    lines: L.lines.items.length,
    lineIds: L.lines.items.map(function (l) { return l.id.id; }),
    lineColors: L.lines.items.map(function (l) { return l.material.uniforms.color.css; }),
    linePts: L.lines.items[0].positions,
    count: countEl.textContent,
    kindCounts: kindCounts,
    summary: text(k, 'infra-summary'), attr: text(k, 'infra-attr'), view: text(k, 'infra-view'),
    feed: k.feed, enabled: window.SDAInfra.isEnabled()
  };
""")


def test_enable_loads_summary_view_and_cables(ran):
    r = ran["enable"]
    assert r["requests"] == [
        "/api/infra/summary",
        "/api/infra/items?bbox=-98.1%2C29.9%2C-96.9%2C31.1&kinds=power%2Cnuclear%2Cairport"
        "%2Cport%2Cdam%2Cdatacenter%2Clanding%2Cradiation&limit=1500",
        "/api/infra/cables",
    ]
    assert r["marks"] == 3
    assert r["cluster"] == [["airport", 2]]
    assert r["singles"] == ["nuclear-WRI1", "power-WRI2"]
    assert r["labels"] == ["2", "Comanche Peak", "Mansfield Dam"]
    assert r["depthClose"] is True
    assert r["lines"] == 3 and r["lineIds"] == ["alpha-link", "alpha-link", "beta-cable"]
    assert r["lineColors"] == ["#3366ff", "#3366ff", "#ff7043"]
    assert r["linePts"] == [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 1}]
    assert r["count"] == "4"
    assert r["kindCounts"]["Airports"] == "2" and r["kindCounts"]["Power plants"] == "1"
    assert r["kindCounts"]["Seaports"] == ""
    assert r["summary"] == ("4 power plants, 1 nuclear plants, 2 airports, 2 cable landings, "
                            "2 cables.")
    assert r["attr"].startswith("Power: WRI GPPD")
    assert r["view"] == "4 sites in view. Click a marker for its card."
    assert r["feed"] == ["Infrastructure: 9 sites", "Infrastructure: 2 submarine cables"]
    assert r["enabled"] is True


scenario("far_view", r"""
  standardRoutes();
  route('/api/infra/items', Object.assign({}, ITEMS, { total: 90000, capped: true }));
  var k = makeCtx(9000000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  return { labels: L.labels.items.map(function (l) { return l.text; }),
           depthFar: L.marks.items.every(function (b) {
             return b.disableDepthTestDistance === 0; }),
           view: text(k, 'infra-view'), count: countEl.textContent };
""")


def test_far_view_hides_names_and_says_when_capped(ran):
    r = ran["far_view"]
    assert r["labels"] == ["2"]  # only the cluster count, no names from far away
    assert r["depthFar"] is True
    assert r["view"].startswith("Showing the 4 largest of 90,000 sites in view")
    assert r["count"] == "90k"


scenario("click_item", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  var cluster = L.marks.items.filter(function (b) { return b.id.type === 'infra-cluster'; })[0];
  cluster.id.sdaPick();
  var flown = k.ctx.viewer.camera.flown.destination;
  REQUESTS.length = 0;
  window.SDAInfra.open(JFK);
  await settle();
  return { flown: { lon: Math.round(flown.lon * 10000) / 10000,
                    lat: Math.round(flown.lat * 10000) / 10000, h: flown.h },
           requests: REQUESTS, card: cardText(k), links: cardLinks(k),
           feed: k.feed[k.feed.length - 1], status: text(k, 'infra-status') };
""")


def test_clicking_opens_cards_and_clusters_fly_in(ran):
    r = ran["click_item"]
    assert r["flown"] == {"lon": -97.7405, "lat": 30.2705, "h": 20000}
    assert r["requests"] == ["/api/infra/item/airport-KJFK"]
    assert "John F Kennedy Intl" in r["card"]
    assert "ICAO KJFK / IATA JFK" in r["card"]
    assert "Source: OurAirports (Public domain)" in r["card"]
    # the javascript: link from the server never reaches the page
    assert r["links"] == [["LiveATC feeds for KJFK", "https://www.liveatc.net/search/?icao=KJFK",
                           "noopener noreferrer"]]
    assert r["feed"].startswith("Infrastructure: John F Kennedy Intl")
    assert r["status"] == ""


scenario("click_cable", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  REQUESTS.length = 0;
  L.lines.items[0].id.sdaPick();
  await settle();
  var widths = L.lines.items.map(function (l) { return l.width; });
  var sole = byClass(k.ctx.mount, 'infra-sole').map(function (e) { return e.textContent; });
  var card = cardText(k);
  var links = cardLinks(k);
  var feed = k.feed[k.feed.length - 1];
  route('/api/infra/cables/beta-cable', {}, 404);
  window.SDAInfra.openCable('beta-cable');
  await settle();
  return { requests: REQUESTS, widths: widths, sole: sole, card: card, links: links,
           feed: feed, status: text(k, 'infra-status'),
           widthsAfter: L.lines.items.map(function (l) { return l.width; }) };
""")


def test_cable_card_lists_countries_and_flags_sole_links(ran):
    r = ran["click_cable"]
    assert r["requests"] == ["/api/infra/cables/alpha-link", "/api/infra/cables/beta-cable"]
    assert r["widths"] == [4, 4, 1.5]
    assert r["sole"] == ["Isle1 landing, no other cable lands there"]
    assert "Mainland1 landing, 2 other cables land there" in r["card"]
    assert "Lands in (other cables in that country):" in r["card"]
    assert [link[0] for link in r["links"]] == ["Cable website", "Submarine Cable Map"]
    assert r["feed"] == "Cable: Alpha Link, only cable for Isle"
    assert r["status"] == "The cable card did not load."
    assert r["widthsAfter"] == [1.5, 1.5, 4]


scenario("landing_card", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  REQUESTS.length = 0;
  window.SDAInfra.open({ id: 'landing-a', name: 'A, Isle' });
  await settle();
  var card = cardText(k);
  var btn = buttonNamed(byClass(k.ctx.mount, 'infra-card')[0], 'Alpha Link');
  btn.fire('click');
  await settle();
  return { requests: REQUESTS, card: card, after: cardText(k) };
""")


def test_landing_card_opens_its_cables(ran):
    r = ran["landing_card"]
    assert r["requests"] == ["/api/infra/item/landing-a", "/api/infra/cables/alpha-link"]
    assert "Cables landing here:" in r["card"]
    assert r["after"].startswith("Alpha Link")


scenario("toggles", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  REQUESTS.length = 0;
  buttonNamed(k.ctx.mount, 'Airports2').fire('click');
  await settle();
  var afterAirports = REQUESTS.slice();
  var marksNoAirports = L.marks.items.map(function (b) { return b.id.kind; });
  var airportBtn = byClass(k.ctx.mount, 'infra-kind').filter(function (b) {
    return b.children[1].textContent === 'Airports'; })[0];
  var pressed = airportBtn.getAttribute('aria-pressed');
  ['power', 'nuclear', 'port', 'dam', 'datacenter', 'landing', 'radiation'].forEach(
    function (key) { window.SDAInfra.toggleKind(key); });
  await settle();
  var allOff = { requests: REQUESTS.length - afterAirports.length,
                 view: text(k, 'infra-view'), marks: L.marks.items.length };
  var cableBtn = byClass(k.ctx.mount, 'infra-kind').filter(function (b) {
    return b.textContent.indexOf('Submarine cables') === 0; })[0];
  cableBtn.fire('click');
  var cablesOff = { show: L.lines.show, pressed: cableBtn.getAttribute('aria-pressed') };
  REQUESTS.length = 0;
  cableBtn.fire('click');
  await settle();
  return { afterAirports: afterAirports, marksNoAirports: marksNoAirports, pressed: pressed,
           allOff: allOff, cablesOff: cablesOff, cablesOn: L.lines.show,
           cableRequests: REQUESTS };
""")


def test_kind_and_cable_toggles(ran):
    r = ran["toggles"]
    assert r["afterAirports"] == [
        "/api/infra/items?bbox=-98.1%2C29.9%2C-96.9%2C31.1&kinds=power%2Cnuclear%2Cport%2Cdam"
        "%2Cdatacenter%2Clanding%2Cradiation&limit=1500"]
    assert sorted(r["marksNoAirports"]) == ["nuclear", "power"]
    assert r["pressed"] == "false"
    # six more kinds switched off ask the server six times; the seventh asks nothing
    assert r["allOff"] == {"requests": 6, "view": "Every kind is switched off.", "marks": 0}
    assert r["cablesOff"] == {"show": False, "pressed": "false"}
    # cables were already drawn, so switching them back on asks for nothing
    assert r["cablesOn"] is True and r["cableRequests"] == []


scenario("move", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var cam = k.ctx.viewer.camera;
  var d = Math.PI / 180;
  REQUESTS.length = 0;
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await wait(450);
  await settle();
  var same = REQUESTS.length;
  cam.rect = { west: -90 * d, south: 40 * d, east: -89 * d, north: 41 * d };
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await wait(450);
  await settle();
  return { same: same, requests: REQUESTS };
""")


def test_camera_moves_are_debounced_and_unchanged_views_skipped(ran):
    r = ran["move"]
    assert r["same"] == 0
    assert r["requests"] == [
        "/api/infra/items?bbox=-90.1%2C39.9%2C-88.9%2C41.1&kinds=power%2Cnuclear%2Cairport"
        "%2Cport%2Cdam%2Cdatacenter%2Clanding%2Cradiation&limit=1500"]


scenario("search", r"""
  standardRoutes();
  route('/api/infra/search', { items: [JFK], count: 1 });
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  REQUESTS.length = 0;
  var input = byClass(k.ctx.mount, 'infra-search')[0];
  input.value = 'k';
  input.fire('input');
  input.value = 'kjfk';
  input.fire('input');
  await wait(340);
  await settle();
  var list = byClass(k.ctx.mount, 'infra-item');
  var status = text(k, 'infra-status');
  list[0].fire('click');
  await settle();
  return { requests: REQUESTS, n: list.length, first: list[0].textContent, status: status,
           flown: k.ctx.viewer.camera.flown.destination };
""")


def test_search_lists_matches_and_flies_to_them(ran):
    r = ran["search"]
    assert r["requests"] == ["/api/infra/search?q=kjfk&limit=20", "/api/infra/item/airport-KJFK"]
    assert r["n"] == 1
    assert r["first"] == " John F Kennedy IntlLarge airport, KJFK / JFK | US"
    assert r["status"] == '1 matches for "kjfk".'
    assert r["flown"] == {"lon": -97.74, "lat": 30.27, "h": 60000}


scenario("cables_loading", r"""
  route('/api/infra/summary', Object.assign({}, SUMMARY, { available: false,
    cables: { available: false, count: 0, landing_points: 0, loading: true } }));
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var first = REQUESTS.slice();
  var summary = text(k, 'infra-summary');
  window.SDAInfra.setEnabled(false);
  route('/api/infra/summary', Object.assign({}, SUMMARY, { available: false,
    cables: { available: true, count: 0, landing_points: 0 } }));
  route('/api/infra/items', { items: [], count: 0, counts: {}, total: 0, capped: false });
  route('/api/infra/cables', { type: 'FeatureCollection', features: [], available: false,
    loading: true, error: null });
  var k2 = makeCtx(50000);
  window.SDAInfra.init(k2.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var loading = text(k2, 'infra-status');
  window.SDAInfra.setEnabled(false);
  return { first: first, summary: summary, loading: loading,
           afterOff: text(k2, 'infra-status') };
""")


def test_missing_catalog_and_cables_still_fetching(ran):
    r = ran["cables_loading"]
    # nothing imported and no cable cache: only the summary is asked, no 503s
    assert r["first"] == ["/api/infra/summary"]
    assert "python -m sda.infra import" in r["summary"]
    assert r["loading"].startswith("The server is fetching the submarine cable map")
    assert r["afterOff"] == ""


scenario("disable", r"""
  standardRoutes();
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var L = layers(k);
  var before = { marks: L.marks.items.length, lines: L.lines.items.length,
                 count: countEl.textContent };
  window.SDAInfra.setEnabled(false);
  var after = { marks: L.marks.items.length, labels: L.labels.items.length,
                lines: L.lines.items.length, shown: L.marks.show || L.lines.show,
                count: countEl.textContent, view: text(k, 'infra-view'),
                enabled: window.SDAInfra.isEnabled() };
  REQUESTS.length = 0;
  window.SDAInfra.setEnabled(true);
  await settle();
  return { before: before, after: after, again: REQUESTS, marks: L.marks.items.length };
""")


def test_disable_clears_and_enable_redraws(ran):
    r = ran["disable"]
    assert r["before"] == {"marks": 3, "lines": 3, "count": "4"}
    assert r["after"] == {"marks": 0, "labels": 0, "lines": 0, "shown": False, "count": "0",
                          "view": "", "enabled": False}
    # the summary is kept; the view and the cables are asked again
    assert len(r["again"]) == 2 and r["marks"] == 3
    assert r["again"][0].startswith("/api/infra/items?") and r["again"][1] == "/api/infra/cables"


scenario("static", r"""
  standardRoutes();
  var k = makeCtx(50000, { isStatic: true });
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  var input = byClass(k.ctx.mount, 'infra-search')[0];
  input.value = 'kjfk';
  input.fire('input');
  window.SDAInfra.toggleKind('airport');
  await wait(340);
  await settle();
  return { requests: REQUESTS.length, summary: text(k, 'infra-summary'),
           status: text(k, 'infra-status'), enabled: window.SDAInfra.isEnabled() };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == 0 and r["enabled"] is False
    assert r["summary"] == "The infrastructure catalog needs the live server."
    assert r["status"] == "The infrastructure catalog needs the live server."


scenario("no_backend", r"""
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var s404 = text(k, 'infra-summary');
  route('/api/infra/summary', {}, 500);
  var k2 = makeCtx(50000);
  window.SDAInfra.init(k2.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  return { requests: REQUESTS, s404: s404, s500: text(k2, 'infra-summary') };
""")


def test_missing_endpoints_degrade_quietly(ran):
    r = ran["no_backend"]
    assert r["requests"] == ["/api/infra/summary", "/api/infra/summary"]
    assert r["s404"] == "This server has no infrastructure endpoints."
    assert r["s500"] == "The infrastructure catalog did not load."


scenario("init_guards", r"""
  var noMount = window.SDAInfra.init({ viewer: makeViewer(1) });
  var noViewer = window.SDAInfra.init({ mount: new El('div') });
  window.SDAInfra.setEnabled(true);  // nothing initialised: a no-op
  var nothing = [window.SDAInfra.refresh(), window.SDAInfra.open(JFK),
                 window.SDAInfra.isEnabled()];
  var k = makeCtx(2e7);
  var ok = window.SDAInfra.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  var collapsed = k.ctx.mount.children[0].has('collapsed');
  return { noMount: noMount, noViewer: noViewer, nothing: nothing, ok: ok,
           collapsed: collapsed, title: head.textContent,
           style: !!document.getElementById('sda-infra-style'),
           buttons: byClass(k.ctx.mount, 'infra-kind').length };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert r == {"noMount": False, "noViewer": False, "nothing": [None, None, False], "ok": True,
                 "collapsed": True, "title": "Infrastructure", "style": True, "buttons": 9}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert "eval(" not in text and "new Function" not in text
    assert all(ord(ch) < 127 for ch in text), "infra.js must stay ASCII"


scenario("cables_once", r"""
  standardRoutes();
  var held = gate('/api/infra/cables');  // the cable map answers late
  var k = makeCtx(50000);
  window.SDAInfra.init(k.ctx);
  window.SDAInfra.setEnabled(true);
  await settle();
  var inFlight = layers(k).lines.items.length;
  window.SDAInfra.setCables(false);
  window.SDAInfra.setCables(true);
  await settle();
  held.release();
  await settle();
  return { inFlight: inFlight,
           cables: REQUESTS.filter(function (u) { return u === '/api/infra/cables'; }).length,
           lines: layers(k).lines.items.length };
""")


def test_cables_are_fetched_once_while_a_fetch_is_in_flight(ran):
    assert ran["cables_once"] == {"inFlight": 0, "cables": 1, "lines": 3}
