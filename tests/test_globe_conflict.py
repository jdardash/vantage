"""Tests for the globe armed conflict module (static/js/conflict.js).

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
    / "src" / "sda" / "templates" / "static" / "js" / "conflict.js"
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
function boxes(el) {
  return byTag(el, 'INPUT').filter(function (x) { return x.attrs.type === 'checkbox'; });
}
function inputs(el, type) {
  return byTag(el, 'INPUT').filter(function (x) { return x.attrs.type === type; });
}
function selects(el) { return byTag(el, 'SELECT'); }
function text(el, cls) { var hit = byClass(el, cls)[0]; return hit ? hit.textContent : null; }

var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  }
};
var countEl = new El('span'); countEl.id = 'count-conflict'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
function DataSource(name) {
  var list = [];
  this.name = name; this.show = true;
  this.entities = { values: list, add: function (e) { list.push(e); return e; },
    removeAll: function () { list.length = 0; } };
}
function Hierarchy(positions, holes) { this.positions = positions; this.holes = holes || []; }
var Cesium = {
  PointPrimitiveCollection: Coll, CustomDataSource: DataSource, PolygonHierarchy: Hierarchy,
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
  NearFarScalar: function () {}
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

// ---- data ---------------------------------------------------------------------
var TODAY = new Date().toISOString().slice(0, 10);
function ago(days) { return window.SDAConflict.daysBefore(TODAY, days); }

var ATTR = { frontline: 'Front line: (c) DeepStateMap (deepstatemap.live).',
  control: 'Wikipedia contributors, CC BY-SA 4.0',
  events: 'Events: GeoConfirmed (geoconfirmed.org).',
  history: 'History: UCDP GED (CC BY 4.0).', acled: 'Events: ACLED (acleddata.com).' };
var THEATRES = [
  { slug: 'ukraine', name: 'Russia-Ukraine', lat: 48.5, lon: 35.5, view_km: 1400, updated: '' },
  { slug: 'sudan', name: 'Sudan', lat: 14, lon: 29, view_km: 2200, updated: '' }];
var HINT = 'No UCDP history imported. On the server run: python -m sda.conflict import';
var ACLED_HINT = 'ACLED needs an account: set ACLED_EMAIL and ACLED_PASSWORD on the server.';
function summary(opts) {
  opts = opts || {};
  return {
    theatres: THEATRES, attribution: ATTR, types: { 1: 'State-based conflict' },
    history: opts.history ? { available: true, events: 15, deaths: 1666, countries: 4,
      year_min: 1989, year_max: 2026, hint: '' } : { available: false, events: 0, hint: HINT },
    acled_configured: !!opts.acled, acled_hint: opts.acled ? '' : ACLED_HINT
  };
}

var LEGEND = { held_2014: 'Under Russian control since 2014',
  held: 'Under Russian control since 2022', contested: 'Contested or status unknown',
  retaken: 'Returned to Ukrainian control' };
var UKR = {
  theatre: 'ukraine', name: 'Russia-Ukraine', snapshot_id: 1795038105,
  snapshot_at: '2026-11-18T21:41:45Z', line_method: 'derived', stale: false, error: '',
  legend: LEGEND,
  areas: [
    { kind: 'held_2014', note: '', rings: [[[33, 45], [35, 45], [35, 46], [33, 46], [33, 45]]] },
    { kind: 'held', note: '', rings: [[[36, 47], [38, 47], [38, 48], [36, 48], [36, 47]],
      [[36.5, 47.2], [36.7, 47.2], [36.7, 47.4], [36.5, 47.2]]] },
    { kind: 'contested', note: '', rings: [[[37, 48], [37.1, 48], [37.1, 48.1], [37, 48]]] },
    { kind: 'retaken', note: '25.03', rings: [[[31, 50], [32, 50], [32, 51], [31, 50]]] },
    { kind: 'mystery', note: '', rings: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
    { kind: 'held', note: '', rings: [[[1, 1], [2, 2]]] }
  ],
  lines: [[[36, 47], [36, 47.5], [36, 48]], [[38, 47]]]
};
var SUDAN = {
  theatre: 'sudan', name: 'Sudan', revid: 1385747157, revised_at: '2026-11-15T01:33:01Z',
  url: 'https://en.wikipedia.org/w/index.php?title=Module:Sudan&oldid=1385747157',
  history_url: 'https://en.wikipedia.org/w/index.php?title=Module:Sudan&action=history',
  attribution: 'Control of settlements: Wikipedia, CC BY-SA 4.0', error: '',
  legend: [{ party: 'Sudanese Armed Forces', color: 'red', hex: '#d62728', count: 2 },
           { party: 'Rapid Support Forces', color: 'blue', hex: '#1f77b4', count: 3 }],
  marks: [
    { lat: 15.5, lon: 32.5, name: 'Khartoum', kind: 'control', p: [0], wiki: 'Khartoum' },
    { lat: 13.6, lon: 25.3, name: 'El Fasher', kind: 'control', p: [1], wiki: '' },
    { lat: 11.2, lon: 27.8, name: 'Babanusa', kind: 'contested', p: [0, 1], wiki: 'Babanusa' },
    { lat: 12.0, lon: 24.0, name: '', kind: 'presence', p: [1] },
    { lat: 10, lon: 10, name: 'Orphan', kind: 'control', p: [9] },
    { lat: 'x', lon: 10, name: 'Bad', kind: 'control', p: [0] }
  ]
};
function gcList() {
  return { events: [
      { id: 'aaa', date: ago(2), conflict: 'Ukraine', faction: 'Russia', color: '#0051ca',
        media: 'video', lat: 48, lon: 37 },
      { id: 'bbb', date: ago(40), conflict: 'Sahel', faction: 'JNIM', color: '#ff0000',
        media: 'photo', lat: 14, lon: -2 },
      { id: 'ccc', date: ago(85), conflict: 'Ukraine', faction: '', color: '', media: '',
        lat: 47.5, lon: 36 },
      { id: 'ddd', date: ago(1), conflict: 'Ukraine', faction: '', color: '', lat: null, lon: 37 }
    ], count: 4, conflicts: { Ukraine: 3, Sahel: 1 },
    conflict_names: { Ukraine: 'Ukraine', Sahel: 'Africa' }, days: 90,
    fetched_at: '2026-11-19T21:00:00Z', stale: false, error: '', attribution: ATTR.events };
}
var EVENT_AAA = { id: 'aaa', date: '2026-11-17', conflict: 'Ukraine', conflict_name: 'Ukraine',
  faction: 'Russia', media: 'drone video', place: 'Pokrovsk, Donetsk Oblast',
  title: 'Strike on a vehicle', text: 'A vehicle is struck near the station.',
  sources: ['https://x.com/a/status/1', 'javascript:alert(1)', 'https://t.me/chan/22'],
  geolocation: ['https://x.com/b/status/2'], url: 'https://geoconfirmed.org/ukraine/aaa',
  equipment: 'BMP-2', units: '', lat: 48, lon: 37 };

function liveRoutes(opts) {
  route('/api/conflict/summary', summary(opts));
  route('/api/conflict/frontline/ukraine', UKR);
  route('/api/conflict/control/sudan', SUDAN);
  route('/api/conflict/events?', gcList());
  route('/api/conflict/events/aaa', EVENT_AAA);
}

function makeViewer(heightM) {
  var moveEnd = [], prims = [], sources = [];
  var d = Math.PI / 180;
  return {
    scene: { primitives: { add: function (c) { prims.push(c); return c; } },
      globe: { ellipsoid: {} } },
    dataSources: { add: function (s) { sources.push(s); return s; } },
    canvas: { clientWidth: 800, clientHeight: 600 },
    camera: {
      positionCartographic: { height: heightM, latitude: 15 * d, longitude: 30 * d },
      rect: { west: -180 * d, south: -90 * d, east: 180 * d, north: 90 * d },
      computeViewRectangle: function () { return this.rect; },
      moveEnd: { addEventListener: function (fn) { moveEnd.push(fn); } },
      flyTo: function (o) { this.flown = o; }
    },
    _moveEnd: moveEnd, _prims: prims, _sources: sources
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

// Four point collections in order (control, events, history, acled), one data source.
function layers(k) {
  var p = k.ctx.viewer._prims;
  return { control: p[0], events: p[1], history: p[2], acled: p[3],
           ds: k.ctx.viewer._sources[0] };
}
function entities(k) { var ds = layers(k).ds; return ds ? ds.entities.values : []; }
function info(k) {
  var node = byClass(k.ctx.mount, 'conflict-info')[0];
  return {
    title: text(k.ctx.mount, 'conflict-info-title'),
    lines: node.children.slice(1).filter(function (c) { return c.children.length === 0; })
      .map(function (c) { return c.textContent; }),
    links: byTag(node, 'A').map(function (a) { return [a.attrs.href, a.textContent,
      a.attrs.target, a.attrs.rel]; })
  };
}
function legendTexts(k) {
  return byClass(k.ctx.mount, 'conflict-legend')[0].children.map(function (c) {
    return c.textContent; });
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 6; i++) await tick(); }
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
              JSON: JSON, Math: Math, Date: Date, encodeURIComponent: encodeURIComponent };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'conflict.js' });
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
      // stop any debounce timer the scenario left running
      try { vm.runInContext('window.SDAConflict && window.SDAConflict.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("conflict")
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
  var P = window.SDAConflict;
  var d = Math.PI / 180;
  return {
    q: P.query([['bbox', '-180,-90,180,90'], ['b', ''], ['c', null], ['cell', 4]]),
    rect: P.boxFromRect({ west: -98 * d, south: 30 * d, east: -97 * d, north: 31 * d }, 0.1),
    wrap: P.boxFromRect({ west: 170 * d, south: -10 * d, east: -170 * d, north: 10 * d }, 0.1),
    whole: P.boxFromRect({ west: -Math.PI, south: -Math.PI / 2, east: Math.PI,
      north: Math.PI / 2 }, 0.1),
    noRect: P.boxFromRect(null),
    around: P.boxAround(0, 179.5, 111.195),
    safe: ['https://x.com/a', 'http://t.me/b', 'javascript:alert(1)', 'https://a b',
           '//x.com/a', 'https://x.com/"onx', 42].map(P.safeHref),
    hosts: [P.hostOf('https://www.X.com:443/a'), P.hostOf('ftp://x'), P.hostOf(null)],
    flat: P.flatPositions([[1, 2], [3, 'x'], null, [5, 6]]),
    cells: [2e7, 5e6, 3e6, 1.5e6, 1e6, NaN].map(P.historyCell),
    modes: [7e5, 7.1e5, undefined].map(P.historyMode),
    colors: [P.deathColor(0), P.deathColor(5000), P.deathColor(1e9)],
    sizes: [P.densitySize(0), P.densitySize(9), P.densitySize(1e12)],
    years: [P.clampYears(2030, 1980, 1989, 2026), P.clampYears(NaN, NaN, 1989, 2026),
            P.clampYears(2000.4, 2010.6, 1989, 2026)],
    types: [P.typesParam({ 1: true, 2: true, 3: true }), P.typesParam({ 1: true }),
            P.typesParam({ 1: true, 3: true }), P.typesParam({})],
    before: [P.daysBefore('2026-11-19', 90), P.daysBefore('bad', 1)],
    recent: [P.recentEvents([{ id: 1, date: '2026-11-18', conflict: 'Ukraine' },
                             { id: 2, date: '2026-08-01', conflict: 'Ukraine' },
                             { id: 3, date: '2026-11-10', conflict: 'Sahel' }, null], 30,
                            '2026-11-19', '').map(function (e) { return e.id; }),
             P.recentEvents([{ id: 1, date: '2026-11-18', conflict: 'Ukraine' },
                             { id: 3, date: '2026-11-10', conflict: 'Sahel' }], 30,
                            '2026-11-19', 'Sahel').map(function (e) { return e.id; })],
    parties: P.partyLegend([SUDAN, { name: 'Yemen', legend: [
      { party: 'Houthis', hex: '#00dd00', count: 7 }, { party: 'Nobody', hex: '', count: 0 }] },
      null], 2),
    links: P.eventLinks(EVENT_AAA),
    lines: P.eventLines(EVENT_AAA),
    ucdp: P.ucdpLines({ date: '1989-01-01', date_end: '1989-12-31',
      type_name: 'One-sided violence', conflict: 'Government of Sudan - civilians',
      side_a: 'Government of Sudan', side_b: 'Civilians', place: 'Bahr el Ghazal',
      country: 'Sudan', best: 1000, low: 1000, high: 1200, civilian_deaths: 1000,
      source: 'Africa Watch 1990', dataset: 'UCDP GED 26.1' }),
    acled: P.acledLines({ date: '2026-11-10', type: 'Battles', sub_type: 'Armed clash',
      actor1: 'Military Forces of Sudan', actor2: 'Rapid Support Forces', place: 'El Fasher',
      country: 'Sudan', fatalities: 12, text: 'Clashes were reported.',
      source: 'Sudan Tribune' }),
    counts: [999, 1500, 25000, 1.2e6, NaN].map(P.formatCount),
    styles: Object.keys(P.UKRAINE_STYLE), types3: P.UCDP_TYPES.map(function (t) { return t.id; })
  };
""")


