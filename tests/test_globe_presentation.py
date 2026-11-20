"""Tests for the presentation module (static/js/presentation.js).

Runs under Node's `vm` with the DOM stub and fake Cesium from tests/globe_vm.py, plus a
fake 2D canvas that records what is drawn, a localStorage stub, and a page skeleton with
the ids worldview.html uses (#controls, #info-panel, #orbit-legend, #basemap-credit,
#loading-overlay). Idle checks run off a fake clock through SDAPresent._check().
"""

from __future__ import annotations

import json

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

MODULE = JS_DIR / "presentation.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

EXTRA = r"""
var NOW = Date.UTC(2026, 10, 19, 12, 0, 0);
var OPS = [];
var NO2D = false;
var realCreate = document.createElement;
document.createElement = function (tag) {
  var e = realCreate.call(document, tag);
  if (String(tag).toLowerCase() === 'canvas') {
    e.getContext = function () {
      if (NO2D) return null;
      return { font: '', fillStyle: '', textBaseline: '',
        measureText: function (t) { return { width: String(t).length * 7 }; },
        fillText: function (t, x, y) { OPS.push(['text', String(t), x, y, this.font]); },
        fillRect: function (x, y, w, h) { OPS.push(['rect', x, y, w, h, this.fillStyle]); },
        drawImage: function (img, x, y) { OPS.push(['image', img.tagName, x, y]); } };
    };
    e.toDataURL = function (type) { return 'data:' + type + ';base64,TEST'; };
  }
  return e;
};
var DOWNLOADS = [];
El.prototype.click = function () {
  if (this.tagName === 'A') DOWNLOADS.push({ href: this.href, name: this.download,
    inBody: document.body.contains(this) });
};
var STORE = {};
this.localStorage = {
  getItem: function (k) { return k in STORE ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); }
};
function shareUrl() { return 'http://127.0.0.1:8000/worldview#v=1&lat=38.1'; }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

function page(opts) {
  opts = opts || {};
  var controls = document.createElement('aside');
  controls.id = 'controls';
  var p1 = document.createElement('div');
  p1.className = 'panel';
  controls.appendChild(p1);
  var inp = document.createElement('input');
  inp.tagName = 'INPUT';
  p1.appendChild(inp);
  var legend = document.createElement('div');
  legend.id = 'orbit-legend';
  [['#00d4ff', 'Low orbit'], ['#00ff88', 'Medium orbit'], ['#ffd700', 'Geostationary'],
   ['#ff4466', 'High or stretched']].forEach(function (r) {
    var s = document.createElement('span');
    var i = document.createElement('i');
    i.style.background = r[0];
    s.appendChild(i);
    s.appendChild(document.createTextNode(r[1]));
    legend.appendChild(s);
  });
  controls.appendChild(legend);
  var credit = document.createElement('div');
  credit.id = 'basemap-credit';
  credit.textContent = 'Esri World Imagery';
  controls.appendChild(credit);
  var mount = document.createElement('div');
  mount.id = 'ext-present';
  controls.appendChild(mount);
  document.body.appendChild(controls);
  var info = document.createElement('aside');
  info.id = 'info-panel';
  document.body.appendChild(info);
  if (opts.overlay) {
    var ov = document.createElement('div');
    ov.id = 'loading-overlay';
    document.body.appendChild(ov);
  }
  var v = makeViewer();
  v.scene.globe.tileLoadProgressEvent = new Cesium.Event();
  v.scene.globe.tilesLoaded = false;
  return { controls: controls, inp: inp, info: info, mount: mount, viewer: v, p1: p1 };
}
function start(opts) {
  var p = page(opts);
  SDAPresent._setClock(function () { return NOW; });
  p.ok = SDAPresent.init({ viewer: p.viewer, mount: p.mount,
    simNow: function () { return new Date(NOW); },
    getVisibleLayers: function () { return ['satellites', 'quakes', 'maritime', 'declutter']; } });
  return p;
}
function at(ms) { SDAPresent._setClock(function () { return NOW + ms; }); }
"""

