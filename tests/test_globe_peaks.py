"""Tests for the globe mountain module (static/js/peaks.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_cameras.py does it. The
fake `fetch` answers from a table, so nothing here touches the network, and a fake
`window.SDASummit` stands in for the conditions module. Skips cleanly when Node is
not installed.

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
    / "src" / "sda" / "templates" / "static" / "js" / "peaks.js"
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
var countEl = new El('span'); countEl.id = 'count-peaks'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var Cesium = {
  PointPrimitiveCollection: Coll, LabelCollection: Coll, BillboardCollection: Coll,
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 };
    } },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  NearFarScalar: function () {},
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0 },
  VerticalOrigin: { CENTER: 0, BOTTOM: 1 },
  Cartographic: { fromCartesian: function (p) {
    return { latitude: p.lat * Math.PI / 180, longitude: p.lon * Math.PI / 180,
      height: (p.h || 0) };
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

// ---- fake conditions module (window.SDASummit) ------------------------------
var SUMMIT_ASKED = [];
var SUMMIT_OPENED = [];
function installSummit(answer) {
  window.SDASummit = {
    open: function (p) { SUMMIT_OPENED.push(p); },
    verdicts: function (points) {
      SUMMIT_ASKED.push(points.map(function (p) { return p.id; }));
      return Promise.resolve(answer ? answer(points) : {});
    }
  };
}

var SUMMARY = { available: true, count: 11866713, named: 1203411, with_prominence: 11866713,
  with_isolation: 4432568, geonames_only: 312000, country_count: 247,
  attribution: 'Summits: Kirmse and de Ferranti 2023. Names: GeoNames (CC BY 4.0).' };

var WHITNEY = { id: 3, name: 'Mount Whitney', lat: 30.27, lon: -97.74, elev_m: 4421,
  prom_m: 3072, iso_km: 2649.4, country: 'US', region: 'California' };
var UNNAMED = { id: 8, name: '', lat: 30.5, lon: -97.5, elev_m: 4200, prom_m: 150,
  iso_km: null, country: '', region: '' };
var KNOB = { id: 9, name: 'Neighbour Knob', lat: 30.271, lon: -97.741, elev_m: 3000,
  prom_m: 40, iso_km: null, country: 'US', region: 'California' };

function makeViewer(heightM) {
  var moveEnd = [];
  var prims = [];
  var d = Math.PI / 180;
  return {
    scene: { primitives: { add: function (c) { prims.push(c); return c; } },
      globe: { ellipsoid: {} } },
    canvas: { clientWidth: 800, clientHeight: 600 },
    camera: {
      positionCartographic: { height: heightM, latitude: 30.27 * Math.PI / 180,
        longitude: -97.74 * Math.PI / 180 },
      rect: { west: -98 * d, south: 30 * d, east: -97 * d, north: 31 * d },
      computeViewRectangle: function () { return this.rect; },
      moveEnd: { addEventListener: function (fn) { moveEnd.push(fn); } },
      flyTo: function (o) { this.flown = o; },
      pickEllipsoid: function () { return { lon: -97.74, lat: 30.27, h: 0 }; }
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

// The module adds three collections: density points, markers, labels.
function layers(k) {
  var p = k.ctx.viewer._prims;
  return { density: p[0], marks: p[1], labels: p[2] };
}
function markOf(k, id) {
  return layers(k).marks.items.filter(function (b) { return b.id.id === id; })[0];
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
              JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'peaks.js' });
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
      // stop the verdict timer the scenario left running
      try { vm.runInContext('window.SDAPeaks && window.SDAPeaks.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("peaks")
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
  var P = window.SDAPeaks;
  var box = { west: -1, south: -1, east: 1, north: 1 };
  var spread = [{ id: 4, lat: 0.9, lon: 0.9 }, { id: 2, lat: 0.01, lon: 0.01 },
                { id: 3, lat: 0.5, lon: 0.5 }, { id: 1, lat: 0, lon: 0 }];
  var sixty = [];
  for (var i = 60; i >= 1; i--) sixty.push({ id: i, lat: i / 100, lon: 0, elev_m: 100 });
  var shown = [{ id: 1, lat: 0, lon: 0, elev_m: 10 }, { id: 2, lat: 1, lon: 1 },
               { id: 3, lat: 2, lon: 2, elev_m: 30 }];
  return {
    modes: [2e7, 1.6e6, 1.5e6, 1e4].map(P.modeForHeight),
    zooms: [4e7, 2e7, 1.5e6, 0, 1e-9].map(P.zoomForHeight),
    cells: [0, 1, 2.5, 4.7, 6, 12].map(P.cellForZoom),
    q: P.query([['bbox', '-180,-90,180,90'], ['b', ''], ['c', null], ['zoom', 1]]),
    elev: [P.formatElev(4421), P.formatElev(8848.9), P.formatElev(null)],
    prom: [P.formatProm(3072), P.formatProm(undefined)],
    counts: [999, 1500, 25000, 11866713].map(P.formatCount),
    markers: ['GO', 'MARG', 'NO', '?'].map(function (v) {
      var mk = P.markerFor({ verdict: v }); return [mk.key, mk.shape, mk.color]; }),
    none: [P.markerFor(null).key, P.markerFor({ verdict: 'BAD' }).key,
           P.markerFor({ verdict: 'toString' }).key, P.markerFor(null).shape],
    icons: Object.keys(P.ICONS).map(function (k) { return P.ICONS[k].indexOf(
      'data:image/svg+xml,') === 0; }),
    iconGo: decodeURIComponent(P.ICONS.GO),
    label: [P.peakLabel(WHITNEY, { verdict: 'GO', day: '2026-11-21' }),
            P.peakLabel(UNNAMED, null), P.peakLabel(KNOB, { verdict: '?', day: '' })],
    declutter: [P.declutter(spread, box, 40), P.declutter(spread, box, 2),
                P.declutter([], box, 5), P.declutter(spread, null, 5)],
    clean: P.cleanVerdicts({ '1': { verdict: 'GO', day: '2026-11-20' },
      '2': { verdict: 'MARG', day: 'tomorrow' }, '3': { verdict: 'none' },
      '4': { verdict: '<b>x</b>' }, 'x': { verdict: 'GO' }, '5': null,
      '6': { verdict: '?', day: '2026-11-21' }, '-1': { verdict: 'NO' }, '2.5': { verdict: 'GO' },
      '7': 'GO' }),
    cleanBad: [P.cleanVerdicts(null), P.cleanVerdicts('GO')],
    points: P.verdictPoints(shown, { 1: 0 }, 1000),
    pointsLater: P.verdictPoints(shown, { 1: 0, 2: 5 }, P.VERDICT_TTL_MS + 1)
      .map(function (p) { return p.id; }),
    top50: P.verdictPoints(sixty, {}, 0).map(function (p) { return p.id; }),
    ttl: P.VERDICT_TTL_MS, max: P.VERDICT_MAX
  };
""")


