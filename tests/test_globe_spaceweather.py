"""Tests for the globe space weather chip (static/js/spaceweather.js).

Plain browser JavaScript, run under Node's `vm` against a hand-written DOM stub (no
jsdom, no browser), the way tests/test_globe_aurora.py does it. The fake `fetch`
answers from a table and `setInterval` only records its callback, so nothing here
touches the network or waits fifteen minutes. Skips cleanly when Node is not
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
    / "src" / "sda" / "templates" / "static" / "js" / "spaceweather.js"
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
function all(el, out) { out = out || []; out.push(el);
  el.children.forEach(function (c) { all(c, out); }); return out; }
function byClass(el, cls) { return all(el).filter(function (x) {
  return x.className.split(/\s+/).indexOf(cls) >= 0; }); }
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }

var document = {
  head: new El('head'), body: new El('body'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) {
      return x.id === id; });
    return hit[0] || null;
  }
};
var window = this;

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

// What GET /api/space-weather/summary answered on 2026-11-19 (forecast trimmed).
var SUMMARY = {
  kp_now: 3.0, kp_time: '2026-11-19T18:00:00+00:00', kp_g_scale: 'G0',
  g_scale_now: 'G0', g_scale_next_3_days_max: 'G0',
  next_3_days: [
    { date: '2026-11-19', g_scale: 'G0', r_minor_prob: 15, r_major_prob: 5, s_prob: 1 },
    { date: '2026-11-20', g_scale: 'G0', r_minor_prob: 15, r_major_prob: 5, s_prob: 1 },
    { date: '2026-11-21', g_scale: 'G0', r_minor_prob: 15, r_major_prob: 5, s_prob: 1 }
  ],
  s_scale_now: 'S0', r_scale_now: 'R0', scales_time: '2026-11-19 22:25:00',
  storm_now: false, storm_forecast: false,
  f107_observed: 107.0, f107_time: '2026-11-19T20:00:00+00:00', f107_90day_mean: 111.0,
  forecast_issued: '2026-11-19T00:00:00Z',
  forecast: [{ date: '2026-11-19', ap: 8, f107: 115 }, { date: '2026-11-20', ap: 12, f107: 115 }],
  forecast_peak_ap: 12, stale: false, stale_products: [], missing_products: [],
  source: 'NOAA SWPC'
};
function summary(over) {
  var s = JSON.parse(JSON.stringify(SUMMARY));
  for (var k in (over || {})) s[k] = over[k];
  return s;
}
function forecastG(level) {   // G<level> forecast on the third day
  var s = summary({ g_scale_next_3_days_max: 'G' + level });
  s.next_3_days[2].g_scale = 'G' + level;
  return s;
}

function makeCtx(opts) {
  opts = opts || {};
  var feed = [];
  var ctx = {
    api: opts.api || '', isStatic: !!opts.isStatic,
    mount: opts.noMount ? undefined : new El('div'),
    addIntelItem: function (t) { feed.push(t); }
  };
  return { ctx: ctx, feed: feed };
}
function text(k, cls) { return byClass(k.ctx.mount, cls)[0].textContent; }
function panel(k) {
  return { title: byTag(k.ctx.mount, 'H3')[0].textContent, kp: text(k, 'sw-kp-value'),
           kpColor: byClass(k.ctx.mount, 'sw-kp-value')[0].style.color,
           kpNote: text(k, 'sw-kp-note'), g: text(k, 'sw-g'), f107: text(k, 'sw-f107'),
           status: text(k, 'sw-status') };
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
              Promise: Promise, JSON: JSON, Math: Math, Date: Date };
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'prelude' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'spaceweather.js' });
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
      try { vm.runInContext('window.SDASpaceWeather && window.SDASpaceWeather.stop()', box); }
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
    work = tmp_path_factory.mktemp("spaceweather")
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


QUIET = "#4dd27a"
ACTIVE = "#ffd84d"
STORM = "#ff9f1a"
SEVERE = "#ff4d4d"
UNKNOWN = "#8899aa"
TAIL = ": drag and TLE in-track error rise for objects below 600 km."


# --- pure helpers --------------------------------------------------------------

scenario("helpers", r"""
  var S = window.SDASpaceWeather;
  var kps = [0, 3.33, 3.67, 4.33, 4.67, 6.33, 6.67, 9, NaN, -1, 10, '5', null, Infinity];
  return {
    colors: kps.map(S.kpColor),
    labels: kps.map(function (k) { var l = S.kpLevel(k); return l ? l.label : null; }),
    kpText: [3, 2.67, 5.333, 9, NaN, '4'].map(S.kpText),
    g: ['G0', 'G5', ' G2 ', 'G6', 'g1', 'S1', '<b>G1</b>', 3, null].map(function (x) {
      return S.scaleLevel(x, 'G'); }),
    s: [S.scaleLevel('S2', 'S'), S.scaleLevel('R3', 'R'), S.scaleLevel('R3', 'S')],
    urls: [S.summaryUrl(''), S.summaryUrl('http://h/'), S.summaryUrl('http://h//'),
           S.summaryUrl(null)],
    times: [S.shortTime('2026-11-19T18:00:00+00:00'), S.shortTime(''), S.shortTime(42)],
    dates: [S.shortDate('2026-11-20'), S.shortDate('soon'), S.shortDate(null)],
    refresh: S.REFRESH_MS
  };
