"""Tests for the globe summit conditions drawer (static/js/summit.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_aurora.py does it. The fake
`fetch` answers from a table (a body may be a function of the request, and a route may
hold its answer until the scenario releases it), and `Date.now` runs on a clock the
scenario can move, so nothing here touches the network or waits ten minutes. Skips
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
    / "src" / "sda" / "templates" / "static" / "js" / "summit.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.checked = false;
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
function texts(list) { return list.map(function (x) { return x.textContent; }); }

// a canvas context that records every call
var OPS = ['clearRect', 'fillRect', 'strokeRect', 'fillText', 'beginPath', 'moveTo', 'lineTo',
           'closePath', 'fill', 'stroke', 'setLineDash', 'save', 'restore'];
function Ctx2d(canvas) { this.canvas = canvas; this.ops = []; }
OPS.forEach(function (k) {
  Ctx2d.prototype[k] = function () {
    this.ops.push([k].concat(Array.prototype.slice.call(arguments))); };
});
function opCount(g, k) { return g.ops.filter(function (o) { return o[0] === k; }).length; }
function painted(g) { return g.ops.filter(function (o) { return o[0] === 'fillText'; })
  .map(function (o) { return o[1]; }); }
var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) {
    var e = new El(tag);
    if (e.tagName === 'CANVAS') {
      e.width = 300; e.height = 150;
      e.getContext = function (kind) { return kind === '2d' ? (this.ctx2d = this.ctx2d ||
        new Ctx2d(this)) : null; };
    }
    return e;
  },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) {
      return x.id === id; });
    return hit[0] || null;
  }
};

// ---- a clock the scenario can move (the verdict cache is ten minutes) ------
var RealDate = Date;
var CLOCK = { shift: 0 };
this.Date = { now: function () { return RealDate.now() + CLOCK.shift; },
              parse: function (s) { return RealDate.parse(s); } };

// ---- fake Cesium -------------------------------------------------------
var HANDLERS = [];
var RAYS = [];
var HEIGHTS = [];
function Cartesian2(x, y) { this.x = x; this.y = y; }
function Handler(canvas) { this.canvas = canvas; this.actions = {}; this.destroyed = false;
  HANDLERS.push(this); }
Handler.prototype.setInputAction = function (fn, type) { this.actions[type] = fn; };
Handler.prototype.destroy = function () { this.destroyed = true; };
function EllipsoidTerrainProvider() {}
function rad(d) { return d * Math.PI / 180; }
var Cesium = {
  Cartesian2: Cartesian2,
  Cartographic: {
    fromDegrees: function (lon, lat) { return { lon: lon, lat: lat }; },
    fromCartesian: function (c) { return c && c.carto ? c.carto : null; }
  },
  Math: { toDegrees: function (r) { return r * 180 / Math.PI; } },
  EllipsoidTerrainProvider: EllipsoidTerrainProvider,
  ScreenSpaceEventHandler: Handler,
  ScreenSpaceEventType: { LEFT_CLICK: 'LEFT_CLICK', RIGHT_CLICK: 'RIGHT_CLICK' }
};
var window = this;
this.Cesium = Cesium;

// screen (x, y) -> latitude 45 + y/100, longitude 6 + x/100
function spot(pos) { return { carto: { latitude: rad(45 + pos.y / 100),
                                       longitude: rad(6 + pos.x / 100) } }; }
function makeViewer(o) {
  o = o || {};
  var canvas = { clientWidth: 800, clientHeight: 600, width: 800, height: 600 };
  return {
    terrainProvider: o.terrain ? { real: true } : new EllipsoidTerrainProvider(),
    scene: {
      canvas: canvas,
      globe: {
        ellipsoid: { name: 'ellipsoid' },
        getHeight: function (c) { HEIGHTS.push(c); return o.height; },
        pick: function (ray) { return o.offGlobe || o.noPick ? undefined : spot(ray.pos); }
      }
    },
    camera: {
      getPickRay: function (pos) { RAYS.push([pos.x, pos.y]); return { pos: pos }; },
      pickEllipsoid: function (pos) { return o.offGlobe ? undefined : spot(pos); }
    }
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: opts.noViewer ? undefined : makeViewer(opts), api: opts.api || '',
    isStatic: !!opts.isStatic, mount: opts.noMount ? undefined : new El('div'),
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

// ---- fake fetch ----------------------------------------------------------
var CALLS = [];
var ROUTES = [];
var HELD = [];
function route(prefix, body, status, hold) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200, hold: !!hold }); }
function release() { var h = HELD.splice(0); h.forEach(function (fn) { fn(); }); }
function fetch(url, opts) {
  var call = { url: url, method: (opts && opts.method) || 'GET',
               type: opts && opts.headers ? opts.headers['Content-Type'] : null,
               body: opts && opts.body ? JSON.parse(opts.body) : null };
  CALLS.push(call);
  for (var i = 0; i < ROUTES.length; i++) {
    if (url.indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      var body = typeof r.body === 'function' ? r.body(call) : r.body;
      var res = { ok: r.status < 400, status: r.status,
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); } };
      if (!r.hold) return Promise.resolve(res);
      return new Promise(function (done) { HELD.push(function () { done(res); }); });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () {
    return Promise.resolve({}); } });
}
function urls() { return CALLS.map(function (c) { return c.url; }); }

// the POST answer: one verdict per id sent
function echoVerdicts(call) {
  var out = {};
  (call.body || []).forEach(function (p) {
    out[p.id] = { verdict: p.lat > 46 ? 'NO' : 'GO', day: '2026-11-21', when: 'tomorrow',
                  top_reason: 'wind ' + Math.round(p.lat) + ' mph (models)' };
  });
  return out;
}
function points(n, base) {
  var out = [];
  for (var i = 0; i < n; i++) out.push({ id: (base || 'p') + i, lat: 45 + i / 100,
                                         lon: 6.8, elev_m: 4000 + i });
  return out;
}

// ---- a recorded-shape conditions answer (three days, 72 hours) -----------
var T0 = RealDate.UTC(2026, 10, 20) / 1000;
function iso(s) { return new RealDate(s * 1000).toISOString(); }
function hours(day) { return { start: iso(T0 + (day * 24 + 9) * 3600),
                               end: iso(T0 + (day * 24 + 13) * 3600), passed: day === 0 }; }
function makeConditions() {
  var hr = { t: [], wind: [], wind_min: [], wind_max: [], gust: [], temp: [], chill: [],
             cloud: [], precip: [], snow: [], freezing_ft: [] };
  for (var i = 0; i < 72; i++) {
    hr.t.push(T0 + i * 3600);
    hr.wind.push(10 + i / 2); hr.wind_min.push(8 + i / 2); hr.wind_max.push(14 + i / 2);
    hr.gust.push(20); hr.temp.push(20 - i / 6); hr.chill.push(5 - i / 6);
    hr.cloud.push(i % 24 < 12 ? 20 : 90); hr.precip.push(i === 50 ? 0.12 : 0);
    hr.snow.push(0); hr.freezing_ft.push(i === 5 ? null : 12000);
  }
  return {
    name: 'Mont Blanc', lat: 45.8326, lon: 6.8652, elev_m: 4808, elev_ft: 15774,
    elev_source: 'given', timezone: 'Europe/Rome', tz_exact: false,
    tz_note: 'summit hours on a fixed UTC offset', summit_hours_local: '10:00-14:00',
    generated: '2026-11-20T16:00:00+00:00',
    screen_note: 'A screen, not a go/no-go: which peak, which day, and what the risk is. ' +
      'Read the forecasts it names and judge the mountain yourself before any attempt.',
    in_us: false, aqi_scale: 'European AQI', levels_hpa: [600, 500, 400],
    calibration: { region: 'California and Nevada', applied: false,
                   note: 'wind odds not calibrated here' },
    exposed_rule: 'the summit takes at least the wind of the next level above',
    limits: { wind_warn_mph: 22, wind_fail_mph: 30, chill_warn_f: -9, chill_fail_f: -19,
              cloud_pct: 80 },
    days: [
      { date: '2026-11-20', label: 'Fri 20 Nov', verdict: 'MARG',
        top_reason: 'wind 24 mph (models)',
        reasons: [{ level: 'warn', text: 'wind 24 mph (models)' }],
        numbers: { wind_mph: 24 }, read: { models: true }, summit_hours: hours(0),
        sunrise: '07:41', sunset: '16:55', polar: '' },
      { date: '2026-11-21', label: 'Sat 21 Nov', verdict: 'GO',
        top_reason: 'no limit crossed: wind 12 mph',
        reasons: [{ level: 'info', text: 'wind odds not calibrated here: the plain median ' +
                    'limits decide' }],
        numbers: { wind_mph: 12.4, wind_by_model: { ECMWF: 12, GFS: 14, ICON: null },
                   chill_f: -3, temp_f: 12, snow_in: 0, precip_in: 0, cloud_h: 1, cape: 0,
                   freezing_level_ft: 11850, gust_surface_mph: 20, ens_p: 0.04, aqi: 15,
                   aqi_scale: 'European AQI', avalanche: null, nws_day: null },
        read: { models: true }, summit_hours: hours(1), sunrise: '07:42', sunset: '16:54',
        polar: '' },
      { date: '2026-11-22', label: 'Sun 22 Nov', verdict: 'NO',
        top_reason: 'wind 45 mph (models)',
        reasons: [{ level: 'fail', text: 'wind 45 mph (models)' },
                  { level: 'warn', text: 'chill -12 F (models)' },
                  { level: 'info', text: '<b>not markup</b>' }],
        numbers: { wind_mph: 45 }, read: { models: true }, summit_hours: hours(2),
        sunrise: null, sunset: null, polar: 'polar night' }
    ],
    sources: [
      { id: 'models', name: 'Open-Meteo models (ECMWF, GFS, ICON, GEM)', required: true,
        read: true, state: 'fresh', age_min: 12, run: '2026-11-20T00:00:00+00:00' },
      { id: 'om-ens', name: 'ECMWF ensemble', required: true, read: true, state: 'stale',
        age_min: 130, error: 'HTTP 500' },
      { id: 'nws-alerts', name: 'NWS alerts', required: false, read: false,
        state: 'skipped', note: 'outside the US: not checked' },
      { id: 'om-aq', name: 'Open-Meteo air quality', required: false, read: false,
        state: 'failed', error: 'HTTP 502' }
    ],
    hourly: hr
  };
}
var CONDITIONS = makeConditions();

function frame() { return byClass(document.body, 'sumt-frame')[0] || null; }
function drawer() {
  var f = frame();
  if (!f) return null;
  var canvas = byTag(f, 'CANVAS')[0];
  var g = canvas.ctx2d;
  return {
    title: byClass(f, 'sumt-title')[0].textContent,
    sub: byClass(f, 'sumt-sub')[0].textContent,
    screen: byClass(f, 'sumt-screen')[0].textContent,
    status: byClass(f, 'sumt-status')[0].textContent,
    strip: byClass(f, 'sumt-day').map(function (b) {
      return { text: b.textContent, sel: b.className.indexOf('sel') >= 0,
               title: b.getAttribute('title'),
               color: byClass(b, 'sumt-verdict')[0].style.color }; }),
    head: (byClass(byClass(f, 'sumt-dayview')[0], 'sumt-h')[0] || { textContent: '' })
      .textContent,
    reasons: byTag(byClass(f, 'sumt-reasons')[0] || new El('ul'), 'LI').map(function (li) {
      return li.textContent; }),
    nums: (byClass(f, 'sumt-nums')[0] || { textContent: '' }).textContent,
    sources: byClass(f, 'sumt-src').map(function (s) {
      return { text: s.textContent, bad: s.className.indexOf('bad') >= 0 }; }),
    meta: byClass(f, 'sumt-sub')[1].textContent,
    canvas: [canvas.width, canvas.height],
    ops: g ? { clear: opCount(g, 'clearRect'), rects: opCount(g, 'fillRect'),
               strokes: opCount(g, 'stroke'), frames: opCount(g, 'strokeRect'),
               fills: opCount(g, 'fill'), text: painted(g) } : null
  };
}

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
              Promise: Promise, JSON: JSON, Math: Math, Date: Date,
              encodeURIComponent: encodeURIComponent };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'summit.js' });
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
      try { vm.runInContext('window.SDASummit && window.SDASummit.close()', box); }
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
    work = tmp_path_factory.mktemp("summit")
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
  var S = window.SDASummit;
  var mb = { id: 'mb', name: 'Mont Blanc', lat: 45.83261234, lon: 6.8652, elev_m: 4807.6 };
  return {
    clean: [S.cleanPoint(mb), S.cleanPoint({ lat: '10', lon: 190, elev_m: 9500, id: 7,
                                            name: 42, prom_m: 'x' }),
            S.cleanPoint({ lat: 91, lon: 0 }), S.cleanPoint({ lat: 'abc', lon: 0 }),
            S.cleanPoint({ lat: true, lon: 0 }), S.cleanPoint(null), S.cleanPoint('x')],
    wrap: [S.wrapLon(190), S.wrapLon(-190), S.wrapLon(180), S.wrapLon(-180), S.wrapLon(540)],
    url: [S.conditionsUrl('', S.cleanPoint(mb), 7),
          S.conditionsUrl('http://h', { lat: 1, lon: 2, elev_m: null, name: '' }, 12),
          S.conditionsUrl('', { lat: 1, lon: 2, elev_m: null, name: 'A&B=C' }, 0),
          S.conditionsUrl(null, { lat: 1, lon: 2 }, 'x')],
    vurl: [S.verdictsUrl('http://h'), S.verdictsUrl(undefined)],
    pts: S.verdictPoints([{ id: 'a', lat: 1, lon: 2, elev_m: 100, name: 'A' },
                          { id: 'b', lat: 3, lon: 4 },
                          { id: 'a', lat: 5, lon: 6, elev_m: 200 },
                          { lat: 7, lon: 8 }, { id: 'c', lat: 95, lon: 0 }, null, 'x']),
    notList: S.verdictPoints('nope'),
    chunks: S.chunk(points(120), 50).map(function (c) { return c.length; }),
    ages: [null, 0.5, 5, 61, 120, 3000, 'x'].map(S.ageText),
    colors: ['GO', 'MARG', 'NO', '?', 'junk', undefined].map(S.verdictColor),
    ranges: [S.niceRange(0, 37), S.niceRange(null, 3), S.niceRange(5, 5), S.niceRange(-23, 31),
             S.niceRange(0, 49.5)],
    constants: [S.MAX_BATCH, S.VERDICT_TTL_MS, S.STATIC_LINE]
  };
""")


