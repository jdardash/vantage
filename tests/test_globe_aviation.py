"""Tests for the globe aviation overlay (static/js/aviation.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub and a
fake Cesium (no jsdom, no browser), the way tests/test_globe_peaks.py does it. The
answers the fake `fetch` serves are produced by the real aviation routes from the
recorded adsb.lol samples in tests/data/aviation, so the browser module is held to the
server's actual contract rather than a hand-copied one. Skips when Node is missing.

Each scenario is the body of an async function returning a JSON-able value, run in a
fresh vm context; the module-scoped `ran` fixture holds every result.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sda.aviation as av
import sda.routes.aviation as routes
import sda.routes.worldview as wv
from tests.test_aviation import ADSB, VRS, Upstream, load, now_ms

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "aviation.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

KT_PER_MS = 1 / 0.514444

# Dead-reckoning cases the browser and the server must agree on.
DR_CASES = [
    {"lat": 50.0, "lon": 10.0, "track": 90.0, "kt": 450.0, "dt": 600.0},
    {"lat": -33.9, "lon": 151.2, "track": 225.0, "kt": 480.0, "dt": 1200.0},
    {"lat": 10.0, "lon": 179.5, "track": 80.0, "kt": 480.0, "dt": 600.0},
    {"lat": 89.0, "lon": 10.0, "track": 5.0, "kt": 500.0, "dt": 900.0},
]

SITE = {"id": "way/1", "name": "Test Airfield", "kind": "airfield", "operator": "",
        "lat": 22.03, "lon": -159.76}

BAW117_HIT = {"ac": [{"hex": "406a93", "type": "adsb_icao", "flight": "BAW117  ",
                      "r": "G-XWBA", "t": "A35K", "alt_baro": 38000, "gs": 480.0,
                      "track": 255.0, "lat": 52.0, "lon": -20.0, "nic": 8, "nac_p": 10,
                      "seen_pos": 1.0}]}


def _payloads() -> dict:
    """Answers from the real routes over the recorded samples, offline."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("ADSBLOL_API_KEY", raising=False)
        mp.delenv("SDA_ADSBLOL_BUDGET", raising=False)
        av.reset_state()
        up = Upstream()
        mp.setattr(av.httpx, "AsyncClient", up.client_class())

        async def fake_sites(south: float, west: float, north: float, east: float) -> dict:
            return {"sites": [SITE], "count": 1, "source": "OpenStreetMap"}

        mp.setattr(wv, "proxy_military_sites", fake_sites)
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        now = now_ms()
        up.on(ADSB + "/v2/mil", load("adsblol_mil.json", now))
        up.on(ADSB + "/v2/sqk/", {"ac": [], "now": now})
        up.on(ADSB + "/v2/sqk/7700", load("adsblol_sqk7700.json", now))
        up.on(ADSB + "/v2/point/", load("adsblol_point_baltic.json", now))
        up.on(ADSB + "/v2/hex/406a93", dict(BAW117_HIT, now=now))
        up.on(VRS + "BA/BAW117.json", load("route_BAW117.json"))
        out = {
            "status": client.get("/api/aviation/status").json(),
            "military": client.get("/api/aviation/military").json(),
            "emergencies": client.get("/api/aviation/emergencies").json(),
            "gps": client.get("/api/aviation/gps-interference?lat=55&lon=20").json(),
            "card_baw": client.get("/api/aviation/aircraft/406a93").json(),
            "card_mil": client.get("/api/aviation/aircraft/ae57d5").json(),
            "card_private": client.get("/api/aviation/aircraft/fff002?callsign=XTEST2").json(),
            "site": client.get("/api/aviation/nearest-site?lat=22.0&lon=-159.05").json(),
            "dr": DR_CASES,
        }
        av.reset_state()
    # One server clock for every answer (they were made milliseconds apart), so the
    # browser's clock offset and Python's expectations use the same instant.
    for value in out.values():
        if isinstance(value, dict) and "server_now" in value:
            value["server_now"] = out["military"]["server_now"]
    return out


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
var countEl = new El('span'); countEl.id = 'count-aviation'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
function Prim(o) { this.o = o; this.instances = o.geometryInstances; }
function GeometryInstance(o) { this.geometry = o.geometry; this.attributes = o.attributes;
  this.id = o.id; }
function PolygonGeometry(o) { this.o = o; this.positions = o.polygonHierarchy.positions; }
function PolygonHierarchy(p) { this.positions = p; }
var HANDLERS = [];
function ScreenSpaceEventHandler(canvas) { this.canvas = canvas; this.actions = {};
  HANDLERS.push(this); }
