"""Tests for the globe object dossier (static/js/dossier.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub (no
jsdom, no browser), the way tests/test_globe_aurora.py does it. The fake `fetch`
answers from a table (a reply can be held back to test answers that arrive out of
order) and `setTimeout` only records its callback, so nothing here touches the
network or waits. categories.js is loaded into the same context so the two filters
can be checked together. Skips cleanly when Node is not installed.

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
JS = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "static" / "js"
MODULE = JS / "dossier.js"
CATEGORIES = JS / "categories.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

ATTRIBUTION = "COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)"

PRELUDE = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
  this.className = ''; this.id = ''; this.parentNode = null; this._text = '';
  this.value = ''; this.disabled = false;
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
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
// categories.js (loaded alongside) toggles classes
Object.defineProperty(El.prototype, 'classList', {
  get: function () {
    var el = this;
    function list() { return el.className.split(/\s+/).filter(Boolean); }
    function set(names) { el.className = names.join(' '); }
    return {
      contains: function (c) { return list().indexOf(c) >= 0; },
      add: function (c) { if (list().indexOf(c) < 0) set(list().concat(c)); },
      remove: function (c) { set(list().filter(function (x) { return x !== c; })); },
      toggle: function (c, force) {
        var on = force === undefined ? !this.contains(c) : !!force;
        if (on) this.add(c); else this.remove(c);
        return on;
      }
    };
  }
});
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

// ---- document with page events ---------------------------------------------
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
    var list = DOC_LISTENERS[t] || [];
    var at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }
};
function emit(type, detail) {
  (DOC_LISTENERS[type] || []).slice().forEach(function (fn) {
    fn({ type: type, detail: detail }); });
}
function listeners(type) { return (DOC_LISTENERS[type] || []).length; }
var window = this;

// ---- timers: record retries instead of waiting for them ----------------------
var realSetTimeout = setTimeout;
var TIMERS = [];
this.setTimeout = function (fn, ms) { TIMERS.push({ fn: fn, ms: ms, done: false });
  return TIMERS.length; };
function runTimers() {
  var due = TIMERS.filter(function (t) { return !t.done; });
  due.forEach(function (t) { t.done = true; t.fn(); });
  return due.length;
}
function tick() { return new Promise(function (r) { realSetTimeout(r, 0); }); }
async function settle() { for (var i = 0; i < 8; i++) await tick(); }

// ---- fake fetch ----------------------------------------------------------------
var REQUESTS = [];
var ROUTES = [];
var HELD = [];
function route(prefix, body, status, held) { ROUTES.unshift({ prefix: prefix, body: body,
  status: status || 200, held: !!held }); }
function answer(r) {
  return { ok: r.status < 400, status: r.status,
    json: function () { return Promise.resolve(JSON.parse(JSON.stringify(r.body))); } };
}
function fetch(url) {
  REQUESTS.push(url);
  for (var i = 0; i < ROUTES.length; i++) {
    if (url.indexOf(ROUTES[i].prefix) === 0) {
      var r = ROUTES[i];
      if (r.held) {
        return new Promise(function (res) { HELD.push(function () { res(answer(r)); }); });
      }
      return Promise.resolve(answer(r));
    }
  }
  return Promise.resolve({ ok: false, status: 404, json: function () {
    return Promise.resolve({}); } });
}
function count(path) { return REQUESTS.filter(function (u) {
  return u.length >= path.length && u.slice(-path.length) === path; }).length; }

// ---- recorded replies (shapes of GET /api/satcat/...) --------------------------
var ATTR = 'COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)';
var CARD_ISS = {
  norad_id: 25544, name: 'ISS (ZARYA)', intl_designator: '1998-067A', owner_code: 'CIS',
  owner: 'Russia / former USSR', object_type: 'PAYLOAD', object_type_label: 'Payload',
  status: 'On orbit', on_orbit: true, launch_date: '1998-11-20', launch_site_code: 'TTMTR',
  launch_site: 'Baikonur Cosmodrome, Kazakhstan', decay_date: '', rcs_size: 'LARGE',
  rcs_label: 'Large (over 1 m2)', spacebook_id: 'bf72c797-cee3-45b2-8de1-5bc16ac62ea8',
  summary: 'Payload, owned by Russia / former USSR, launched 1998, on orbit.', notes: [],
  details: { mass_kg: 450000.0, span_m: 108.0, size_source: 'COMSPOC', manufacturer: 'KHRR',
             bus: '77KS', operator: 'JSC', parent: 'S25545', parent_norad_id: 25545,
             parent_name: 'SL-13 R/B', source: 'via Spacebook',
             fetched_at: '2026-11-19T22:31:13+00:00' },
  details_status: 'loaded', source: 'spacebook', fetched_at: '2026-11-19T22:30:50+00:00',
  age_hours: 1.0, stale: false, attribution: ATTR
};
var CARD_CSS = Object.assign({}, CARD_ISS, {
  norad_id: 48274, name: 'CSS (TIANHE-1)', intl_designator: '2021-035A', owner_code: 'PRC',
  owner: 'China', launch_date: '2021-04-29', launch_site_code: 'WSC',
  launch_site: 'Wenchang Space Launch Site, China',
  summary: 'Payload, owned by China, launched 2021, on orbit.', details: null,
  details_status: 'unavailable', age_hours: 30.2, stale: true
});
var CARD_DEB = Object.assign({}, CARD_ISS, {
  norad_id: 29734, name: 'FENGYUN 1C DEB', intl_designator: '1999-025Y', owner_code: 'PRC',
  owner: 'China', object_type: 'DEBRIS', object_type_label: 'Debris', launch_site: 'TSC',
  launch_site_code: 'TSC', rcs_label: '', rcs_size: 'SMALL', decay_date: '2031-01-02',
  status: 'Decayed', notes: ['Note one.', 'Note two.'],
  details: { mass_kg: 2, span_m: 0.2, size_source: 'GCAT', manufacturer: '', bus: '',
             operator: 'CASC', parent: 'A09513', parent_norad_id: null, parent_name: '' },
  source: 'space-track', age_hours: 0.25
});
var FACETS = {
  total: 36671, on_orbit_only: true, year_min: 1958, year_max: 2026, no_launch_date: 1114,
  owners: [{ code: 'US', name: 'United States', count: 18736 },
           { code: 'PRC', name: 'China', count: 6245 },
           { code: '', name: 'Not published', count: 1114 },
           { code: '<b>', name: 'evil', count: 1 },
           { code: 'NATO', name: 'NATO', count: 9 }, 'junk', null],
  types: [], rcs: [], decades: [], years: [], source: 'spacebook', attribution: ATTR
};

// ---- page context ---------------------------------------------------------------
function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var refreshes = { n: 0 };
  var list = opts.sats || [25544, 29733, 29734, 48274, 66906].map(function (id) {
    return { noradId: id, name: 'SAT ' + id }; });
  var sats = list.map(function (s) {
    return { noradId: s.noradId, name: s.name, regime: s.regime || 'LEO', hidden: false }; });
  var byNoradId = {};
  sats.forEach(function (s) { byNoradId[s.noradId] = s; });
  var engine = { sats: sats, byNoradId: byNoradId,
                 selectedId: opts.selectedId == null ? null : opts.selectedId };
  var ctx = {
    api: opts.api || '', isStatic: !!opts.isStatic,
    mount: opts.noMount ? undefined : new El('div'), satEngine: engine,
    addIntelItem: function (t) { if (opts.throwing) throw new Error('feed'); feed.push(t); },
    refreshSatVisibility: function () { refreshes.n++;
      if (opts.throwing) throw new Error('refresh'); }
  };
  return { ctx: ctx, feed: feed, refreshes: refreshes, sats: sats, engine: engine };
}
function text(k, cls) { var e = byClass(k.ctx.mount, cls)[0]; return e ? e.textContent : null; }
function cls(k, c) { var e = byClass(k.ctx.mount, c)[0]; return e ? e.className : null; }
function rows(k) { return byClass(k.ctx.mount, 'info-row').map(function (r) {
  return [r.children[0].textContent, r.children[1].textContent]; }); }
function rowValue(k, label) { var r = rows(k).filter(function (x) { return x[0] === label; })[0];
  return r ? r[1] : null; }
function control(k, c) { return byClass(k.ctx.mount, c)[0]; }
function setVal(k, c, v) { control(k, c).value = v; }
function click(k, c) { control(k, c).fire('click'); }
function hidden(k) { var out = {}; k.sats.forEach(function (s) { out[s.noradId] = s.hidden; });
  return out; }
function masks(k) { var out = {};
  k.sats.forEach(function (s) { out[s.noradId] = s.hideMask | 0; }); return out; }
function visible(k) { return k.sats.filter(function (s) { return !s.hidden; }).map(function (s) {
  return s.noradId; }); }
function select(k, id) { k.engine.selectedId = id; emit('sda:satellite-selected',
  { noradId: id, name: 'x' }); }
function deselect(k, id) { k.engine.selectedId = null; emit('sda:satellite-deselected',
  { noradId: id }); }
"""

DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modules = JSON.parse(process.argv[3]);
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = { URL: URL, setTimeout: setTimeout, clearTimeout: clearTimeout,
              Promise: Promise, JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  modules.forEach(function (m) {
    vm.runInContext(fs.readFileSync(m, 'utf8'), box, { filename: m });
  });
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
    work = tmp_path_factory.mktemp("dossier")
    (work / "driver.js").write_text(DRIVER, encoding="utf-8")
    (work / "prelude.js").write_text(PRELUDE, encoding="utf-8")
    (work / "scenarios.json").write_text(json.dumps(SCENARIOS), encoding="utf-8")
    modules = json.dumps([CATEGORIES.as_posix(), MODULE.as_posix()])
    proc = subprocess.run(
        [NODE, str(work / "driver.js"), str(work / "prelude.js"), modules,
         str(work / "scenarios.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- pure helpers --------------------------------------------------------------

scenario("pure", r"""
  var D = window.SDADossier;
  return {
    keys: [25544, '25544', '025544', ' 7 ', 'A0001', 'a0001', 'Z9999', 'I0001', 'O1234',
           0, -1, 1.5, '', null, {}, NaN, Infinity, '1234567'].map(D.noradKey),
    mass: [450000, 1234.5, 12.34, 1.234, 0, -1, null, 'x', NaN].map(D.fmtMass),
    span: [108, 12.34, 0.3, 0, null, -2].map(D.fmtSpan),
    age: [0.001, 0.5, 1, 23.94, 72, null, 'old'].map(D.fmtAge),
    query: [
      D.buildQuery({ owner: 'PRC', type: 'PAYLOAD', rcs: '', yearFrom: 1998, yearTo: 2005 }),
      D.buildQuery({ type: 'ROCKET BODY' }),
      D.buildQuery({ owner: 'A&B=C', rcs: 'SMALL' }),
      D.buildQuery({ yearFrom: 2000.4 }),
      D.buildQuery({}), D.buildQuery(null), D.buildQuery('x'),
      D.buildQuery({ owner: 7, yearFrom: '1999' })
    ],
    rowsNull: D.cardRows(null), rowsString: D.cardRows('x'),
    hideBit: D.HIDE_BIT
  };
""")


def test_norad_keys_match_the_server_decoder(ran):
    assert ran["pure"]["keys"] == [
        "25544", "25544", "25544", "7", "100001", "100001", "339999", "", "",
        "", "", "", "", "", "", "", "", ""]


def test_formatters(ran):
    p = ran["pure"]
    assert p["mass"] == ["450,000 kg", "1,235 kg", "12.3 kg", "1.23 kg", "", "", "", "", ""]
    assert p["span"] == ["108 m", "12.3 m", "0.3 m", "", "", ""]
    assert p["age"] == ["1 min old", "30 min old", "1 h old", "23.9 h old", "3 days old", "", ""]


def test_build_query(ran):
    assert ran["pure"]["query"] == [
        "owner=PRC&type=PAYLOAD&year_from=1998&year_to=2005",
        "type=ROCKET%20BODY",
        "owner=A%26B%3DC&rcs=SMALL",
        "year_from=2000",
        "", "", "",
        "owner=7",
    ]
    assert ran["pure"]["rowsNull"] == [] and ran["pure"]["rowsString"] == []
    assert ran["pure"]["hideBit"] == 2


# --- the card ---------------------------------------------------------------------

scenario("card", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/25544', CARD_ISS);
  route('/api/satcat/29734', CARD_DEB);
  var k = makeCtx();
  var ok = window.SDADossier.init(k.ctx);
  var idle = { status: text(k, 'dos-status'), title: byTag(k.ctx.mount, 'H3')[0].textContent };
  emit('sda:satellite-selected', { noradId: 25544, name: 'ISS (ZARYA)' });
  var loading = { title: text(k, 'dos-title'), status: text(k, 'dos-status') };
  await settle();
  var iss = { title: text(k, 'dos-title'), summary: text(k, 'dos-summary'), rows: rows(k),
              notes: text(k, 'dos-note'), status: text(k, 'dos-status'),
              source: text(k, 'dos-source') };
  emit('sda:satellite-selected', { noradId: '25544' });  // the same object again
  window.SDADossier.show(25544);
  await settle();
  var again = REQUESTS.slice();
  emit('sda:satellite-selected', { noradId: 29734 });
  await settle();
  var deb = { rows: rows(k), notes: text(k, 'dos-note'), source: text(k, 'dos-source') };
  return { ok: ok, idle: idle, loading: loading, iss: iss, again: again, deb: deb,
           timers: TIMERS.length, styles: byTag(document.head, 'STYLE').length };
""")


def test_card_fills_on_select(ran):
    c = ran["card"]
    assert c["ok"] is True
    assert c["idle"]["title"] == "Object Dossier"
    assert c["idle"]["status"].startswith("Select a satellite")
    assert c["loading"] == {"title": "NORAD 25544", "status": "Loading dossier..."}
    iss = c["iss"]
    assert iss["title"] == "ISS (ZARYA)"
    assert iss["summary"] == "Payload, owned by Russia / former USSR, launched 1998, on orbit."
    assert iss["rows"] == [
        ["NORAD ID", "25544"], ["INTL DESIGNATOR", "1998-067A"],
        ["OWNER", "Russia / former USSR (CIS)"], ["TYPE", "Payload"], ["STATUS", "On orbit"],
        ["LAUNCHED", "1998-11-20"], ["LAUNCH SITE", "Baikonur Cosmodrome, Kazakhstan (TTMTR)"],
        ["RCS SIZE", "Large (over 1 m2)"], ["MASS", "450,000 kg"], ["SPAN", "108 m"],
        ["SIZE FROM", "COMSPOC adopted"], ["MANUFACTURER", "KHRR"], ["BUS", "77KS"],
        ["OPERATOR", "JSC"], ["PARENT", "SL-13 R/B (S25545)"],
    ]
    assert iss["notes"] == "" and iss["status"] == ""
    assert iss["source"] == f"Source: COMSPOC Spacebook, 1 h old. {ATTRIBUTION}."


def test_same_object_is_fetched_once(ran):
    c = ran["card"]
    assert c["again"] == ["/api/satcat/facets", "/api/satcat/25544"]
    assert c["timers"] == 0 and c["styles"] == 1


def test_card_rows_skip_what_is_empty(ran):
    deb = ran["card"]["deb"]
    labels = [r[0] for r in deb["rows"]]
    assert "DECAYED" in labels and "MANUFACTURER" not in labels and "BUS" not in labels
    assert dict(deb["rows"])["LAUNCH SITE"] == "TSC"  # no name: the code alone, once
    assert dict(deb["rows"])["RCS SIZE"] == "SMALL"
    assert dict(deb["rows"])["SIZE FROM"] == "GCAT (J. McDowell)"
    assert dict(deb["rows"])["PARENT"] == "A09513"
    assert deb["notes"] == "Note one. Note two."
    assert deb["source"] == f"Source: Space-Track, 15 min old. {ATTRIBUTION}."


scenario("details_loading", r"""
  var waiting = Object.assign({}, CARD_ISS, { details: null, details_status: 'loading' });
  route('http://h/api/satcat/facets', FACETS);
  route('http://h/api/satcat/25544', waiting);
  var k = makeCtx({ api: 'http://h' });
  window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 25544 });
  await settle();
  var first = { status: text(k, 'dos-status'), mass: rowValue(k, 'MASS'),
                timers: TIMERS.map(function (t) { return t.ms; }) };
  route('http://h/api/satcat/25544', CARD_ISS);
  runTimers();
  await settle();
  var second = { status: text(k, 'dos-status'), mass: rowValue(k, 'MASS'),
                 requests: count('/api/satcat/25544'), pending: TIMERS.filter(function (t) {
                   return !t.done; }).length };
  // details that never finish loading are asked for six more times, then left
  route('http://h/api/satcat/48274', Object.assign({}, CARD_CSS,
    { details: null, details_status: 'loading' }));
  emit('sda:satellite-selected', { noradId: 48274 });
  await settle();
  for (var i = 0; i < 10; i++) { runTimers(); await settle(); }
  return { first: first, second: second, css: count('/api/satcat/48274'),
           base: REQUESTS[0] };
""")


def test_card_refetches_while_details_load(ran):
    d = ran["details_loading"]
    assert d["first"] == {"status": "Loading mass, span, manufacturer and bus...",
                          "mass": None, "timers": [5000]}
    assert d["second"] == {"status": "", "mass": "450,000 kg", "requests": 2, "pending": 0}
    assert d["css"] == 7
    assert d["base"] == "http://h/api/satcat/facets"


scenario("stale_timer", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/25544', Object.assign({}, CARD_ISS,
    { details: null, details_status: 'loading' }));
  route('/api/satcat/48274', CARD_CSS);
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 25544 });
  await settle();
  emit('sda:satellite-selected', { noradId: 48274 });
  await settle();
  runTimers();
  await settle();
  return { iss: count('/api/satcat/25544'), title: text(k, 'dos-title'),
           status: text(k, 'dos-status'), source: text(k, 'dos-source') };
""")


def test_retry_for_an_old_selection_does_nothing(ran):
    s = ran["stale_timer"]
    assert s["iss"] == 1 and s["title"] == "CSS (TIANHE-1)"
    assert s["status"] == "Physical details are not available."
    assert s["source"] == (f"Source: COMSPOC Spacebook, 30.2 h old (refresh failed, older copy). "
                           f"{ATTRIBUTION}.")


scenario("out_of_order", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/25544', CARD_ISS, 200, true);  // held back
  route('/api/satcat/48274', CARD_CSS);
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 25544 });
  emit('sda:satellite-selected', { noradId: 48274 });
  await settle();
  var mid = text(k, 'dos-title');
  HELD.forEach(function (f) { f(); });
  await settle();
  return { mid: mid, end: text(k, 'dos-title'), id: rowValue(k, 'NORAD ID') };
""")


def test_a_late_answer_for_an_older_pick_is_dropped(ran):
    assert ran["out_of_order"] == {"mid": "CSS (TIANHE-1)", "end": "CSS (TIANHE-1)",
                                   "id": "48274"}


scenario("errors", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/99999', { detail: 'NORAD 99999 is not in the SATCAT' }, 404);
  route('/api/satcat/25544', { detail: { message: 'SATCAT not loaded' } }, 503);
  route('/api/satcat/48274', 'not a card');
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  function snap() { return { title: text(k, 'dos-title'), status: text(k, 'dos-status'),
                             cls: cls(k, 'dos-status'), rows: rows(k).length,
                             source: text(k, 'dos-source') }; }
  var out = {};
  emit('sda:satellite-selected', { noradId: 99999 }); await settle(); out.missing = snap();
  emit('sda:satellite-selected', { noradId: 25544 }); await settle(); out.down = snap();
  emit('sda:satellite-selected', { noradId: 48274 }); await settle(); out.bad = snap();
  var before = REQUESTS.length;
  emit('sda:satellite-selected', { noradId: 'junk' });
  emit('sda:satellite-selected', { noradId: null });
  emit('sda:satellite-selected', null);
  emit('sda:satellite-selected', {});
  await settle();
  out.ignored = { requests: REQUESTS.length - before, title: text(k, 'dos-title') };
  // a failed card is asked for again when the same object is picked again
  route('/api/satcat/48274', CARD_CSS);
  emit('sda:satellite-selected', { noradId: 48274 }); await settle();
  out.retried = { title: text(k, 'dos-title'), requests: count('/api/satcat/48274') };
  return out;
""")