SCENARIOS = {
    "pure": r"""
var m = function (t) { return t.length; };
return {
  legend: SDAPresent.layerLegend(['satellites', 'declutter', 'quakes', 'nope']),
  attr: SDAPresent.attributionText(['a', 'A', '', null, ' b  c ']),
  attr2: SDAPresent.attributionText(['Imagery: Esri', 'esri', 'Data: USGS']),
  fit: SDAPresent.wrapText(m, 'aaa bbb ccc ddd', 7, 2),
  cut: SDAPresent.wrapText(m, 'aaa bbb ccc ddd eee', 7, 2),
  long: SDAPresent.wrapText(m, 'abcdefghij', 5, 1),
  none: SDAPresent.wrapText(m, '', 5, 2),
  cams: [SDAPresent.formatCamera(38.1, -142.3, 600000), SDAPresent.formatCamera(-5, 10, 850),
    SDAPresent.formatCamera(NaN, 0, 0)],
  stamp: SDAPresent.utcStamp(new Date(NOW)),
  badStamp: SDAPresent.utcStamp(new Date(NaN)),
  choices: SDAPresent.IDLE_CHOICES
};
""",
    "idle": r"""
var p = start();
var out = { ok: p.ok, start: SDAPresent.state().idle };
at(9000); SDAPresent._check(); out.at9 = SDAPresent.state().idle;
at(10000); SDAPresent._check(); out.at10 = document.body.has('sda-idle');
fireDoc('keydown', { key: 'x' }); out.afterKey = document.body.has('sda-idle');
fireDoc('pointermove', { target: p.inp });
at(80000); SDAPresent._check(); out.overPanel = [SDAPresent.state().overPanel,
  SDAPresent.state().idle];
fireDoc('pointermove', { target: document.body });
at(90000); SDAPresent._check(); out.offPanel = SDAPresent.state().idle;
fireDoc('pointerdown', { target: document.body });
document.activeElement = p.inp;
at(200000); SDAPresent._check(); out.typing = SDAPresent.state().idle;
document.activeElement = null;
var ov = document.createElement('div'); ov.id = 'loading-overlay'; document.body.appendChild(ov);
SDAPresent._check(); out.loading = SDAPresent.state().idle;
ov.remove();
SDAPresent._check(); out.loaded = SDAPresent.state().idle;
SDAPresent.setIdleHide(false);
out.off = [SDAPresent.state().idle, JSON.parse(STORE['sda.present.v1'])];
at(900000); SDAPresent._check(); out.offLater = SDAPresent.state().idle;
var css = document.getElementById('sda-present-style').textContent;
out.css = [css.indexOf('body.sda-idle #controls') >= 0,
  css.indexOf('body.sda-touring #info-panel') >= 0,
  css.indexOf('body.sda-narrow #controls') >= 0];
return out;
""",
    "narrow": r"""
window.innerWidth = 600;
var p = start();
var h = p.controls.children[0];
var out = { narrow: document.body.has('sda-narrow'), handle: h.className, text: h.textContent,
  aria: h.getAttribute('aria-expanded') };
h.fire('click');
out.open = [document.body.has('sda-sheet-open'), h.getAttribute('aria-expanded'), h.textContent];
at(600000); SDAPresent._check(); out.idle = SDAPresent.state().idle;
out.status = byClass(p.mount, 'sdapresent-status')[0].textContent;
window.innerWidth = 1400;
fireWin('resize');
out.wide = [document.body.has('sda-narrow'), document.body.has('sda-sheet-open'),
  SDAPresent.state().narrow];
window.innerWidth = 500;
fireWin('resize');
out.handles = byClass(p.controls, 'sda-sheet-handle').length;
return out;
""",
    "panel": r"""
var p = start();
var root = p.mount.children[0];
var cb = byTag(root, 'INPUT')[0];
var sel = byTag(root, 'SELECT')[0];
var out = { title: byTag(root, 'H3')[0].textContent, collapsed: root.has('collapsed'),
  checked: cb.checked, delay: sel.value,
  options: byTag(sel, 'OPTION').map(function (o) { return o.textContent; }),
  share: !!buttonNamed(root, 'SHARE VIEW (PNG)') };
sel.value = '20'; sel.fire('change');
cb.checked = false; cb.fire('change');
out.saved = JSON.parse(STORE['sda.present.v1']);
out.status = byClass(root, 'sdapresent-status')[0].textContent;
out.bad = SDAPresent.setIdleDelay(7);
var q = page();
SDAPresent.init({ viewer: q.viewer, mount: q.mount });
var root2 = q.mount.children[0];
out.reloaded = [byTag(root2, 'INPUT')[0].checked, byTag(root2, 'SELECT')[0].value];
return out;
""",
    "settle_tiles": r"""
var p = page({ overlay: true });
window.viewer = p.viewer;
SDAPresent._setTiming({ pollMs: 5, minSettleMs: 0, skipAfterMs: 1e9 });
var g = p.viewer.scene.globe;
var pr = SDAPresent.whenSettled(5000);
var detail = document.getElementById('sda-loading-detail');
g.tileLoadProgressEvent.raiseEvent(12);
var t12 = detail.textContent;
g.tileLoadProgressEvent.raiseEvent(3);
var fill = document.getElementById('sda-loading-tiles').children[0].style.width;
g.tileLoadProgressEvent.raiseEvent(1);
var t1 = detail.textContent;
await wait(30);
var early = detail.textContent;
g.tilesLoaded = true;
var why = await pr;
return { t12: t12, fill: fill, t1: t1, early: early, why: why, done: detail.textContent,
  renders: (p.viewer.scene.renders || 0) > 0, listeners: g.tileLoadProgressEvent.fns.length,
  skipShown: document.getElementById('sda-loading-skip').has('on') };
""",
    "settle_other": r"""
var p = page({ overlay: true });
window.viewer = p.viewer;
SDAPresent._setTiming({ pollMs: 5, minSettleMs: 0, skipAfterMs: 0, noViewerMs: 5 });
var why1 = await SDAPresent.whenSettled(30);
var skip = document.getElementById('sda-loading-skip');
var pr = SDAPresent.whenSettled(10000);
await wait(30);
var shown = skip.has('on');
skip.fire('click');
var why2 = await pr;
window.viewer = undefined;
var why3 = await SDAPresent.whenSettled(1000);
return { timeout: why1, shown: shown, skipped: why2, noViewer: why3,
  overlayKids: document.getElementById('loading-overlay').children.length };
""",
    "settle_uses_module_viewer": r"""
var p = start();
window.viewer = undefined;
SDAPresent._setTiming({ pollMs: 5, minSettleMs: 0 });
p.viewer.scene.globe.tilesLoaded = true;
return await SDAPresent.whenSettled(2000);
""",
    "share": r"""
var p = start();
setCamera(p.viewer, 142.3, 38.1, 600000);
p.viewer.cesiumWidget = { creditContainer: { textContent: 'Esri World Imagery' } };
window.SDAClouds = { state: function () { return { enabled: true, source: 'geo' }; },
  SOURCES: { geo: { label: 'x', layers: [{ credit: 'NOAA GOES-East via NASA GIBS' },
    { credit: 'JMA Himawari via NASA GIBS' }] } } };
var parts = SDAPresent.shareParts();
var pr = SDAPresent.shareImage();
var waiting = p.viewer.scene.postRender.fns.length;
p.viewer.scene.postRender.raiseEvent();
var url = await pr;
var texts = OPS.filter(function (o) { return o[0] === 'text'; }).map(function (o) {
  return o[1]; });
var rects = OPS.filter(function (o) { return o[0] === 'rect'; });
var band = rects[0];
return { parts: parts, waiting: waiting, url: url, downloads: DOWNLOADS,
  image: OPS[0], texts: texts, band: band, chipColours: rects.slice(2).map(function (r) {
    return r[5]; }),
  after: p.viewer.scene.postRender.fns.length,
  status: byClass(p.mount, 'sdapresent-status')[0].textContent };
""",
    "share_errors": r"""
var p = start();
var first = SDAPresent.shareImage({ download: false });
var second = null;
try { await SDAPresent.shareImage(); } catch (e) { second = e.message; }
NO2D = true;
p.viewer.scene.postRender.raiseEvent();
var failed = null;
try { await first; } catch (e) { failed = e.message; }
var status = byClass(p.mount, 'sdapresent-status')[0].textContent;
NO2D = false;
var third = SDAPresent.shareImage({ download: false });
p.viewer.scene.postRender.raiseEvent();
var url = await third;
return { second: second, failed: failed, status: status, url: url, downloads: DOWNLOADS.length };
""",
    "init_guards": r"""
var p = page();
var noMount = SDAPresent.init({ viewer: p.viewer });
var noViewer = SDAPresent.init({ mount: p.mount });
var notReady = null;
try { await SDAPresent.shareImage(); } catch (e) { notReady = e.message; }
return { noMount: noMount, noViewer: noViewer, notReady: notReady,
  state: SDAPresent.state(), parts: SDAPresent.shareParts() };
""",
}


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_scenarios(tmp_path_factory.mktemp("present"), ["presentation.js"], SCENARIOS,
                         extra_prelude=EXTRA)