ScreenSpaceEventHandler.prototype.setInputAction = function (fn, type) {
  this.actions[type] = fn; };
var Cesium = {
  BillboardCollection: Coll, LabelCollection: Coll, Primitive: Prim,
  GeometryInstance: GeometryInstance, PolygonGeometry: PolygonGeometry,
  PolygonHierarchy: PolygonHierarchy,
  PerInstanceColorAppearance: function (o) { this.o = o; },
  ColorGeometryInstanceAttribute: { fromColor: function (c) { return { color: c }; } },
  ScreenSpaceEventHandler: ScreenSpaceEventHandler,
  ScreenSpaceEventType: { LEFT_CLICK: 2 },
  JulianDate: { now: function () { return 'now'; } },
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: {
    UNIT_Z: { x: 0, y: 0, z: 1 },
    fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; },
    fromDegreesArray: function (a) {
      var out = [];
      for (var i = 0; i < a.length; i += 2) out.push({ lon: a[i], lat: a[i + 1], h: 0 });
      return out;
    }
  },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  NearFarScalar: function () {},
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0, LEFT: 1 },
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

// ---- viewer and helpers --------------------------------------------------------
var NOW = PAYLOADS.military.server_now * 1000;
var PICKED = null;

function makeViewer() {
  var prims = [];
  return {
    scene: {
      primitives: {
        add: function (c) { prims.push(c); return c; },
        remove: function (c) { var at = prims.indexOf(c); if (at >= 0) prims.splice(at, 1);
          return at >= 0; }
      },
      globe: { ellipsoid: {} }, canvas: {},
      pick: function () { return PICKED; }
    },
    canvas: { clientWidth: 800, clientHeight: 600 },
    camera: {
      positionCartographic: { height: 2e6, latitude: 55 * Math.PI / 180,
        longitude: 20 * Math.PI / 180 },
      flyTo: function (o) { this.flown = o; },
      pickEllipsoid: function () { return { lon: 20, lat: 55, h: 0 }; }
    },
    _prims: prims
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    viewer: makeViewer(), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}

function liveRoutes() {
  route('/api/aviation/status', PAYLOADS.status);
  route('/api/aviation/military', PAYLOADS.military);
  route('/api/aviation/emergencies', PAYLOADS.emergencies);
  route('/api/aviation/gps-interference', PAYLOADS.gps);
  route('/api/sanctions/index', { regs: [], hex: [], list_loaded: true });
}

async function enabled(opts) {
  window.SDAAviation._setClock(function () { return NOW; });
  var k = makeCtx(opts);
  window.SDAAviation.init(k.ctx);
  window.SDAAviation.setEnabled(true);
  await settle();
  return k;
}

function iconName(img) {
  var I = window.SDAAviation.ICONS;
  for (var key in I) if (I[key] === img) return key;
  return null;
}

// Every drawn track keyed by hex: icon names, position, label.
function snapshot(k) {
  var prims = k.ctx.viewer._prims;
  var icons = prims[0], labels = prims[1];
  var out = {};
  if (!icons) return out;
  icons.items.forEach(function (b) {
    var h = b.id.hex;
    out[h] = out[h] || { icons: [] };
    out[h].icons.push(iconName(b.image));
    out[h].lat = b.position.lat; out[h].lon = b.position.lon;
  });
  labels.items.forEach(function (l) { out[l.id.hex].label = l.text; });
  return out;
}
function hexPrims(k) {
  return k.ctx.viewer._prims.filter(function (x) { return x instanceof Prim; });
}
function panelText(k, cls, at) { return byClass(k.ctx.mount, cls)[at || 0].textContent; }
function cardOf(k) { return byClass(k.ctx.mount, 'avi-card')[0]; }
function banner() { return document.getElementById('aviation-emergency-banner'); }

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
              JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'aviation.js' });
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
    // stop the poll and tick timers the scenario left running
    try { if (box) vm.runInContext('window.SDAAviation.setEnabled(false)', box); } catch (e) {}
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
    def __init__(self, raw: dict, payloads: dict):
        self._raw = raw
        self.payloads = payloads

    def __getitem__(self, name: str):
        result = self._raw[name]
        assert result["ok"], result["error"]
        return result["value"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Ran:
    payloads = _payloads()
    work = tmp_path_factory.mktemp("aviation")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(
        "var PAYLOADS = " + json.dumps(payloads) + ";\n" + PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), MODULE.as_posix(),
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout), payloads)


def expected_tracks(payloads: dict, now_s: float) -> dict[str, dict]:
    """Python's own verdict on every feed aircraft at now_s (sda.aviation.position_state)."""
    out = {}
    for name, kind in (("military", "mil"), ("emergencies", "emerg")):
        for row in payloads[name]["aircraft"]:
            state = av.position_state(row, now_s)
            if state["state"] != "lost":
                out[row["hex"]] = dict(state, kind=kind)
    return out