def test_clean_point_and_urls(ran):
    h = ran["helpers"]
    assert h["clean"][0] == {"id": "mb", "name": "Mont Blanc", "lat": 45.83261234,
                             "lon": 6.8652, "elev_m": 4807.6, "prom_m": None}
    # wrapped longitude, an impossible height dropped, ids kept as strings
    assert h["clean"][1] == {"id": "7", "name": "", "lat": 10, "lon": -170, "elev_m": None,
                             "prom_m": None}
    assert h["clean"][2:] == [None] * 5
    assert h["wrap"] == [-170, 170, 180, -180, 180]
    assert h["url"] == [
        "/api/summit/conditions?lat=45.83261&lon=6.8652&elev_m=4808&name=Mont%20Blanc&days=7",
        "http://h/api/summit/conditions?lat=1&lon=2&days=7",
        "/api/summit/conditions?lat=1&lon=2&name=A%26B%3DC&days=1",
        "/api/summit/conditions?lat=1&lon=2&days=7",
    ]
    assert h["vurl"] == ["http://h/api/summit/verdicts", "/api/summit/verdicts"]


def test_verdict_points_dedupe_and_chunk(ran):
    h = ran["helpers"]
    # one entry per id, the last one wins, in first-seen order; no id or a bad lat is out
    assert h["pts"] == [{"id": "a", "lat": 5, "lon": 6, "elev_m": 200},
                        {"id": "b", "lat": 3, "lon": 4, "elev_m": None}]
    assert h["notList"] == []
    assert h["chunks"] == [50, 50, 20]
    assert h["constants"] == [50, 600000, "Summit conditions need the live server."]