""")


def test_kp_colour_by_level(ran):
    h = ran["helpers"]
    assert h["colors"] == [QUIET, QUIET, ACTIVE, ACTIVE, STORM, STORM, SEVERE, SEVERE,
                           UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN]
    assert h["labels"][:8] == ["quiet", "quiet", "active", "active", "storm", "storm",
                               "severe storm", "severe storm"]
    assert h["kpText"] == ["3.0", "2.67", "5.33", "9.0", "-", "-"]


def test_scale_labels_are_matched_not_trusted(ran):
    h = ran["helpers"]
    assert h["g"] == [0, 5, 2, None, None, None, None, None, None]
    assert h["s"] == [2, 3, None]


def test_url_and_time_helpers(ran):
    h = ran["helpers"]
    assert h["urls"] == ["/api/space-weather/summary", "http://h/api/space-weather/summary",
                         "http://h/api/space-weather/summary", "/api/space-weather/summary"]
    assert h["times"] == ["18:00 UTC", "", ""]
    assert h["dates"] == ["11-20", "", ""]
    assert h["refresh"] == 15 * 60 * 1000


scenario("storm_lines", r"""
  var S = window.SDASpaceWeather;
  var both = forecastG(3);
  both.kp_now = 6; both.kp_g_scale = 'G2'; both.g_scale_now = 'G1';
  var hostile = summary({ g_scale_now: '<b>G4</b>', kp_g_scale: 9,
                          g_scale_next_3_days_max: 'G9', next_3_days: 'x' });
  return {
    quiet: S.stormLine(SUMMARY),
    forecast: S.stormLine(forecastG(2)),
    current: S.stormLine(summary({ kp_now: 5.33, kp_g_scale: 'G1' })),
    scalesNow: S.stormLine(summary({ g_scale_now: 'G1', kp_now: null })),
    both: S.stormLine(both),
    hostile: S.stormLine(hostile),
    nothing: [S.stormLine(null), S.stormLine({}), S.stormLine([])]
  };