def test_query_and_boxes(ran):
    h = ran["helpers"]
    assert h["q"] == "?bbox=-180%2C-90%2C180%2C90&cell=4"
    assert h["rect"] == {"west": -98.1, "south": 29.9, "east": -96.9, "north": 31.1}
    assert h["wrap"] == {"west": 168, "south": -12, "east": -168, "north": 12}
    assert h["whole"] == {"west": -180, "south": -90, "east": 180, "north": 90}
    assert h["noRect"] is None
    assert h["around"] == {"west": 178.5, "south": -1, "east": -179.5, "north": 1}
    assert h["flat"] == [1, 2, 5, 6]


def test_links_are_only_ever_http(ran):
    h = ran["helpers"]
    assert h["safe"] == ["https://x.com/a", "http://t.me/b", "", "", "", "", ""]
    assert h["hosts"] == ["x.com", "", ""]
    assert h["links"] == [
        {"href": "https://x.com/a/status/1", "text": "Source 1 (x.com)"},
        {"href": "https://t.me/chan/22", "text": "Source 2 (t.me)"},
        {"href": "https://x.com/b/status/2", "text": "Geolocation (x.com)"},
        {"href": "https://geoconfirmed.org/ukraine/aaa", "text": "GeoConfirmed"},
    ]


def test_history_scales_and_colours(ran):
    h = ran["helpers"]
    assert h["cells"] == [4, 2, 1, 0.5, 0.25, 4]
    assert h["modes"] == ["detail", "density", "density"]
    assert h["colors"] == ["#ffd666", "#be0016", "#be0016"]
    assert h["sizes"][0] == 0 and h["sizes"][1] == pytest.approx(6.2) and h["sizes"][2] == 30
    assert h["years"] == [{"from": 1989, "to": 2026}, {"from": 1989, "to": 2026},
                          {"from": 2000, "to": 2011}]
    assert h["types"] == ["", "1", "1,3", ""]
    assert h["types3"] == [1, 2, 3]
    assert h["styles"] == ["held_2014", "held", "contested", "retaken"]