def test_age_colour_and_range_helpers(ran):
    h = ran["helpers"]
    assert h["ages"] == ["age unknown", "just now", "5 min old", "1 h 1 min old", "2 h old",
                         "2 d old", "age unknown"]
    assert h["colors"][:4] == ["#3ddc84", "#ffb000", "#ff4d4d", "#888888"]
    assert h["colors"][4:] == ["#888888", "#888888"]
    assert h["ranges"] == [[0, 40], [0, 1], [4, 6], [-40, 40], [0, 50]]


scenario("lines", r"""
  var S = window.SDASummit;
  return {
    sources: CONDITIONS.sources.map(S.sourceLine).concat([S.sourceLine(null),
      S.sourceLine({ id: 'x', read: false, state: 'failed' })]),
    strip: S.stripDays(CONDITIONS).concat(S.stripDays({ days: [{ verdict: 'MAYBE' }, null] })),
    noStrip: S.stripDays(null),
    numbers: S.numberLines(CONDITIONS.days[1]),
    empty: S.numberLines({}),
    us: S.numberLines({ numbers: { wind_mph: 18, aqi: 160, aqi_scale: 'US AQI',
                                   avalanche: 'ESAC 3 Considerable',
                                   nearest_fire: 'Lone 3 mi', nws_day: 'Sunny, 18F' } }),
    intel: S.intelLine(CONDITIONS),
    intelBare: S.intelLine({ days: [] })
  };
""")


