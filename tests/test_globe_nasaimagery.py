"""Tests for the globe NASA imagery and events module (static/js/nasaimagery.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_aurora.py does it. The
fake `fetch` answers from a table and `setInterval` only records its callback, so
nothing here touches the network or waits thirty minutes. GIBS tiles are requested
by Cesium's imagery provider, never by `fetch`, so the fake provider only records the
URL template it was given. Skips cleanly when Node is not installed.

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
    / "src" / "sda" / "templates" / "static" / "js" / "nasaimagery.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.selected = false; this.checked = false; this.type = '';
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
// a checkbox click flips it and fires change, as a browser does
El.prototype.click = function () {
  if (this.type === 'checkbox') this.checked = !this.checked;
  this.fire('click'); this.fire('change');
};
function all(el, out) { out = out || []; out.push(el);
  el.children.forEach(function (c) { all(c, out); }); return out; }
function byClass(el, cls) { return all(el).filter(function (x) {
  return x.className.split(/\s+/).indexOf(cls) >= 0; }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }
function one(el, cls) { return byClass(el, cls)[0]; }

var DOC_LISTENERS = {};
var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) {
      return x.id === id; });
    return hit[0] || null;
  },
  addEventListener: function (t, fn) { (DOC_LISTENERS[t] = DOC_LISTENERS[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    var l = DOC_LISTENERS[t] || []; var i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
  fire: function (type, ev) { ev = ev || {}; ev.type = type;
    (DOC_LISTENERS[type] || []).slice().forEach(function (fn) { fn(ev); }); return ev; }
};
var countEl = new El('span'); countEl.id = 'count-eonet'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
var PROVIDERS = [];
function Opts(o) { for (var k in o) this[k] = o[k]; }
function UrlTemplateImageryProvider(o) { Opts.call(this, o); PROVIDERS.push(this); }
function ImageryLayer(provider, o) { this.imageryProvider = provider; this.alpha = 1;
  this.splitDirection = 0; this.destroyed = false; this.options = o || null; }
function Credit(text) { this.text = text; }
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Layers() { this.list = []; }
Layers.prototype.add = function (layer, index) {
  if (index === undefined) { this.list.push(layer); return; }
  if (index < 0 || index > this.list.length) throw new Error('index out of range');
  this.list.splice(index, 0, layer);
};
Layers.prototype.remove = function (layer, destroy) {
  var i = this.list.indexOf(layer); if (i < 0) return false;
  this.list.splice(i, 1); if (destroy) layer.destroyed = true; return true; };
Layers.prototype.contains = function (layer) { return this.list.indexOf(layer) >= 0; };
Layers.prototype.indexOf = function (layer) { return this.list.indexOf(layer); };
function CEvent() { this.listeners = []; }
CEvent.prototype.addEventListener = function (fn) { var self = this; this.listeners.push(fn);
  return function () { var i = self.listeners.indexOf(fn);
    if (i >= 0) self.listeners.splice(i, 1); }; };
CEvent.prototype.raise = function () { this.listeners.slice().forEach(function (fn) { fn(); }); };
var Cesium = {
  UrlTemplateImageryProvider: UrlTemplateImageryProvider,
  ImageryLayer: ImageryLayer,
  Credit: Credit,
  SplitDirection: { LEFT: -1, NONE: 0, RIGHT: 1 },
  PointPrimitiveCollection: Coll,
  Cartesian3: { fromDegrees: function (lon, lat, h) { return [lon, lat, h]; } },
  Color: {
    fromCssColorString: function (s) { return { css: s }; },
    BLACK: { withAlpha: function (a) { return { css: '#000000', alpha: a }; } }
  }
};
var window = this;
this.Cesium = Cesium;

// ---- timers: record the refresh interval instead of waiting for it -------
var INTERVALS = [];
function setInterval(fn, ms) { INTERVALS.push({ fn: fn, ms: ms, live: true });
  return INTERVALS.length; }
function clearInterval(id) { if (INTERVALS[id - 1]) INTERVALS[id - 1].live = false; }
function live() { return INTERVALS.filter(function (t) { return t.live; }); }

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
        json: function () {
          if (r.body === '__THROW__') return Promise.reject(new Error('bad json'));
          return Promise.resolve(JSON.parse(JSON.stringify(r.body))); } });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () {
    return Promise.resolve({}); } });
}

var DAY = 86400000;
var SIM = { t: Date.now() };
function sim(iso) { SIM.t = Date.parse(iso); }

var EVENTS = {
  events: [
    { id: 'EONET_26494', title: 'Tropical Cyclone Akvan',
      categories: [{ id: 'severeStorms', title: 'Severe Storms' }], point: [85.3, 16],
      geometry_type: 'Point', date: '2026-11-19T12:00:00Z', magnitude_value: 45,
      magnitude_unit: 'kts', observations: 12, closed: null,
      sources: [{ id: 'JTWC', url: 'https://www.metoc.navy.mil/jtwc/products/io0526.tcw' }] },
    { id: 'EONET_26500', title: 'Ridge Fire, Plumas, California',
      categories: [{ id: 'wildfires', title: 'Wildfires' }], point: [-120.9, 39.9],
      geometry_type: 'Point', date: '2026-11-18T00:00:00Z', magnitude_value: 2378.77,
      magnitude_unit: 'acres', observations: 1,
      sources: [{ id: 'IRWIN', url: 'https://irwin.doi.gov/observer/incidents/abc' },
                { id: 'InciWeb', url: 'http://inciweb.example/x' }] },
    { id: 'EONET_26510', title: 'Iceberg D35',
      categories: [{ id: 'seaLakeIce', title: 'Sea and Lake Ice' }], point: [-39, -59],
      geometry_type: 'Polygon', date: '2026-11-15T00:00:00Z', magnitude_value: null,
      magnitude_unit: null, observations: 1, sources: [] },
    { id: 'EONET_26520', title: 'Bush fire', categories: [{ id: 'wildfires',
      title: 'Wildfires' }], point: [140, -30], date: '2026-11-17', sources: [] }
  ],
  count: 4, days: 30, status: 'open', source: 'NASA EONET v3', stale: false
};

function makeViewer() {
  var added = [];
  var container = new El('div');
  container.getBoundingClientRect = function () { return { left: 100, width: 1000 }; };
  var flights = [];
  var base = new ImageryLayer({ url: 'basemap' });
  var layers = new Layers();
  layers.add(base);
  return {
    scene: { primitives: {
      add: function (c) { added.push(c); return c; },
      remove: function (c) { var i = added.indexOf(c); if (i >= 0) added.splice(i, 1); } },
      splitPosition: 0 },
    imageryLayers: layers, container: container,
    clock: { onTick: new CEvent() },
    camera: { flyTo: function (o) { flights.push(o); } },
    _added: added, _flights: flights, _base: base
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(), api: opts.api || '', isStatic: !!opts.isStatic,
    mount: opts.noMount ? undefined : new El('div'),
    addIntelItem: function (t) { feed.push(t); },
    simNow: function () { return new Date(SIM.t); }
  };
  return { ctx: ctx, feed: feed };
}

var N = function () { return window.SDANasaImagery; };
function urls() { return PROVIDERS.map(function (p) { return p.url; }); }
function dayOf(url) { var m = /\/default\/(\d{4}-\d{2}-\d{2})\//.exec(url); return m && m[1]; }
function stack(k) { return k.ctx.viewer.imageryLayers.list.map(function (l) {
  return l === k.ctx.viewer._base ? 'base' : dayOf(l.imageryProvider.url); }); }
function ours(k) { return k.ctx.viewer.imageryLayers.list.filter(function (l) {
  return l !== k.ctx.viewer._base; }); }
function points(k) { var c = k.ctx.viewer._added[0]; return c ? c.items : []; }
function toggle(k, cls, on) { var b = one(k.ctx.mount, cls); b.checked = on; b.fire('change'); }
function yesterday() { return N().addDays(N().isoDay(Date.now()), -1); }

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 6; i++) await tick(); }
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              Promise: Promise, JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'nasaimagery.js' });
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
    work = tmp_path_factory.mktemp("nasaimagery")
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


# --- pure helpers: days and the date the layer shows ------------------------------------

scenario("dates", r"""
  var A = N();
  var terra = A.LAYERS[0];
  var night = A.LAYERS.filter(function (l) { return l.id === 'viirs_night'; })[0];
  var wall = Date.UTC(2026, 10, 19, 15, 0);  // 2026-11-19 15:00 UTC
  function p(simMs, layer) { var r = A.pickDate(new Date(simMs), wall, layer || terra);
    return [r.day, r.reason]; }
  return {
    live: p(wall + 60000),
    liveEdge: p(wall - A.LIVE_WINDOW_MS),
    todayScrubbed: p(wall - A.LIVE_WINDOW_MS - 60000),
    scrubbed: p(Date.UTC(2026, 10, 10, 3, 0)),
    dayStart: p(Date.UTC(2026, 10, 10, 0, 0)),
    dayEnd: p(Date.UTC(2026, 10, 10, 23, 59, 59)),
    future: p(Date.UTC(2026, 10, 25, 0, 0)),
    tomorrow: p(Date.UTC(2026, 10, 20, 0, 30)),
    beforeTerra: p(Date.UTC(1999, 0, 1)),
    beforeNight: p(Date.UTC(2023, 0, 1), night),
    junk: [A.pickDate(NaN, wall, terra).day, A.pickDate(null, wall, terra).reason,
           A.pickDate('soon', wall, terra).day, A.pickDate(new Date('x'), wall).day],
    isoDay: [A.isoDay(Date.UTC(2026, 0, 5, 23, 59)), A.isoDay(new Date('x')), A.isoDay('2026'),
             A.isoDay(null), A.isoDay(1e18)],
    valid: ['2026-02-28', '2026-02-29', '2024-02-29', '2026-13-01', '2026-1-01', '', null,
            '2026-11-19T00:00:00Z'].map(A.validDay),
    add: [A.addDays('2026-12-31', 1), A.addDays('2024-02-28', 1), A.addDays('2026-03-01', -1),
          A.addDays('2026-11-19', -7), A.addDays('bad', 1), A.addDays('2026-11-19', NaN)],
    clamp: [A.clampDay('2026-11-19', terra, wall), A.clampDay('2030-01-01', terra, wall),
            A.clampDay('1990-01-01', terra, wall), A.clampDay('2026-11-01', terra, wall),
            A.clampDay('2026-02-30', terra, wall), A.clampDay(42, terra, wall)],
    reasons: ['live', 'today', 'clock', 'future', 'before-start'].map(function (r) {
      return A.reasonText({ reason: r }, terra); })
  };
