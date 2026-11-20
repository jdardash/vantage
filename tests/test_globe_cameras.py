"""Tests for the globe public-camera module (static/js/cameras.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_isscam.py does it. The
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
    / "src" / "sda" / "templates" / "static" / "js" / "cameras.js"
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
El.prototype.canPlayType = function () { return ''; };
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

var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  }
};
var countEl = new El('span'); countEl.id = 'count-cctv'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var Cesium = {
  PointPrimitiveCollection: Coll, LabelCollection: Coll,
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 };
    } },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  NearFarScalar: function () {},
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0 }, VerticalOrigin:
    { CENTER: 0 },
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

var SUMMARY = { available: true, count: 193821, playable: 171758, country_count: 173,
  source_count: 382, groups: { traffic: 103711, city: 53047, beach: 15406 },
  attribution: 'Camera catalog from Argus.' };

function makeViewer(heightM) {
  var moveEnd = [];
  return {
    scene: { primitives: { add: function (c) { return c; } }, globe: { ellipsoid: {} } },
    canvas: { clientWidth: 800, clientHeight: 600 },
    camera: {
      positionCartographic: { height: heightM, latitude: 30.27 * Math.PI / 180,
        longitude: -97.74 * Math.PI / 180 },
      computeViewRectangle: function () {
        var d = Math.PI / 180;
        return { west: -98 * d, south: 30 * d, east: -97 * d, north: 31 * d };
      },
      moveEnd: { addEventListener: function (fn) { moveEnd.push(fn); } },
      flyTo: function (o) { this.flown = o; },
      pickEllipsoid: function () { return { lon: -97.74, lat: 30.27, h: 0 }; }
    },
    _moveEnd: moveEnd
  };
}

function makeCtx(heightM, opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(heightM), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    satEngine: opts.satEngine || { byNoradId: {}, selectedId: null },
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 6; i++) await tick(); }
function frames() { return byClass(document.body, 'cams-frame'); }
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
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'cameras.js' });
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
      // stop any refresh interval the scenario left running
      try { vm.runInContext('window.SDACameras && window.SDACameras.close()', box); } catch (e) {}
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
    work = tmp_path_factory.mktemp("cameras")
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
  var C = window.SDACameras;
  return {
    safe: ['https://a.org/x.jpg', 'http://a.org', 'javascript:alert(1)', 'data:image/png;base64,x',
           'faa-weathercam://1', '//a.org/x', 'https://a.org/x y', '', null,
             'https://'].map(C.safeHttpUrl),
    yt: [C.youtubeEmbedUrl('BMQifTcOgts'), C.youtubeEmbedUrl('BMQifTcOgt"'),
      C.youtubeEmbedUrl('x')],
    bust: [C.bustUrl('https://a.org/x.jpg', 5), C.bustUrl('https://a.org/x.jpg?k=1#f', 6),
           C.bustUrl('javascript:x', 1)],
    cells: [2e7, 7e6, 4e6, 2e6, 6e5].map(C.cellForHeight),
    modes: [2e7, 5e5, 4.5e5, 1e4].map(C.modeForHeight),
    q: C.query([['a', 1], ['b', ''], ['c', null], ['d', 'x y,z']]),
    fp: Math.round(C.footprintKm(420, 30)),
    fp0: Math.round(C.footprintKm(420, 0)),
    box: C.boxFromRect({ west: 170 * Math.PI / 180, south: -20 * Math.PI / 180,
                         east: -170 * Math.PI / 180, north: -10 * Math.PI / 180 }, 0),
    boxNull: C.boxFromRect(undefined),
    around: C.boxAround(0, 179.9, 111.32),
    sizes: [0, 1, 100, 1e6].map(C.densitySize),
    counts: [999, 1500, 25000, 2500000].map(C.formatCount)
  };
""")


def test_safe_http_url(ran):
    out = ran["helpers"]["safe"]
    assert out[:2] == ["https://a.org/x.jpg", "http://a.org"]
    assert out[2:] == [""] * 8