def test_card_errors_are_said_plainly(ran):
    e = ran["errors"]
    assert e["missing"]["title"] == "NORAD 99999" and e["missing"]["rows"] == 0
    assert e["missing"]["status"].startswith("Not in the SATCAT")
    assert e["missing"]["cls"] == "dos-status"
    assert e["missing"]["source"] == f"{ATTRIBUTION}."
    assert "not loaded the SATCAT" in e["down"]["status"]
    assert e["down"]["cls"] == "dos-status dos-error"
    assert e["bad"]["status"] == "Dossier unavailable (bad reply)."
    assert e["ignored"] == {"requests": 0, "title": "NORAD 48274"}
    assert e["retried"] == {"title": "CSS (TIANHE-1)", "requests": 2}


scenario("hostile", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/5', {
    norad_id: 5, name: '<img src=x onerror=alert(1)>', summary: '<b>bold</b>',
    notes: ['<script>x</script>', 7, null], owner: { x: 1 }, owner_code: ['CIS'],
    details: { mass_kg: 'heavy', span_m: -3, manufacturer: { x: 1 }, bus: ['a'],
               parent: 12 },
    age_hours: 'old', source: 42, stale: 'yes', attribution: null
  });
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 5 });
  await settle();
  return { title: text(k, 'dos-title'), summary: text(k, 'dos-summary'), rows: rows(k),
           notes: text(k, 'dos-note'), source: text(k, 'dos-source') };