""")


def test_live_clock_shows_yesterday(ran):
    d = ran["dates"]
    assert d["live"] == ["2026-11-18", "live"]
    assert d["liveEdge"] == ["2026-11-18", "live"]  # 15 minutes off still counts as live
    assert d["junk"] == ["2026-11-18", "live", "2026-11-18", "2026-11-18"]


def test_scrubbed_clock_shows_that_day(ran):
    d = ran["dates"]
    assert d["scrubbed"] == ["2026-11-10", "clock"]
    assert d["dayStart"] == d["dayEnd"] == ["2026-11-10", "clock"]
    assert d["todayScrubbed"] == ["2026-11-19", "today"]


def test_dates_outside_the_layer_are_clamped(ran):
    d = ran["dates"]
    assert d["future"] == ["2026-11-18", "future"]
    assert d["tomorrow"] == ["2026-11-18", "future"]
    assert d["beforeTerra"] == ["2000-02-24", "before-start"]
    assert d["beforeNight"] == ["2024-03-25", "before-start"]
    assert d["clamp"] == ["2026-11-18", "2026-11-18", "2000-02-24", "2026-11-01", None, None]


def test_day_helpers(ran):
    d = ran["dates"]
    assert d["isoDay"] == ["2026-01-05", None, None, None, None]
    assert d["valid"] == [True, False, True, False, False, False, False, False]
    assert d["add"] == ["2027-01-01", "2024-02-29", "2026-02-28", "2026-11-12", None, None]
    live, today, clock, future, before = d["reasons"]
    assert "yesterday" in live and "still arriving" in live
    assert "partly" in today and clock == "the day on the clock"
    assert "yesterday" in future and "first day" in before


scenario("urls_and_numbers", r"""
  var A = N();
  var byId = {}; A.LAYERS.forEach(function (l) { byId[l.id] = l; });
  return {
    ids: A.LAYERS.map(function (l) { return l.id; }),
    terra: A.tileUrl(byId.modis_terra, '2026-11-18'),
    night: A.tileUrl(byId.viirs_night, '2026-11-18'),
    split: [A.clampSplit(0.5), A.clampSplit(-1), A.clampSplit(2), A.clampSplit(NaN),
            A.clampSplit('0.3')],
    pointer: [A.splitFromPointer(350, { left: 100, width: 1000 }),
              A.splitFromPointer(0, { left: 100, width: 1000 }),
              A.splitFromPointer(5000, { left: 100, width: 1000 }),
              A.splitFromPointer(350, { left: 100, width: 0 }),
              A.splitFromPointer(350, null), A.splitFromPointer('350', { width: 10 }),
              A.splitFromPointer(5, { width: 10 })],
    opacity: [100, 0, 40, '55', ' ', 'abc', null, 250, -5].map(A.opacityFrom),
    events: [A.eventsUrl('', 30), A.eventsUrl('http://h', 7), A.eventsUrl(null, 'x'),
             A.eventsUrl('', 9999)],
    safe: ['https://a.example/x', 'http://a.example', 'javascript:alert(1)',
           'data:text/html,x', '//a.example', 'https://', 'https://a b', ' https://a.example',
           'ftp://a.example', null, 5].map(A.safeUrl),
    numbers: [2378.77, 35, 1234567.5, 0.004, -12.5, 2378.7, NaN].map(A.formatNumber),
    when: [A.shortDateTime('2026-11-19T12:00:00Z'), A.shortDateTime('2026-11-17'),
           A.shortDateTime('later'), A.shortDateTime(5)],
    colors: [A.colorFor('wildfires'), A.colorFor('severeStorms'), A.colorFor('nope'),
             A.colorFor('constructor'), A.colorFor(null)]
  };