def test_event_windows_and_legend(ran):
    h = ran["helpers"]
    assert h["before"] == ["2026-08-21", ""]
    assert h["recent"] == [[1, 3], [3]]
    assert h["parties"] == [
        {"party": "Houthis", "hex": "#00dd00", "count": 7, "theatre": "Yemen"},
        {"party": "Rapid Support Forces", "hex": "#1f77b4", "count": 3, "theatre": "Sudan"},
    ]
    assert h["counts"] == ["999", "1.5k", "25k", "1.2M", "0"]


def test_info_lines(ran):
    h = ran["helpers"]
    assert h["lines"] == [
        "2026-11-17 | Ukraine", "Russia | evidence: drone video", "Pokrovsk, Donetsk Oblast",
        "A vehicle is struck near the station.", "Equipment: BMP-2"]
    assert h["ucdp"] == [
        "1989-01-01 to 1989-12-31 | One-sided violence", "Government of Sudan - civilians",
        "Government of Sudan vs Civilians", "Bahr el Ghazal, Sudan",
        "Deaths, best estimate 1,000 (range 1,000 to 1,200), civilians 1,000",
        "Reported by: Africa Watch 1990", "UCDP GED 26.1"]
    assert h["acled"] == [
        "2026-11-10 | Battles | Armed clash", "Military Forces of Sudan vs Rapid Support Forces",
        "El Fasher, Sudan", "Reported fatalities: 12", "Clashes were reported.",
        "Sources: Sudan Tribune"]