""")


def test_hostile_reply_is_text_and_junk_is_dropped(ran):
    h = ran["hostile"]
    assert h["title"] == "<img src=x onerror=alert(1)>"  # as text; innerHTML would throw
    assert h["summary"] == "<b>bold</b>"
    assert h["rows"] == [["NORAD ID", "5"], ["PARENT", "12"]]
    assert h["notes"] == "<script>x</script> 7"
    assert h["source"] == f"Source: 42. {ATTRIBUTION}."


scenario("deselect", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/25544', CARD_ISS);
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 25544 });
  await settle();
  emit('sda:satellite-deselected', { noradId: 48274 });  // crossed: about an older pick
  var crossed = text(k, 'dos-title');
  emit('sda:satellite-deselected', { noradId: 25544 });
  var after = { title: text(k, 'dos-title'), rows: rows(k).length,
                status: text(k, 'dos-status'), source: text(k, 'dos-source') };
  emit('sda:satellite-selected', { noradId: 25544 });
  await settle();
  var again = { title: text(k, 'dos-title'), requests: count('/api/satcat/25544') };
  emit('sda:satellite-deselected', {});
  return { crossed: crossed, after: after, again: again, last: text(k, 'dos-title') };
""")


def test_deselect_clears_the_card(ran):
    d = ran["deselect"]
    assert d["crossed"] == "ISS (ZARYA)"
    assert d["after"]["title"] == "" and d["after"]["rows"] == 0
    assert d["after"]["source"] == "" and d["after"]["status"].startswith("Select a satellite")
    assert d["again"] == {"title": "ISS (ZARYA)", "requests": 2}
    assert d["last"] == ""