# --- pure helpers --------------------------------------------------------------

scenario("helpers", r"""
  var A = window.SDAAviation;
  var t = { lat: 50, lon: 10, track: 90, gs_kt: 400, seen_at: 1000, on_ground: false };
  function at(over, s) { return A.positionAt(Object.assign({}, t, over), s).state; }
  return {
    dr: PAYLOADS.dr.map(function (c) {
      var p = A.deadReckon(c.lat, c.lon, c.track, c.kt, c.dt); return [p.lat, p.lon]; }),
    states: [at({}, 1060), at({}, 1120), at({}, 1121), at({}, 2200), at({}, 2201),
             at({ on_ground: true }, 1600), at({ gs_kt: 20 }, 1600),
             at({ track: null }, 1600), A.positionAt({}, 1000).state],
    alt: [A.fmtAlt(10000), A.fmtAlt(null, true), A.fmtAlt(null)],
    speed: [A.fmtSpeed(450), A.fmtSpeed(null)],
    age: [A.fmtAge(30), A.fmtAge(600), A.fmtAge(null)],
    reg: [A.normReg('n-123ab '), A.normReg(null)],
    links: [A.safeLiveAtc('https://www.liveatc.net/search/?icao=KJFK'),
            A.safeLiveAtc('javascript:alert(1)'),
            A.safeLiveAtc('https://www.liveatc.net/search/?icao=KJFK&x=1'),
            A.safeLiveAtc('https://evil.example/?u=https://www.liveatc.net/search/?icao=KJFK'),
            A.safeLiveAtc(null)],
    query: [A.query([['a', 'x y'], ['b', null], ['c', ''], ['d', 0]]), A.query([])],
    names: [A.trackName({ hex: 'ae57d5', callsign: 'C2011' }),
            A.trackName({ hex: 'ae57d5', registration: '2011' }),
            A.trackName({ hex: 'ae57d5' }),
            A.trackName({ hex: 'fff002', callsign: 'XTEST2', private: true })],
    kinds: [A.trackKind({ squawk: '7700' }), A.trackKind({ squawk: '7700', inEmerg: false }),
            A.trackKind({ squawk: '7700', inEmerg: false, military: true }),
            A.trackKind({ squawk: '1200' })],
    label: A.labelFor({ hex: 'fff001', callsign: 'TST7700', squawk: '7700', kind: 'emerg' },
                      { state: 'estimated', age_s: 300 }),
    banner: A.bannerText([
      { t: { squawk: '7500', callsign: 'B', type: 'B738' }, pos: { state: 'reported' } },
      { t: { squawk: '7700', callsign: 'A' }, pos: { state: 'estimated', age_s: 300 } }]),
    empty: A.bannerText([])
  };
""")


def test_dead_reckoning_matches_the_server(ran):
    got = ran["helpers"]["dr"]
    for case, (lat, lon) in zip(DR_CASES, got, strict=True):
        want = av.dead_reckon(case["lat"], case["lon"], case["track"], case["kt"], case["dt"])
        assert lat == pytest.approx(want[0], abs=1e-9)
        assert lon == pytest.approx(want[1], abs=1e-9)
    assert -180 <= got[2][1] < -178  # carried over the antimeridian


def test_position_states_follow_the_server_limits(ran):
    assert ran["helpers"]["states"] == [
        "reported", "reported", "estimated", "estimated", "lost",
        "lost", "lost", "lost", "lost",
    ]
    assert av.REPORT_FRESH_S == 120 and av.DEAD_RECKON_MAX_S == 1200


def test_formatting_and_link_guard(ran):
    r = ran["helpers"]
    assert r["alt"] == ["32,808 ft (10,000 m)", "on the ground", "unknown"]
    assert r["speed"] == ["450 kt (833 km/h)", "unknown"]
    assert r["age"] == ["30 s", "10 min", ""]
    assert r["reg"] == ["N123AB", ""]
    assert r["links"] == ["https://www.liveatc.net/search/?icao=KJFK", None, None, None, None]
    assert r["query"] == ["?a=x%20y&d=0", ""]
    assert r["names"] == ["C2011", "2011", "AE57D5", "PRIVATE"]
    assert r["kinds"] == ["emerg", "civ", "mil", "civ"]
    assert r["label"] == "TST7700 7700 EST 5 min"


def test_banner_puts_the_most_serious_code_first(ran):
    r = ran["helpers"]
    assert r["banner"] == (
        "EMERGENCY SQUAWK 2 airborne. 7700 general emergency: A, estimated, last report "
        "5 min ago | 7500 unlawful interference: B (B738)"
    )
    assert r["empty"] == ""