# --- enable: front lines, control and verified events -----------------------------------

scenario("enable", r"""
  liveRoutes();
  var k = makeCtx(2e7);
  var ok = window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  var L = layers(k);
  var ents = entities(k);
  var out = {
    ok: ok, enabled: window.SDAConflict.isEnabled(), requests: REQUESTS.slice(),
    count: countEl.textContent,
    entities: ents.map(function (e) {
      return { name: e.name, type: e.properties.type, rows: e.properties.detail_rows,
        poly: !!e.polygon,
        holes: e.polygon ? e.polygon.hierarchy.holes.length : null,
        fill: e.polygon ? [e.polygon.material.css, e.polygon.material.a] : null,
        line: e.polyline ? [e.polyline.positions.length, e.polyline.width,
          e.polyline.clampToGround, e.polyline.material.css] : null };
    }),
    control: L.control.items.map(function (p) { return [p.id.type, p.color.css, p.pixelSize,
      p.outlineColor.css]; }),
    events: L.events.items.map(function (p) { return [p.id.id, p.color.css]; }),
    history: L.history.items.length, acled: L.acled.items.length,
    front: text(k.ctx.mount, 'conflict-view'),
    legend: legendTexts(k),
    eventsInfo: byClass(k.ctx.mount, 'conflict-view')[1].textContent,
    conflicts: byTag(selects(k.ctx.mount)[2], 'OPTION').map(function (o) {
      return [o.value, o.textContent]; }),
    theatres: byTag(selects(k.ctx.mount)[0], 'OPTION').map(function (o) {
      return [o.value, o.textContent]; }),
    summary: text(k.ctx.mount, 'conflict-summary'),
    attr: text(k.ctx.mount, 'conflict-attr'),
    acledInfo: byClass(k.ctx.mount, 'conflict-view')[3].textContent,
    historyInfo: byClass(k.ctx.mount, 'conflict-view')[2].textContent,
    status: text(k.ctx.mount, 'conflict-status'),
    feed: k.feed
  };
  // picks: a verified event (detail fetched), then a control mark
  await L.events.items[0].id.sdaPick();
  await settle();
  out.eventInfo = info(k);
  L.control.items[2].id.sdaPick();
  out.markInfo = info(k);
  L.control.items[0].id.sdaPick();
  out.markInfo0 = info(k);
  // narrower windows
  var days = selects(k.ctx.mount)[1];
  days.value = '30'; days.fire('change');
  out.days30 = L.events.items.map(function (p) { return p.id.id; });
  days.value = '90'; days.fire('change');
  var conflictSel = selects(k.ctx.mount)[2];
  conflictSel.value = 'Sahel'; conflictSel.fire('change');
  out.sahel = L.events.items.map(function (p) { return p.id.id; });
  out.sahelInfo = byClass(k.ctx.mount, 'conflict-view')[1].textContent;
  // switching events off hides them and drops them from the count
  var evBox = boxes(k.ctx.mount)[1];
  evBox.checked = false; evBox.fire('change');
  out.eventsOff = [L.events.items.length, countEl.textContent,
    byClass(k.ctx.mount, 'conflict-view')[1].textContent];
  // disable clears everything
  window.SDAConflict.setEnabled(false);
  out.after = { count: countEl.textContent, entities: entities(k).length,
    control: L.control.items.length, shown: [L.control.show, L.events.show, L.ds.show],
    info: byClass(k.ctx.mount, 'conflict-info')[0].children.length,
    enabled: window.SDAConflict.isEnabled() };
  return out;
""")


def test_enable_loads_every_source_once(ran):
    r = ran["enable"]
    assert r["ok"] is True and r["enabled"] is True
    assert sorted(r["requests"]) == [
        "/api/conflict/control/sudan", "/api/conflict/events?days=90",
        "/api/conflict/frontline/ukraine", "/api/conflict/summary"]
    assert r["requests"][0] == "/api/conflict/summary"
    assert r["history"] == 0 and r["acled"] == 0  # both start off
    assert r["count"] == "11"  # 4 areas + 4 marks + 3 events
    assert r["status"] == ""


def test_ukraine_areas_and_line_of_contact(ran):
    ents = ran["enable"]["entities"]
    polys = [e for e in ents if e["poly"]]
    lines = [e for e in ents if e["line"]]
    assert len(polys) == 4 and len(lines) == 1  # bad kinds and short rings are skipped
    assert polys[0]["name"] == "Under Russian control since 2014 (DeepStateMap, 2026-11-18)"
    assert polys[1]["holes"] == 1 and polys[1]["fill"] == ["#a52714", 0.34]
    assert polys[3]["name"] == "Returned to Ukrainian control (DeepStateMap, 2026-11-18, 25.03)"
    assert {e["type"] for e in polys} == {"conflict area"}
    assert lines[0]["name"] == "Line of contact (derived from DeepStateMap, 2026-11-18)"
    assert lines[0]["type"] == "line of contact"
    assert lines[0]["line"] == [3, 3, True, "#ff3b1f"]
    # a click on an area or the line says what it is, not just its name
    assert polys[0]["rows"] == [["CONTROL", "Under Russian control since 2014"],
                                ["AS OF", "2026-11-18"],
                                ["SOURCE", "DeepStateMap, volunteer analysts, redrawn daily"]]
    assert ["NOTE", "25.03"] in polys[3]["rows"]
    assert lines[0]["rows"][0] == ["MEANING", "Edge of the ground held by Russian forces"]
    assert lines[0]["rows"][1] == ["AS OF", "2026-11-18"]