@needs_node
def test_pure_helpers(ran):
    r = value(ran, "pure")
    assert r["legend"] == [{"key": "satellites", "label": "Satellites", "source": "CelesTrak"},
                           {"key": "quakes", "label": "Earthquakes", "source": "USGS"}]
    assert r["attr"] == "a. b c"
    assert r["attr2"] == "Imagery: Esri. Data: USGS"
    assert r["fit"] == ["aaa bbb", "ccc ddd"]
    assert r["cut"] == ["aaa bbb", "ccc ..."]
    assert r["long"] == ["ab..."]
    assert r["none"] == []
    assert r["cams"] == ["38.10N 142.30W, alt 600 km", "5.00S 10.00E, alt 850 m", ""]
    assert r["stamp"] == "2026-11-19 12:00:00 UTC" and r["badStamp"] == ""
    assert r["choices"] == [5, 10, 20, 60]


@needs_node
def test_idle_hide(ran):
    r = value(ran, "idle")
    assert r["ok"] is True and r["start"] is False
    assert r["at9"] is False and r["at10"] is True
    assert r["afterKey"] is False
    assert r["overPanel"] == [True, False]       # pointer resting on a panel keeps it up
    assert r["offPanel"] is True
    assert r["typing"] is False                  # focus in an input keeps it up
    assert r["loading"] is False and r["loaded"] is True
    assert r["off"] == [False, {"idleHide": False, "idleS": 10}]
    assert r["offLater"] is False
    assert r["css"] == [True, True, True]


