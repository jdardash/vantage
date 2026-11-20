"""Tests for the globe aurora oval module (static/js/aurora.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_cameras.py does it. The
fake `fetch` answers from a table and `setInterval` only records its callback, so
nothing here touches the network or waits ten minutes. Skips cleanly when Node is not
installed.

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
    / "src" / "sda" / "templates" / "static" / "js" / "aurora.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.selected = false;
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

// a canvas that records what was painted into it
function Ctx2d(canvas) { this.canvas = canvas; this.draws = []; this.filter = 'none';
  this.puts = []; }
Ctx2d.prototype.createImageData = function (w, h) {
  return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; };
Ctx2d.prototype.putImageData = function (img, x, y) { this.puts.push(img);
  this.canvas.image = img; };
Ctx2d.prototype.drawImage = function (src, x, y, w, h) {
  this.draws.push({ src: src, x: x, y: y, w: w, h: h, filter: this.filter }); };
var CANVASES = [];
var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) {
    var e = new El(tag);
    if (e.tagName === 'CANVAS') {
      e.width = 300; e.height = 150; CANVASES.push(e);
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
var countEl = new El('span'); countEl.id = 'count-aurora'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Opts(o) { for (var k in o) this[k] = o[k]; }
function EllipsoidSurfaceAppearance(o) { Opts.call(this, o); }
EllipsoidSurfaceAppearance.VERTEX_FORMAT = 'POSITION_AND_ST';
var Cesium = {
  PrimitiveCollection: Coll,
  Primitive: function (o) { Opts.call(this, o); },
  GeometryInstance: function (o) { Opts.call(this, o); },
  RectangleGeometry: function (o) { Opts.call(this, o); },
  EllipsoidSurfaceAppearance: EllipsoidSurfaceAppearance,
  Rectangle: { fromDegrees: function (w, s, e, n) { return [w, s, e, n]; } },
  Material: { fromType: function (type, uniforms) { return { type: type,
    uniforms: uniforms }; } },
  Color: function (r, g, b, a) { this.red = r; this.green = g; this.blue = b; this.alpha = a; }
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
        json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r.body))); } });
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () {
    return Promise.resolve({}); } });
}

var OVAL = {
  observation_time: '2026-11-19T12:00:00Z', forecast_time: '2026-11-19T12:30:00Z',
  cells: [[20, 68, 42], [-160, 65, 61], [-1, 70, 7], [180, 66, 5], [100, -65, 18]],
  count: 5, max_prob: 61, hemisphere_max: { north: 61, south: 18 },
  source: 'NOAA SWPC OVATION Prime', stale: false
};

function makeViewer() {
  var added = [];
  return {
    scene: { primitives: { add: function (c) { added.push(c); return c; } } },
    _added: added
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(), api: opts.api || '', isStatic: !!opts.isStatic,
    mount: opts.noMount ? undefined : new El('div'),
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

// the hemisphere shells currently in the layer
function bands(k) {
  var c = k.ctx.viewer._added[0];
  return c ? c.items : [];
}
function drawn() { return Number(countEl.textContent); }
function band(p) {
  var geo = p.geometryInstances.geometry;
  var tex = p.appearance.material.uniforms.image;
  var draws = tex.ctx2d.draws;
  return { hemi: p.geometryInstances.id.hemisphere, rect: geo.rectangle, height: geo.height,
           format: geo.vertexFormat, aboveGround: p.appearance.aboveGround,
           translucent: p.appearance.translucent, flat: p.appearance.flat,
           material: p.appearance.material.type,
           alpha: (p.appearance.material.uniforms.color || {}).alpha,
           size: [tex.width, tex.height], small: [draws[0].src.width, draws[0].src.height],
           xs: draws.map(function (d) { return d.x; }), filter: draws[0].filter,
           sync: p.asynchronous };
}
function status(k) { return byClass(k.ctx.mount, 'aurora-status')[0].textContent; }

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
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'aurora.js' });
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
      try { vm.runInContext('window.SDAAurora && window.SDAAurora.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("aurora")
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

scenario("ramp", r"""
  var A = window.SDAAurora;
  var probs = [0, 1, 5, 10, 20, 30, 40, 50, 75, 100];
  return {
    colors: probs.map(A.colorFor),
    alphas: probs.map(A.alphaFor),
    odd: [NaN, -5, Infinity, '50', null, 150].map(A.colorFor),
    oddAlpha: [NaN, -5, Infinity, '50', null].map(A.alphaFor),
    alpha150: A.alphaFor(150)
  };
