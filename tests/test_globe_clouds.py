"""Tests for the live clouds module (static/js/clouds.js).

Runs under Node's `vm` with the DOM stub and fake Cesium from tests/globe_vm.py, plus a
fake imagery stack and a fake 2D canvas, so no tile is fetched. The URLs are checked
against the GIBS Web Mercator layout and the nowCOAST WMS request that were confirmed by
hand (2026-11-19: both answer 200 with Access-Control-Allow-Origin: *).
"""

from __future__ import annotations

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

MODULE = JS_DIR / "clouds.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

EXTRA = r"""
function UrlTemplateImageryProvider(o) {
  this.url = o.url; this.maximumLevel = o.maximumLevel; this.credit = o.credit;
  this.errorEvent = new Cesium.Event(); this.tilingScheme = 'mercator'; this.tileWidth = 256;
  this.tileHeight = 256; this.minimumLevel = 0; this.rectangle = 'world';
}
UrlTemplateImageryProvider.prototype.requestImage = function (x, y, l) {
  return TILE === undefined ? undefined : Promise.resolve(TILE);
};
UrlTemplateImageryProvider.prototype.getTileCredits = function () { return ['credit']; };
function ImageryLayer(p) { this.imageryProvider = p; this.alpha = 1; }
function Credit(t) { this.text = t; }
Cesium.UrlTemplateImageryProvider = UrlTemplateImageryProvider;
Cesium.ImageryLayer = ImageryLayer;
Cesium.Credit = Credit;
function Layers() { this.list = []; this.removed = []; }
Layers.prototype.add = function (l) { this.list.push(l); return l; };
Layers.prototype.remove = function (l, destroy) { var at = this.list.indexOf(l);
  if (at >= 0) { this.list.splice(at, 1); this.removed.push(!!destroy); } return at >= 0; };

// A 2D canvas that keeps its pixels in a plain array.
var TILE;
var realCreate = document.createElement;
document.createElement = function (tag) {
  var e = realCreate.call(document, tag);
  if (String(tag).toLowerCase() === 'canvas') {
    e.getContext = function () {
      var c = { data: null };
      c.drawImage = function (img) { c.data = img.pixels.slice(); };
      c.getImageData = function () { return { data: c.data }; };
      c.putImageData = function (px) { e.pixels = px.data.slice(); };
      return c;
    };
  }
  return e;
};

var NOW = Date.UTC(2026, 10, 20, 4, 13, 0);
SDAClouds._setClock(function () { return NOW; });
SDAClouds._setTiming({ swapMs: 0 });
var v = makeViewer();
v.imageryLayers = new Layers();
var ctx = { viewer: v, mount: new El('div') };
function status() { return byClass(ctx.mount, 'sdaclouds-status')[0].textContent; }
function urls() { return v.imageryLayers.list.map(function (l) {
  var p = l.imageryProvider; return (p._inner || p).url; }); }
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = EXTRA + body


scenario("pure", r"""
var S = SDAClouds.SOURCES;
var slot = SDAClouds.slotFor(NOW, 600000);
return {
  slot: SDAClouds.utcClock(slot),
  gibs: SDAClouds.urlFor(S.geo.layers[0], 123),
  wms: SDAClouds.urlFor(S.mosaic.layers[0], 456),
  geocolor: SDAClouds.urlFor(S.geocolor.layers[1], 7),
  order: SDAClouds.ORDER
};
""")

scenario("cloud_pixels", r"""
var px = [
  0, 0, 0, 0,          // transparent stays transparent
  50, 50, 50, 255,     // warm sea: gone
  230, 230, 230, 255,  // cold top: opaque white
  150, 150, 150, 255,  // in between
  255, 40, 40, 200,    // enhanced colour on the coldest tops: cloud, keeps source alpha
  210, 210, 210, 128   // bright but half transparent in the source
];
SDAClouds.cloudPixels(px);
return px;
""")

scenario("panel_and_default_off", r"""
var ok = SDAClouds.init(ctx);
return { ok: ok, title: byTag(ctx.mount, 'H3')[0].textContent,
  options: byTag(ctx.mount, 'OPTION').map(function (o) { return o.value; }),
  layers: v.imageryLayers.list.length, status: status(), enabled: SDAClouds.isEnabled(),
  range: byTag(ctx.mount, 'INPUT').filter(function (i) { return i.type === 'range'; })[0].value };