def test_control_marks_and_legend(ran):
    r = ran["enable"]
    assert r["control"] == [
        ["conflict-control", "#d62728", 7, "#000"],
        ["conflict-control", "#1f77b4", 7, "#000"],
        ["conflict-control", "#d62728", 8, "#1f77b4"],  # contested: ring of the other party
        ["conflict-control", "#1f77b4", 4, "#000"],
    ]
    assert r["front"] == ("Russia-Ukraine as of 2026-11-18 (DeepStateMap). "
                          "Sudan: 4 places, Wikipedia revision of 2026-11-15.")
    assert r["legend"] == [
        "Under Russian control since 2014", "Under Russian control since 2022",
        "Contested or status unknown", "Returned to Ukrainian control",
        "Line of contact (derived)", "Rapid Support Forces (Sudan)",
        "Sudanese Armed Forces (Sudan)"]
    assert r["theatres"] == [["all", "All theatres (2)"], ["ukraine", "Russia-Ukraine"],
                             ["sudan", "Sudan"]]


def test_verified_events_and_filters(ran):
    r = ran["enable"]
    assert r["events"] == [["aaa", "#0051ca"], ["bbb", "#ff0000"], ["ccc", "#999999"]]
    assert r["eventsInfo"] == ("3 events in the last 90 days, all conflicts, fetched "
                               "2026-11-19 21:00 UTC.")
    assert r["conflicts"] == [["", "All conflicts"], ["Ukraine", "Ukraine (3)"],
                              ["Sahel", "Africa (1)"]]
    assert r["days30"] == ["aaa"]
    assert r["sahel"] == ["bbb"]
    assert r["sahelInfo"].startswith("1 events in the last 90 days, Africa")
    assert r["eventsOff"] == [0, "8", ""]


def test_panel_text_and_attribution(ran):
    r = ran["enable"]
    assert r["summary"].startswith("2 front-line theatres and verified events for 90 days.")
    assert "python -m sda.conflict import" in r["summary"]
    assert "DeepStateMap" in r["attr"] and "GeoConfirmed" in r["attr"]
    assert "1 detailed-map modules (CC BY-SA 4.0)" in r["attr"]
    assert "UCDP" not in r["attr"] and "ACLED" not in r["attr"]  # those layers are off
    assert r["acledInfo"].startswith("ACLED needs an account")
    assert r["historyInfo"] == ""
    assert "Conflict layer: 2 theatres" in r["feed"]
    assert any(f.startswith("Conflict: Russia-Ukraine map of 2026-11-18") for f in r["feed"])


def test_event_pick_shows_sources(ran):
    i = ran["enable"]["eventInfo"]
    assert i["title"] == "Strike on a vehicle"
    assert i["lines"][0] == "2026-11-17 | Ukraine"
    assert [link[:2] for link in i["links"]] == [
        ["https://x.com/a/status/1", "Source 1 (x.com)"],
        ["https://t.me/chan/22", "Source 2 (t.me)"],
        ["https://x.com/b/status/2", "Geolocation (x.com)"],
        ["https://geoconfirmed.org/ukraine/aaa", "GeoConfirmed"],
    ]
    assert all(link[2] == "_blank" and "noopener" in link[3] for link in i["links"])


def test_control_pick_links_the_revision(ran):
    r = ran["enable"]
    assert r["markInfo"]["title"] == "Babanusa"
    assert r["markInfo"]["lines"] == [
        "contested between Sudanese Armed Forces and Rapid Support Forces",
        "Sudan, per Wikipedia revision of 2026-11-15"]
    assert [link[1] for link in r["markInfo"]["links"]] == [
        "Place on Wikipedia", "Map module revision", "Edit history"]
    assert r["markInfo"]["links"][0][0] == "https://en.wikipedia.org/wiki/Babanusa"
    assert r["markInfo"]["links"][1][0].endswith("oldid=1385747157")
    assert r["markInfo0"]["lines"][0] == "held by Sudanese Armed Forces"


def test_disable_clears_everything(ran):
    a = ran["enable"]["after"]
    assert a == {"count": "0", "entities": 0, "control": 0, "shown": [False, False, False],
                 "info": 0, "enabled": False}


# --- theatre select and date picker ----------------------------------------------------

scenario("theatre_and_date", r"""
  liveRoutes();
  route('/api/conflict/frontline/ukraine?date=2023-06-01',
        Object.assign({}, UKR, { snapshot_at: '2023-06-01T19:00:00Z', lines: [],
          line_method: 'outline', areas: [UKR.areas[1]] }));
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  var L = layers(k);
  var sel = selects(k.ctx.mount)[0];
  sel.value = 'sudan'; sel.fire('change');
  await settle();
  var sudan = { flown: k.ctx.viewer.camera.flown.destination, entities: entities(k).length,
    control: L.control.items.length, legend: legendTexts(k),
    front: text(k.ctx.mount, 'conflict-view'),
    controlRequests: REQUESTS.filter(function (u) { return u.indexOf('/control/') > 0; }).length };
  // a date moves the view back to Russia-Ukraine and loads that day's map
  var date = inputs(k.ctx.mount, 'date')[0];
  date.value = '2023-06-01'; date.fire('change');
  await settle();
  var latest = buttonNamed(k.ctx.mount, 'LATEST');
  var past = { theatre: sel.value, latestClass: latest.className,
    entities: entities(k).map(function (e) { return [e.name, !!e.polyline]; }),
    control: L.control.items.length, front: text(k.ctx.mount, 'conflict-view') };
  // a malformed date is ignored
  var before = REQUESTS.length;
  date.value = '06/01/2023'; date.fire('change');
  await settle();
  var ignored = REQUESTS.length - before;
  latest.fire('click');
  await settle();
  return { sudan: sudan, past: past, ignored: ignored, requests: REQUESTS,
           latest: { value: date.value, cls: latest.className,
             names: entities(k).map(function (e) { return e.name; }).slice(0, 1) },
           minDate: date.attrs.min };
""")