def test_view_helpers(ran):
    h = ran["helpers"]
    assert h["modes"] == ["density", "density", "detail", "detail"]
    assert h["zooms"] == [0, 1, 4.7, 22, 22]
    assert h["cells"] == [16, 8, 4, 1, 0.25, 0.25]
    assert h["q"] == "?bbox=-180%2C-90%2C180%2C90&zoom=1"
    assert h["counts"] == ["999", "1.5k", "25k", "11.9M"]


def test_units_are_metres_and_feet(ran):
    h = ran["helpers"]
    assert h["elev"] == ["4,421 m / 14,505 ft", "8,849 m / 29,032 ft", "height unknown"]
    assert h["prom"] == ["prom 3,072 m / 10,079 ft", "prominence unknown"]
    assert h["label"] == [
        "Mount Whitney\n4,421 m / 14,505 ft\nGO 2026-11-21",
        "4,200 m / 13,780 ft",
        "Neighbour Knob\n3,000 m / 9,843 ft\n?",
    ]


def test_markers_follow_the_peak_map(ran):
    h = ran["helpers"]
    assert h["markers"] == [
        ["GO", "circle", "#0ca30c"],
        ["MARG", "diamond", "#fab219"],
        ["NO", "square", "#d03b3b"],
        ["?", "triangle", "#6b6a66"],
    ]
    assert h["none"] == ["none", "none", "none", "triangle"]
    assert all(h["icons"]) and len(h["icons"]) == 5
    assert h["iconGo"].startswith("data:image/svg+xml,<svg ") and "#0ca30c" in h["iconGo"]