""")

scenario("enable_mosaic_then_switch", r"""
SDAClouds.init(ctx);
var box = byTag(ctx.mount, 'INPUT').filter(function (i) { return i.type === 'checkbox'; })[0];
box.checked = true; box.fire('change');
var mosaic = { urls: urls(), alpha: v.imageryLayers.list[0].alpha,
  wrapped: v.imageryLayers.list[0].imageryProvider instanceof SDAClouds.CloudProvider,
  status: status() };
var sel = byTag(ctx.mount, 'SELECT')[0];
sel.value = 'geo'; sel.fire('change');
var geo = { urls: urls(), removed: v.imageryLayers.removed.slice() };
SDAClouds.setSource('geocolor');
var gc = { urls: urls(), wrapped: v.imageryLayers.list[0].imageryProvider instanceof
  SDAClouds.CloudProvider };
SDAClouds.setSource('nope');
return { mosaic: mosaic, geo: geo, gc: gc, state: SDAClouds.state() };
""")

scenario("opacity", r"""
SDAClouds.init(ctx);
SDAClouds.setEnabled(true);
var range = byTag(ctx.mount, 'INPUT').filter(function (i) { return i.type === 'range'; })[0];
range.value = '35'; range.fire('input');
var a = v.imageryLayers.list[0].alpha;
SDAClouds.setOpacity(3);
return { a: a, clamped: v.imageryLayers.list[0].alpha, label: byTag(ctx.mount, 'SPAN')
  .map(function (s) { return s.textContent; }).filter(function (t) { return /%$/.test(t); }) };
""")

scenario("refresh_swaps_layers", r"""
SDAClouds.init(ctx);
SDAClouds.setSource('geo');
SDAClouds.setEnabled(true);
var first = urls();
SDAClouds._check();                 // same slot: nothing new
var same = v.imageryLayers.list.length;
NOW += 10 * 60000;
SDAClouds._check();
var during = v.imageryLayers.list.length;
await settle();
return { first: first[0], same: same, during: during, after: v.imageryLayers.list.length,
  next: urls()[0], state: SDAClouds.state() };
""")

scenario("tile_errors_reported", r"""
SDAClouds.init(ctx);
SDAClouds.setEnabled(true);
var inner = v.imageryLayers.list[0].imageryProvider._inner;
inner.errorEvent.raiseEvent({}); inner.errorEvent.raiseEvent({});
SDAClouds._check();
return status();
""")

scenario("provider_wraps_tiles", r"""
SDAClouds.init(ctx);
SDAClouds.setEnabled(true);
var p = v.imageryLayers.list[0].imageryProvider;
var props = { tiling: p.tilingScheme, max: p.maximumLevel, width: p.tileWidth,
  alpha: p.hasAlphaChannel, credits: p.getTileCredits(0, 0, 0), pick: p.pickFeatures() };
TILE = undefined;
var throttled = p.requestImage(0, 0, 0);
TILE = { width: 2, height: 1, pixels: [40, 40, 40, 255, 240, 240, 240, 255] };
var out = await p.requestImage(1, 1, 1);
var broken = { width: 2, height: 1 };           // no pixels: drawImage throws
TILE = broken;
var same = (await p.requestImage(1, 1, 1)) === broken;
return { props: props, throttled: throttled === undefined, pixels: out.pixels, same: same };
""")

scenario("disable_clears", r"""
SDAClouds.init(ctx);
SDAClouds.setEnabled(true);
NOW += 20 * 60000;
SDAClouds._check();
SDAClouds.setEnabled(false);
await settle();
return { layers: v.imageryLayers.list.length, state: SDAClouds.state(), status: status(),
  destroyed: v.imageryLayers.removed.every(function (d) { return d; }) };