def test_one_theatre_flies_there_and_hides_the_others(ran):
    s = ran["theatre_and_date"]["sudan"]
    assert s["flown"] == {"lon": 29, "lat": 14, "h": 2200000}
    assert s["entities"] == 0 and s["control"] == 4
    assert s["legend"] == ["Rapid Support Forces", "Sudanese Armed Forces"]
    assert s["front"] == "Sudan: 4 places, Wikipedia revision of 2026-11-15."
    assert s["controlRequests"] == 1  # the loaded module is reused


def test_date_picker_loads_that_day(ran):
    r = ran["theatre_and_date"]
    p = r["past"]
    assert p["theatre"] == "ukraine" and p["latestClass"] == "mode-btn"
    # outline fallback: the held area's own edge is drawn as the line
    assert p["entities"] == [
        ["Under Russian control since 2022 (DeepStateMap, 2023-06-01)", False],
        ["Line of contact (derived from DeepStateMap, 2023-06-01)", True]]
    assert p["control"] == 0
    assert p["front"].startswith("Russia-Ukraine as of 2023-06-01")
    assert r["ignored"] == 0
    assert "/api/conflict/frontline/ukraine?date=2023-06-01" in r["requests"]
    assert r["requests"][-1] == "/api/conflict/frontline/ukraine"
    assert r["latest"] == {
        "value": "", "cls": "mode-btn active",
        "names": ["Under Russian control since 2014 (DeepStateMap, 2026-11-18)"]}
    assert r["minDate"] == "2022-04-03"


# --- history ---------------------------------------------------------------------------

scenario("history", r"""
  liveRoutes({ history: true });
  route('/api/conflict/history/density', { cell_deg: 4, events: 15, deaths: 1666,
    year_from: 1989, year_to: 2026, attribution: ATTR.history, cells: [
      { lat: 12, lon: 30, events: 9, deaths: 1500 }, { lat: 34, lon: 69, events: 6,
        deaths: 166 }] });
  route('/api/conflict/history/events', { count: 2, total: 2, total_capped: false,
    capped: false, attribution: ATTR.history, events: [
      { id: 7615, type: 3, type_name: 'One-sided violence', date: '1989-01-01',
        date_end: '1989-12-31', dyad: 'Government of Sudan - civilians',
        conflict: 'Government of Sudan - civilians', side_a: 'Government of Sudan',
        side_b: 'Civilians', place: 'Bahr el Ghazal', country: 'Sudan', lat: 8.5, lon: 28,
        best: 1000, low: 1000, high: 1000, civilian_deaths: 1000, source: 'Africa Watch',
        dataset: 'UCDP GED 26.1', url: 'https://ucdp.uu.se/onesided/480' },
      { id: 11, type: 1, type_name: 'State-based conflict', date: '1990-03-01',
        date_end: '1990-03-01', dyad: '', conflict: 'Sudan: Government', side_a: 'A',
        side_b: 'B', place: '', country: 'Sudan', lat: 15, lon: 32, best: 3, low: 3,
        high: 3, civilian_deaths: 0, source: '', dataset: 'UCDP GED 26.1',
        url: 'javascript:alert(1)' }] });
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  // the history box turns the whole layer on
  var hBox = boxes(k.ctx.mount)[2];
  hBox.checked = true; hBox.fire('change');
  await settle();
  var L = layers(k);
  var hInfo = function () { return byClass(k.ctx.mount, 'conflict-view')[2].textContent; };
  var out = { enabled: window.SDAConflict.isEnabled(),
    density: REQUESTS.filter(function (u) { return u.indexOf('/history/') > 0; }),
    cells: L.history.items.map(function (p) { return [p.id.type, p.color.a]; }),
    info: hInfo(), count: countEl.textContent,
    years: [inputs(k.ctx.mount, 'number')[0].value, inputs(k.ctx.mount, 'number')[1].value],
    summary: text(k.ctx.mount, 'conflict-summary'), attr: text(k.ctx.mount, 'conflict-attr') };
  L.history.items[0].id.sdaPick();
  out.cellInfo = info(k);
  out.cellFly = k.ctx.viewer.camera.flown.destination;
  // zoom in over Sudan: single events after the debounce
  var d = Math.PI / 180;
  var cam = k.ctx.viewer.camera;
  cam.positionCartographic.height = 5e5;
  cam.rect = { west: 20 * d, south: 5 * d, east: 35 * d, north: 20 * d };
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await wait(450);
  await settle();
  out.detail = REQUESTS.filter(function (u) { return u.indexOf('/history/events') > 0; });
  out.points = L.history.items.map(function (p) { return [p.id.id, p.color.css]; });
  out.detailInfo = hInfo();
  // the same view again asks for nothing
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await wait(450);
  await settle();
  out.sameView = REQUESTS.filter(function (u) { return u.indexOf('/history/') > 0; }).length;
  L.history.items[0].id.sdaPick();
  out.eventInfo = info(k);
  L.history.items[1].id.sdaPick();
  out.badLink = info(k).links.length;
  // type buttons and years
  var state = buttonNamed(k.ctx.mount, 'STATE');
  state.fire('click');
  await settle();
  out.stateOff = [state.attrs['aria-pressed'], state.className, REQUESTS[REQUESTS.length - 1]];
  var from = inputs(k.ctx.mount, 'number')[0];
  from.value = '2030'; from.fire('change');
  await settle();
  out.yearsLater = [from.value, inputs(k.ctx.mount, 'number')[1].value,
    REQUESTS[REQUESTS.length - 1]];
  // turning every type off keeps the last one on
  buttonNamed(k.ctx.mount, 'NON-STATE').fire('click');
  buttonNamed(k.ctx.mount, 'ONE-SIDED').fire('click');
  out.lastType = ['STATE', 'NON-STATE', 'ONE-SIDED'].map(function (n) {
    return buttonNamed(k.ctx.mount, n).attrs['aria-pressed']; });
  return out;
""")