def test_declutter_keeps_the_most_prominent(ran):
    d = ran["helpers"]["declutter"]
    assert d[0] == [1, 3, 4]  # 2 sits on top of 1
    assert d[1] == [1, 3]
    assert d[2] == [] and d[3] == []


def test_verdict_answers_are_cleaned(ran):
    h = ran["helpers"]
    assert h["clean"] == {
        "1": {"verdict": "GO", "day": "2026-11-20"},
        "2": {"verdict": "MARG", "day": ""},
        "6": {"verdict": "?", "day": "2026-11-21"},
    }
    assert h["cleanBad"] == [{}, {}]


def test_verdict_points(ran):
    h = ran["helpers"]
    assert h["points"] == [{"id": 2, "lat": 1, "lon": 1, "elev_m": None},
                           {"id": 3, "lat": 2, "lon": 2, "elev_m": 30}]
    assert h["pointsLater"] == [1, 3]  # 2 was asked 5 ms after 1
    assert h["top50"] == list(range(1, 51))
    assert h["ttl"] == 600000 and h["max"] == 50


# --- density mode ------------------------------------------------------------------

scenario("density", r"""
  installSummit(function () { return { 3: { verdict: 'GO', day: '2026-11-21' } }; });
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks/density', { cell_deg: 8, total: 11866713, cells: [
    { lat: 30, lon: -97, count: 1200, top: WHITNEY },
    { lat: 51, lon: 0, count: 100, top: UNNAMED },
    { lat: -10, lon: 20, count: 30, top: KNOB }] });
  var k = makeCtx(2e7);
  var ok = window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  var L = layers(k);
  return {
    ok: ok, requests: REQUESTS, count: countEl.textContent,
    summary: byClass(k.ctx.mount, 'peaks-summary')[0].textContent,
    attr: byClass(k.ctx.mount, 'peaks-attr')[0].textContent,
    view: byClass(k.ctx.mount, 'peaks-view')[0].textContent,
    clusters: L.density.items.length,
    marks: L.marks.items.map(function (b) { return [b.id.id, b.image === window.SDAPeaks.ICONS[
      b.id.id === 3 ? 'GO' : 'none']]; }),
    labels: L.labels.items.map(function (l) { return l.text; }),
    asked: SUMMIT_ASKED, feed: k.feed
  };
""")


def test_density_mode(ran):
    d = ran["density"]
    assert d["ok"] is True
    assert d["requests"][0] == "/api/peaks/summary"
    assert d["requests"][1] == "/api/peaks/density?bbox=-180%2C-90%2C180%2C90&zoom=1"
    assert len(d["requests"]) == 2
    assert d["count"] == "11.9M"
    assert "11,866,713 summits worldwide" in d["summary"] and "247 countries" in d["summary"]
    assert "GeoNames" in d["attr"]
    assert "11,866,713 summits in view" in d["view"]
    assert d["clusters"] == 3
    # the top peak of each cluster is a marker; Whitney turned GO when its verdict came
    assert sorted(m[0] for m in d["marks"]) == [3, 8, 9]
    assert all(m[1] for m in d["marks"])
    # count labels only for clusters of 50+ without a peak label of their own: none here
    assert "Mount Whitney\n4,421 m / 14,505 ft\nGO 2026-11-21" in d["labels"]
    assert d["asked"] == [[3, 8, 9]]
    assert any("11,866,713 summits" in line for line in d["feed"])


# --- detail mode, verdicts and the conditions drawer ------------------------------------