""")


def test_storm_banner_text(ran):
    s = ran["storm_lines"]
    assert s["quiet"] == ""
    assert s["forecast"] == "Geomagnetic storm forecast by NOAA SWPC, up to G2 (11-21 G2)" + TAIL
    assert s["current"] == "Geomagnetic storm G1 in progress (NOAA SWPC, Kp 5.33)" + TAIL
    assert s["scalesNow"] == "Geomagnetic storm G1 in progress (NOAA SWPC)" + TAIL
    assert s["both"] == ("Geomagnetic storm G2 in progress (NOAA SWPC, Kp 6.0), "
                         "up to G3 forecast" + TAIL)
    assert s["hostile"] == ""
    assert s["nothing"] == ["", "", ""]
    for line in (s["forecast"], s["current"], s["both"]):
        assert all(ord(ch) < 128 for ch in line)


# --- init, panel, refresh ----------------------------------------------------------

scenario("init_quiet", r"""
  route('/api/space-weather/summary', SUMMARY);
  var k = makeCtx();
  var ok = window.SDASpaceWeather.init(k.ctx);
  var before = panel(k);
  await settle();
  var first = { ok: ok, before: before, requests: REQUESTS.slice(), panel: panel(k),
                intervals: live().map(function (t) { return t.ms; }), feed: k.feed.slice(),
                data: window.SDASpaceWeather.data().kp_now };
  live()[0].fn();
  await settle();
  return { first: first, requests: REQUESTS.length, feed: k.feed.length };
""")


def test_panel_shows_kp_g_and_f107(ran):
    f = ran["init_quiet"]["first"]
    assert f["ok"] is True
    assert f["before"]["status"] == "Loading NOAA SWPC space weather..."
    assert f["requests"] == ["/api/space-weather/summary"]
    p = f["panel"]
    assert p["title"] == "Space weather"
    assert p["kp"] == "Kp 3.0" and p["kpColor"] == QUIET
    assert p["kpNote"] == " quiet, 18:00 UTC"
    assert p["g"] == "G0 now; next 3 days: 11-19 G0, 11-20 G0, 11-21 G0 | S0 R0"
    assert p["f107"] == "F10.7 107 SFU (90-day mean 111)"
    assert p["status"] == "NOAA SWPC, refreshed every 15 min"
    assert f["intervals"] == [900000]
    assert f["feed"] == []  # quiet: no banner
    assert f["data"] == 3.0


def test_refreshes_every_fifteen_minutes(ran):
    r = ran["init_quiet"]
    assert r["requests"] == 2 and r["feed"] == 0


scenario("banner", r"""
  route('http://h/api/space-weather/summary', forecastG(1));
  var k = makeCtx({ api: 'http://h' });
  window.SDASpaceWeather.init(k.ctx);
  await settle();
  var first = k.feed.slice();
  live()[0].fn(); await settle();                       // same storm: not posted again
  var repeat = k.feed.length;
  var kpEl = byClass(k.ctx.mount, 'sw-kp-value')[0];
  route('http://h/api/space-weather/summary', summary({ kp_now: 7.33, kp_g_scale: 'G3' }));
  live()[0].fn(); await settle();                       // now in progress: new line
  var severe = { line: k.feed[k.feed.length - 1], color: kpEl.style.color, n: k.feed.length };
  route('http://h/api/space-weather/summary', SUMMARY);
  live()[0].fn(); await settle();                       // quiet again: nothing posted
  var quiet = k.feed.length;
  route('http://h/api/space-weather/summary', forecastG(1));
  live()[0].fn(); await settle();                       // a new storm is posted again
  return { requests: REQUESTS, first: first, repeat: repeat, severe: severe, quiet: quiet,
           again: k.feed.length, last: k.feed[k.feed.length - 1] };
""")


def test_storm_banner_posted_once_per_storm(ran):
    b = ran["banner"]
    assert b["requests"][0] == "http://h/api/space-weather/summary"
    assert b["first"] == ["Geomagnetic storm forecast by NOAA SWPC, up to G1 (11-21 G1)" + TAIL]
    assert b["repeat"] == 1
    assert b["severe"]["n"] == 2 and b["severe"]["color"] == SEVERE
    assert b["severe"]["line"].startswith("Geomagnetic storm G3 in progress (NOAA SWPC, Kp 7.33)")
    assert b["quiet"] == 2
    assert b["again"] == 3 and b["last"] == b["first"][0]


# --- static build, failures, stale, hostile ---------------------------------------------

scenario("static", r"""
  route('/api/space-weather/summary', forecastG(3));
  var k = makeCtx({ isStatic: true });
  var ok = window.SDASpaceWeather.init(k.ctx);
  await window.SDASpaceWeather.refresh();
  await settle();
  return { ok: ok, requests: REQUESTS.length, intervals: INTERVALS.length, feed: k.feed,
           status: text(k, 'sw-status'), kp: text(k, 'sw-kp-value') };