# --- panel, static build, failures ---------------------------------------------------

scenario("init_guards", r"""
  var noMount = window.SDAAviation.init({ viewer: makeViewer() });
  var noViewer = window.SDAAviation.init({ mount: new El('div') });
  window.SDAAviation.setEnabled(true);  // nothing initialised: a no-op
  var k = makeCtx();
  var ok = window.SDAAviation.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  var buttons = byTag(k.ctx.mount, 'BUTTON').map(function (b) {
    return [b.textContent, b.getAttribute('aria-pressed')]; });
  return { noMount: noMount, noViewer: noViewer, ok: ok, title: head.textContent,
           collapsed: k.ctx.mount.children[0].has('collapsed'), buttons: buttons,
           style: !!document.getElementById('sda-aviation-style'), requests: REQUESTS,
           handler: HANDLERS.length, enabled: window.SDAAviation.isEnabled(),
           method: panelText(k, 'avi-method') };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert (r["noMount"], r["noViewer"], r["ok"]) == (False, False, True)
    assert r["title"] == "Aviation intel" and r["collapsed"] and r["style"]
    assert r["buttons"] == [["MIL", "true"], ["SQUAWK", "true"], ["EST", "true"],
                            ["GPS", "true"]]
    assert r["requests"] == [] and r["handler"] == 1 and r["enabled"] is False
    assert "inferred from aircraft reports, not measured" in r["method"]


scenario("static", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  liveRoutes();
  var k = makeCtx({ isStatic: true });
  window.SDAAviation.init(k.ctx);
  window.SDAAviation.setEnabled(true);
  var card = window.SDAAviation.openCard('ae57d5', {});
  await settle();
  return { requests: REQUESTS, summary: panelText(k, 'avi-summary'), card: card,
           cardText: cardOf(k).textContent, enabled: window.SDAAviation.isEnabled() };
""")


def test_static_build_makes_no_requests(ran):
    r = ran["static"]
    assert r["requests"] == [] and r["enabled"] is False and r["card"] is None
    assert r["summary"] == "Aviation intel needs the live server."
    assert r["cardText"] == "Aircraft cards need the live server."


scenario("failures", r"""
  ['/api/aviation/', '/api/sanctions/'].forEach(function (p) { route(p, {}, 500); });
  var k = await enabled();
  return { status: panelText(k, 'avi-status'), budget: panelText(k, 'avi-summary', 1),
           gps: panelText(k, 'avi-summary', 2), banner: !!banner(), count: countEl.textContent,
           tracks: snapshot(k), hexes: hexPrims(k).length };
""")


def test_upstream_failures_degrade_quietly(ran):
    r = ran["failures"]
    assert "military feed did not load" in r["status"]
    assert "squawk feed did not load" in r["status"]
    assert r["budget"] == "Aviation service did not answer."
    assert r["gps"] == "GPS interference did not load."
    assert r["banner"] is False and r["tracks"] == {} and r["hexes"] == 0
    assert r["count"] == "0"


# --- the live overlay ---------------------------------------------------------------

scenario("enable", r"""
  liveRoutes();
  var k = await enabled();
  var hp = hexPrims(k);
  var b = banner();
  return {
    requests: REQUESTS, tracks: snapshot(k), count: countEl.textContent, feed: k.feed,
    banner: b && b.style.display, bannerText: b && b.textContent,
    bannerRole: b && b.getAttribute('role'), status: panelText(k, 'avi-status'),
    budget: panelText(k, 'avi-summary', 1), gps: panelText(k, 'avi-summary', 2),
    hexes: hp.length === 1 ? hp[0].instances.map(function (i) {
      var c = i.attributes.color.color;
      return { color: c.css, alpha: c.a, corners: i.geometry.positions.length,
               pick: typeof i.id.sdaPick }; }) : null,
    translucent: hp.length === 1 ? hp[0].o.appearance.o.translucent : null,
    levels: window.SDAAviation.LEVELS
  };
""")


def test_enable_draws_both_feeds_by_the_server_clock(ran):
    r = ran["enable"]
    p = ran.payloads
    want = expected_tracks(p, p["military"]["server_now"])
    assert set(r["tracks"]) == set(want)
    for hex_id, track in r["tracks"].items():
        icon = want[hex_id]["kind"] + ("-est" if want[hex_id]["state"] == "estimated" else "")
        assert track["icons"] == [icon], hex_id
    assert r["count"] == str(len(want))
    assert r["tracks"]["fff001"]["label"] == "TST7700 7700"
    assert r["tracks"]["fff002"]["label"] == "PRIVATE 7700"  # private: no callsign drawn
    assert "XTEST2" not in json.dumps(r["tracks"])