""")


def test_colour_ramp_runs_green_to_yellow_to_red(ran):
    c = dict(zip([0, 1, 5, 10, 20, 30, 40, 50, 75, 100], ran["ramp"]["colors"], strict=True))
    # transparent green at the bottom
    assert c[0]["a"] == 0 and c[0]["g"] > c[0]["r"]
    # bright green at 10 %
    assert c[10]["g"] > 0.85 and c[10]["r"] < 0.05
    # yellow at 30 %: red and green both high, no blue
    assert c[30]["r"] == 1 and c[30]["g"] > 0.8 and c[30]["b"] == 0
    # red from 50 %
    for p in (50, 75, 100):
        assert c[p]["r"] == 1 and c[p]["g"] < 0.1
    # between stops the colour is interpolated, not stepped
    assert c[10]["r"] < c[20]["r"] < c[30]["r"]
    assert c[30]["g"] > c[40]["g"] > c[50]["g"]
    for col in c.values():
        assert all(0 <= col[k] <= 1 for k in "rgba")


def test_alpha_rises_with_probability(ran):
    alphas = ran["ramp"]["alphas"]
    assert alphas[0] == 0
    assert all(a < b for a, b in zip(alphas[:7], alphas[1:8], strict=True))
    assert alphas == sorted(alphas)
    assert max(alphas) <= 0.8 and alphas[2] < 0.25  # fainter at the edge of the oval
    assert ran["ramp"]["alpha150"] == alphas[-1]


def test_ramp_survives_junk(ran):
    r = ran["ramp"]
    assert r["oddAlpha"] == [0, 0, 0, 0, 0]
    assert all(col["a"] == 0 for col in r["odd"][:5])
    assert r["odd"][5]["a"] > 0  # 150 is clamped to 100, not dropped


scenario("threshold_helpers", r"""
  var A = window.SDAAurora;
  var cells = [
    [20, 68, 42], [200, 65, 61], [-1, 70, 4], [10, 60, 5], [10, 60, 101], [10, 91, 50],
    [-181, 60, 50], [NaN, 60, 50], [10, null, 50], ['10', 60, 50], [10, 60], null, 'x',
    [180, 66, 30], [-180, -66, 30]
  ];
  return {
    clamp: [5, 0, -3, 1, 100, 101, 7.4, 7.6, '20', ' ', 'abc', null, undefined, NaN,
            Infinity].map(A.clampMin),
    url: [A.auroraUrl('', 5), A.auroraUrl('http://h', 0), A.auroraUrl(null, 'abc'),
          A.auroraUrl('', 250)],
    at5: A.cleanCells(cells, 5),
    at40: A.cleanCells(cells, 40),
    atJunk: A.cleanCells(cells, 'abc').length,
    notList: A.cleanCells({ length: 2 }, 5),
    hemiServer: A.hemisphereMax({ hemisphere_max: { north: 61, south: 18 } }, []),
    hemiCells: A.hemisphereMax({}, [[0, 60, 12], [0, 61, 30], [0, -60, 9], [0, 0, 80]]),
    times: [A.shortTime('2026-11-19T12:30:00Z'), A.shortTime(''), A.shortTime(42)]
  };