# --- static build ---------------------------------------------------------------

scenario("static", r"""
  route('/api/satcat', CARD_ISS);
  var k = makeCtx({ isStatic: true, selectedId: 25544 });
  var ok = window.SDADossier.init(k.ctx);
  emit('sda:satellite-selected', { noradId: 48274 });
  var shown = window.SDADossier.show(29733);
  var applied = await window.SDADossier.apply();
  click(k, 'dos-apply');
  click(k, 'dos-clear');
  await settle();
  var disabled = ['dos-owner', 'dos-type', 'dos-rcs', 'dos-year-from', 'dos-year-to',
                  'dos-apply', 'dos-clear'].map(function (c) { return control(k, c).disabled; });
  return { ok: ok, shown: shown, applied: applied, requests: REQUESTS.length,
           timers: TIMERS.length, status: text(k, 'dos-status'), fnote: text(k, 'dos-fnote'),
           disabled: disabled, hidden: visible(k).length, title: text(k, 'dos-title') };
""")


def test_static_build_makes_no_requests(ran):
    s = ran["static"]
    assert s["ok"] is True and s["shown"] is True and s["applied"] is False
    assert s["requests"] == 0 and s["timers"] == 0
    assert "needs the live server" in s["status"]
    assert s["fnote"] == "Filters need the live server."
    assert s["disabled"] == [True] * 7
    assert s["hidden"] == 5 and s["title"] == ""