def test_enable_requests_once_each(ran):
    r = ran["enable"]
    assert sorted(r["requests"]) == sorted([
        "/api/aviation/status", "/api/aviation/military", "/api/aviation/emergencies",
        "/api/aviation/gps-interference?lat=55&lon=20", "/api/sanctions/index?kind=aircraft",
    ])
    assert r["budget"] == ran.payloads["status"]["note"]
    assert "military" in r["status"] and "squawking 7500/7600/7700" in r["status"]


def test_emergency_banner_and_intel_note(ran):
    r = ran["enable"]
    assert r["banner"] == "block" and r["bannerRole"] == "alert"
    assert r["bannerText"].startswith("EMERGENCY SQUAWK 1 airborne. ")
    assert "7700 general emergency: TST7700 (A320)" in r["bannerText"]
    assert "PRIVATE" not in r["bannerText"]  # the private C172 is on the ground
    assert r["feed"].count("Squawk 7700 (general emergency): TST7700 A320") == 1
    assert not any("XTEST2" in f or "PRIVATE" in f for f in r["feed"])


def test_gps_hexes_follow_the_server_levels(ran):
    r = ran["enable"]
    hexes = ran.payloads["gps"]["hexes"]
    assert hexes and r["hexes"] is not None and len(r["hexes"]) == len(hexes)
    for drawn, h in zip(r["hexes"], hexes, strict=True):
        level = r["levels"][h["level"]]
        assert drawn == {"color": level["color"], "alpha": level["alpha"], "corners": 6,
                         "pick": "function"}
    assert r["translucent"] is True
    assert r["gps"].startswith(f"{len(hexes)} hexes from "
                               f"{ran.payloads['gps']['aircraft_counted']} aircraft")


scenario("dead_reckoning", r"""
  liveRoutes();
  var k = await enabled();
  NOW += 600 * 1000;
  window.SDAAviation._tick();
  var mid = snapshot(k);
  var midBanner = banner().textContent;
  buttonNamed(k.ctx.mount, 'EST').fire('click');
  var estOff = snapshot(k);
  var pressed = buttonNamed(k.ctx.mount, 'EST').getAttribute('aria-pressed');
  buttonNamed(k.ctx.mount, 'EST').fire('click');
  NOW += 700 * 1000;
  window.SDAAviation._tick();
  return { mid: mid, midBanner: midBanner, estOff: estOff, pressed: pressed,
           after: snapshot(k), count: countEl.textContent, banner: banner().style.display };
""")


def test_dead_reckoned_tracks_are_hollow_estimates(ran):
    r = ran["dead_reckoning"]
    p = ran.payloads
    want = expected_tracks(p, p["military"]["server_now"] + 600)
    assert set(r["mid"]) == set(want)
    for hex_id, track in r["mid"].items():
        w = want[hex_id]
        if w["state"] == "estimated":
            assert track["icons"] == [w["kind"] + "-est"], hex_id
            assert " EST " in track["label"]
            assert track["lat"] == pytest.approx(w["lat"], abs=1e-4)
            assert track["lon"] == pytest.approx(w["lon"], abs=1e-4)
        else:
            assert track["icons"] == [w["kind"]] and " EST " not in track["label"]
    assert "fff001" in r["mid"] and r["mid"]["fff001"]["icons"] == ["emerg-est"]
    assert "estimated, last report 10 min ago" in r["midBanner"]


def test_estimates_hide_and_expire(ran):
    r = ran["dead_reckoning"]
    assert r["pressed"] == "false"
    assert all(" EST " not in t["label"] for t in r["estOff"].values())
    assert r["after"] == {} and r["count"] == "0" and r["banner"] == "none"


scenario("emergency_cleared", r"""
  liveRoutes();
  var k = await enabled();
  var before = banner().style.display;
  route('/api/aviation/emergencies', { aircraft: [], count: 0,
    server_now: PAYLOADS.emergencies.server_now });
  await window.SDAAviation.refresh();
  await settle();
  return { before: before, after: banner().style.display, tracks: Object.keys(snapshot(k)),
           requests: REQUESTS.filter(function (u) { return u.indexOf('sanctions') >= 0; }) };
""")


def test_cleared_emergency_leaves_the_map(ran):
    r = ran["emergency_cleared"]
    assert r["before"] == "block" and r["after"] == "none"
    assert "fff001" not in r["tracks"] and "fff002" not in r["tracks"]
    assert r["requests"] == ["/api/sanctions/index?kind=aircraft"]  # loaded once an hour