def test_youtube_and_bust(ran):
    h = ran["helpers"]
    assert h["yt"][0].startswith("https://www.youtube-nocookie.com/embed/BMQifTcOgts?")
    assert h["yt"][1:] == ["", ""]
    assert h["bust"] == ["https://a.org/x.jpg?sda_t=5", "https://a.org/x.jpg?k=1&sda_t=6#f", ""]


def test_view_geometry(ran):
    h = ran["helpers"]
    assert h["cells"] == [8, 4, 2, 1, 0.5]
    assert h["modes"] == ["density", "density", "detail", "detail"]
    assert h["q"] == "?a=1&d=x%20y%2Cz"
    # ISS at 420 km is above 30 deg elevation within about 630 km of its ground point
    assert 600 <= h["fp"] <= 660
    assert 2200 <= h["fp0"] <= 2350
    assert h["box"] == {"west": 170, "south": -20, "east": -170, "north": -10}
    assert h["boxNull"] is None
    assert h["around"]["west"] > h["around"]["east"]  # wraps the antimeridian
    assert h["sizes"][0] == 0 and h["sizes"][1] < h["sizes"][2] <= h["sizes"][3] == 30
    assert h["counts"] == ["999", "1.5k", "25k", "2.5M"]


# --- feed plans ------------------------------------------------------------------

scenario("plans", r"""
  var P = window.SDACameras.feedPlan;
  var base = { rid: 7, refresh_s: 30, page_url: '' };
  function plan(extra) { return P(Object.assign({}, base, extra), 'http://h'); }
  return {
    image: plan({ feed: 'image', feed_url: 'https://a.org/x.jpg', refresh_s: 3 }),
    mjpeg: plan({ feed: 'mjpeg', feed_url: 'https://a.org/v.cgi' }),
    txdot: plan({ feed: 'txdot', feed_url: 'https://its.txdot.gov/x' }),
    hls: plan({ feed: 'hls', feed_url: 'https://a.org/p.m3u8' }),
    mp4: plan({ feed: 'mp4', feed_url: 'https://a.org/c.mp4' }),
    yt: plan({ feed: 'youtube', feed_url: 'BMQifTcOgts' }),
    page: plan({ feed: 'page', feed_url: 'https://a.org/page' }),
    hostileImg: plan({ feed: 'image', feed_url: 'javascript:alert(1)' }),
    hostileYt: plan({ feed: 'youtube', feed_url: '"><script>' }),
    hostilePage: plan({ feed: 'page', feed_url: 'javascript:alert(1)', page_url: 'data:x' }),
    unknown: plan({ feed: 'telnet', feed_url: 'https://a.org' }),
    nothing: P(null)
  };
""")


def test_feed_plans(ran):
    p = ran["plans"]
    assert p["image"] == {"mode": "img", "src": "https://a.org/x.jpg", "refreshS": 10,
                          "link": "https://a.org/x.jpg"}
    assert p["mjpeg"]["mode"] == "img" and p["mjpeg"]["refreshS"] == 0
    assert p["txdot"]["src"] == "http://h/api/cameras/7/snapshot"
    assert p["txdot"]["refreshS"] == 60
    assert p["hls"]["mode"] == "hls" and p["mp4"]["mode"] == "video"
    assert p["yt"]["mode"] == "youtube"
    assert p["yt"]["src"].startswith("https://www.youtube-nocookie.com/embed/BMQifTcOgts")
    assert p["yt"]["link"] == "https://www.youtube.com/watch?v=BMQifTcOgts"
    assert p["page"] == {"mode": "link", "src": "", "refreshS": 0, "link": "https://a.org/page"}
    for name in ("hostileImg", "hostileYt", "hostilePage", "unknown", "nothing"):
        assert p[name]["mode"] == "none" and p[name]["src"] == "" and p[name]["link"] == "", name


# --- the panel and the layer -------------------------------------------------------