# --- filters ---------------------------------------------------------------------

scenario("filter", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/ids', { count: 4, ids: [29733, 29734, 48274, 999999], attribution: ATTR });
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  await settle();
  var options = byTag(control(k, 'dos-owner'), 'OPTION').map(function (o) {
    return [o.value, o.textContent]; });
  var types = byTag(control(k, 'dos-type'), 'OPTION').map(function (o) { return o.value; });
  var sizes = byTag(control(k, 'dos-rcs'), 'OPTION').map(function (o) { return o.value; });
  var idle = text(k, 'dos-fnote');
  setVal(k, 'dos-owner', 'PRC');
  setVal(k, 'dos-year-from', '2021');  // the wrong way round: swapped
  setVal(k, 'dos-year-to', ' 1999 ');
  click(k, 'dos-apply');
  var during = text(k, 'dos-fnote');
  await settle();
  var on = { url: REQUESTS[REQUESTS.length - 1], hidden: hidden(k), masks: masks(k),
             refreshes: k.refreshes.n, fnote: text(k, 'dos-fnote'), feed: k.feed.slice() };
  click(k, 'dos-clear');
  var off = { hidden: hidden(k), masks: masks(k), refreshes: k.refreshes.n,
              fnote: text(k, 'dos-fnote'), feed: k.feed.slice(),
              inputs: ['dos-owner', 'dos-year-from', 'dos-year-to'].map(function (c) {
                return control(k, c).value; }) };
  var before = REQUESTS.length;
  setVal(k, 'dos-year-from', '19x9');  // not a year: ignored, so no filter at all
  click(k, 'dos-apply');
  await settle();
  var empty = { requests: REQUESTS.length - before, feed: k.feed.length,
                fnote: text(k, 'dos-fnote') };
  setVal(k, 'dos-type', 'ROCKET BODY');
  setVal(k, 'dos-rcs', 'LARGE');
  setVal(k, 'dos-year-from', '1990');
  click(k, 'dos-apply');
  await settle();
  return { options: options, types: types, sizes: sizes, idle: idle, during: during, on: on,
           off: off, empty: empty, url2: REQUESTS[REQUESTS.length - 1],
           fnote2: text(k, 'dos-fnote') };
""")


def test_filter_controls_come_from_the_facets(ran):
    f = ran["filter"]
    assert f["options"] == [["", "All owners"], ["US", "United States (US) 18,736"],
                            ["PRC", "China (PRC) 6,245"], ["NATO", "NATO 9"]]
    assert f["types"] == ["", "PAYLOAD", "ROCKET BODY", "DEBRIS", "UNKNOWN"]
    assert f["sizes"] == ["", "SMALL", "MEDIUM", "LARGE", "UNKNOWN"]
    assert f["idle"] == "36,671 objects on orbit in the SATCAT."


def test_apply_hides_what_does_not_match(ran):
    f = ran["filter"]
    assert f["during"] == "Filtering..."
    on = f["on"]
    assert on["url"] == "/api/satcat/ids?owner=PRC&year_from=1999&year_to=2021"
    assert on["hidden"] == {"25544": True, "29733": False, "29734": False, "48274": False,
                            "66906": True}
    assert on["masks"] == {"25544": 2, "29733": 0, "29734": 0, "48274": 0, "66906": 2}
    assert on["refreshes"] == 1
    assert on["fnote"] == ("Showing 3 of 5 satellites (owner China, launched 1999 to 2021; "
                           "4 match in the SATCAT).")
    assert on["feed"] == ["Dossier filter: owner China, launched 1999 to 2021 (3 of 5 shown)"]


def test_show_all_clears_the_filter(ran):
    off = ran["filter"]["off"]
    assert set(off["hidden"].values()) == {False} and set(off["masks"].values()) == {0}
    assert off["refreshes"] == 2
    assert off["fnote"] == "No dossier filter: showing all 5 satellites."
    assert off["feed"][-1] == "Dossier filter cleared, showing all 5 satellites"
    assert off["inputs"] == ["", "", ""]
    # nothing set: apply is a clear, with no request and no second log line
    assert ran["filter"]["empty"] == {"requests": 0, "feed": 2,
                                      "fnote": "No dossier filter: showing all 5 satellites."}
    assert ran["filter"]["url2"] == "/api/satcat/ids?type=ROCKET%20BODY&rcs=LARGE&year_from=1990"
    assert ran["filter"]["fnote2"].endswith(
        "(rocket body, RCS large (over 1 m2), launched from 1990; 4 match in the SATCAT).")


scenario("filter_failures", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/ids', { detail: { message: 'SATCAT not loaded' } }, 503);
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  await settle();
  setVal(k, 'dos-owner', 'US');
  var r1 = await window.SDADossier.apply();
  var down = { ok: r1, fnote: text(k, 'dos-fnote'), visible: visible(k).length };
  route('/api/satcat/ids', { ids: 'nope' });
  var r2 = await window.SDADossier.apply();
  var bad = { ok: r2, fnote: text(k, 'dos-fnote'), visible: visible(k).length };
  // a slow answer to an older filter loses to a newer click
  route('/api/satcat/ids', { ids: [25544] }, 200, true);
  var slow = window.SDADossier.apply();
  window.SDADossier.clear();
  HELD.forEach(function (f) { f(); });
  var r3 = await slow;
  await settle();
  return { down: down, bad: bad, late: { ok: r3, visible: visible(k).length,
           fnote: text(k, 'dos-fnote') } };
""")


