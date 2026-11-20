"""Tests for the globe maritime module (static/js/maritime.js).

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

from sda import maritime

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src" / "sda" / "templates" / "static" / "js" / "maritime.js"
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
  ev = ev || {}; ev.type = type; ev.target = ev.target || this;
  var fns = this.listeners[type] || [];
  for (var i = 0; i < fns.length; i++) fns[i].call(this, ev);
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
var countEl = new El('span'); countEl.id = 'count-maritime'; document.body.appendChild(countEl);

// ---- fake Cesium -------------------------------------------------------
function Coll() { this.items = []; this.show = true; }
Coll.prototype.add = function (o) { this.items.push(o); return o; };
Coll.prototype.removeAll = function () { this.items = []; };
function Col(css, a) { this.css = css; this.a = a === undefined ? 1 : a; }
Col.prototype.withAlpha = function (a) { return new Col(this.css, a); };
var Cesium = {
  PointPrimitiveCollection: function () { Coll.call(this); this.kind = 'points'; },
  LabelCollection: function () { Coll.call(this); this.kind = 'labels'; },
  PolylineCollection: function () { Coll.call(this); this.kind = 'lines'; },
  Material: { fromType: function (type, u) { return { type: type, uniforms: u }; } },
  Color: { fromCssColorString: function (c) { return new Col(c); }, BLACK: new Col('#000'),
    WHITE: new Col('#fff') },
  Cartesian3: {
    fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; },
    fromDegreesArray: function (flat) {
      var out = [];
      for (var i = 0; i < flat.length; i += 2) out.push({ lon: flat[i], lat: flat[i + 1] });
      return out;
    }
  },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  LabelStyle: { FILL_AND_OUTLINE: 2 }, HorizontalOrigin: { CENTER: 0 },
  VerticalOrigin: { CENTER: 0, BOTTOM: 1 }
};
['PointPrimitiveCollection', 'LabelCollection', 'PolylineCollection'].forEach(function (k) {
  Cesium[k].prototype = Object.create(Coll.prototype);
});
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

function makeViewer() {
  var prims = [];
  var moveEnd = [];
  var d = Math.PI / 180;
  return {
    scene: { primitives: { add: function (c) { prims.push(c); return c; } },
      globe: { ellipsoid: {} } },
    camera: {
      rect: { west: -10 * d, south: 30 * d, east: 5 * d, north: 40 * d },
      computeViewRectangle: function () { return this.rect; },
      moveEnd: { addEventListener: function (fn) { moveEnd.push(fn); } },
      flyTo: function (o) { this.flown = o; }
    },
    _prims: prims, _moveEnd: moveEnd
  };
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = { viewer: makeViewer(), api: '', isStatic: !!opts.isStatic, mount: new El('div'),
    addIntelItem: function (t) { feed.push(t); } };
  return { ctx: ctx, feed: feed };
}

// Collections in the order the module adds them.
function layers(k) {
  var p = k.ctx.viewer._prims;
  return { chokeLines: p[0], chokeLabels: p[1], trails: p[2], points: p[3], labels: p[4] };
}

function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }

function chokeAnswer(withKey) {
  var cps = window.SDAMaritime.CHOKEPOINTS.map(function (cp) {
    return { id: cp.id, name: cp.name, polygon: cp.polygon, note: 'note ' + cp.id,
      inside_now: withKey ? 3 : null, under_way_now: withKey ? 2 : null,
      transits_total: withKey ? 40 : null, transits_today: withKey ? 7 : null,
      by_day: withKey ? { '2026-11-18': 33, '2026-11-19': 7 } : {},
      sanctioned_now: withKey ? (cp.id === 'gibraltar' ? 1 : 0) : null };
  });
  return { key_configured: withKey, connected: withKey, error: withKey ? null :
    'AISSTREAM_API_KEY is needed for live vessels (a free key from aisstream.io)',
    chokepoints: cps, counting_since: withKey ? '2026-11-19T10:00:00Z' : '',
    server_started: '2026-11-19T09:59:00Z', day: '2026-11-19',
    rules: { transit_rule: 'Transit rule.', dark_rule: 'Dark rule.' } };
}

var ARTAVIL = { mmsi: '572469210', name: 'ARTAVIL', lat: 35.94, lon: -5.6, sog_kn: 11.2,
  cog_deg: 88, age_s: 12, chokepoint: 'gibraltar',
  sanction: { listed: true, matched_on: 'mmsi', source: 'OFAC SDN', programme: 'IRAN',
    name: 'ARTAVIL', imo: '9187629', remarks: 'Linked To: NATIONAL IRANIAN TANKER COMPANY.',
    url: 'https://sanctionssearch.ofac.treas.gov/Details.aspx?id=15036' } };
var LEAD = { mmsi: '222222222', name: 'GALAXY', lat: 36.5, lon: -4.0, sog_kn: 9, cog_deg: 10,
  age_s: 60, name_match: { listed: false, source: 'OFAC SDN', programme: 'RUSSIA-EO14024',
    name: 'GALAXY', imo: '9826902', remarks: 'r', note: 'Name only. Not confirmed.',
    url: 'javascript:alert(1)' } };
var PLAIN = { mmsi: 111111111, name: '', lat: 37.0, lon: -3.0, sog_kn: 0.1, cog_deg: null,
  age_s: 300 };
var DARK = { mmsi: '333333333', name: 'QUIET ONE', lat: 38.2, lon: 2.0, last_lat: 38.0,
  last_lon: 2.0, estimate: true, silent_s: 2700, run_km: 21.4, sog_kn: 14, cog_deg: 0,
  label: 'EST QUIET ONE (no AIS 45 min)' };
var BAD = { mmsi: '9', name: 'NOWHERE', lat: 'x', lon: 0 };

function vesselsAnswer() {
  return { key_configured: true, connected: true, vessels: [ARTAVIL, LEAD, PLAIN, BAD],
    dark: [DARK], count: 3, dark_count: 1, sanctioned_count: 1, capped: false };
}
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
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'maritime.js' });
  vm.runInContext(prelude, box, { filename: 'prelude' });
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
      try { vm.runInContext('window.SDAMaritime && window.SDAMaritime.setEnabled(false)', box); }
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
    work = tmp_path_factory.mktemp("maritime")
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


# --- pure helpers ----------------------------------------------------------------

scenario("helpers", r"""
  var M = window.SDAMaritime;
  var d = Math.PI / 180;
  function texts(lines) { return lines.map(function (l) { return l.text; }); }
  return {
    box: M.viewBox({ west: -10 * d, south: 30 * d, east: 5 * d, north: 40 * d }),
    boxCross: M.viewBox({ west: 170 * d, south: -10 * d, east: -170 * d, north: 10 * d }),
    boxNone: M.viewBox(null),
    ring: M.closedRing([[0, 1], [2, 3], [4, 5]]),
    centroid: M.centroid([[0, 0], [2, 0], [2, 2], [0, 2]]),
    ages: [M.formatAge(12), M.formatAge(2700), M.formatAge(7200), M.formatAge(null)],
    styles: [ARTAVIL, LEAD, PLAIN, DARK].map(function (v) {
      var s = M.vesselStyle(v); return [s.key, s.color, s.size, s.hollow]; }),
    labels: [ARTAVIL, LEAD, PLAIN, DARK].map(M.vesselLabel),
    cardListed: texts(M.vesselCard(ARTAVIL)),
    cardLead: texts(M.vesselCard(LEAD)),
    cardDark: texts(M.vesselCard(DARK)),
    lineKey: M.chokeLine(chokeAnswer(true).chokepoints[0]),
    lineNoKey: M.chokeLine(chokeAnswer(false).chokepoints[1]),
    labelKey: M.chokeLabel(chokeAnswer(true).chokepoints[1]),
    labelNoKey: M.chokeLabel(chokeAnswer(false).chokepoints[3]),
    clean: M.cleanVessels([ARTAVIL, BAD, PLAIN, null]).map(function (v) { return v.mmsi; }),
    cleanBad: [M.cleanVessels(null).length, M.cleanVessels('x').length],
    urls: [M.safeUrl('https://example.org/a'), M.safeUrl('javascript:alert(1)'),
           M.safeUrl('http://x'), M.safeUrl(null)],
    q: M.query([['south', 30], ['x', ''], ['y', null], ['limit', 3000]]),
    chokes: M.CHOKEPOINTS
  };