scenario("density", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras/density', { cell_deg: 8, total: 1300,
    cells: [{ lat: 30, lon: -97, count: 1200, playable: 1100 }, { lat: 51, lon: 0, count: 100,
      playable: 0 }] });
  var k = makeCtx(2e7);
  var ok = window.SDACameras.init(k.ctx);
  var chipsBefore = byClass(k.ctx.mount, 'cams-chip').length;
  window.SDACameras.setEnabled(true);
  await settle();
  return {
    ok: ok,
    chipsBefore: chipsBefore,
    chipsAfter: byClass(k.ctx.mount, 'cams-chip').length,
    requests: REQUESTS,
    count: countEl.textContent,
    summary: byClass(k.ctx.mount, 'cams-summary')[0].textContent,
    view: byClass(k.ctx.mount, 'cams-view')[0].textContent,
    feed: k.feed
  };
""")


def test_density_mode(ran):
    d = ran["density"]
    assert d["ok"] is True
    assert d["chipsBefore"] == 10  # every group before the summary arrives
    assert d["chipsAfter"] == 3  # only groups the catalog holds
    assert d["requests"][0] == "/api/cameras/summary"
    assert d["requests"][1].startswith("/api/cameras/density?west=-180&south=-90&east=180")
    assert "cell=8" in d["requests"][1]
    assert d["count"] == "1.3k"
    assert "193,821 public cameras in 173 countries" in d["summary"]
    assert "1,300 cameras in view" in d["view"]
    assert any("193,821" in line for line in d["feed"])


scenario("detail", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras?', { count: 2, total: 2, capped: false, cameras: [
    { rid: 11, name: 'Congress Ave', lat: 30.27, lon: -97.74, group: 'traffic', feed: 'image',
      playable: true, country: 'US', city: 'Austin' },
    { rid: 12, name: 'Lamar', lat: 30.28, lon: -97.75, group: 'city', feed: 'page',
      playable: false, country: 'US', city: 'Austin' }
  ] });
  route('/api/cameras/11', { rid: 11, id: 'x', name: 'Congress Ave', type: 'traffic',
    group: 'traffic',
    city: 'Austin', country: 'US', lat: 30.27, lon: -97.74, feed: 'image',
    feed_url: 'https://cams.example.org/11.jpg', page_url: '', source: 'test', playable: true,
      refresh_s: 30 });
  var k = makeCtx(20000);
  window.SDACameras.init(k.ctx);
  window.SDACameras.setEnabled(true);
  await settle();
  var listReq = REQUESTS.filter(function (u) { return u.indexOf('/api/cameras?') === 0; });
  // open camera 11 the way the page click handler would
  window.SDACameras.open(11);
  await settle();
  var f = frames();
  var img = f.length ? byTag(f[0], 'IMG')[0] : null;
  var links = f.length ? byTag(f[0], 'A') : [];
  var title = f.length ? byClass(f[0], 'cams-title')[0].textContent : '';
  var openBefore = window.SDACameras.isOpen();
  window.SDACameras.close();
  return {
    listReq: listReq, count: countEl.textContent,
    view: byClass(k.ctx.mount, 'cams-view')[0].textContent,
    frames: f.length, title: title, img: img ? img.src : null,
    referrer: img ? img.getAttribute('referrerpolicy') : null,
    links: links.map(function (a) { return [a.getAttribute('href'), a.getAttribute('rel'),
      a.getAttribute('target')]; }),
    openBefore: openBefore, openAfter: window.SDACameras.isOpen(), framesAfter: frames().length,
    feed: k.feed
  };
""")


def test_detail_mode_and_viewer(ran):
    d = ran["detail"]
    assert len(d["listReq"]) == 1
    assert d["listReq"][0].startswith("/api/cameras?west=")
    assert "limit=1500" in d["listReq"][0]
    assert d["count"] == "2"
    assert "2 cameras in view" in d["view"]
    assert d["frames"] == 1 and d["title"] == "Congress Ave"
    assert d["img"].startswith("https://cams.example.org/11.jpg?sda_t=")
    assert d["referrer"] == "no-referrer"
    assert d["links"] == [["https://cams.example.org/11.jpg", "noopener noreferrer", "_blank"]]
    assert d["openBefore"] is True and d["openAfter"] is False and d["framesAfter"] == 0
    assert any("Congress Ave" in line for line in d["feed"])