""")


def test_gibs_tile_urls_match_capabilities(ran):
    r = ran["urls_and_numbers"]
    assert r["ids"] == ["modis_terra", "modis_aqua", "viirs_noaa20", "viirs_snpp", "viirs_night"]
    assert r["terra"] == (
        "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
        "MODIS_Terra_CorrectedReflectance_TrueColor/default/2026-11-18/"
        "GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpeg")
    assert r["night"] == (
        "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
        "VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance/default/2026-11-18/"
        "GoogleMapsCompatible_Level8/{z}/{y}/{x}.png")


def test_split_and_opacity_helpers(ran):
    r = ran["urls_and_numbers"]
    assert r["split"] == [0.5, 0.02, 0.98, 0.5, 0.5]
    assert r["pointer"] == [0.25, 0.02, 0.98, None, None, None, 0.5]
    assert r["opacity"] == [1, 0, 0.4, 0.55, 1, 1, 1, 1, 0]


def test_event_helpers(ran):
    r = ran["urls_and_numbers"]
    assert r["events"] == ["/api/eonet?days=30&status=open", "http://h/api/eonet?days=7&status=open",
                           "/api/eonet?days=30&status=open", "/api/eonet?days=365&status=open"]
    assert r["safe"] == ["https://a.example/x", "http://a.example", None, None, None, None, None,
                         None, None, None, None]
    assert r["numbers"] == ["2,378.77", "35", "1,234,567.5", "0", "-12.5", "2,378.7", ""]
    assert r["when"] == ["2026-11-19 12:00 UTC", "2026-11-17", "", ""]
    assert r["colors"][0] != r["colors"][1]
    assert r["colors"][2] == r["colors"][3] == r["colors"][4]  # unknown ids share one grey


# --- imagery follows the clock -------------------------------------------------------

scenario("imagery_live", r"""
  SIM.t = Date.now();
  var k = makeCtx();
  var ok = N().init(k.ctx);
  var before = { providers: PROVIDERS.length, stack: stack(k),
                 date: one(k.ctx.mount, 'nasa-date').textContent,
                 title: byTag(k.ctx.mount, 'H3')[0].textContent };
  toggle(k, 'nasa-imagery-toggle', true);
  var first = { providers: urls(), stack: stack(k), on: N().isImageryOn(),
                day: N().currentDay(), want: yesterday(), feed: k.feed.slice(),
                date: one(k.ctx.mount, 'nasa-date').textContent,
                alpha: ours(k)[0].alpha, split: ours(k)[0].splitDirection,
                level: PROVIDERS[0].maximumLevel, credit: PROVIDERS[0].credit.text };
  // the clock runs on through the day: no new layer, no new tiles
  var syncs = [];
  for (var i = 0; i < 5; i++) { SIM.t += 60000; syncs.push(N().sync()); }
  k.ctx.viewer.clock.onTick.raise();
  k.ctx.viewer.clock.onTick.raise();
  var later = { providers: PROVIDERS.length, syncs: syncs, stack: stack(k) };
  toggle(k, 'nasa-imagery-toggle', false);
  var off = { stack: stack(k), on: N().isImageryOn(), day: N().currentDay(),
              sync: N().sync(), providers: PROVIDERS.length };
  return { ok: ok, before: before, first: first, later: later, off: off,
           requests: REQUESTS.length };