""")


def test_view_box_and_geometry_helpers(ran):
    h = ran["helpers"]
    assert h["box"] == {"south": 30, "west": -10, "north": 40, "east": 5}
    assert h["boxCross"] == {"south": -10, "west": -180, "north": 10, "east": 180}
    assert h["boxNone"] == {"south": -90, "west": -180, "north": 90, "east": 180}
    assert h["ring"] == [0, 1, 2, 3, 4, 5, 0, 1]
    assert h["centroid"] == {"lon": 1, "lat": 1}
    assert h["ages"] == ["12 s", "45 min", "2.0 h", ""]
    assert h["q"] == "?south=30&limit=3000"


def test_vessel_classes_and_labels(ran):
    h = ran["helpers"]
    assert h["styles"] == [
        ["listed", "#ff4040", 9, False],
        ["lead", "#ffb000", 7, False],
        ["vessel", "#7fd1ff", 5, False],
        ["dark", "#c58cff", 9, True],
    ]
    assert h["labels"] == ["SANCTIONED ARTAVIL", "GALAXY", "MMSI 111111111",
                           "EST QUIET ONE (no AIS 45 min)"]


def test_cards_say_what_is_known_and_what_is_estimated(ran):
    h = ran["helpers"]
    listed = h["cardListed"]
    assert listed[0] == "ARTAVIL"
    assert "SANCTIONED: OFAC SDN, IRAN" in listed
    assert "Listed as ARTAVIL, IMO 9187629 (MMSI match)" in listed
    assert "Linked To: NATIONAL IRANIAN TANKER COMPANY." in listed
    assert "Inside Strait of Gibraltar" in listed
    lead = h["cardLead"]
    assert "NAME MATCH, UNCONFIRMED: OFAC SDN, RUSSIA-EO14024" in lead
    assert not any(line.startswith("SANCTIONED") for line in lead)
    dark = h["cardDark"]
    assert any(line.startswith("ESTIMATE: no AIS for 45 min. Position dead-reckoned 21.4 km")
               for line in dark)
    assert "Last report at 38.000, 2.000" in dark


def test_chokepoint_text(ran):
    h = ran["helpers"]
    assert h["lineKey"] == ("Strait of Gibraltar: 3 in | 7 today | 40 since start | "
                            "1 sanctioned")
    assert h["lineNoKey"] == "Strait of Hormuz"
    assert h["labelKey"] == "HORMUZ  3 IN / 7 TODAY"
    assert h["labelNoKey"] == "SUEZ CANAL"


def test_cleaning_and_links(ran):
    h = ran["helpers"]
    assert h["clean"] == ["572469210", "111111111"]
    assert h["cleanBad"] == [0, 0]
    assert h["urls"] == ["https://example.org/a", "", "", ""]


def test_built_in_polygons_match_the_server(ran):
    js = ran["helpers"]["chokes"]
    assert [c["id"] for c in js] == [cp.id for cp in maritime.CHOKEPOINTS]
    for c, cp in zip(js, maritime.CHOKEPOINTS, strict=True):
        assert c["name"] == cp.name
        assert c["polygon"] == [list(p) for p in cp.polygon], cp.id


# --- module behaviour ---------------------------------------------------------------

scenario("static_build", r"""
  var k = makeCtx({ isStatic: true });
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  var L = layers(k);
  var status = byClass(k.ctx.mount, 'mar-status')[0].textContent;
  return { requests: REQUESTS, lines: L.chokeLines.items.length,
           labels: L.chokeLabels.items.map(function (l) { return l.text; }),
           ring: L.chokeLines.items[0].positions.length, status: status };