scenario("toggles_and_disable", r"""
  liveRoutes();
  var k = await enabled();
  buttonNamed(k.ctx.mount, 'GPS').fire('click');
  var gpsOff = { hexes: hexPrims(k).length,
                 pressed: buttonNamed(k.ctx.mount, 'GPS').getAttribute('aria-pressed') };
  buttonNamed(k.ctx.mount, 'MIL').fire('click');
  var milOff = Object.keys(snapshot(k)).sort();
  buttonNamed(k.ctx.mount, 'SQUAWK').fire('click');
  var noBanner = banner().style.display;
  window.SDAAviation.setEnabled(false);
  var prims = k.ctx.viewer._prims;
  return { gpsOff: gpsOff, milOff: milOff, noBanner: noBanner,
           icons: prims[0].items.length, labels: prims[1].items.length, shown: prims[0].show,
           count: countEl.textContent, enabled: window.SDAAviation.isEnabled(),
           status: panelText(k, 'avi-status') };
""")


def test_toggles_and_disable_clear_everything(ran):
    r = ran["toggles_and_disable"]
    assert r["gpsOff"] == {"hexes": 0, "pressed": "false"}
    assert r["milOff"] == ["fff001", "fff002"]  # only the squawks remain
    assert r["noBanner"] == "none"
    assert (r["icons"], r["labels"], r["shown"]) == (0, 0, False)
    assert r["count"] == "0" and r["enabled"] is False and r["status"] == ""


scenario("sanctioned_ring", r"""
  liveRoutes();
  route('/api/sanctions/index', { regs: ['XTEST1'], hex: [], list_loaded: true });
  var k = await enabled();
  return snapshot(k).fff001;
""")


def test_sanctioned_aircraft_get_a_ring(ran):
    r = ran["sanctioned_ring"]
    assert r["icons"] == ["emerg", "sanction"]
    assert r["label"] == "TST7700 7700 SANCTIONS LIST"


# --- the aircraft card ---------------------------------------------------------------

scenario("card_route", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  route('/api/aviation/aircraft/406a93', PAYLOADS.card_baw);
  route('/api/sanctions/aircraft', { listed: false, matches: [], possible_matches: [],
    list_loaded: true });
  var k = makeCtx();
  window.SDAAviation.init(k.ctx);
  await window.SDAAviation.openCard('406A93', { callsign: 'BAW117', lat: 52, lon: -20 });
  await settle();
  var card = cardOf(k);
  return { requests: REQUESTS, text: card.textContent,
           links: byTag(card, 'A').map(function (a) {
             return [a.getAttribute('href'), a.textContent, a.getAttribute('rel'),
                     a.getAttribute('target')]; }) };
""")


def test_card_shows_route_and_liveatc_links(ran):
    r = ran["card_route"]
    assert r["requests"] == [
        "/api/aviation/aircraft/406a93?callsign=BAW117&lat=52.0000&lon=-20.0000",
        "/api/sanctions/aircraft?reg=G-XWBA",
    ]
    for want in ("BAW117", "G-XWBA", "A35K", "38,000 ft", "480 kt", "Reported",
                 "EGLL", "KJFK", "not reported by the aircraft", "BAW (airline code"):
        assert want in r["text"], want
    assert "Sanctions" not in r["text"] and "Nearest" not in r["text"]
    assert r["links"] == [
        ["https://www.liveatc.net/search/?icao=EGLL", "EGLL", "noopener noreferrer", "_blank"],
        ["https://www.liveatc.net/search/?icao=KJFK", "KJFK", "noopener noreferrer", "_blank"],
    ]


scenario("card_sanctions", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  var c = JSON.parse(JSON.stringify(PAYLOADS.card_baw));
  c.hex = 'fff009'; c.registration = 'X-TEST9'; c.callsign = 'XTEST9'; c.route = null;
  route('/api/aviation/aircraft/fff009', c);
  var k = makeCtx();
  window.SDAAviation.init(k.ctx);
  var out = {};
  var answers = {
    listed: [{ listed: true, programme: 'TEST-PROGRAMME', name: 'TEST LISTING',
               source: 'test list', possible_matches: [], list_loaded: true }, 200],
    possible: [{ listed: false, possible_matches: [{ reg: 'XTEST9' }], list_loaded: true }, 200],
    notloaded: [{ listed: true, programme: 'X', list_loaded: false }, 200],
    missing: [{}, 404],
    broken: [{}, 500]
  };
  for (var key in answers) {
    route('/api/sanctions/aircraft', answers[key][0], answers[key][1]);
    await window.SDAAviation.openCard('fff009', {});
    await settle();
    out[key] = cardOf(k).textContent;
  }
  out.requests = REQUESTS.filter(function (u) { return u.indexOf('/api/sanctions/') === 0; });
  return out;
""")