scenario("detail", r"""
  var T = 1e12;
  window.SDAPeaks._setClock(function () { return T; });
  installSummit(function () {
    return { 3: { verdict: 'GO', day: '2026-11-21' }, 8: { verdict: 'NO', day: '2026-11-21' },
             99: { verdict: 'GO' } };
  });
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks?', { peaks: [WHITNEY, UNNAMED, KNOB], count: 3, total: 3,
    total_capped: false, capped: false });
  var k = makeCtx(50000);
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  var listReq = REQUESTS.filter(function (u) { return u.indexOf('/api/peaks?') === 0; });
  var L = layers(k);
  var first = {
    images: [3, 8, 9].map(function (id) { return markOf(k, id).image; }),
    labels: L.labels.items.map(function (l) { return l.text; }),
    view: byClass(k.ctx.mount, 'peaks-view')[0].textContent, count: countEl.textContent
  };
  // the page's click handler calls picked.id.sdaPick()
  markOf(k, 9).id.sdaPick();
  await settle();
  var picked = {
    opened: SUMMIT_OPENED, info: byClass(k.ctx.mount, 'peaks-info')[0].textContent,
    width: markOf(k, 9).width, labels: layers(k).labels.items.length
  };
  // same view again: nothing new to ask
  window.SDAPeaks.refresh();
  await settle();
  var askedSame = SUMMIT_ASKED.length;
  // a new peak comes into view: only it is asked about
  route('/api/peaks?', { peaks: [WHITNEY, UNNAMED, KNOB,
    { id: 20, name: 'New One', lat: 30.8, lon: -97.2, elev_m: 1000, prom_m: 20 }], count: 4,
    total: 4, total_capped: false, capped: false });
  window.SDAPeaks.refresh();
  await settle();
  var askedNew = SUMMIT_ASKED.slice();
  // ten minutes later the timer asks again for everything in view, once
  T += window.SDAPeaks.VERDICT_TTL_MS;
  var p = window.SDAPeaks.recheck();
  await settle();
  var again = window.SDAPeaks.recheck();
  return { listReq: listReq, first: first, picked: picked, askedSame: askedSame,
           askedNew: askedNew, askedLater: SUMMIT_ASKED, recheckAgain: again,
           icons: window.SDAPeaks.ICONS, feed: k.feed };
""")


def test_detail_mode_draws_peaks_with_verdicts(ran):
    d = ran["detail"]
    assert len(d["listReq"]) == 1
    req = d["listReq"][0]
    assert req.startswith("/api/peaks?bbox=-98.1%2C29.9%2C-96.9%2C31.1&limit=300")
    assert "min_prom" not in req
    icons = d["icons"]
    assert d["first"]["images"] == [icons["GO"], icons["NO"], icons["none"]]
    # Knob sits under Whitney's label, so it is not labelled
    assert d["first"]["labels"] == ["Mount Whitney\n4,421 m / 14,505 ft\nGO 2026-11-21",
                                    "4,200 m / 13,780 ft\nNO 2026-11-21"]
    assert d["first"]["count"] == "3"
    assert "3 summits in view" in d["first"]["view"]


def test_click_opens_conditions(ran):
    p = ran["detail"]["picked"]
    assert p["opened"] == [{"id": 9, "name": "Neighbour Knob", "lat": 30.271, "lon": -97.741,
                            "elev_m": 3000, "prom_m": 40}]
    assert p["info"] == ("Neighbour Knob | 3,000 m / 9,843 ft | prom 40 m / 131 ft | "
                         "California, US")
    assert p["width"] == 22  # the selected marker is drawn larger
    assert p["labels"] == 3  # and always labelled


def test_verdicts_are_asked_only_when_the_view_changes_or_go_stale(ran):
    d = ran["detail"]
    assert d["askedSame"] == 1
    assert d["askedNew"] == [[3, 8, 9], [20]]
    assert d["askedLater"] == [[3, 8, 9], [20], [3, 8, 9, 20]]
    assert d["recheckAgain"] is None


scenario("no_summit", r"""
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks?', { peaks: [WHITNEY], count: 1, total: 1, total_capped: false,
    capped: false });
  var k = makeCtx(50000);
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  var image = markOf(k, 3).image;
  markOf(k, 3).id.sdaPick();
  await settle();
  return { neutral: image === window.SDAPeaks.ICONS.none,
           status: byClass(k.ctx.mount, 'peaks-status')[0].textContent,
           info: byClass(k.ctx.mount, 'peaks-info')[0].textContent };
""")