""")


def test_static_build_draws_chokepoints_without_requests(ran):
    r = ran["static_build"]
    assert r["requests"] == []
    assert r["lines"] == 9
    assert r["ring"] == 5  # four corners, closed
    assert r["labels"][0] == "GIBRALTAR"
    assert "Static demo" in r["status"]


scenario("no_key", r"""
  route('/api/maritime/chokepoints', chokeAnswer(false));
  route('/api/maritime/vessels', { key_configured: false, vessels: [], dark: [] });
  var k = makeCtx();
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  var L = layers(k);
  var items = byClass(k.ctx.mount, 'mar-item').map(function (b) { return b.textContent; });
  // Clicking a chokepoint explains why there are no counts.
  byClass(k.ctx.mount, 'mar-item')[1].fire('click');
  return { requests: REQUESTS, lines: L.chokeLines.items.length,
           status: byClass(k.ctx.mount, 'mar-status')[0].textContent, items: items,
           card: byClass(k.ctx.mount, 'mar-card')[0].textContent, feed: k.feed,
           rule: byClass(k.ctx.mount, 'mar-rule')[0].textContent };
""")


def test_without_the_key_polygons_draw_and_vessels_are_not_asked(ran):
    r = ran["no_key"]
    assert r["requests"] == ["/api/maritime/chokepoints"]
    assert r["lines"] == 9
    assert "AISSTREAM_API_KEY" in r["status"]
    assert r["items"][:2] == ["Strait of Gibraltar", "Strait of Hormuz"]
    assert "AISSTREAM_API_KEY" in r["card"]
    assert r["feed"] == ["Maritime: 9 chokepoint monitors (no AIS key on the server)"]
    assert r["rule"] == "Transits: Transit rule. Dark ships: Dark rule."


scenario("server_missing", r"""
  var k = makeCtx();
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  return { requests: REQUESTS, lines: layers(k).chokeLines.items.length,
           status: byClass(k.ctx.mount, 'mar-status')[0].textContent };