def test_card_sanctions_line_only_when_the_list_answers(ran):
    r = ran["card_sanctions"]
    assert "LISTED, TEST-PROGRAMME: TEST LISTING (test list)" in r["listed"]
    assert "Possible match on a previous registration, a lead only (1)" in r["possible"]
    for key in ("notloaded", "missing", "broken"):
        assert "Sanctions" not in r[key] and "X-TEST9" in r[key], key
    assert set(r["requests"]) == {"/api/sanctions/aircraft?reg=X-TEST9"}
    assert len(r["requests"]) == 5


scenario("card_private", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  route('/api/aviation/aircraft/fff002', PAYLOADS.card_private);
  var k = makeCtx();
  window.SDAAviation.init(k.ctx);
  await window.SDAAviation.openCard('fff002', { callsign: 'XTEST2' });
  await settle();
  return { requests: REQUESTS, text: cardOf(k).textContent };
""")


def test_private_card_withholds_identity(ran):
    r = ran["card_private"]
    assert r["requests"] == ["/api/aviation/aircraft/fff002?callsign=XTEST2"]  # no sanctions call
    assert r["text"].startswith("PRIVATE")
    assert "privacy" in r["text"]
    for hidden in ("Callsign", "Registration", "Operator", "X-TEST2", "XTEST2", "Route"):
        assert hidden not in r["text"], hidden


scenario("card_military", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  route('/api/aviation/aircraft/ae57d5', PAYLOADS.card_mil);
  route('/api/aviation/nearest-site', PAYLOADS.site);
  var k = makeCtx();
  window.SDAAviation.init(k.ctx);
  await window.SDAAviation.openCard('ae57d5', {});
  await settle();
  var text = cardOf(k).textContent;
  buttonNamed(cardOf(k), 'FLY TO').fire('click');
  return { requests: REQUESTS, text: text, flown: k.ctx.viewer.camera.flown.destination };
""")


def test_military_card_names_the_nearest_site(ran):
    r = ran["card_military"]
    card = ran.payloads["card_mil"]
    pos = card["position"]
    assert r["requests"][0] == "/api/aviation/aircraft/ae57d5"
    assert r["requests"][2] == (
        f"/api/aviation/nearest-site?lat={pos['lat']:.3f}&lon={pos['lon']:.3f}")
    near = ran.payloads["site"]["nearest"]
    assert (f"Nearest mapped military site: Test Airfield (airfield), {near['distance_km']:g} km"
            in r["text"])
    assert "(military)" in r["text"] and "C30J" in r["text"]
    assert r["flown"]["lat"] == pytest.approx(pos["lat"]) and r["flown"]["h"] == 120000


scenario("card_guards", r"""
  window.SDAAviation._setClock(function () { return NOW; });
  var c = JSON.parse(JSON.stringify(PAYLOADS.card_baw));
  c.route.origin.liveatc = 'javascript:alert(1)';
  c.route.destination.liveatc = 'https://www.liveatc.net/search/?icao=KJFK';
  route('/api/aviation/aircraft/406a93', c);
  var k = makeCtx();
  window.SDAAviation.init(k.ctx);
  var bad = [window.SDAAviation.openCard('xyz123', {}), window.SDAAviation.openCard(null),
             window.SDAAviation.openCard('12345', {})];
  var badRequests = REQUESTS.length;
  await window.SDAAviation.openCard('406a93', {});
  await settle();
  var hrefs = byTag(cardOf(k), 'A').map(function (a) { return a.getAttribute('href'); });
  await window.SDAAviation.openCard('abcdef', {});
  await settle();
  return { bad: bad, badRequests: badRequests, hrefs: hrefs, failed: cardOf(k).textContent };
""")


def test_card_guards_hex_and_links(ran):
    r = ran["card_guards"]
    assert r["bad"] == [None, None, None] and r["badRequests"] == 0
    assert r["hrefs"] == ["https://www.liveatc.net/search/?icao=KJFK"]
    assert r["failed"] == "Aircraft card did not load."


# --- clicks, the banner and civil estimates ----------------------------------------------