def test_works_without_the_conditions_module(ran):
    r = ran["no_summit"]
    assert r["neutral"] is True
    assert r["status"] == "Mountain conditions are not loaded on this page."
    assert r["info"].startswith("Mount Whitney | 4,421 m / 14,505 ft")


scenario("verdict_failure", r"""
  window.SDASummit = { open: function () { throw new Error('drawer broke'); },
    verdicts: function () { throw new Error('no forecast'); } };
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks?', { peaks: [WHITNEY], count: 1, total: 1, total_capped: false,
    capped: false });
  var k = makeCtx(50000);
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  var afterVerdicts = byClass(k.ctx.mount, 'peaks-status')[0].textContent;
  markOf(k, 3).id.sdaPick();
  return { afterVerdicts: afterVerdicts,
           afterOpen: byClass(k.ctx.mount, 'peaks-status')[0].textContent,
           marks: layers(k).marks.items.length };
""")


def test_a_failing_conditions_module_does_not_break_the_layer(ran):
    r = ran["verdict_failure"]
    assert r["afterVerdicts"] == "Conditions did not load for the peaks in view."
    assert r["afterOpen"] == "Conditions did not open for Mount Whitney."
    assert r["marks"] == 1


# --- panel actions ---------------------------------------------------------------------

scenario("search", r"""
  installSummit(null);
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks/search', { peaks: [WHITNEY], count: 1 });
  var k = makeCtx(2e7);
  window.SDAPeaks.init(k.ctx);
  var input = byClass(k.ctx.mount, 'peaks-search')[0];
  input.value = 'w';
  input.fire('input');
  input.value = 'whit';
  input.fire('input');
  await wait(340);
  await settle();
  var items = byClass(k.ctx.mount, 'peaks-item');
  var text = items.map(function (b) { return b.textContent; });
  items[0].fire('click');
  await settle();
  return { requests: REQUESTS, items: text, flown: k.ctx.viewer.camera.flown.destination,
           opened: SUMMIT_OPENED.map(function (p) { return p.id; }),
           status: byClass(k.ctx.mount, 'peaks-status')[0].textContent };
""")


def test_search_flies_to_the_peak_and_opens_conditions(ran):
    r = ran["search"]
    assert r["requests"] == ["/api/peaks/search?q=whit&limit=20"]
    assert r["items"] == ["Mount Whitney4,421 m | California, US"]
    assert r["flown"] == {"lon": -97.74, "lat": 30.27, "h": 30000}
    assert r["opened"] == [3]
    assert r["status"] == '1 mountains match "whit".'


scenario("prominence_and_near", r"""
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks?', { peaks: [WHITNEY], count: 1, total: 1, total_capped: false,
    capped: false });
  route('/api/peaks/nearest', { peaks: [Object.assign({ distance_km: 0.4 }, KNOB)], count: 1 });
  var CALLS = [];
  var box = new El('input'); box.id = 'layer-peaks'; document.body.appendChild(box);
  window.toggleLayer = function (name, on) { CALLS.push([name, on]);
    window.SDAPeaks.setEnabled(on); };
  var k = makeCtx(50000);
  window.SDAPeaks.init(k.ctx);
  // a filter button with the layer off turns the layer on through the page
  buttonNamed(k.ctx.mount, '1000 M+').fire('click');
  await settle();
  var pressed = byTag(k.ctx.mount, 'BUTTON').filter(function (b) {
    return b.getAttribute('aria-pressed') === 'true'; }).map(function (b) {
    return b.textContent; });
  buttonNamed(k.ctx.mount, 'NEAR VIEW').fire('click');
  await settle();
  return { calls: CALLS, checked: box.checked, pressed: pressed,
           enabled: window.SDAPeaks.isEnabled(), requests: REQUESTS,
           items: byClass(k.ctx.mount, 'peaks-item').map(function (b) { return b.textContent; }) };
""")