def test_filter_failures_hide_nothing(ran):
    f = ran["filter_failures"]
    assert f["down"] == {"ok": False, "visible": 5,
                         "fnote": "The server has not loaded the SATCAT yet; filter not applied."}
    assert f["bad"] == {"ok": False, "visible": 5,
                        "fnote": "Filter failed (bad reply); nothing hidden."}
    assert f["late"] == {"ok": False, "visible": 5,
                         "fnote": "No dossier filter: showing all 5 satellites."}


scenario("facets_retry", r"""
  route('/api/satcat/facets', { detail: 'x' }, 503);
  var k = makeCtx();
  window.SDADossier.init(k.ctx);
  await settle();
  var down = { fnote: text(k, 'dos-fnote'), timers: TIMERS.map(function (t) { return t.ms; }),
               options: byTag(control(k, 'dos-owner'), 'OPTION').length };
  route('/api/satcat/facets', FACETS);
  runTimers();
  await settle();
  var up = { fnote: text(k, 'dos-fnote'),
             options: byTag(control(k, 'dos-owner'), 'OPTION').length,
             requests: count('/api/satcat/facets') };
  // a server that never loads is asked eight more times, then left alone
  var k2 = makeCtx();
  route('/api/satcat/facets', { detail: 'x' }, 503);
  REQUESTS.length = 0;
  window.SDADossier.init(k2.ctx);
  await settle();
  for (var i = 0; i < 12; i++) { runTimers(); await settle(); }
  return { down: down, up: up, giveUp: count('/api/satcat/facets') };
""")


def test_facets_retry_until_the_server_has_the_catalog(ran):
    f = ran["facets_retry"]
    assert f["down"] == {"fnote": "SATCAT not loaded on the server yet.", "timers": [15000],
                         "options": 1}
    assert f["up"] == {"fnote": "36,671 objects on orbit in the SATCAT.", "options": 4,
                       "requests": 2}
    assert f["giveUp"] == 9


scenario("selected_stays_visible", r"""
  route('/api/satcat/', CARD_ISS);  // any card; the routes added later win
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/ids', { ids: [29733, 29734, 48274] });
  var k = makeCtx({ selectedId: 66906 });
  window.SDADossier.init(k.ctx);
  await settle();
  setVal(k, 'dos-owner', 'PRC');
  await window.SDADossier.apply();
  var applied = { visible: visible(k), masks: masks(k), fnote: text(k, 'dos-fnote') };
  var n0 = k.refreshes.n;
  select(k, 25544);  // hidden by the filter: shown, and 66906 goes back under it
  var moved = { visible: visible(k), refreshes: k.refreshes.n - n0 };
  select(k, 29733);  // a match: nothing to force, 25544 goes back
  var match = { visible: visible(k), refreshes: k.refreshes.n - n0 };
  select(k, 66906);
  deselect(k, 66906);
  var cleared = { visible: visible(k), refreshes: k.refreshes.n - n0 };
  // the page swaps the record array on a reload: the filter follows the new records
  var fresh = k.sats.map(function (s) { return { noradId: s.noradId, name: s.name,
                                                 hidden: false }; });
  k.sats = fresh; k.engine.sats = fresh;
  k.engine.byNoradId = {};
  fresh.forEach(function (s) { k.engine.byNoradId[s.noradId] = s; });
  k.engine.selectedId = 25544;
  emit('sda:catalog-loaded', { count: fresh.length });
  var reloaded = { visible: visible(k), masks: masks(k) };
  return { applied: applied, moved: moved, match: match, cleared: cleared,
           reloaded: reloaded };
""")


def test_the_selected_satellite_is_never_hidden(ran):
    s = ran["selected_stays_visible"]
    assert s["applied"]["visible"] == [29733, 29734, 48274, 66906]
    assert s["applied"]["masks"]["66906"] == 2  # hidden by the filter, shown as the selection
    assert s["applied"]["fnote"].endswith("Selected stays visible.")
    assert s["moved"] == {"visible": [25544, 29733, 29734, 48274], "refreshes": 1}
    assert s["match"] == {"visible": [29733, 29734, 48274], "refreshes": 2}
    assert s["cleared"] == {"visible": [29733, 29734, 48274], "refreshes": 4}
    assert s["reloaded"]["visible"] == [25544, 29733, 29734, 48274]
    assert s["reloaded"]["masks"] == {"25544": 2, "29733": 0, "29734": 0, "48274": 0,
                                      "66906": 2}


# --- together with the constellation filter (categories.js) -------------------------