scenario("clicks", r"""
  liveRoutes();
  route('/api/aviation/aircraft/', PAYLOADS.card_mil);
  var k = await enabled();
  var start = REQUESTS.length;
  var click = HANDLERS[0].actions[2];
  PICKED = { id: { name: 'DAL1173 ', properties: {
    type: { getValue: function () { return 'aircraft'; } }, icao24: 'a1b2c3' },
    position: { getValue: function () { return { lon: -84.5, lat: 36, h: 10000 }; } } } };
  click({ position: { x: 1, y: 2 } });
  await settle();
  PICKED = { id: { name: 'ISS', properties: { type: 'satellite', icao24: 'a1b2c4' } } };
  click({ position: { x: 1, y: 2 } });
  PICKED = { id: { sdaPick: function () {}, properties: { type: 'aircraft', icao24: 'a1b2c5' } } };
  click({ position: { x: 1, y: 2 } });
  await settle();
  var own = k.ctx.viewer._prims[0].items.filter(function (b) {
    return b.id.hex === 'ae57d5'; })[0];
  own.id.sdaPick();
  await settle();
  var asked = REQUESTS.slice(start).filter(function (u) {
    return u.indexOf('/api/aviation/aircraft/') === 0; });
  window.SDAAviation.setEnabled(false);
  var off = REQUESTS.length;
  PICKED = { id: { name: 'X', properties: { type: 'aircraft', icao24: 'a1b2c6' } } };
  click({ position: { x: 1, y: 2 } });
  await settle();
  return { asked: asked, afterOff: REQUESTS.length - off };
""")


def test_clicks_on_page_aircraft_and_own_markers_open_the_card(ran):
    r = ran["clicks"]
    assert r["asked"] == [
        "/api/aviation/aircraft/a1b2c3?callsign=DAL1173&lat=36.0000&lon=-84.5000",
        "/api/aviation/aircraft/ae57d5?callsign=C2011&lat=21.9995&lon=-159.0460",
    ]
    assert r["afterOff"] == 0  # the layer is off: clicks are left to the page


scenario("banner_click", r"""
  liveRoutes();
  route('/api/aviation/aircraft/', PAYLOADS.card_mil);
  var k = await enabled();
  banner().fire('click');
  await settle();
  return { flown: k.ctx.viewer.camera.flown.destination,
           asked: REQUESTS.filter(function (u) {
             return u.indexOf('/api/aviation/aircraft/') === 0; }) };
""")


def test_banner_click_flies_to_the_aircraft(ran):
    r = ran["banner_click"]
    fff001 = next(a for a in ran.payloads["emergencies"]["aircraft"] if a["hex"] == "fff001")
    assert r["flown"]["lat"] == pytest.approx(fff001["lat"], abs=1e-3)
    assert r["asked"] == ["/api/aviation/aircraft/fff001?callsign=TST7700&lat=47.2000&lon=8.9000"]


scenario("civil_estimate", r"""
  route('/api/aviation/', { aircraft: [], count: 0, hexes: [],
    server_now: PAYLOADS.military.server_now });
  route('/api/aviation/aircraft/', PAYLOADS.card_mil);
  var pos = { lon: -84.5, lat: 36, h: 10000 };
  window.layerState = { flights: true };
  function ent(name, vel, hd) { return { name: name, position: { getValue: function () {
    return pos; } }, properties: { type: 'aircraft', velocity: vel, heading: hd } }; }
  window.flightEntities = { 'flight-a1b2c3': ent('DAL1173', 231.5, 45),
                            'flight-b2c3d4': ent('UAL1', 240, 90) };
  var k = await enabled();
  await window.SDAAviation.openCard('a1b2c3', { callsign: 'DAL1173', lat: 36, lon: -84.5 });
  window.SDAAviation._tick();
  var present = snapshot(k);
  delete window.flightEntities['flight-a1b2c3'];
  delete window.flightEntities['flight-b2c3d4'];
  NOW += 300 * 1000;
  window.SDAAviation._tick();
  var est = snapshot(k);
  window.layerState.flights = false;
  window.SDAAviation._tick();
  return { present: present, est: est, off: snapshot(k) };
""")


def test_opened_civil_aircraft_is_carried_as_an_estimate(ran):
    r = ran["civil_estimate"]
    assert r["present"] == {}  # the page's own layer draws it while it is there
    assert list(r["est"]) == ["a1b2c3"]  # never-opened aircraft are not carried
    track = r["est"]["a1b2c3"]
    assert track["icons"] == ["civ-est"] and track["label"] == "DAL1173 EST 5 min"
    lat, lon = av.dead_reckon(36.0, -84.5, 45.0, 231.5 * KT_PER_MS, 300.0)
    assert track["lat"] == pytest.approx(lat, abs=1e-7)
    assert track["lon"] == pytest.approx(lon, abs=1e-7)
    assert math.isclose(av.haversine_km(36.0, -84.5, lat, lon), 231.5 * 300 / 1000, rel_tol=1e-3)
    assert r["off"] == {}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "aviation.js must stay ASCII"