def test_source_lines_say_read_age_and_why(ran):
    s = ran["lines"]["sources"]
    assert s[0] == ("Open-Meteo models (ECMWF, GFS, ICON, GEM) (required): read, 12 min old, "
                    "model run 2026-11-20 00:00 UTC")
    assert s[1] == "ECMWF ensemble (required): read, 2 h 10 min old (stale copy: HTTP 500)"
    assert s[2] == "NWS alerts: not checked (outside the US: not checked)"
    assert s[3] == "Open-Meteo air quality: NOT READ (HTTP 502)"
    assert s[4] == "" and s[5] == "x: NOT READ"


def test_strip_and_number_lines(ran):
    r = ran["lines"]
    assert [(d["label"], d["verdict"], d["color"]) for d in r["strip"]] == [
        ("Fri 20 Nov", "MARG", "#ffb000"), ("Sat 21 Nov", "GO", "#3ddc84"),
        ("Sun 22 Nov", "NO", "#ff4d4d"), ("", "?", "#888888"), ("", "?", "#888888")]
    assert r["strip"][1]["top"] == "no limit crossed: wind 12 mph"
    assert r["noStrip"] == []
    assert r["numbers"] == [
        "wind 12 mph", "by model ECMWF 12, GFS 14", "chill -3 F", "temp 12 F", "snow 0.0 in",
        "precip 0.00 in", "cloud 1.0 h", "CAPE 0 J/kg", "freezing level 11,850 ft",
        "surface gust 20 mph", "ensemble p 0.04", "European AQI 15"]
    assert r["empty"] == []
    assert r["us"] == ["wind 18 mph", "US AQI 160", "avalanche ESAC 3 Considerable",
                       "nearest fire Lone 3 mi", "NWS: Sunny, 18F"]
    assert r["intel"] == "Summit conditions: Mont Blanc 4,808 m: Fri MARG, Sat GO, Sun NO"
    assert r["intelBare"] == "Summit conditions: point: "


scenario("meteogram", r"""
  var S = window.SDASummit;
  var m = S.meteogramModel(CONDITIONS, 480, 300, 1);
  var canvas = document.createElement('canvas');
  var drew = S.drawMeteogram(canvas, m);
  var blank = document.createElement('canvas');
  var none = S.meteogramModel({ hourly: { t: [] } }, 480, 300, 0);
  var drewNone = S.drawMeteogram(blank, none);
  return {
    size: [m.width, m.height, m.left, m.right], span: [m.t1 - m.t0, m.t.length],
    panels: m.panels.map(function (p) {
      return { key: p.key, y: [p.ymin, p.ymax], lines: p.lines, top: p.top, bottom: p.bottom,
               series: p.series.map(function (s) { return s.key; }),
               band: !!p.band, bars: p.barMax }; }),
    shades: m.shades.map(function (s) { return [s.t1 - s.t0, s.day, s.selected, s.verdict]; }),
    ticks: m.ticks.map(function (t) { return t.label; }),
    gap: m.panels[3].series[0].values[5],
    drew: drew, ops: { text: painted(canvas.ctx2d), strokes: opCount(canvas.ctx2d, 'stroke'),
                       rects: opCount(canvas.ctx2d, 'fillRect'),
                       fills: opCount(canvas.ctx2d, 'fill') },
    none: none, drewNone: drewNone, blankText: painted(blank.ctx2d),
    junk: S.meteogramModel(null), junk2: S.meteogramModel({ hourly: { t: [5, 5] } })
  };
""")