""")


def test_imagery_starts_off(ran):
    b = ran["imagery_live"]["before"]
    assert ran["imagery_live"]["ok"] is True
    assert b["providers"] == 0 and b["stack"] == ["base"]
    assert b["title"] == "NASA imagery and events"
    assert "Turn on" in b["date"]


def test_live_clock_loads_yesterday_and_says_so(ran):
    f = ran["imagery_live"]["first"]
    assert f["on"] is True
    assert f["day"] == f["want"]
    assert len(f["providers"]) == 1 and f"/default/{f['want']}/" in f["providers"][0]
    assert "MODIS_Terra_CorrectedReflectance_TrueColor" in f["providers"][0]
    assert f["stack"] == ["base", f["want"]]  # above the basemap
    assert f["alpha"] == 1 and f["split"] == 0 and f["level"] == 9
    assert f["credit"] == f"NASA GIBS, MODIS Terra true colour, {f['want']}"
    assert "live clock" in f["date"] and "yesterday" in f["date"]
    assert len(f["feed"]) == 1 and f["feed"][0].startswith("NASA GIBS imagery: MODIS Terra")
    assert "live clock" in f["feed"][0]


def test_no_new_tiles_within_a_day(ran):
    later = ran["imagery_live"]["later"]
    assert later["providers"] == 1
    assert later["syncs"] == [False] * 5
    assert len(later["stack"]) == 2


def test_imagery_off_removes_the_layer(ran):
    off = ran["imagery_live"]["off"]
    assert off["stack"] == ["base"] and off["on"] is False and off["day"] is None
    assert off["sync"] is False and off["providers"] == 1
    assert ran["imagery_live"]["requests"] == 0  # tiles never go through fetch


scenario("imagery_scrub", r"""
  var k = makeCtx();
  N().init(k.ctx);
  // another module's imagery sits above ours; a day change must not move us past it
  sim('2026-03-10T01:00:00Z');
  N().setImagery(true);
  var other = new ImageryLayer({ url: 'other' });
  k.ctx.viewer.imageryLayers.add(other);
  var first = { day: N().currentDay(), providers: PROVIDERS.length,
                date: one(k.ctx.mount, 'nasa-date').textContent };
  sim('2026-03-10T23:59:00Z');
  var sameDay = N().sync();
  sim('2026-03-11T00:30:00Z');
  var nextDay = N().sync();
  var list = k.ctx.viewer.imageryLayers.list;
  var afterNext = { day: N().currentDay(), providers: PROVIDERS.length,
    index: list.indexOf(ours(k).filter(function (l) { return l !== other; })[0]),
    otherIndex: list.indexOf(other), length: list.length };
  // the clock tick checks the day at most once a second
  sim('2026-03-12T05:00:00Z');
  k.ctx.viewer.clock.onTick.raise();
  var ticked = N().currentDay();
  sim('2026-03-13T05:00:00Z');
  k.ctx.viewer.clock.onTick.raise();
  var throttled = N().currentDay();
  sim('2026-03-10T12:00:00Z');
  var back = N().sync();
  return { first: first, sameDay: sameDay, nextDay: nextDay, afterNext: afterNext,
           ticked: ticked, throttled: throttled, back: back, backDay: N().currentDay(),
           days: urls().map(dayOf), feed: k.feed.length };
""")


def test_scrubbed_clock_follows_the_day(ran):
    s = ran["imagery_scrub"]
    assert s["first"]["day"] == "2026-03-10" and s["first"]["providers"] == 1
    assert "the day on the clock" in s["first"]["date"]
    assert s["sameDay"] is False
    assert s["nextDay"] is True
    # the new day's layer took the old one's slot, under the other module's layer
    assert s["afterNext"] == {"day": "2026-03-11", "providers": 2, "index": 1,
                              "otherIndex": 2, "length": 3}
    assert s["ticked"] == "2026-03-12"
    assert s["throttled"] == "2026-03-12"  # a second tick inside the same second is skipped
    assert s["back"] is True and s["backDay"] == "2026-03-10"
    assert s["days"] == ["2026-03-10", "2026-03-11", "2026-03-12", "2026-03-10"]
    assert s["feed"] == 1  # day changes update the panel, not the intel feed


scenario("layer_select", r"""
  sim('2026-03-10T12:00:00Z');
  var k = makeCtx();
  N().init(k.ctx);
  var select = one(k.ctx.mount, 'nasa-layer-select');
  var options = byTag(select, 'OPTION').map(function (o) { return [o.value, o.textContent]; });
  select.value = 'viirs_night';
  select.fire('change');
  var offChange = { providers: PROVIDERS.length, layer: N().layerId() };
  N().setImagery(true);
  var night = { url: urls()[0], level: PROVIDERS[0].maximumLevel, day: N().currentDay(),
                date: one(k.ctx.mount, 'nasa-date').textContent };
  select.value = 'modis_aqua';
  select.fire('change');
  var aqua = { url: urls()[1], stack: stack(k).length, feed: k.feed.slice() };
  var junk = N().setLayer('constructor');
  return { options: options, offChange: offChange, night: night, aqua: aqua, junk: junk,
           selectValue: select.value, layer: N().layerId(), providers: PROVIDERS.length };