def test_history_density_from_the_panel(ran):
    r = ran["history"]
    assert r["enabled"] is True
    assert r["density"] == [
        "/api/conflict/history/density?bbox=-180%2C-90%2C180%2C90&cell=4&year_from=1989"
        "&year_to=2026"]
    assert r["cells"] == [["conflict-history-cell", 0.6], ["conflict-history-cell", 0.6]]
    assert r["info"] == ("15 events, 1,666 deaths in view, 1989 to 2026 (4 degree cells). "
                         "Zoom in for single events.")
    assert r["count"] == "26"  # 4 areas + 4 marks + 3 events + 15 history events
    assert r["years"] == ["1989", "2026"]
    assert "15 UCDP events 1989 to 2026" in r["summary"]
    assert "UCDP" in r["attr"]
    assert r["cellInfo"]["title"] == "UCDP events here, 1989 to 2026"
    assert r["cellInfo"]["lines"] == [
        "9 events, 1,500 deaths (best estimate). Zoom in for single events."]
    assert r["cellFly"] == {"lon": 30, "lat": 12, "h": 560000}


def test_history_single_events_when_close(ran):
    r = ran["history"]
    assert r["detail"] == [
        "/api/conflict/history/events?bbox=18.5%2C3.5%2C36.5%2C21.5&year_from=1989"
        "&year_to=2026&limit=800"]
    assert r["points"] == [[7615, "#c2189b"], [11, "#e4572e"]]
    assert r["detailInfo"] == "2 events in view."
    assert r["sameView"] == 2
    assert r["eventInfo"]["title"] == "Government of Sudan - civilians"
    assert r["eventInfo"]["links"][0][:2] == ["https://ucdp.uu.se/onesided/480",
                                              "UCDP conflict page"]
    assert r["badLink"] == 0


def test_history_filters(ran):
    r = ran["history"]
    assert r["stateOff"][0] == "false" and r["stateOff"][1] == "mode-btn"
    assert r["stateOff"][2].endswith("&types=2%2C3&limit=800")
    assert r["yearsLater"][:2] == ["2026", "2026"]
    assert "year_from=2026&year_to=2026" in r["yearsLater"][2]
    assert r["lastType"] == ["false", "false", "true"]


scenario("history_missing", r"""
  liveRoutes();
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  var hBox = boxes(k.ctx.mount)[2];
  hBox.checked = true; hBox.fire('change');
  await settle();
  return { info: byClass(k.ctx.mount, 'conflict-view')[2].textContent,
    asked: REQUESTS.filter(function (u) { return u.indexOf('/history/') > 0; }) };
""")


def test_history_not_imported_says_how(ran):
    r = ran["history_missing"]
    assert r["info"] == ("No UCDP history imported. On the server run: "
                         "python -m sda.conflict import")
    assert r["asked"] == []  # no 503s


# --- ACLED -----------------------------------------------------------------------------

scenario("acled", r"""
  liveRoutes({ acled: true });
  route('/api/conflict/acled?', { configured: true, error: '', days: 30, count: 2,
    attribution: ATTR.acled, events: [
      { id: 'SDN1234', date: '2026-11-10', type: 'Battles', lat: 13.6, lon: 25.3,
        fatalities: 12 },
      { id: 'SDN99', date: '2026-11-11', type: 'Something new', lat: 13, lon: 25,
        fatalities: 0 }] });
  route('/api/conflict/acled/SDN1234', { id: 'SDN1234', date: '2026-11-10', type: 'Battles',
    sub_type: 'Armed clash', actor1: 'Military Forces of Sudan',
    actor2: 'Rapid Support Forces', place: 'El Fasher', country: 'Sudan', fatalities: 12,
    text: 'Clashes were reported.', source: 'Sudan Tribune', url: '' });
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  var before = byClass(k.ctx.mount, 'conflict-view')[3].textContent;
  var aBox = boxes(k.ctx.mount)[3];
  aBox.checked = true; aBox.fire('change');
  await settle();
  var L = layers(k);
  var pts = L.acled.items.map(function (p) { return [p.id.id, p.color.css]; });
  L.acled.items[0].id.sdaPick();
  await settle();
  return { before: before, points: pts,
    infoText: byClass(k.ctx.mount, 'conflict-view')[3].textContent, pick: info(k),
    attr: text(k.ctx.mount, 'conflict-attr'), count: countEl.textContent,
    asked: REQUESTS.filter(function (u) { return u.indexOf('/acled') > 0; }) };
""")