def test_meteogram_model_has_four_panels_and_the_summit_hours(ran):
    m = ran["meteogram"]
    assert m["size"] == [480, 300, 36, 474]
    assert m["span"] == [71 * 3600, 72]
    keys = [p["key"] for p in m["panels"]]
    assert keys == ["wind", "temp", "wet", "fz"]
    wind, temp, wet, fz = m["panels"]
    assert wind["y"] == [0, 50] and wind["lines"] == [22, 30] and wind["band"] is True
    assert wind["series"] == ["wind", "gust"]
    # the limit lines widen the range, so freezing and both chill limits always show
    assert temp["series"] == ["temp", "chill"] and temp["lines"] == [32, -9, -19]
    assert temp["y"][0] <= -19 and temp["y"][1] >= 32
    assert wet["y"] == [0, 100] and wet["lines"] == [80] and wet["bars"] == 0.12
    assert fz["series"] == ["freezing_ft"] and fz["lines"] == [15774]
    # panels stack top to bottom without overlapping
    assert all(a["bottom"] < b["top"] for a, b in zip(m["panels"], m["panels"][1:],
                                                      strict=False))
    # one shaded 4 h block per day, the selected one marked
    assert m["shades"] == [[14400, 0, False, "MARG"], [14400, 1, True, "GO"],
                           [14400, 2, False, "NO"]]
    assert m["ticks"] == ["Fri 20 Nov", "Sat 21 Nov", "Sun 22 Nov"]
    assert m["gap"] is None  # a missing hour stays a gap, not a zero


def test_meteogram_draws_onto_the_canvas(ran):
    m = ran["meteogram"]
    assert m["drew"] is True
    ops = m["ops"]
    assert ops["rects"] >= 12 + 1  # 3 shades x 4 panels, plus the precip bar
    assert ops["strokes"] >= 4 * 2  # every series line and limit line
    assert ops["fills"] == 1  # the model-range band
    for title in ("Summit wind, mph (median, model range, surface gust)",
                  "Summit temp and wind chill, F", "Freezing level, ft (summit line)",
                  "Fri 20 Nov", "bars to 0.12 in"):
        assert title in ops["text"]
    assert m["none"] is None and m["drewNone"] is False
    assert m["blankText"] == ["No hourly forecast to draw."]
    assert m["junk"] is None and m["junk2"] is None


# --- the drawer -------------------------------------------------------------------

scenario("open", r"""
  route('/api/summit/conditions', CONDITIONS);
  var k = makeCtx();
  var ok = window.SDASummit.init(k.ctx);
  var panel = { title: byTag(k.ctx.mount, 'H3')[0].textContent,
                status: byClass(k.ctx.mount, 'sumt-status')[0].textContent,
                buttons: texts(byTag(k.ctx.mount, 'BUTTON')),
                box: byTag(k.ctx.mount, 'INPUT')[0].checked };
  var p = window.SDASummit.open({ id: 'mb', name: 'Mont Blanc', lat: 45.8326, lon: 6.8652,
                                  elev_m: 4808, prom_m: 4695 });
  var loading = drawer().status;
  var data = await p;
  var first = drawer();
  var styles = byTag(document.head, 'STYLE').length;
  // pick another day
  byClass(frame(), 'sumt-day')[2].fire('click');
  var picked = drawer();
  // a second open replaces the drawer, it does not stack another
  route('/api/summit/conditions', Object.assign(makeConditions(), { name: 'Other' }));
  await window.SDASummit.open({ lat: 1, lon: 2, elev_m: 100 });
  var frames = byClass(document.body, 'sumt-frame').length;
  var styles2 = byTag(document.head, 'STYLE').length;
  // close
  byClass(frame(), 'sumt-btn')[0].fire('click');
  return { ok: ok, panel: panel, loading: loading, gotData: data && data.name, first: first,
           picked: picked, frames: frames, styles: [styles, styles2],
           openAfter: window.SDASummit.isOpen(), urls: urls(), feed: k.feed };
""")


def test_panel_offers_the_view_centre_and_right_click(ran):
    o = ran["open"]
    assert o["ok"] is True
    assert o["panel"]["title"] == "Summit conditions"
    assert o["panel"]["buttons"] == ["Conditions at view centre"]
    assert o["panel"]["box"] is True
    assert "Right-click the globe" in o["panel"]["status"]


def test_open_renders_the_strip_reasons_canvas_and_sources(ran):
    o = ran["open"]
    assert o["loading"] == "Loading seven days of summit conditions..."
    assert o["gotData"] == "Mont Blanc"
    assert o["urls"][0] == ("/api/summit/conditions?lat=45.8326&lon=6.8652&elev_m=4808"
                            "&name=Mont%20Blanc&days=7")
    d = o["first"]
    assert d["title"] == "Mont Blanc, 4,808 m, prominence 4,695 m"
    assert d["sub"] == "45.8326, 6.8652"
    assert d["screen"].startswith("A screen, not a go/no-go")
    assert d["status"] == ""
    assert [(s["text"], s["sel"], s["color"]) for s in d["strip"]] == [
        ("Fri 20 NovMARG", False, "#ffb000"), ("Sat 21 NovGO", True, "#3ddc84"),
        ("Sun 22 NovNO", False, "#ff4d4d")]
    assert d["strip"][1]["title"] == "no limit crossed: wind 12 mph"
    # today's summit hours have passed, so tomorrow opens first
    assert d["head"] == ("Sat 21 Nov: GO over the summit hours 10:00-14:00 local; "
                         "sun 07:42-16:54")
    assert d["reasons"] == ["notewind odds not calibrated here: the plain median limits decide"]
    assert d["nums"].startswith("wind 12 mph; by model ECMWF 12, GFS 14; chill -3 F")
    assert [s["bad"] for s in d["sources"]] == [False, False, False, True]
    assert d["sources"][1]["text"].endswith("(stale copy: HTTP 500)")
    assert "time zone Europe/Rome (fixed offset)" in d["meta"]
    assert "levels 600/500/400 hPa" in d["meta"] and "wind odds not calibrated here" in d["meta"]
    assert "scored 2026-11-20 16:00" in d["meta"]
    assert d["canvas"] == [480, 300]
    assert d["ops"]["clear"] == 1 and d["ops"]["fills"] == 1
    assert "Summit wind, mph (median, model range, surface gust)" in d["ops"]["text"]
    assert o["feed"] == ["Summit conditions: Mont Blanc 4,808 m: Fri MARG, Sat GO, Sun NO",
                         "Summit conditions: Other 4,808 m: Fri MARG, Sat GO, Sun NO"]