@needs_node
def test_bottom_sheet(ran):
    r = value(ran, "narrow")
    assert r["narrow"] is True and r["handle"] == "sda-sheet-handle"
    assert r["text"] == "LAYERS AND TOOLS" and r["aria"] == "false"
    assert r["open"] == [True, "true", "HIDE LAYERS AND TOOLS"]
    assert r["idle"] is False                    # the sheet replaces idle hiding
    assert r["status"].endswith("narrow screen: panels in the bottom sheet.")
    assert r["wide"] == [False, False, False]
    assert r["handles"] == 1                     # the handle is reused, never duplicated


@needs_node
def test_panel_and_settings(ran):
    r = value(ran, "panel")
    assert r["title"] == "Presentation" and r["collapsed"] is True
    assert r["checked"] is True and r["delay"] == "10"
    assert r["options"] == ["5 s", "10 s", "20 s", "60 s"] and r["share"] is True
    assert r["saved"] == {"idleHide": False, "idleS": 20}
    assert r["status"] == "Panels stay up."
    assert r["bad"] is False
    assert r["reloaded"] == [False, "20"]


@needs_node
def test_settle_on_tiles(ran):
    r = value(ran, "settle_tiles")
    assert r["t12"] == "LOADING IMAGERY: 12 TILES"
    assert r["fill"] == "75%"
    assert r["t1"] == "LOADING IMAGERY: 1 TILE"
    assert r["early"] == "LOADING IMAGERY: 1 TILE"   # the poll does not overwrite the count
    assert r["why"] == "tiles" and r["done"] == "IMAGERY READY"
    assert r["renders"] is True and r["listeners"] == 0
    assert r["skipShown"] is False