scenario("same_view_while_loading", r"""
  route('/api/cameras/summary', SUMMARY);
  var release;
  var held = new Promise(function (r) { release = r; });
  var plain = window.fetch;
  window.fetch = function (url) {
    if (url.indexOf('/api/cameras?') !== 0) return plain(url);
    REQUESTS.push(url);
    return held.then(function () {
      return { ok: true, status: 200, json: function () { return Promise.resolve({
        count: 1, total: 1, capped: false, cameras: [{ rid: 11, name: 'Congress Ave',
          lat: 30.27, lon: -97.74, group: 'traffic', feed: 'image', playable: true,
          country: 'US', city: 'Austin' }] }); } };
    });
  };
  var k = makeCtx(20000);
  window.SDACameras.init(k.ctx);
  window.SDACameras.setEnabled(true);
  await settle();
  // the camera stops again on the same view while the list is still on its way
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await new Promise(function (r) { setTimeout(r, 420); });
  release();
  await settle();
  window.fetch = plain;
  return {
    lists: REQUESTS.filter(function (u) { return u.indexOf('/api/cameras?') === 0; }).length,
    count: countEl.textContent, view: byClass(k.ctx.mount, 'cams-view')[0].textContent
  };
""")


def test_a_repeat_refresh_does_not_throw_away_the_answer_on_its_way(ran):
    """Found by the all-layers browser audit of 2026-11-20: a refresh for the view
    already asked for moved the sequence on, so the list that was loading was
    discarded when it arrived and New York showed no cameras at all."""
    d = ran["same_view_while_loading"]
    assert d["lists"] == 1
    assert d["count"] == "1"
    assert "1 cameras in view" in d["view"]


scenario("youtube_and_page", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras/5', { rid: 5, id: 'y', name: 'Times Square', type: 'landmark', group: 'city',
    city: 'New York', country: 'US', lat: 40.75, lon: -73.98, feed: 'youtube',
      feed_url: 'BMQifTcOgts',
    page_url: '', source: 'opencctv', playable: true, refresh_s: 60 });
  route('/api/cameras/6', { rid: 6, id: 'p', name: 'Airfield', type: 'airport', group: 'aviation',
    city: '', country: 'IT', lat: 45, lon: 9, feed: 'page', feed_url: 'https://www.example.it/cam',
    page_url: '', source: 'opencctv', playable: false, refresh_s: 60 });
  route('/api/cameras/7', { rid: 7, id: 'h', name: 'Hostile', type: 'traffic', group: 'traffic',
    city: '', country: 'US', lat: 1, lon: 1, feed: 'image', feed_url: 'javascript:alert(1)',
    page_url: 'javascript:alert(2)', source: 'x', playable: true, refresh_s: 60 });
  var k = makeCtx(2e7);
  window.SDACameras.init(k.ctx);
  window.SDACameras.open(5);
  await settle();
  var yt = byTag(frames()[0], 'IFRAME').map(function (f) { return f.getAttribute('src'); });
  window.SDACameras.open(6);
  await settle();
  var page = { frames: frames().length, iframes: byTag(frames()[0], 'IFRAME').length,
    links: byTag(frames()[0], 'A').map(function (a) { return [a.getAttribute('href'),
      a.getAttribute('rel')]; }) };
  window.SDACameras.open(7);
  await settle();
  var hostile = { iframes: byTag(frames()[0], 'IFRAME').length, imgs: byTag(frames()[0],
    'IMG').length,
    links: byTag(frames()[0], 'A').length, msg: byClass(frames()[0], 'cams-msg')[0].textContent };
  return { yt: yt, page: page, hostile: hostile };