""")


def test_a_server_without_the_module_degrades_quietly(ran):
    r = ran["server_missing"]
    assert r["requests"] == ["/api/maritime/chokepoints"]  # one 404, then quiet
    assert r["lines"] == 9
    assert "not on this server" in r["status"]


scenario("with_key", r"""
  route('/api/maritime/chokepoints', chokeAnswer(true));
  route('/api/maritime/vessels', vesselsAnswer());
  var k = makeCtx();
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  await window.SDAMaritime.refresh();  // a second poll must not repeat the intel note
  await settle();
  var L = layers(k);
  var pts = L.points.items.map(function (p) {
    return { id: p.id.id, size: p.pixelSize, css: p.color.css, a: p.color.a,
             outline: p.outlineColor.css, ow: p.outlineWidth };
  });
  var lists = byClass(k.ctx.mount, 'mar-list').map(function (l) {
    return byClass(l, 'mar-item').map(function (b) { return b.textContent; }); });
  return {
    requests: REQUESTS, points: pts,
    labels: L.labels.items.map(function (l) { return l.text; }),
    trails: L.trails.items.map(function (t) { return [t.positions, t.material.type]; }),
    chokeLabel: L.chokeLabels.items[0].text, count: countEl.textContent,
    summary: byClass(k.ctx.mount, 'mar-summary')[0].textContent,
    status: byClass(k.ctx.mount, 'mar-status')[0].textContent,
    lists: lists, feed: k.feed
  };
""")


def test_vessels_drawn_by_class(ran):
    r = ran["with_key"]
    assert r["requests"][0] == "/api/maritime/chokepoints"
    assert r["requests"][1] == ("/api/maritime/vessels?south=30&west=-10&north=40&east=5"
                                "&limit=3000")
    by_id = {p["id"]: p for p in r["points"]}
    assert set(by_id) == {"572469210", "222222222", "111111111", "333333333"}  # BAD dropped
    assert by_id["572469210"]["css"] == "#ff4040" and by_id["572469210"]["size"] == 9
    assert by_id["222222222"]["css"] == "#ffb000"
    assert by_id["111111111"]["size"] == 5
    hollow = by_id["333333333"]
    assert hollow["a"] == 0 and hollow["outline"] == "#c58cff" and hollow["ow"] == 2
    # Listed and dark win the label budget; a plain vessel is not labelled.
    assert r["labels"] == ["SANCTIONED ARTAVIL", "EST QUIET ONE (no AIS 45 min)", "GALAXY"]
    assert r["trails"] == [[[{"lon": 2, "lat": 38}, {"lon": 2, "lat": 38.2}], "PolylineDash"]]
    assert r["chokeLabel"] == "GIBRALTAR  3 IN / 7 TODAY"
    assert r["count"] == "4"
    assert r["summary"] == "3 vessels in view | 1 sanctioned | 1 dark (estimated)"
    assert "Counting since 2026-11-19T10:00:00Z" in r["status"]


def test_lists_and_intel_feed(ran):
    r = ran["with_key"]
    chokes, listed, dark = r["lists"]
    assert len(chokes) == 9 and chokes[0].startswith("Strait of Gibraltar: 3 in")
    assert listed == ["ARTAVIL | IRAN"]
    assert dark == ["QUIET ONE | no AIS 45 min"]
    assert r["feed"] == ["Maritime: 9 chokepoint monitors",
                         "Sanctioned vessel in view: ARTAVIL (OFAC SDN, IRAN)"]


scenario("picks", r"""
  route('/api/maritime/chokepoints', chokeAnswer(true));
  route('/api/maritime/vessels', vesselsAnswer());
  var k = makeCtx();
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  var L = layers(k);
  var card = byClass(k.ctx.mount, 'mar-card')[0];
  function pick(mmsi) {
    L.points.items.filter(function (p) { return p.id.id === mmsi; })[0].id.sdaPick();
    var links = byTag(card, 'A').map(function (a) { return [a.getAttribute('href'),
      a.getAttribute('rel')]; });
    return { text: card.children.map(function (c) { return c.textContent; }), links: links };
  }
  var listed = pick('572469210');
  var selectedSize = L.points.items.filter(function (p) {
    return p.id.id === '572469210'; })[0].pixelSize;
  var lead = pick('222222222');
  var dark = pick('333333333');
  L.chokeLines.items[3].id.sdaPick();
  var choke = card.children.map(function (c) { return c.textContent; });
  return { listed: listed, lead: lead, dark: dark, choke: choke, selectedSize: selectedSize,
           flown: !!k.ctx.viewer.camera.flown };