@needs_node
def test_settle_timeout_skip_and_no_viewer(ran):
    r = value(ran, "settle_other")
    assert r["timeout"] == "timeout"
    assert r["shown"] is True and r["skipped"] == "skipped"
    assert r["noViewer"] == "no viewer"
    assert r["overlayKids"] == 3                 # detail, bar and button added once


@needs_node
def test_settle_uses_module_viewer(ran):
    assert value(ran, "settle_uses_module_viewer") == "tiles"


@needs_node
def test_share_image(ran):
    r = value(ran, "share")
    parts = r["parts"]
    assert parts["title"] == "VANTAGE"
    assert parts["when"] == "2026-11-19 12:00:00 UTC"
    assert parts["camera"] == "38.10N 142.30E, alt 600 km"
    assert parts["layers"] == ["Satellites", "Earthquakes", "Shipping"]
    assert [c["label"] for c in parts["chips"]] == ["Low orbit", "Medium orbit", "Geostationary",
                                                    "High or stretched"]
    # The Cesium credit repeats the basemap credit and is listed once.
    assert parts["attribution"] == (
        "Imagery: Esri World Imagery. "
        "Clouds: NOAA GOES-East via NASA GIBS. Clouds: JMA Himawari via NASA GIBS. "
        "Data: CelesTrak, USGS, aisstream.io. Rendered with CesiumJS")
    assert parts["link"] == "http://127.0.0.1:8000/worldview#v=1&lat=38.1"
    assert r["waiting"] == 1 and r["after"] == 0
    assert r["url"] == "data:image/png;base64,TEST"
    assert r["downloads"] == [{"href": "data:image/png;base64,TEST",
                               "name": "vantage-20261119T120000Z.png", "inBody": True}]
    assert r["image"] == ["image", "CANVAS", 0, 0]
    assert r["band"][1:4] == [0, 900, 1400] and r["band"][4] > 60   # x, y, w, band height
    assert r["chipColours"][:4] == ["#00d4ff", "#00ff88", "#ffd700", "#ff4466"]
    texts = r["texts"]
    assert texts[0] == "VANTAGE"
    assert texts[1] == "2026-11-19 12:00:00 UTC   38.10N 142.30E, alt 600 km"
    assert "Layers: Satellites, Earthquakes, Shipping" in texts
    assert any(t.startswith("Imagery: Esri World Imagery") for t in texts)
    assert texts[-1] == "http://127.0.0.1:8000/worldview#v=1&lat=38.1"
    assert r["status"].startswith("Share image saved (1400 x ")


@needs_node
def test_share_errors(ran):
    r = value(ran, "share_errors")
    assert r["second"] == "A share image is already being made"
    assert r["failed"] == "2D canvas is not available"
    assert r["status"] == "Share image failed: 2D canvas is not available"
    assert r["url"] == "data:image/png;base64,TEST" and r["downloads"] == 0


@needs_node
def test_init_guards(ran):
    r = value(ran, "init_guards")
    assert r == {"noMount": False, "noViewer": False, "notReady": "not ready", "state": None,
                 "parts": None}


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert text.isascii() and "innerHTML" not in text
    assert "global.SDAPresent" in text
    assert json.dumps("sda.present.v1") in text.replace("'", '"')