def test_picking_a_day_redraws_its_reasons(ran):
    d = ran["open"]["picked"]
    assert [s["sel"] for s in d["strip"]] == [False, False, True]
    assert d["head"] == "Sun 22 Nov: NO over the summit hours 10:00-14:00 local; polar night"
    # text stays text: the angle brackets are shown, never parsed
    assert d["reasons"] == ["NOwind 45 mph (models)", "MARGchill -12 F (models)",
                            "note<b>not markup</b>"]
    assert d["ops"]["clear"] == 2


def test_one_drawer_at_a_time_and_close(ran):
    o = ran["open"]
    assert o["frames"] == 1 and o["styles"] == [1, 1]
    assert o["openAfter"] is False


scenario("stale_answer", r"""
  route('/api/summit/conditions?lat=1&', Object.assign(makeConditions(), { name: 'First' }),
        200, true);
  route('/api/summit/conditions?lat=2&', Object.assign(makeConditions(), { name: 'Second' }));
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var a = window.SDASummit.open({ lat: 1, lon: 1, elev_m: 10 });
  var b = window.SDASummit.open({ lat: 2, lon: 2, elev_m: 10 });
  await b;
  release();
  var ra = await a;
  await settle();
  return { title: drawer().title, a: ra, feed: k.feed };
""")


def test_a_late_answer_for_an_earlier_point_is_dropped(ran):
    s = ran["stale_answer"]
    assert s["title"] == "2.000 N 2.000 E, 10 m"  # the second point, unnamed
    assert s["a"] is None
    assert s["feed"] == ["Summit conditions: Second 4,808 m: Fri MARG, Sat GO, Sun NO"]


scenario("open_failure", r"""
  route('/api/summit/conditions', { detail: 'no elevation for this point' }, 502);
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var r = await window.SDASummit.open({ lat: 10, lon: 10 });
  var failed = drawer();
  route('/api/summit/conditions', { name: 'x', days: 'nope', sources: [] });
  await window.SDASummit.open({ lat: 10, lon: 10 });
  var odd = drawer();
  var bad = [await window.SDASummit.open(null), await window.SDASummit.open({ lat: 'x' })];
  return { r: r, failed: failed.status, strip: failed.strip.length, odd: odd.status,
           oddStrip: odd.strip.length, bad: bad, calls: CALLS.length, feed: k.feed };
""")


def test_a_failed_or_odd_answer_says_so(ran):
    f = ran["open_failure"]
    assert f["r"] is None
    assert f["failed"] == "Conditions did not load (HTTP 502)."
    assert f["strip"] == 0
    assert f["odd"] == "No days in the answer." and f["oddStrip"] == 0
    assert f["bad"] == [None, None] and f["calls"] == 2
    assert f["feed"] == ["Summit conditions: x: "]


# --- static build ---------------------------------------------------------------------

scenario("static", r"""
  route('/api/summit', CONDITIONS);
  var k = makeCtx({ isStatic: true });
  window.SDASummit.init(k.ctx);
  var r = await window.SDASummit.open({ lat: 45.8, lon: 6.8, elev_m: 4808, name: 'Mont Blanc' });
  var shown = drawer();
  await window.SDASummit.open({ lat: 1, lon: 2 });
  var v = await window.SDASummit.verdicts(points(3));
  return { r: r, status: shown.status, screen: shown.screen, strip: shown.strip.length,
           calls: CALLS.length, feed: k.feed, v: v,
           panel: byClass(k.ctx.mount, 'sumt-status')[0].textContent };
""")


def test_static_build_makes_no_requests(ran):
    s = ran["static"]
    assert s["calls"] == 0
    assert s["r"] is None and s["v"] == {}
    assert s["status"] == "Summit conditions need the live server."
    assert s["panel"] == "Summit conditions need the live server."
    assert s["screen"].startswith("A screen, not a go/no-go")
    assert s["strip"] == 0
    assert s["feed"] == ["Summit conditions need the live server."]  # said once


# --- verdicts --------------------------------------------------------------------------