""")


def test_clicking_a_vessel_opens_its_card(ran):
    r = ran["picks"]
    assert "SANCTIONED: OFAC SDN, IRAN" in r["listed"]["text"]
    assert r["listed"]["links"] == [["https://sanctionssearch.ofac.treas.gov/Details.aspx?id=15036",
                                     "noopener noreferrer"]]
    assert r["selectedSize"] == 13
    assert r["lead"]["links"] == []  # a javascript: link is never set
    assert any(t.startswith("ESTIMATE") for t in r["dark"]["text"])


def test_clicking_a_chokepoint_shows_its_days(ran):
    r = ran["picks"]
    assert r["choke"][0] == "Suez Canal"
    assert "2026-11-18: 33 transits" in r["choke"]
    assert r["flown"] is True


scenario("disable", r"""
  route('/api/maritime/chokepoints', chokeAnswer(true));
  route('/api/maritime/vessels', vesselsAnswer());
  var k = makeCtx();
  window.SDAMaritime.init(k.ctx);
  window.SDAMaritime.setEnabled(true);
  await settle();
  window.SDAMaritime.setEnabled(false);
  var L = layers(k);
  var before = REQUESTS.length;
  k.ctx.viewer._moveEnd.forEach(function (fn) { fn(); });
  await new Promise(function (r) { setTimeout(r, 450); });
  return { points: L.points.items.length, labels: L.labels.items.length,
           shown: [L.points.show, L.chokeLines.show], count: countEl.textContent,
           enabled: window.SDAMaritime.isEnabled(), asked: REQUESTS.length - before };
""")


def test_disable_clears_and_stops_asking(ran):
    r = ran["disable"]
    assert r == {"points": 0, "labels": 0, "shown": [False, False], "count": "0",
                 "enabled": False, "asked": 0}


scenario("init_guards", r"""
  var noMount = window.SDAMaritime.init({ viewer: makeViewer() });
  var noViewer = window.SDAMaritime.init({ mount: new El('div') });
  window.SDAMaritime.setEnabled(true);  // nothing initialised: a no-op
  var k = makeCtx();
  var ok = window.SDAMaritime.init(k.ctx);
  var head = byTag(k.ctx.mount, 'H3')[0];
  head.fire('click');
  return { noMount: noMount, noViewer: noViewer, ok: ok,
           collapsed: k.ctx.mount.children[0].has('collapsed'), title: head.textContent,
           style: !!document.getElementById('sda-maritime-style'), requests: REQUESTS };
""")


def test_init_guards_and_panel(ran):
    r = ran["init_guards"]
    assert r == {"noMount": False, "noViewer": False, "ok": True, "collapsed": True,
                 "title": "Maritime", "style": True, "requests": []}


def test_module_text_is_ascii_and_never_sets_html():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert "insertAdjacentHTML" not in text and "outerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "maritime.js must stay ASCII"
    assert "AISSTREAM_API_KEY" in text and "aisstream.io" in text