def test_acled_when_configured(ran):
    r = ran["acled"]
    assert r["before"] == ""
    assert r["asked"] == ["/api/conflict/acled?days=30", "/api/conflict/acled/SDN1234"]
    assert r["points"] == [["SDN1234", "#e4572e"], ["SDN99", "#b0b0b0"]]
    assert r["infoText"] == "2 ACLED events in the last 30 days."
    assert r["pick"]["title"] == "ACLED SDN1234"
    assert "Military Forces of Sudan vs Rapid Support Forces" in r["pick"]["lines"]
    assert r["pick"]["links"] == []
    assert "ACLED" in r["attr"]
    assert r["count"] == "13"


scenario("acled_unconfigured", r"""
  liveRoutes();
  route('/api/conflict/acled?', { configured: false, error: ACLED_HINT, events: [],
    count: 0, days: 30 });
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  var aBox = boxes(k.ctx.mount)[3];
  aBox.checked = true; aBox.fire('change');
  await settle();
  return { info: byClass(k.ctx.mount, 'conflict-view')[3].textContent,
    points: layers(k).acled.items.length, attr: text(k.ctx.mount, 'conflict-attr') };
""")


def test_acled_unconfigured_names_the_keys(ran):
    r = ran["acled_unconfigured"]
    assert r["info"] == ("ACLED needs an account: set ACLED_EMAIL and ACLED_PASSWORD "
                         "on the server.")
    assert r["points"] == 0
    assert "acleddata" not in r["attr"]


# --- failures, static build, page wiring ------------------------------------------------

scenario("upstream_errors", r"""
  route('/api/conflict/summary', summary());
  route('/api/conflict/frontline/ukraine', { error: 'DeepStateMap unavailable', areas: [],
    lines: [], legend: LEGEND, snapshot_at: '', line_method: 'none' });
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  return { front: text(k.ctx.mount, 'conflict-view'),
    events: byClass(k.ctx.mount, 'conflict-view')[1].textContent,
    status: text(k.ctx.mount, 'conflict-status'), entities: entities(k).length,
    count: countEl.textContent };
""")


def test_upstream_errors_are_reported_not_thrown(ran):
    r = ran["upstream_errors"]
    assert r["front"] == "Russia-Ukraine: DeepStateMap unavailable."
    assert r["events"] == "Verified events did not load."
    assert r["status"] in ("Russia-Ukraine: DeepStateMap unavailable.",
                           "Control map for sudan did not load.")
    assert r["entities"] == 0 and r["count"] == "0"


scenario("no_server", r"""
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  await settle();
  return { requests: REQUESTS, status: text(k.ctx.mount, 'conflict-status'),
    enabled: window.SDAConflict.isEnabled() };
""")


def test_a_server_without_the_layer_degrades_quietly(ran):
    r = ran["no_server"]
    assert r["requests"] == ["/api/conflict/summary"]
    assert r["status"] == "The conflict layer is not available on this server."
    assert r["enabled"] is True


scenario("static", r"""
  var k = makeCtx(2e7, { isStatic: true });
  window.SDAConflict.init(k.ctx);
  window.SDAConflict.setEnabled(true);
  var hBox = boxes(k.ctx.mount)[2];
  hBox.checked = true; hBox.fire('change');
  var sel = selects(k.ctx.mount)[0];
  sel.value = 'ukraine'; sel.fire('change');
  await window.SDAConflict.refresh();
  await settle();
  return { requests: REQUESTS.length, summary: text(k.ctx.mount, 'conflict-summary'),
    enabled: window.SDAConflict.isEnabled() };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == 0
    assert r["summary"] == "The conflict layer needs the live server."
    assert r["enabled"] is False


scenario("panel_turns_layer_on", r"""
  liveRoutes();
  var CALLS = [];
  var box = new El('input'); box.id = 'layer-conflict'; document.body.appendChild(box);
  window.toggleLayer = function (name, on) { CALLS.push([name, on]);
    window.SDAConflict.setEnabled(on); };
  var k = makeCtx(2e7);
  window.SDAConflict.init(k.ctx);
  var sel = selects(k.ctx.mount)[0];
  sel.value = 'sudan'; sel.fire('change');
  await settle();
  return { calls: CALLS, checked: box.checked, enabled: window.SDAConflict.isEnabled(),
    requests: REQUESTS.slice().sort() };
""")


def test_panel_controls_turn_the_page_layer_on(ran):
    r = ran["panel_turns_layer_on"]
    assert r["calls"] == [["conflict", True]] and r["checked"] is True
    assert r["enabled"] is True
    # only the chosen theatre loads
    assert r["requests"] == ["/api/conflict/control/sudan", "/api/conflict/events?days=90",
                             "/api/conflict/summary"]


scenario("init_guards", r"""
  var noMount = window.SDAConflict.init({ viewer: makeViewer(1) });
  var noViewer = window.SDAConflict.init({ mount: new El('div') });
  window.SDAConflict.setEnabled(true);  // nothing initialised: a no-op
  var noop = window.SDAConflict.isEnabled();
  await window.SDAConflict.refresh();
  var k = makeCtx(2e7);
  var ok = window.SDAConflict.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  var collapsed = k.ctx.mount.children[0].has('collapsed');
  return { noMount: noMount, noViewer: noViewer, noop: noop, ok: ok, collapsed: collapsed,
           title: head.textContent, style: !!document.getElementById('sda-conflict-style'),
           boxes: boxes(k.ctx.mount).map(function (b) { return !!b.checked; }),
           requests: REQUESTS.length };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert r == {"noMount": False, "noViewer": False, "noop": False, "ok": True,
                 "collapsed": True, "title": "Armed conflict", "style": True,
                 "boxes": [True, True, False, False], "requests": 0}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert "eval(" not in text
    assert all(ord(ch) < 127 for ch in text), "conflict.js must stay ASCII"