scenario("verdicts", r"""
  route('http://h/api/summit/verdicts', echoVerdicts);
  var k = makeCtx({ api: 'http://h' });
  window.SDASummit.init(k.ctx);
  var pts = points(120);
  var first = await window.SDASummit.verdicts(pts.concat([pts[0]]));
  var posts = CALLS.map(function (c) {
    return { url: c.url, method: c.method, type: c.type, n: c.body.length,
             keys: Object.keys(c.body[0]).sort() }; });
  var again = await window.SDASummit.verdicts(pts.slice(0, 60));
  var afterCache = CALLS.length;
  // a moved point is a new key, the rest still cached
  var moved = await window.SDASummit.verdicts([{ id: 'p0', lat: 47, lon: 6.8, elev_m: 4000 }]);
  var afterMove = CALLS.length;
  CLOCK.shift = 11 * 60 * 1000;
  await window.SDASummit.verdicts(pts.slice(0, 10));
  return { n: Object.keys(first).length, p0: first.p0, p119: first.p119, posts: posts,
           again: Object.keys(again).length, afterCache: afterCache, moved: moved,
           afterMove: afterMove, afterTtl: CALLS.length,
           lastN: CALLS[CALLS.length - 1].body.length, feed: k.feed };
""")


def test_verdicts_post_in_batches_of_50(ran):
    v = ran["verdicts"]
    assert v["n"] == 120
    assert v["p0"] == {"verdict": "GO", "day": "2026-11-21", "when": "tomorrow",
                       "top_reason": "wind 45 mph (models)"}
    assert v["p119"]["verdict"] == "NO"
    assert [p["n"] for p in v["posts"]] == [50, 50, 20]  # the repeated id is sent once
    for p in v["posts"]:
        assert p["url"] == "http://h/api/summit/verdicts" and p["method"] == "POST"
        assert p["type"] == "application/json"
        assert p["keys"] == ["elev_m", "id", "lat", "lon"]
    assert v["feed"] == []  # verdicts feed the peaks layer, not the intel feed


def test_verdicts_are_cached_for_ten_minutes(ran):
    v = ran["verdicts"]
    assert v["again"] == 60 and v["afterCache"] == 3
    assert v["moved"]["p0"]["verdict"] == "NO" and v["afterMove"] == 4
    assert v["afterTtl"] == 5 and v["lastN"] == 10


scenario("inflight", r"""
  route('/api/summit/verdicts', echoVerdicts, 200, true);
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var a = window.SDASummit.verdicts(points(60));
  var b = window.SDASummit.verdicts(points(60).concat(points(2, 'q')));
  var sent = CALLS.map(function (c) { return c.body.length; });
  await tick();
  release();
  await tick();
  release();
  var ra = await a;
  var rb = await b;
  return { sent: sent, total: CALLS.map(function (c) { return c.body.length; }),
           a: Object.keys(ra).length, b: Object.keys(rb).length };
""")


def test_verdicts_share_a_request_already_in_flight(ran):
    f = ran["inflight"]
    assert f["sent"] == [50, 10, 2]  # the second call asks only for the two new ids
    assert f["total"] == [50, 10, 2]
    assert f["a"] == 60 and f["b"] == 62


scenario("verdict_failure", r"""
  route('/api/summit/verdicts', { detail: 'upstream' }, 502);
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var r1 = await window.SDASummit.verdicts(points(3));
  var r2 = await window.SDASummit.verdicts(points(3));
  route('/api/summit/verdicts', echoVerdicts);
  var r3 = await window.SDASummit.verdicts(points(3));
  var none = await window.SDASummit.verdicts('nope');
  return { r1: r1, r2: r2, r3: Object.keys(r3).length, none: none, calls: CALLS.length,
           feed: k.feed };
""")


def test_a_verdict_failure_is_noted_once_and_not_cached(ran):
    f = ran["verdict_failure"]
    assert f["r1"] == {} and f["r2"] == {}
    assert f["r3"] == 3 and f["none"] == {}
    assert f["calls"] == 3
    assert f["feed"] == ["Summit verdicts did not load (HTTP 502)"]


# --- the globe: right-click, view centre, terrain height -----------------------------------

scenario("right_click", r"""
  route('/api/summit/conditions', CONDITIONS);
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var h = HANDLERS[0];
  var reg = { n: HANDLERS.length, canvas: h.canvas === k.ctx.viewer.scene.canvas,
              types: Object.keys(h.actions) };
  h.actions.RIGHT_CLICK({ position: new Cesium.Cartesian2(100, 50) });
  await settle();
  var opened = { url: CALLS[0].url, open: window.SDASummit.isOpen(), rays: RAYS.slice(),
                 heights: HEIGHTS.length };
  var box = byTag(k.ctx.mount, 'INPUT')[0];
  box.checked = false;
  box.fire('change');
  var off = { destroyed: h.destroyed, n: HANDLERS.length };
  window.SDASummit.setRightClick(true);
  var on = { n: HANDLERS.length, live: !HANDLERS[1].destroyed };
  // a second init tears the first handler down
  window.SDASummit.init(makeCtx().ctx);
  return { reg: reg, opened: opened, off: off, on: on, afterInit: HANDLERS[1].destroyed,
           handlers: HANDLERS.length };
""")


scenario("right_click_yields_to_map_tools", r"""
  window.SDAMapTools = { init: function () { return true; } };
  var k = makeCtx();
  window.SDASummit.init(k.ctx);
  var box = byTag(k.ctx.mount, 'INPUT')[0];
  var before = { n: HANDLERS.length, checked: box.checked };
  window.SDASummit.setRightClick(true);
  var after = { n: HANDLERS.length, checked: box.checked };
  delete window.SDAMapTools;
  return { before: before, after: after };
""")