""")


def test_layer_choice(ran):
    r = ran["layer_select"]
    assert [o[0] for o in r["options"]] == ["modis_terra", "modis_aqua", "viirs_noaa20",
                                            "viirs_snpp", "viirs_night"]
    assert r["offChange"] == {"providers": 0, "layer": "viirs_night"}
    n = r["night"]
    # March 2026 is after the NOAA-20 Day/Night Band begins, so the clock's day stands
    assert "VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance" in n["url"] and n["url"].endswith(".png")
    assert n["level"] == 8 and n["day"] == "2026-03-10"
    assert "night side only" in n["date"]
    a = r["aqua"]
    assert "MODIS_Aqua_CorrectedReflectance_TrueColor" in a["url"] and a["stack"] == 2
    assert a["feed"][-1].startswith("NASA GIBS imagery: MODIS Aqua true colour for 2026-03-10")
    assert r["junk"] is False and r["selectValue"] == "modis_aqua"
    assert r["layer"] == "modis_aqua" and r["providers"] == 2


# --- compare split and divider -------------------------------------------------------

scenario("compare", r"""
  sim('2026-03-10T12:00:00Z');
  var k = makeCtx();
  N().init(k.ctx);
  var scene = k.ctx.viewer.scene;
  var box = k.ctx.viewer.container;
  // compare on while the imagery is off turns the imagery on too
  toggle(k, 'nasa-compare-toggle', true);
  var div = one(box, 'nasa-split');
  var right = function () { return ours(k).filter(function (l) {
    return dayOf(l.imageryProvider.url) === N().currentDay(); })[0]; };
  var left = function () { return ours(k).filter(function (l) {
    return dayOf(l.imageryProvider.url) === N().compareDay(); })[0]; };
  var on = {
    imagery: N().isImageryOn(), compare: N().isCompareOn(),
    imageryBox: one(k.ctx.mount, 'nasa-imagery-toggle').checked,
    day: N().currentDay(), compareDay: N().compareDay(), stack: stack(k),
    rightSplit: right().splitDirection, leftSplit: left().splitDirection,
    splitPosition: scene.splitPosition, dividerLeft: div.style.left,
    labels: [one(div, 'nasa-split-left').textContent, one(div, 'nasa-split-right').textContent],
    input: one(k.ctx.mount, 'nasa-compare-date').value,
    role: div.getAttribute('role'), feed: k.feed.slice()
  };
  // drag: the container spans x = 100..1100
  div.fire('pointerdown', { clientX: 600, pointerId: 1, preventDefault: function () {} });
  document.fire('pointermove', { clientX: 350 });
  var dragged = { split: scene.splitPosition, left: div.style.left,
                  aria: div.getAttribute('aria-valuenow') };
  document.fire('pointermove', { clientX: 5000 });
  var far = scene.splitPosition;
  document.fire('pointerup', {});
  document.fire('pointermove', { clientX: 600 });
  var released = scene.splitPosition;
  div.fire('keydown', { key: 'ArrowLeft', preventDefault: function () {} });
  var keyed = scene.splitPosition;
  div.fire('keydown', { key: 'Enter' });
  // opacity applies to both dates
  var range = one(k.ctx.mount, 'nasa-opacity');
  range.value = '40';
  range.fire('input');
  var alphas = ours(k).map(function (l) { return l.alpha; });
  // a new compare date rebuilds only the left layer; junk and future dates are tamed
  var input = one(k.ctx.mount, 'nasa-compare-date');
  input.value = '2026-01-01'; input.fire('change');
  var newDate = { compareDay: N().compareDay(), providers: PROVIDERS.length, stack: stack(k),
                  leftSplit: left().splitDirection, alpha: left().alpha,
                  label: one(div, 'nasa-split-left').textContent };
  input.value = 'garbage'; input.fire('change');
  var afterJunk = { compareDay: N().compareDay(), input: input.value,
                    providers: PROVIDERS.length };
  N().setCompareDate('2099-01-01');
  var future = N().compareDay();
  // the clock moves on: the right side follows, the left side stays put
  N().setCompareDate('2026-01-01');
  sim('2026-03-11T12:00:00Z');
  N().sync();
  var moved = { stack: stack(k), rightSplit: right().splitDirection,
                labels: [one(div, 'nasa-split-left').textContent,
                         one(div, 'nasa-split-right').textContent] };
  toggle(k, 'nasa-compare-toggle', false);
  var off = { compare: N().isCompareOn(), stack: stack(k), split: right().splitDirection,
              divider: byClass(box, 'nasa-split').length, imagery: N().isImageryOn() };
  N().setCompare(true);
  var again = { stack: stack(k), compareDay: N().compareDay(),
                divider: byClass(box, 'nasa-split').length, split: scene.splitPosition };
  N().setImagery(false);
  var allOff = { stack: stack(k), compare: N().isCompareOn(),
                 compareBox: one(k.ctx.mount, 'nasa-compare-toggle').checked,
                 divider: byClass(box, 'nasa-split').length };
  return { on: on, dragged: dragged, far: far, released: released, keyed: keyed,
           alphas: alphas, newDate: newDate, afterJunk: afterJunk, future: future,
           wantFuture: yesterday(), moved: moved, off: off, again: again, allOff: allOff };