""")


def test_threshold_helpers(ran):
    h = ran["threshold_helpers"]
    assert h["clamp"] == [5, 1, 1, 1, 100, 100, 7, 8, 20, 5, 5, 5, 5, 5, 5]
    assert h["url"] == ["/api/aurora?min=5", "http://h/api/aurora?min=1",
                        "/api/aurora?min=5", "/api/aurora?min=100"]
    assert h["at5"] == [[20, 68, 42], [-160, 65, 61], [10, 60, 5], [180, 66, 30],
                        [-180, -66, 30]]
    assert h["at40"] == [[20, 68, 42], [-160, 65, 61]]
    assert h["atJunk"] == 5  # an unreadable threshold falls back to the default
    assert h["notList"] == []
    assert h["hemiServer"] == {"north": 61, "south": 18}
    assert h["hemiCells"] == {"north": 30, "south": 9}
    assert h["times"] == ["12:30 UTC", "", ""]


scenario("raster", r"""
  var A = window.SDAAurora;
  function texel(band, lon, lat) {
    var row = Math.min(69, Math.floor(band.north ? 90 - lat : -20 - lat));
    var col = ((Math.round(lon + 180) % 360) + 360) % 360;
    var i = (row * 360 + col) * 4;
    return Array.prototype.slice.call(band.pixels, i, i + 4);
  }
  var cells = [[20, 68, 42], [-160, 65, 61], [180, 66, 5], [-180, 66, 30], [100, -65, 18],
               [0, 19, 50], [0, -19, 50], [0, 90, 10], [0, -90, 10]];
  var n = A.rasterBand(cells, true);
  var s = A.rasterBand(cells, false);
  var c42 = A.colorFor(42);
  return {
    sizes: [n.width, n.height, n.pixels.length, s.width, s.height],
    counts: [n.count, s.count], rects: [n.rect, s.rect],
    at42: texel(n, 20, 68),
    want42: [Math.round(c42.r * 255), Math.round(c42.g * 255), Math.round(c42.b * 255),
             Math.round(c42.a * 255)],
    seam: texel(n, 180, 66), seamA30: Math.round(A.colorFor(30).a * 255),
    south: texel(s, 100, -65)[3] > 0, pole: [texel(n, 0, 90)[3] > 0, texel(s, 0, -90)[3] > 0],
    edge: texel(n, 0, 20), empty: A.rasterBand(null, true).count
  };
""")


def test_raster_band_places_each_cell(ran):
    r = ran["raster"]
    assert r["sizes"] == [360, 70, 360 * 70 * 4, 360, 70]
    # 180 E and 180 W are one texel; the 19-degree cells fall outside both bands
    assert r["counts"] == [4, 2]
    assert r["rects"] == [[-180, 20, 180, 90], [-180, -90, 180, -20]]
    assert r["at42"] == r["want42"]
    assert r["seam"][3] == r["seamA30"]  # where two cells share a texel the stronger wins
    assert r["south"] is True and r["pole"] == [True, True]
    assert r["edge"] == [0, 0, 0, 0] and r["empty"] == 0


# --- enable, refresh, disable --------------------------------------------------------

scenario("enable", r"""
  route('/api/aurora', OVAL);
  var k = makeCtx();
  var ok = window.SDAAurora.init(k.ctx);
  var before = { requests: REQUESTS.length, status: status(k),
                 title: byTag(k.ctx.mount, 'H3')[0].textContent };
  window.SDAAurora.setEnabled(true);
  window.SDAAurora.setEnabled(true);  // a second "on" changes nothing
  await settle();
  var first = {
    ok: ok, before: before, requests: REQUESTS.slice(), collections: k.ctx.viewer._added.length,
    n: drawn(), count: countEl.textContent, enabled: window.SDAAurora.isEnabled(),
    bands: bands(k).map(band), show: k.ctx.viewer._added[0].show,
    intervals: live().map(function (t) { return t.ms; }),
    feed: k.feed.slice(), status: status(k)
  };
  // ten minutes later the interval fires: one more request, same oval, no repeat line
  live()[0].fn();
  await settle();
  var refreshed = { requests: REQUESTS.length, feed: k.feed.length, n: drawn(),
                    bands: bands(k).length };
  window.SDAAurora.setEnabled(false);
  await settle();
  var off = { n: bands(k).length, show: k.ctx.viewer._added[0].show,
              count: countEl.textContent, intervals: live().length,
              enabled: window.SDAAurora.isEnabled(), requests: REQUESTS.length };
  return { first: first, refreshed: refreshed, off: off };