def test_prominence_filter_and_near_view(ran):
    r = ran["prominence_and_near"]
    assert r["calls"] == [["peaks", True]] and r["checked"] is True
    assert r["enabled"] is True
    assert r["pressed"] == ["1000 M+"]
    lists = [u for u in r["requests"] if u.startswith("/api/peaks?")]
    assert len(lists) == 1 and lists[0].endswith("&limit=300&min_prom=1000")
    near = [u for u in r["requests"] if u.startswith("/api/peaks/nearest")]
    assert near == ["/api/peaks/nearest?lat=30.27&lon=-97.74&n=20&radius_km=100&min_prom=1000"]
    assert r["items"] == ["Neighbour Knob3,000 m | 0.4 km"]


# --- enable, disable, static build, no catalog ----------------------------------------

scenario("disable", r"""
  installSummit(null);
  route('/api/peaks/summary', SUMMARY);
  route('/api/peaks?', { peaks: [WHITNEY, KNOB], count: 2, total: 2, total_capped: false,
    capped: false });
  var k = makeCtx(50000);
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  var before = { count: countEl.textContent, marks: layers(k).marks.items.length };
  window.SDAPeaks.setEnabled(false);
  await settle();
  return { before: before, after: countEl.textContent, marks: layers(k).marks.items.length,
           shown: layers(k).marks.show, view: byClass(k.ctx.mount, 'peaks-view')[0].textContent,
           recheck: window.SDAPeaks.recheck(), requests: REQUESTS.length,
           asked: SUMMIT_ASKED.length };
""")


def test_disable_clears(ran):
    r = ran["disable"]
    assert r["before"] == {"count": "2", "marks": 2}
    assert r["after"] == "0" and r["marks"] == 0 and r["shown"] is False and r["view"] == ""
    assert r["recheck"] is None
    assert r["requests"] == 2 and r["asked"] == 1


scenario("static", r"""
  installSummit(null);
  var k = makeCtx(50000, { isStatic: true });
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  var input = byClass(k.ctx.mount, 'peaks-search')[0];
  input.value = 'whitney';
  input.fire('input');
  buttonNamed(k.ctx.mount, 'NEAR VIEW').fire('click');
  await wait(340);
  await settle();
  return { requests: REQUESTS.length, asked: SUMMIT_ASKED.length,
           summary: byClass(k.ctx.mount, 'peaks-summary')[0].textContent,
           status: byClass(k.ctx.mount, 'peaks-status')[0].textContent };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == 0 and r["asked"] == 0
    assert r["summary"] == "The peak catalog needs the live server."
    assert r["status"] == "The peak catalog needs the live server."


scenario("missing_catalog", r"""
  route('/api/peaks/summary', { available: false, count: 0,
    hint: 'Run: python -m sda.peaks import' });
  var k = makeCtx(2e7);
  window.SDAPeaks.init(k.ctx);
  window.SDAPeaks.setEnabled(true);
  await settle();
  return { requests: REQUESTS, summary: byClass(k.ctx.mount, 'peaks-summary')[0].textContent };
""")


def test_missing_catalog_says_how_to_import(ran):
    r = ran["missing_catalog"]
    assert r["requests"] == ["/api/peaks/summary"]  # no 503s from the data endpoints
    assert "python -m sda.peaks import" in r["summary"]


scenario("init_guards", r"""
  var noMount = window.SDAPeaks.init({ viewer: makeViewer(1) });
  var noViewer = window.SDAPeaks.init({ mount: new El('div') });
  window.SDAPeaks.setEnabled(true);  // nothing initialised: a no-op
  var k = makeCtx(2e7);
  var ok = window.SDAPeaks.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  var collapsed = k.ctx.mount.children[0].has('collapsed');
  return { noMount: noMount, noViewer: noViewer, ok: ok, collapsed: collapsed,
           title: head.textContent, style: !!document.getElementById('sda-peaks-style') };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert r == {"noMount": False, "noViewer": False, "ok": True, "collapsed": True,
                 "title": "Mountains", "style": True}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "peaks.js must stay ASCII"