""")


def test_compare_splits_two_dates(ran):
    on = ran["compare"]["on"]
    assert on["imagery"] is True and on["compare"] is True and on["imageryBox"] is True
    assert on["day"] == "2026-03-10" and on["compareDay"] == "2026-03-03"  # a week back
    assert on["stack"] == ["base", "2026-03-10", "2026-03-03"]
    assert on["rightSplit"] == 1 and on["leftSplit"] == -1  # clock right, compare left
    assert on["splitPosition"] == 0.5 and on["dividerLeft"] == "50.00%"
    assert on["labels"] == ["2026-03-03", "2026-03-10"]
    assert on["input"] == "2026-03-03" and on["role"] == "separator"
    assert on["feed"][-1] == "NASA GIBS compare: 2026-03-03 on the left, 2026-03-10 on the right"


def test_divider_drag_and_keys(ran):
    r = ran["compare"]
    assert r["dragged"] == {"split": 0.25, "left": "25.00%", "aria": "25"}
    assert r["far"] == 0.98
    assert r["released"] == 0.98  # moves after pointerup do nothing
    assert abs(r["keyed"] - 0.96) < 1e-9


def test_compare_date_and_opacity(ran):
    r = ran["compare"]
    assert r["alphas"] == [0.4, 0.4]
    nd = r["newDate"]
    assert nd["compareDay"] == "2026-01-01" and nd["providers"] == 3
    assert nd["stack"] == ["base", "2026-03-10", "2026-01-01"]
    assert nd["leftSplit"] == -1 and nd["alpha"] == 0.4 and nd["label"] == "2026-01-01"
    assert r["afterJunk"] == {"compareDay": "2026-01-01", "input": "2026-01-01", "providers": 3}
    assert r["future"] == r["wantFuture"]


def test_compare_follows_the_clock_on_the_right_only(ran):
    m = ran["compare"]["moved"]
    assert m["stack"] == ["base", "2026-03-11", "2026-01-01"]
    assert m["rightSplit"] == 1
    assert m["labels"] == ["2026-01-01", "2026-03-11"]


def test_compare_off_and_imagery_off(ran):
    r = ran["compare"]
    assert r["off"] == {"compare": False, "stack": ["base", "2026-03-11"], "split": 0,
                        "divider": 0, "imagery": True}
    assert r["again"]["stack"] == ["base", "2026-03-11", "2026-01-01"]  # the date is kept
    assert r["again"]["divider"] == 1 and abs(r["again"]["split"] - 0.96) < 1e-9
    assert r["allOff"] == {"stack": ["base"], "compare": False, "compareBox": False,
                           "divider": 0}


# --- EONET events --------------------------------------------------------------------

scenario("events", r"""
  sim('2026-03-10T12:00:00Z');
  route('/api/eonet', EVENTS);
  var k = makeCtx();
  N().init(k.ctx);
  var before = REQUESTS.length;
  toggle(k, 'nasa-events-toggle', true);
  toggle(k, 'nasa-events-toggle', true);
  await settle();
  var pts = points(k);
  var first = {
    before: before, requests: REQUESTS.slice(), n: pts.length, count: countEl.textContent,
    enabled: N().isEventsEnabled(), intervals: live().map(function (t) { return t.ms; }),
    colors: pts.map(function (p) { return p.color.css; }),
    positions: pts.map(function (p) { return p.position; }),
    ids: pts.map(function (p) { return p.id.type + ':' + p.id.eventId; }),
    size: pts[0].pixelSize, outline: pts[0].outlineColor,
    status: one(k.ctx.mount, 'nasa-events-status').textContent, feed: k.feed.slice()
  };
  // a click on the storm: the card opens in the panel and the camera flies there
  pts[0].id.sdaPick();
  var card = one(k.ctx.mount, 'nasa-event-card');
  var links = byClass(card, 'nasa-card-link');
  var storm = {
    title: one(card, 'nasa-card-title').textContent,
    category: one(card, 'nasa-card-category').textContent,
    date: one(card, 'nasa-card-date').textContent,
    magnitude: one(card, 'nasa-card-magnitude').textContent,
    position: one(card, 'nasa-card-position').textContent,
    links: links.map(function (a) { return [a.textContent, a.getAttribute('href'),
      a.getAttribute('target'), a.getAttribute('rel')]; }),
    flights: k.ctx.viewer._flights.map(function (f) { return f.destination; })
  };
  // the fire: two links; then Fly to, then Compare on this day
  N().openEvent('EONET_26500');
  var cards = byClass(k.ctx.mount, 'nasa-event-card').length;
  card = one(k.ctx.mount, 'nasa-event-card');
  var fire = { title: one(card, 'nasa-card-title').textContent,
    magnitude: one(card, 'nasa-card-magnitude').textContent,
    date: one(card, 'nasa-card-date').textContent,
    links: byClass(card, 'nasa-card-link').map(function (a) { return a.getAttribute('href'); }) };
  one(card, 'nasa-card-fly').fire('click');
  var flights = k.ctx.viewer._flights.length;
  one(card, 'nasa-card-compare').fire('click');
  var compared = { compare: N().isCompareOn(), compareDay: N().compareDay(),
                   day: N().currentDay() };
  // the iceberg has no magnitude and no sources
  N().openEvent('EONET_26510');
  card = one(k.ctx.mount, 'nasa-event-card');
  var ice = { magnitude: byClass(card, 'nasa-card-magnitude').length,
              sources: byClass(card, 'nasa-card-sources').length,
              date: one(card, 'nasa-card-date').textContent };
  one(card, 'nasa-card-close').fire('click');
  var closed = byClass(k.ctx.mount, 'nasa-event-card').length;
  var missing = N().openEvent('EONET_NOPE');
  // thirty minutes later the interval fires: same events, no repeat line
  live()[0].fn();
  await settle();
  var refreshed = { requests: REQUESTS.length, n: points(k).length,
    feed: k.feed.filter(function (l) { return l.indexOf('NASA EONET') === 0; }).length };
  N().openEvent('EONET_26494');
  toggle(k, 'nasa-events-toggle', false);
  await settle();
  var off = { n: points(k).length, show: k.ctx.viewer._added[0].show,
              count: countEl.textContent, intervals: live().length,
              enabled: N().isEventsEnabled(), requests: REQUESTS.length,
              card: byClass(k.ctx.mount, 'nasa-event-card').length };
  return { first: first, storm: storm, cards: cards, fire: fire, flights: flights,
           compared: compared, ice: ice, closed: closed, missing: missing,
           refreshed: refreshed, off: off };