""")


def test_enable_draws_the_oval(ran):
    f = ran["enable"]["first"]
    assert f["ok"] is True
    assert f["before"]["requests"] == 0  # nothing loads until the layer is on
    assert "Turn on" in f["before"]["status"] and f["before"]["title"] == "Aurora oval"
    assert f["requests"] == ["/api/aurora?min=5"]
    assert f["collections"] == 1 and f["show"] is True and f["enabled"] is True
    assert f["n"] == 5 and f["count"] == "5"
    north, south = f["bands"]
    assert north["hemi"] == "north" and south["hemi"] == "south"
    assert north["rect"] == [-180, 20, 180, 90] and south["rect"] == [-180, -90, 180, -20]
    for b in (north, south):
        # one translucent shell per hemisphere at aurora height, built at once
        assert b["height"] == 110000 and b["format"] == "POSITION_AND_ST"
        assert b["aboveGround"] is True and b["translucent"] is True
        assert b["flat"] is True  # unlit, or the polar-night oval goes black
        assert b["material"] == "Image" and b["sync"] is False
        # colour alpha under 1 keeps the shell in the translucent pass; at 1 the
        # Image material is opaque and hid every translucent point beneath it
        assert 0.99 <= b["alpha"] < 1.0
        # one texel per cell, drawn 4x with a blur, three copies so 180 deg has no seam
        assert b["small"] == [360, 70] and b["size"] == [1440, 280]
        assert b["xs"] == [-1440, 0, 1440] and b["filter"].startswith("blur(")
    assert f["intervals"] == [600000]
    assert len(f["feed"]) == 1
    line = f["feed"][0]
    assert "61% north" in line and "18% south" in line
    assert "5 cells at 5% or more" in line and "forecast 12:30 UTC" in line
    assert "stale" not in line
    assert f["status"] == line


def test_refresh_every_ten_minutes(ran):
    r = ran["enable"]["refreshed"]
    assert r["requests"] == 2
    assert r["feed"] == 1  # an unchanged oval is not posted again
    assert r["n"] == 5 and r["bands"] == 2


def test_disable_clears(ran):
    off = ran["enable"]["off"]
    assert off == {"n": 0, "show": False, "count": "0", "intervals": 0, "enabled": False,
                   "requests": 2}


scenario("late_answer", r"""
  route('/api/aurora', OVAL);
  var k = makeCtx();
  window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  window.SDAAurora.setEnabled(false);  // turned off before the answer lands
  await settle();
  return { n: bands(k).length, feed: k.feed, count: countEl.textContent };
""")


def test_answer_after_disable_is_dropped(ran):
    assert ran["late_answer"] == {"n": 0, "feed": [], "count": "0"}


# --- threshold -----------------------------------------------------------------------

scenario("threshold", r"""
  // the server would filter too; the module re-checks every row on its own
  route('http://h/api/aurora', OVAL);
  var k = makeCtx({ api: 'http://h' });
  window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  await settle();
  window.SDAAurora.setMin(20);
  await settle();
  var at20 = { n: drawn(), min: window.SDAAurora.minProb(),
               hemis: bands(k).map(function (p) {
                 return p.geometryInstances.id.hemisphere; }) };
  window.SDAAurora.setMin(20);  // unchanged: no request
  var select = byClass(k.ctx.mount, 'aurora-min-select')[0];
  var options = byTag(select, 'OPTION').map(function (o) { return o.value; });
  select.value = '50';
  select.fire('change');
  await settle();
  var at50 = { n: drawn(), min: window.SDAAurora.minProb() };
  window.SDAAurora.setMin('nonsense');
  await settle();
  return { requests: REQUESTS, at20: at20, at50: at50, options: options,
           afterJunk: window.SDAAurora.minProb(), selectValue: select.value,
           lines: k.feed.length, last: k.feed[k.feed.length - 1] };