def test_right_click_is_left_to_the_map_tools_popup(ran):
    """With the map tools loaded their popup owns right-click (it carries a
    CONDITIONS button); two handlers would open both at once. The checkbox
    reflects that and turning it on still works."""
    r = ran["right_click_yields_to_map_tools"]
    assert r["before"] == {"n": 0, "checked": False}
    assert r["after"] == {"n": 1, "checked": True}


def test_right_click_opens_the_drawer_where_clicked(ran):
    r = ran["right_click"]
    assert r["reg"] == {"n": 1, "canvas": True, "types": ["RIGHT_CLICK"]}
    # (100, 50) on the fake globe is 45.5 N 7 E; the ellipsoid has no height to give
    assert r["opened"]["url"] == "/api/summit/conditions?lat=45.5&lon=7&days=7"
    assert r["opened"]["open"] is True and r["opened"]["rays"] == [[100, 50]]
    assert r["opened"]["heights"] == 0
    assert r["off"] == {"destroyed": True, "n": 1}
    assert r["on"] == {"n": 2, "live": True}
    assert r["afterInit"] is True and r["handlers"] == 3


scenario("view_centre", r"""
  route('/api/summit/conditions', CONDITIONS);
  var k = makeCtx({ terrain: true, height: 4790.4 });
  window.SDASummit.init(k.ctx);
  byTag(k.ctx.mount, 'BUTTON')[0].fire('click');
  await settle();
  var first = { url: CALLS[0].url, rays: RAYS.slice(), heights: HEIGHTS.slice() };
  var off = makeCtx({ offGlobe: true });
  window.SDASummit.init(off.ctx);
  var r = window.SDASummit.viewCentre();
  var noPick = makeCtx({ noPick: true, terrain: true });
  window.SDASummit.init(noPick.ctx);
  window.SDASummit.viewCentre();
  await settle();
  return { first: first, off: r, offStatus: byClass(off.ctx.mount, 'sumt-status')[0].textContent,
           calls: urls() };
""")


def test_view_centre_button_uses_the_middle_of_the_view_and_terrain_height(ran):
    v = ran["view_centre"]
    assert v["first"]["rays"] == [[400, 300]]
    assert v["first"]["heights"] == [{"lon": 10, "lat": 48}]
    assert v["first"]["url"] == "/api/summit/conditions?lat=48&lon=10&elev_m=4790&days=7"
    assert v["off"] is None and v["offStatus"] == "That point is off the globe."
    # globe.pick missed but the ellipsoid caught it; no height came back, so none is sent
    assert v["calls"] == [v["first"]["url"], "/api/summit/conditions?lat=48&lon=10&days=7"]


# --- before init, guards, text rules ------------------------------------------------------

scenario("before_init", r"""
  var FEED = [];
  window.API = 'http://x';
  window.addIntelItem = function (t) { FEED.push(t); };
  route('http://x/api/summit', CONDITIONS);
  route('http://x/api/summit/verdicts', echoVerdicts);
  var data = await window.SDASummit.open({ lat: 45.8326, lon: 6.8652, elev_m: 4808 });
  var v = await window.SDASummit.verdicts(points(2));
  return { name: data && data.name, urls: urls(), v: Object.keys(v), feed: FEED,
           open: window.SDASummit.isOpen(), handlers: HANDLERS.length,
           centre: window.SDASummit.viewCentre() };
""")


def test_open_and_verdicts_work_before_init(ran):
    b = ran["before_init"]
    assert b["name"] == "Mont Blanc"
    assert b["urls"] == [
        "http://x/api/summit/conditions?lat=45.8326&lon=6.8652&elev_m=4808&days=7",
        "http://x/api/summit/verdicts"]
    assert b["v"] == ["p0", "p1"]
    assert b["feed"] == ["Summit conditions: Mont Blanc 4,808 m: Fri MARG, Sat GO, Sun NO"]
    assert b["open"] is True
    assert b["handlers"] == 0 and b["centre"] is None  # no viewer, so no globe hooks


scenario("before_init_static", r"""
  window.STATIC_DEMO = true;
  route('/api/summit', CONDITIONS);
  var r = await window.SDASummit.open({ lat: 1, lon: 2 });
  return { r: r, calls: CALLS.length, status: drawer().status };
""")


def test_static_flag_is_read_before_init_too(ran):
    s = ran["before_init_static"]
    assert s == {"r": None, "calls": 0, "status": "Summit conditions need the live server."}


scenario("guards", r"""
  var k = makeCtx({ noMount: true });
  var noMount = window.SDASummit.init(k.ctx);
  var noViewer = window.SDASummit.init({ mount: new El('div') });
  var saved = window.Cesium;
  window.Cesium = undefined;
  var noCesium = window.SDASummit.init(makeCtx().ctx);
  window.SDASummit.setRightClick(true);
  window.Cesium = saved;
  return { noMount: noMount, noViewer: noViewer, noCesium: noCesium, nothing:
           window.SDASummit.init(null), handlers: HANDLERS.length };
""")


def test_init_guards(ran):
    g = ran["guards"]
    # one handler from the first init; none without a viewer or without Cesium
    assert g == {"noMount": True, "noViewer": True, "noCesium": True, "nothing": False,
                 "handlers": 1}


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "summit.js must stay ASCII"
    assert "global.SDASummit" in text or "window.SDASummit" in text
    assert "eval(" not in text and "new Function" not in text