""")


def test_events_load_and_draw_by_category(ran):
    f = ran["events"]["first"]
    assert f["before"] == 0  # nothing loads until the events are on
    assert f["requests"] == ["/api/eonet?days=30&status=open"]
    assert f["n"] == 4 and f["count"] == "4" and f["enabled"] is True
    assert f["intervals"] == [1800000]
    storm, fire, ice, bush = f["colors"]
    assert fire == bush and len({storm, fire, ice}) == 3
    assert f["positions"][0] == [85.3, 16, 0]
    assert f["ids"][0] == "eonet:EONET_26494"
    assert f["size"] == 10 and f["outline"]["alpha"] == 0.8
    line = ("NASA EONET: 4 open events in the last 30 days (2 wildfires, 1 severe storms, "
            "1 sea and lake ice)")
    assert f["feed"] == [line] and f["status"] == line


def test_event_card(ran):
    s = ran["events"]["storm"]
    assert s["title"] == "Tropical Cyclone Akvan"
    assert s["category"] == "Severe Storms"
    assert s["date"] == "Latest 2026-11-19 12:00 UTC (12 observations)"
    assert s["magnitude"] == "Magnitude 45 kts"
    assert s["position"] == "At 16.00, 85.30"
    assert s["links"] == [["JTWC", "https://www.metoc.navy.mil/jtwc/products/io0526.tcw",
                           "_blank", "noopener noreferrer"]]
    assert s["flights"] == [[85.3, 16, 1500000]]


def test_card_buttons(ran):
    r = ran["events"]
    assert r["cards"] == 1  # opening another event replaces the card
    assert r["fire"]["title"] == "Ridge Fire, Plumas, California"
    assert r["fire"]["magnitude"] == "Magnitude 2,378.77 acres"
    assert r["fire"]["date"] == "Latest 2026-11-18 00:00 UTC"
    assert r["fire"]["links"] == ["https://irwin.doi.gov/observer/incidents/abc",
                                  "http://inciweb.example/x"]
    assert r["flights"] == 3  # the storm pick, the fire pick, then Fly to
    assert r["compared"] == {"compare": True, "compareDay": "2026-11-18", "day": "2026-03-10"}
    assert r["ice"] == {"magnitude": 0, "sources": 0, "date": "Latest 2026-11-15 00:00 UTC"}
    assert r["closed"] == 0 and r["missing"] is False


def test_events_refresh_and_off(ran):
    r = ran["events"]
    assert r["refreshed"] == {"requests": 2, "feed": 1, "n": 4}
    assert r["off"] == {"n": 0, "show": False, "count": "0", "intervals": 0, "enabled": False,
                        "requests": 2, "card": 0}


scenario("page_toggle", r"""
  route('http://h/api/eonet', EVENTS);
  // the page's layer checkbox, wired the way worldview.html wires its layers
  var pageBox = new El('input'); pageBox.id = 'layer-eonet'; pageBox.type = 'checkbox';
  document.body.appendChild(pageBox);
  var calls = [];
  pageBox.addEventListener('change', function () {
    calls.push(this.checked); N().setEventsEnabled(this.checked); });
  var k = makeCtx({ api: 'http://h' });
  N().init(k.ctx);
  toggle(k, 'nasa-events-toggle', true);
  await settle();
  var on = { calls: calls.slice(), page: pageBox.checked, requests: REQUESTS.slice(),
             n: points(k).length };
  pageBox.click();  // off from the page's side
  await settle();
  return { on: on, calls: calls, panel: one(k.ctx.mount, 'nasa-events-toggle').checked,
           enabled: N().isEventsEnabled(), n: points(k).length };
""")


def test_panel_toggle_goes_through_the_page_checkbox(ran):
    r = ran["page_toggle"]
    assert r["on"] == {"calls": [True], "page": True,
                       "requests": ["http://h/api/eonet?days=30&status=open"], "n": 4}
    assert r["calls"] == [True, False]
    assert r["panel"] is False and r["enabled"] is False and r["n"] == 0


scenario("late_answer", r"""
  route('/api/eonet', EVENTS);
  var k = makeCtx();
  N().init(k.ctx);
  N().setEventsEnabled(true);
  N().setEventsEnabled(false);  // turned off before the answer lands
  await settle();
  return { n: points(k).length, feed: k.feed, count: countEl.textContent };
""")


def test_answer_after_disable_is_dropped(ran):
    assert ran["late_answer"] == {"n": 0, "feed": [], "count": "0"}


# --- static build, hostile answers, guards ----------------------------------------------

scenario("static", r"""
  sim('2026-03-10T12:00:00Z');
  route('/api/eonet', EVENTS);
  var k = makeCtx({ isStatic: true });
  var ok = N().init(k.ctx);
  var status0 = one(k.ctx.mount, 'nasa-events-status').textContent;
  toggle(k, 'nasa-events-toggle', true);
  N().setEventsEnabled(true);
  await N().refreshEvents();
  await settle();
  // the imagery half needs no server: tiles come straight from GIBS
  N().setImagery(true);
  N().setCompare(true);
  return { ok: ok, requests: REQUESTS, intervals: INTERVALS.length,
           collections: k.ctx.viewer._added.length, feed: k.feed, status0: status0,
           status: one(k.ctx.mount, 'nasa-events-status').textContent,
           box: one(k.ctx.mount, 'nasa-events-toggle').checked,
           enabled: N().isEventsEnabled(), stack: stack(k), providers: urls() };
""")


def test_static_build_makes_no_api_request(ran):
    s = ran["static"]
    assert s["ok"] is True
    assert s["requests"] == [] and s["intervals"] == 0 and s["collections"] == 0
    assert s["enabled"] is False and s["box"] is False
    assert "live server" in s["status0"] and "live server" in s["status"]
    assert s["feed"][0] == "NASA EONET events need the live server."
    assert sum("live server" in line for line in s["feed"]) == 1
    assert s["stack"] == ["base", "2026-03-10", "2026-03-03"]
    assert all(u.startswith("https://gibs.earthdata.nasa.gov/") for u in s["providers"])


scenario("hostile", r"""
  route('/api/eonet', { events: [null, 'x', 5, [], { id: 'A' }, { id: 'B', point: [500, 0] },
    { id: 'C', point: ['1', '2'] }, { id: 'D', point: [1] }, { id: {}, point: [1, 2] },
    { id: 'E', point: [1, null] },
    { id: 'GOOD', title: '<img src=x onerror=alert(1)>', categories: 'nope', point: [10, 20],
      date: 42, magnitude_value: 'big', magnitude_unit: {}, observations: -3,
      sources: [{ id: 'evil', url: 'javascript:alert(1)' }, { id: 'data',
        url: 'data:text/html,hi' }, null, 'x', { url: 'https://ok.example/x' },
        { id: 'sp', url: 'https://ok.example/a b' }] },
    { id: 'GOOD', point: [0, 0] }], days: 'x', stale: 'yes' });
  var k = makeCtx();
  N().init(k.ctx);
  N().setEventsEnabled(true);
  await settle();
  var first = { n: points(k).length, line: k.feed[0] };
  points(k)[0].id.sdaPick();
  var card = one(k.ctx.mount, 'nasa-event-card');
  var shown = { title: one(card, 'nasa-card-title').textContent,
    category: one(card, 'nasa-card-category').textContent,
    links: byClass(card, 'nasa-card-link').map(function (a) {
      return [a.textContent, a.getAttribute('href')]; }),
    rows: ['nasa-card-date', 'nasa-card-magnitude'].map(function (c) {
      return byClass(card, c).length; }) };
  var answers = [];
  var bodies = ['garbage', null, { events: 'nope' }, { events: [{ id: 'Z', point: [1, 2] }],
                stale: true }];
  for (var i = 0; i < bodies.length; i++) {
    route('/api/eonet', bodies[i]);
    live()[0].fn();
    await settle();
    answers.push(points(k).length);
  }
  var staleLine = k.feed[k.feed.length - 1];
  route('/api/eonet', '__THROW__');
  live()[0].fn();
  await settle();
  route('/api/eonet', { detail: 'NASA EONET events feed unavailable' }, 502);
  live()[0].fn();
  await settle();
  var failed = { feed: k.feed.slice(-2), n: points(k).length,
                 status: one(k.ctx.mount, 'nasa-events-status').textContent };
  return { first: first, shown: shown, answers: answers, staleLine: staleLine,
           failed: failed, card: byClass(k.ctx.mount, 'nasa-event-card').length };