""")


def test_static_build_makes_no_requests(ran):
    s = ran["static"]
    assert s["ok"] is True
    assert s["requests"] == 0 and s["intervals"] == 0 and s["feed"] == []
    assert s["status"] == "Space weather needs the live server (NOAA SWPC)."
    assert s["kp"] == "Kp -"


scenario("failure", r"""
  route('/api/space-weather/summary', { detail: 'NOAA SWPC space weather unavailable' }, 502);
  var k = makeCtx();
  window.SDASpaceWeather.init(k.ctx);
  await settle();
  var failed = { status: text(k, 'sw-status'), feed: k.feed.slice(), kp: text(k, 'sw-kp-value') };
  route('/api/space-weather/summary', summary({ stale: true }));
  live()[0].fn(); await settle();
  return { failed: failed, status: text(k, 'sw-status'), kp: text(k, 'sw-kp-value'),
           requests: REQUESTS.length };
""")


def test_failure_then_stale_recovery(ran):
    r = ran["failure"]
    assert r["failed"]["status"] == "NOAA SWPC space weather did not load; retrying in 15 minutes."
    assert r["failed"]["feed"] == [] and r["failed"]["kp"] == "Kp -"
    assert r["requests"] == 2 and r["kp"] == "Kp 3.0"
    assert r["status"] == "NOAA SWPC, stale: last good copy, refreshed every 15 min"


scenario("hostile", r"""
  route('/api/space-weather/summary', { kp_now: '<script>', kp_time: 7, g_scale_now: 'G<b>',
    next_3_days: [null, 'x', { date: '<i>', g_scale: 'G1' },
                  { date: '2026-11-22', g_scale: 'GG' }],
    s_scale_now: 5, r_scale_now: 'R9', f107_observed: 1e9, f107_90day_mean: '111' });
  var k = makeCtx();
  window.SDASpaceWeather.init(k.ctx);
  await settle();
  var junk = panel(k);
  route('/api/space-weather/summary', [1, 2, 3]);
  live()[0].fn(); await settle();
  return { junk: junk, feed: k.feed, afterArray: text(k, 'sw-status') };
""")


def test_malformed_answers_do_not_throw(ran):
    h = ran["hostile"]
    j = h["junk"]
    assert j["kp"] == "Kp -" and j["kpColor"] == UNKNOWN and j["kpNote"] == ""
    assert j["g"] == "- now; next 3 days: 11-22 - | - -"
    assert j["f107"] == "F10.7 -"
    assert h["feed"] == []
    assert "did not load" in h["afterArray"]


scenario("guards", r"""
  route('/api/space-weather/summary', forecastG(2));
  var k = makeCtx({ noMount: true });
  var ok = window.SDASpaceWeather.init(k.ctx);
  await settle();
  var feed = k.feed.slice();
  var k2 = makeCtx();
  var again = window.SDASpaceWeather.init(k2.ctx);   // re-init stops the first timer
  await settle();
  return { ok: ok, feed: feed, again: again, liveTimers: live().length,
           nothing: window.SDASpaceWeather.init(null),
           notObject: window.SDASpaceWeather.init('ctx') };
""")


def test_init_without_a_panel_and_guards(ran):
    g = ran["guards"]
    assert g["ok"] is True and len(g["feed"]) == 1 and "up to G2" in g["feed"][0]
    assert g["again"] is True and g["liveTimers"] == 1
    assert g["nothing"] is False and g["notObject"] is False


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in text
    assert all(ord(ch) < 127 for ch in text), "spaceweather.js must stay ASCII"
    assert "global.SDASpaceWeather" in text