""")

scenario("threshold_reloads", r"""
SDAClouds.init(ctx);
SDAClouds.setEnabled(true);
var before = v.imageryLayers.list[0];
SDAClouds.setThreshold(0.9, 0.2);    // rejected
var kept = v.imageryLayers.list[0] === before;
SDAClouds.setThreshold(0.3, 0.6);
await settle();
var p = v.imageryLayers.list[0].imageryProvider;
return { kept: kept, lo: p._lo, hi: p._hi, count: v.imageryLayers.list.length };
""")


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_scenarios(tmp_path_factory.mktemp("clouds"), ["clouds.js"], SCENARIOS)


@needs_node
def test_urls_and_slots(ran):
    r = value(ran, "pure")
    assert r["slot"] == "04:10 UTC"
    assert r["gibs"] == (
        "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/GOES-East_ABI_Band13_Clean_Infrared"
        "/default/default/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png?_=123")
    assert r["wms"].startswith(
        "https://nowcoast.noaa.gov/geoserver/satellite/wms?service=WMS&version=1.3.0"
        "&request=GetMap&layers=global_longwave_imagery_mosaic&")
    assert "crs=EPSG:3857" in r["wms"] and "transparent=true" in r["wms"]
    assert r["wms"].endswith(
        "&bbox={westProjected},{southProjected},{eastProjected},{northProjected}&_=456")
    assert "GOES-West_ABI_GeoColor/default/default/GoogleMapsCompatible_Level7/" in r["geocolor"]
    assert r["order"] == ["mosaic", "geo", "geocolor"]


@needs_node
def test_cloud_pixels(ran):
    px = value(ran, "cloud_pixels")
    assert px[0:4] == [0, 0, 0, 0]
    assert px[7] == 0
    assert px[8:12] == [255, 255, 255, 255]
    assert 0 < px[15] < 255
    assert px[19] == 200
    assert px[20:24] == [255, 255, 255, 128]


@needs_node
def test_panel_defaults(ran):
    r = value(ran, "panel_and_default_off")
    assert r["ok"] and r["title"] == "Live clouds"
    assert r["options"] == ["mosaic", "geo", "geocolor"]
    assert r["layers"] == 0 and r["enabled"] is False and r["range"] == "70"
    assert r["status"] == "Off. Live imagery, not tied to the sim clock."


@needs_node
def test_sources_switch(ran):
    r = value(ran, "enable_mosaic_then_switch")
    mosaic = r["mosaic"]
    assert len(mosaic["urls"]) == 1
    assert "global_longwave_imagery_mosaic" in mosaic["urls"][0]
    assert mosaic["alpha"] == pytest.approx(0.7) and mosaic["wrapped"]
    assert mosaic["status"].startswith("NOAA GMGSI global infrared, live (not the sim clock).")
    assert "refreshes every 15 min" in mosaic["status"]
    assert [u.split("/best/")[1].split("/")[0] for u in r["geo"]["urls"]] == [
        "GOES-East_ABI_Band13_Clean_Infrared", "GOES-West_ABI_Band13_Clean_Infrared",
        "Himawari_AHI_Band13_Clean_Infrared"]
    assert r["geo"]["removed"] == [True]
    assert len(r["gc"]["urls"]) == 2 and r["gc"]["wrapped"] is False
    assert r["state"]["source"] == "geocolor" and r["state"]["layers"] == 2


@needs_node
def test_opacity(ran):
    r = value(ran, "opacity")
    assert r["a"] == pytest.approx(0.35) and r["clamped"] == 1
    assert r["label"] == ["100%"]


@needs_node
def test_refresh(ran):
    r = value(ran, "refresh_swaps_layers")
    assert r["same"] == 3 and r["during"] == 6 and r["after"] == 3
    assert r["first"] != r["next"]
    assert r["state"]["retiring"] == 0


@needs_node
def test_tile_errors(ran):
    assert value(ran, "tile_errors_reported").endswith("2 tiles failed to load.")


@needs_node
def test_provider(ran):
    r = value(ran, "provider_wraps_tiles")
    # pickFeatures() answers undefined, which JSON drops.
    assert r["props"] == {"tiling": "mercator", "max": 5, "width": 256, "alpha": True,
                          "credits": ["credit"]}
    assert r["throttled"] is True
    assert r["pixels"][3] == 0 and r["pixels"][4:8] == [255, 255, 255, 255]
    assert r["same"] is True


@needs_node
def test_disable(ran):
    r = value(ran, "disable_clears")
    assert r["layers"] == 0 and r["state"]["layers"] == 0 and r["state"]["retiring"] == 0
    assert r["status"].startswith("Off") and r["destroyed"]


@needs_node
def test_threshold(ran):
    assert value(ran, "threshold_reloads") == {"kept": True, "lo": 0.3, "hi": 0.6, "count": 1}


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert text.isascii() and "innerHTML" not in text
    assert "epsg4326" not in text            # the geographic grid draws the world in a corner