""")


def test_threshold_changes_request_and_filters(ran):
    t = ran["threshold"]
    assert t["requests"] == ["http://h/api/aurora?min=5", "http://h/api/aurora?min=20",
                             "http://h/api/aurora?min=50", "http://h/api/aurora?min=5"]
    assert t["at20"] == {"n": 2, "min": 20, "hemis": ["north"]}  # the south cell is 18 %
    assert t["at50"] == {"n": 1, "min": 50}
    assert t["options"] == ["1", "5", "10", "20", "30", "50"]
    assert t["afterJunk"] == 5 and t["selectValue"] == "5"
    assert t["lines"] == 4  # each threshold changes the line, so each one is posted
    assert "5 cells at 5% or more" in t["last"]


# --- static build, failures, stale -----------------------------------------------------

scenario("static", r"""
  route('/api/aurora', OVAL);
  var k = makeCtx({ isStatic: true });
  var ok = window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  window.SDAAurora.setEnabled(false);
  window.SDAAurora.setEnabled(true);
  window.SDAAurora.setMin(30);
  await window.SDAAurora.refresh();
  await settle();
  return { ok: ok, requests: REQUESTS.length, intervals: INTERVALS.length,
           collections: k.ctx.viewer._added.length, feed: k.feed, status: status(k),
           enabled: window.SDAAurora.isEnabled() };
""")


def test_static_build_makes_no_requests(ran):
    s = ran["static"]
    assert s["ok"] is True
    assert s["requests"] == 0 and s["intervals"] == 0 and s["collections"] == 0
    assert s["enabled"] is False
    assert s["feed"] == ["Aurora oval needs the live server (NOAA SWPC OVATION)."]
    assert "live server" in s["status"]


scenario("failure", r"""
  route('/api/aurora', { detail: 'SWPC OVATION aurora feed unavailable' }, 502);
  var k = makeCtx();
  window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  await settle();
  live()[0].fn();
  await settle();
  var failed = { feed: k.feed.slice(), n: bands(k).length, status: status(k),
                 requests: REQUESTS.length };
  route('/api/aurora', Object.assign({}, OVAL, { stale: true }));
  live()[0].fn();
  await settle();
  return { failed: failed, n: drawn(), last: k.feed[k.feed.length - 1] };
""")


def test_failure_is_reported_once_then_recovers(ran):
    r = ran["failure"]
    assert r["failed"]["requests"] == 2
    assert r["failed"]["feed"] == ["Aurora oval: NOAA SWPC OVATION feed unavailable"]
    assert r["failed"]["n"] == 0
    assert "did not load" in r["failed"]["status"]
    assert r["n"] == 5
    assert "stale" in r["last"]


scenario("hostile", r"""
  route('/api/aurora', { cells: [[20, 68, 42], 'x', [1, 2], [NaN, 1, 9], [10, 60, 1e9]],
    hemisphere_max: 'nope', forecast_time: '<b>12:00</b>', stale: false });
  var k = makeCtx();
  window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  await settle();
  route('/api/aurora', { cells: 'not a list' });
  live()[0].fn();
  await settle();
  return { n: bands(k).length, feed: k.feed };
""")


def test_malformed_answers_do_not_throw(ran):
    h = ran["hostile"]
    assert h["n"] == 0  # the second answer had no usable cells, so the oval cleared
    assert h["feed"][0].startswith("Aurora oval (NOAA SWPC OVATION): max 42% north, 0% south")
    assert "forecast" not in h["feed"][0]
    assert "0 cells" in h["feed"][1]


scenario("no_mount", r"""
  route('/api/aurora', OVAL);
  var k = makeCtx({ noMount: true });
  var ok = window.SDAAurora.init(k.ctx);
  window.SDAAurora.setEnabled(true);
  await settle();
  var saved = window.Cesium;
  var noCesium = (window.Cesium = undefined, window.SDAAurora.init(makeCtx().ctx));
  window.Cesium = saved;
  return { ok: ok, n: drawn(), noCesium: noCesium,
           noViewer: window.SDAAurora.init({ mount: new El('div') }),
           nothing: window.SDAAurora.init(null) };
""")


def test_init_without_a_panel_and_guards(ran):
    r = ran["no_mount"]
    assert r == {"ok": True, "n": 5, "noCesium": False, "noViewer": False, "nothing": False}


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "aurora.js must stay ASCII"
    assert "window.SDAAurora" in text or "global.SDAAurora" in text