""")


def test_hostile_answers_do_not_throw(ran):
    h = ran["hostile"]
    assert h["first"]["n"] == 1
    assert h["first"]["line"] == "NASA EONET: 1 open event in the last 30 days (1 other)"
    s = h["shown"]
    assert s["title"] == "<img src=x onerror=alert(1)>"  # text, never markup
    assert s["category"] == "Uncategorised"
    assert s["links"] == [["Source", "https://ok.example/x"]]
    assert s["rows"] == [0, 0]
    assert h["answers"] == [0, 0, 0, 1]
    assert "stale" in h["staleLine"]
    assert h["card"] == 0  # the picked event went away, so its card closed


def test_failures_are_reported_once(ran):
    f = ran["hostile"]["failed"]
    assert f["feed"][-1] == "NASA EONET: events feed unavailable"
    assert "unavailable" not in f["feed"][0]
    assert "did not load" in f["status"]
    assert f["n"] == 1  # the last good markers stay up


scenario("guards", r"""
  sim('2026-03-10T12:00:00Z');
  var k = makeCtx({ noMount: true });
  var ok = N().init(k.ctx);
  N().setImagery(true);
  N().setCompare(true);
  var noMount = { stack: stack(k), divider: byClass(k.ctx.viewer.container,
    'nasa-split').length };
  // re-init: the old panel, layers, divider and clock listener go away
  var k2 = makeCtx();
  var oldListeners = k.ctx.viewer.clock.onTick.listeners.length;
  N().init(k2.ctx);
  var reinit = { oldStack: stack(k), oldDivider: byClass(k.ctx.viewer.container,
    'nasa-split').length, oldListeners: oldListeners,
    oldTick: k.ctx.viewer.clock.onTick.listeners.length,
    newTick: k2.ctx.viewer.clock.onTick.listeners.length, docMoves:
    (DOC_LISTENERS.pointermove || []).length };
  var k3 = makeCtx();
  N().init(k3.ctx);
  var panels = byClass(k3.ctx.mount, 'panel').length;
  var oldPanel = byClass(k2.ctx.mount, 'panel').length;
  // a Cesium without split support refuses compare instead of half-working
  var saved = Cesium.SplitDirection;
  Cesium.SplitDirection = undefined;
  var k4 = makeCtx();
  N().init(k4.ctx);
  N().setCompare(true);
  var noSplit = { compare: N().isCompareOn(), stack: stack(k4),
                  date: one(k4.ctx.mount, 'nasa-date').textContent };
  Cesium.SplitDirection = saved;
  var savedC = window.Cesium;
  var noCesium = (window.Cesium = undefined, N().init(makeCtx().ctx));
  window.Cesium = savedC;
  var v = makeViewer(); delete v.imageryLayers;
  var noLayers = N().init({ viewer: v, mount: new El('div') });
  // a simNow that throws falls back to the wall clock
  var k5 = makeCtx();
  k5.ctx.simNow = function () { throw new Error('no clock'); };
  N().init(k5.ctx);
  N().setImagery(true);
  return { ok: ok, noMount: noMount, reinit: reinit, panels: panels, oldPanel: oldPanel,
           noSplit: noSplit, noCesium: noCesium, noLayers: noLayers,
           noViewer: N().init({ mount: new El('div') }), nothing: N().init(null),
           brokenClock: N().currentDay(), wantBroken: yesterday() };
""")


def test_guards_and_reinit(ran):
    g = ran["guards"]
    assert g["ok"] is True
    # no panel: still draws, and the divider still lives on the globe
    assert g["noMount"] == {"stack": ["base", "2026-03-10", "2026-03-03"], "divider": 1}
    r = g["reinit"]
    assert r["oldStack"] == ["base"] and r["oldDivider"] == 0
    assert r["oldListeners"] == 1 and r["oldTick"] == 0 and r["newTick"] == 1
    assert r["docMoves"] == 0  # the old divider's document listeners are gone
    assert g["panels"] == 1 and g["oldPanel"] == 0
    assert g["noSplit"]["compare"] is False and g["noSplit"]["stack"] == ["base"]
    assert "no imagery split" in g["noSplit"]["date"]
    assert g["noCesium"] is False and g["noLayers"] is False
    assert g["noViewer"] is False and g["nothing"] is False


def test_failed_sim_clock_falls_back_to_live(ran):
    g = ran["guards"]
    assert g["brokenClock"] == g["wantBroken"]


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "document.write" not in text
    assert all(ord(ch) < 127 for ch in text), "nasaimagery.js must stay ASCII"
    assert "window.SDANasaImagery" in text or "global.SDANasaImagery" in text
    assert "'noopener noreferrer'" in text