""")


def test_youtube_page_and_hostile_feeds(ran):
    r = ran["youtube_and_page"]
    assert len(r["yt"]) == 1
    assert r["yt"][0].startswith("https://www.youtube-nocookie.com/embed/BMQifTcOgts?")
    # a page feed is a link, never a frame, and opening it replaced the YouTube viewer
    assert r["page"] == {"frames": 1, "iframes": 0,
                         "links": [["https://www.example.it/cam", "noopener noreferrer"]]}
    assert r["hostile"] == {"iframes": 0, "imgs": 0, "links": 0,
                            "msg": "No playable feed for this camera."}


scenario("under_sat", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras/density', { cell_deg: 8, total: 0, cells: [] });
  route('/api/cameras/nearest', { count: 1, cameras: [
    { rid: 3, name: 'Cam under ISS', lat: 12.5, lon: -45.2, group: 'beach', feed: 'image',
      playable: true,
      country: 'BR', city: 'X', distance_km: 12.3 }] });
  var sat = { name: 'ISS (ZARYA)', point: { position: { lon: -45.25, lat: 12.5, h: 420000 } } };
  var k = makeCtx(2e7, { satEngine: { byNoradId: { 25544: sat }, selectedId: 25544 } });
  window.SDACameras.init(k.ctx);
  byTag(k.ctx.mount, 'BUTTON').filter(function (b) { return b.textContent === 'UNDER SAT';
    })[0].fire('click');
  await settle();
  var near = REQUESTS.filter(function (u) { return u.indexOf('/api/cameras/nearest') === 0; });
  return {
    near: near,
    items: byClass(k.ctx.mount, 'cams-item').map(function (b) { return b.textContent; }),
    status: byClass(k.ctx.mount, 'cams-status')[0].textContent
  };
""")


def test_cameras_under_selected_satellite(ran):
    r = ran["under_sat"]
    assert len(r["near"]) == 1
    q = r["near"][0]
    assert "lat=12.5" in q and "lon=-45.25" in q
    radius = int(q.split("radius_km=")[1].split("&")[0])
    assert 600 <= radius <= 660
    assert r["items"] == [" Cam under ISS12.3 km"]
    assert "ISS (ZARYA)" in r["status"]


scenario("no_selection", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras/density', { cell_deg: 8, total: 0, cells: [] });
  var k = makeCtx(2e7);
  window.SDACameras.init(k.ctx);
  byTag(k.ctx.mount, 'BUTTON').filter(function (b) { return b.textContent === 'UNDER SAT';
    })[0].fire('click');
  await settle();
  return byClass(k.ctx.mount, 'cams-status')[0].textContent;
""")


def test_under_sat_needs_a_selection(ran):
    assert ran["no_selection"] == "Select a satellite on the globe first."


scenario("disable", r"""
  route('/api/cameras/summary', SUMMARY);
  route('/api/cameras/density', { cell_deg: 8, total: 5, cells: [{ lat: 1, lon: 1, count: 5,
    playable: 5 }] });
  var k = makeCtx(2e7);
  window.SDACameras.init(k.ctx);
  window.SDACameras.setEnabled(true);
  await settle();
  var before = countEl.textContent;
  window.SDACameras.setEnabled(false);
  await settle();
  return { before: before, after: countEl.textContent,
           view: byClass(k.ctx.mount, 'cams-view')[0].textContent, requests: REQUESTS.length };
""")


def test_disable_clears(ran):
    r = ran["disable"]
    assert r["before"] == "5" and r["after"] == "0" and r["view"] == ""
    assert r["requests"] == 2


scenario("static", r"""
  var k = makeCtx(2e7, { isStatic: true });
  window.SDACameras.init(k.ctx);
  window.SDACameras.setEnabled(true);
  await settle();
  return { requests: REQUESTS.length, summary: byClass(k.ctx.mount,
    'cams-summary')[0].textContent };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == 0
    assert "live server" in r["summary"]


scenario("missing_catalog", r"""
  route('/api/cameras/summary', { available: false, count: 0, groups: {}, feeds: {}, countries: [],
    hint: 'Run: python -m sda.cameras import' });
  route('/api/cameras/density', { cell_deg: 8, total: 0, cells: [] });
  var k = makeCtx(2e7);
  window.SDACameras.init(k.ctx);
  window.SDACameras.setEnabled(true);
  await settle();
  return byClass(k.ctx.mount, 'cams-summary')[0].textContent;
""")


def test_missing_catalog_says_how_to_import(ran):
    assert "python -m sda.cameras import" in ran["missing_catalog"]


def test_hls_pin_matches_the_page_policy():
    text = MODULE.read_text(encoding="utf-8")
    assert "hls.js@1.7.4/dist/hls.min.js" in text
    assert "sha384-" in text
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "cameras.js must stay ASCII"