COMPOSE = r"""
  route('/api/satcat/', CARD_ISS);  // any card; the routes added later win
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/ids', { ids: [29733, 48274] });
  var k = makeCtx({ sats: [
    { noradId: 25544, name: 'ISS (ZARYA)' }, { noradId: 48274, name: 'CSS (TIANHE-1)' },
    { noradId: 29733, name: 'FENGYUN 1C DEB' }, { noradId: 44713, name: 'STARLINK-1007' },
    { noradId: 66906, name: 'DUPLEX' } ] });
  var catMount = new El('div');
  function initCats() {
    window.SDACategories.init(Object.assign({}, k.ctx, { mount: catMount })); }
  function initDossier() { window.SDADossier.init(k.ctx); }
  __ORDER__
  await settle();
  function pill(key) { return all(catMount).filter(function (x) {
    return x.getAttribute('data-cat') === key; })[0]; }
  async function dossierPRC() { setVal(k, 'dos-owner', 'PRC'); await window.SDADossier.apply(); }
  var out = { cats: k.sats.map(function (s) { return window.SDACategories.classify(s.name); }) };
  pill('STATIONS').fire('click');
  out.stations = visible(k);
  await dossierPRC();
  out.both = visible(k);
  click(k, 'dos-clear');
  out.categoriesOnly = visible(k);
  await dossierPRC();
  pill('ALL').fire('click');
  out.dossierOnly = visible(k);
  pill('STATIONS').fire('click');
  select(k, 44713);  // hidden by both filters
  out.selectedBoth = visible(k);
  deselect(k, 44713);
  out.afterBoth = visible(k);
  select(k, 25544);  // a station, hidden by the dossier only
  out.selectedDossier = visible(k);
  deselect(k, 25544);
  out.afterDossier = visible(k);
  select(k, 29733);  // PRC, hidden by the constellation filter only
  out.selectedCats = visible(k);
  select(k, 25544);  // the selection moves straight on
  out.moved = visible(k);
  deselect(k, 25544);
  out.end = visible(k);
  out.masks = masks(k);
  return out;
"""

scenario("compose", COMPOSE.replace("__ORDER__", "initCats(); initDossier();"))
scenario("compose_reversed", COMPOSE.replace("__ORDER__", "initDossier(); initCats();"))


@pytest.mark.parametrize("name", ["compose", "compose_reversed"])
def test_two_filters_never_undo_each_other(ran, name):
    c = ran[name]
    assert c["cats"] == ["STATIONS", "STATIONS", "DEBRIS", "STARLINK", "OTHER"]
    assert c["stations"] == [25544, 48274]
    assert c["both"] == [48274]  # a station and Chinese
    assert c["categoriesOnly"] == [25544, 48274]  # clearing the dossier keeps the pill
    assert c["dossierOnly"] == [48274, 29733]  # "All" keeps the dossier filter
    assert c["selectedBoth"] == [48274, 44713]
    assert c["afterBoth"] == [48274]
    assert c["selectedDossier"] == [25544, 48274]
    assert c["afterDossier"] == [48274]
    assert c["selectedCats"] == [48274, 29733]
    assert c["moved"] == [25544, 48274]
    assert c["end"] == [48274]
    assert c["masks"] == {"25544": 2, "48274": 0, "29733": 1, "44713": 3, "66906": 3}


# --- init and guards ----------------------------------------------------------------------

scenario("init_guards", r"""
  route('/api/satcat/facets', FACETS);
  route('/api/satcat/ids', { ids: [29733] });
  var k = makeCtx({ throwing: true });
  var first = window.SDADossier.init(k.ctx);
  var second = window.SDADossier.init(k.ctx);  // takes over; the first stops listening
  var counts = ['sda:satellite-selected', 'sda:satellite-deselected', 'sda:catalog-loaded']
    .map(listeners);
  await settle();
  byClass(k.ctx.mount, 'dos-owner')[1].value = 'PRC';  // the second panel is the live one
  var applied = await window.SDADossier.apply();  // page callbacks throw: flags still set
  var bare = makeCtx();
  delete bare.ctx.satEngine;
  var noEngine = window.SDADossier.init(bare.ctx);
  emit('sda:satellite-selected', { noradId: 25544 });
  emit('sda:catalog-loaded', {});
  await settle();
  return { first: first, second: second, counts: counts, applied: applied,
           visible: visible(k), noEngine: noEngine,
           noMount: window.SDADossier.init(makeCtx({ noMount: true }).ctx),
           nothing: window.SDADossier.init(null),
           panels: byClass(k.ctx.mount, 'panel').length,
           styles: byTag(document.head, 'STYLE').length };
""")


def test_init_guards(ran):
    g = ran["init_guards"]
    assert g["first"] is True and g["second"] is True
    assert g["counts"] == [1, 1, 1]  # the second init replaced the first's listeners
    assert g["applied"] is True and g["visible"] == [29733]
    assert g["noEngine"] is True
    assert g["noMount"] is False and g["nothing"] is False
    assert g["panels"] == 2 and g["styles"] == 1


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "dossier.js must stay ASCII"
    assert "global.SDADossier" in text
    assert ATTRIBUTION in text
