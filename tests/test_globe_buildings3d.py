"""Tests for the globe's 3D city module (static/js/buildings3d.js).

The JavaScript runs under Node's `vm` with the DOM stub and vector fake Cesium from
tests/globe_vm.py; nothing touches the network. Google's tiles are a fake
createGooglePhotorealistic3DTileset that resolves or rejects as each scenario needs,
and /api/buildings answers from the fake fetch table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "sda" / "templates" / "worldview.html"
MODULE = JS_DIR / "buildings3d.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The two imagery overlays the photorealistic mode hides in the scenario below,
# under the names their own panels give them.
NASA_LABEL = "Dated imagery (NASA GIBS)"
RADAR_LABEL = "Radar (RainViewer)"

PRELUDE = r"""
Cesium.HeightReference = { NONE: 0, CLAMP_TO_GROUND: 1, RELATIVE_TO_GROUND: 2 };
Cesium.PolygonHierarchy = function (positions) { this.positions = positions; };
Cesium.Cartesian3.fromDegreesArray = function (flat) {
  var out = [];
  for (var i = 0; i + 1 < flat.length; i += 2) {
    out.push(Cesium.Cartesian3.fromDegrees(flat[i], flat[i + 1], 0));
  }
  return out;
};
Cesium.Ion = { defaultAccessToken: undefined };
var TILE_CALLS = [];
var TILES_FAIL = false;
var TILES_DEFER = false;   // hold the tileset back until RELEASE_TILES() is called
var HELD = [];
var TILESETS = [];
function fakeTileset() {
  var t = { kind: 'google-tiles', show: true, destroyed: false,
    tileFailed: new Event(), tileLoad: new Event(),
    destroy: function () { this.destroyed = true; },
    isDestroyed: function () { return this.destroyed; } };
  TILESETS.push(t);
  return t;
}
function RELEASE_TILES() { HELD.splice(0).forEach(function (fn) { fn(); }); }
Cesium.createGooglePhotorealistic3DTileset = function (key, opts) {
  TILE_CALLS.push({ key: key === undefined ? null : key,
    ion: Cesium.Ion.defaultAccessToken || null,
    credits: !!(opts && opts.showCreditsOnScreen) });
  if (TILES_FAIL) return Promise.reject(new Error('RuntimeError: quota'));
  var t = fakeTileset();
  if (!TILES_DEFER) return Promise.resolve(t);
  return new Promise(function (resolve) { HELD.push(function () { resolve(t); }); });
};
function failTile() { v.scene.primitives.list[0].tileFailed.raiseEvent(
  { url: 'https://tile.googleapis.com/v1/3dtiles/datasets/x.glb', message: '429' }); }
var INTEL = [];
var v = makeViewer();
v.creditDisplay = { container: new El('div') };
var mount = new El('div');
document.body.appendChild(mount);
var credit = new El('div'); credit.id = 'basemap-credit'; document.body.appendChild(credit);
var VISIBLE = ['satellites'];
var ctx = { viewer: v, mount: mount, api: '', isStatic: false,
  addIntelItem: function (t) { INTEL.push(t); },
  getVisibleLayers: function () { return VISIBLE.slice(); } };
var KANO = { buildings: [
  { id: 'way/1', name: 'Kano Central Mosque', kind: 'mosque', height_m: 9.6,
    height_from: 'levels', levels: 3,
    ring: [[8.587, 12.009], [8.5872, 12.009], [8.5872, 12.0092], [8.587, 12.0092],
      [8.587, 12.009]] },
  { id: 'way/2', name: '', kind: 'building', height_m: 8, height_from: 'default', levels: null,
    ring: [[8.588, 12.01], [8.5882, 12.01], [8.5882, 12.0102], [8.588, 12.01]] }
], count: 2, truncated: false,
  attribution: 'Buildings: OpenStreetMap contributors (ODbL), via Overpass' };
route('/api/buildings', KANO);
function buildingCalls() {
  return REQUESTS.filter(function (u) { return u.indexOf('/api/buildings') === 0; });
}
function q(url, k) {
  var m = new RegExp('[?&]' + k + '=([^&]+)').exec(url);
  return m ? Number(m[1]) : null;
}
function ents() {
  var ds = v.dataSources.list.filter(function (d) { return d.name === 'sda-buildings3d'; })[0];
  return ds ? ds.entities.values : []; }
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


scenario("choose", r"""
var c = SDABuildings3D.chooseCityLayer;
return [c({ google_maps_api_key: 'g', cesium_ion_token: 't' }), c({ cesium_ion_token: 't' }),
  c({})];
""")

scenario("choose_odd", r"""
var c = SDABuildings3D.chooseCityLayer;
return [c(null), c(undefined), c({ google_maps_api_key: null, cesium_ion_token: '' }),
  c({ google_maps_api_key: '', cesium_ion_token: 't' })];
""")

scenario("visible", r"""
var f = SDABuildings3D.visibleAt;
return [f(465), f(2999), f(3000), f(20000000), f(NaN), f(undefined), f(-5)];
""")

scenario("hidden", r"""
return SDABuildings3D.hiddenBy3D(
  { satellites: true, nasa_imagery: true, radar: true, clouds: false });
""")

scenario("fallback", r"""
TILES_FAIL = true;
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token', google_maps_api_key: null });
await SDABuildings3D.setCityMode(true);
await settle();
var s = SDABuildings3D.state();
return { layer: s.layer, said: INTEL.indexOf(SDABuildings3D.FAIL_TEXT) >= 0 };
""")

scenario("fallback_details", r"""
TILES_FAIL = true;
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
await SDABuildings3D.setCityMode(true);
await settle();
return { calls: TILE_CALLS, globe: v.scene.globe.show, primitives: v.scene.primitives.list.length,
  buildings: ents().length, status: mount.textContent.indexOf(SDABuildings3D.FAIL_TEXT) >= 0 };
""")

scenario("google_ion", r"""
setCamera(v, 8.58719, 12.00949, 465);
window.SDANasaImagery = { isImageryOn: function () { return true; } };
window.SDAWeather = { isEnabled: function () { return true; },
  isLayerOn: function (n) { return n === 'radar'; } };
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
await SDABuildings3D.setCityMode(true);
await settle();
var on = { layer: SDABuildings3D.state().layer, globe: v.scene.globe.show,
  primitives: v.scene.primitives.list.map(function (p) { return p.kind; }),
  calls: TILE_CALLS.slice(),
  buildingRequests: buildingCalls().length, credit: credit.textContent,
  creditsShown: !!v.creditDisplay.container.parentNode,
  hiddenSaid: INTEL.filter(function (t) { return t.indexOf('hides') >= 0; }) };
await SDABuildings3D.setCityMode(false);
return { on: on, off: { layer: SDABuildings3D.state().layer, globe: v.scene.globe.show,
  primitives: v.scene.primitives.list.length, tilesShown: TILESETS[0].show,
  creditsShown: !!v.creditDisplay.container.parentNode } };
""")

scenario("google_key", r"""
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ google_maps_api_key: 'gkey', cesium_ion_token: 'free-token' });
await SDABuildings3D.setCityMode(true);
return { layer: SDABuildings3D.state().layer, calls: TILE_CALLS };
""")

scenario("osm", r"""
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: null, google_maps_api_key: null });
await SDABuildings3D.setCityMode(true);
await settle();
var urls = buildingCalls();
var e = ents();
var mosque = e.filter(function (x) { return x.id === 'bld-way/1'; })[0];
var plain = e.filter(function (x) { return x.id === 'bld-way/2'; })[0];
var u = urls[0];
var out = {
  layer: SDABuildings3D.state().layer, tileCalls: TILE_CALLS.length, globe: v.scene.globe.show,
  requests: urls.length,
  spanLat: Math.round((q(u, 'north') - q(u, 'south')) * 1e4) / 1e4,
  spanLon: Math.round((q(u, 'east') - q(u, 'west')) * 1e4) / 1e4,
  inside: q(u, 'south') < 12.00949 && 12.00949 < q(u, 'north') &&
    q(u, 'west') < 8.58719 && 8.58719 < q(u, 'east'),
  count: e.length, name: mosque.name, plainName: plain.name, type: mosque.properties.type,
  rows: mosque.properties.detail_rows, plainRows: plain.properties.detail_rows,
  extruded: mosque.polygon.extrudedHeight, hr: mosque.polygon.heightReference,
  ehr: mosque.polygon.extrudedHeightReference,
  positions: mosque.polygon.hierarchy.positions.length,
  colours: [mosque.polygon.material.css, plain.polygon.material.css],
  tokenNote: mount.textContent.indexOf('CESIUM_ION_TOKEN') >= 0,
  credit: credit.textContent
};
// The same view again asks for nothing new.
v.camera.moveEnd.raiseEvent();
await settle();
out.again = buildingCalls().length;
// Climbing past 3,000 m hides the buildings without dropping them.
setCamera(v, 8.58719, 12.00949, 20000);
v.camera.moveEnd.raiseEvent();
await settle();
out.high = { show: v.dataSources.list[0].show, count: ents().length,
  requests: buildingCalls().length };
await SDABuildings3D.setCityMode(false);
out.off = { layer: SDABuildings3D.state().layer, count: ents().length };
return out;
""")

scenario("osm_error", r"""
setCamera(v, 8.58719, 12.00949, 465);
route('/api/buildings', { buildings: [], count: 0, truncated: false,
  error: 'OpenStreetMap buildings did not load (the free Overpass server is busy or offline);' +
    ' move the map to try again.' });
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({});
await SDABuildings3D.setCityMode(true);
await settle();
var said = mount.textContent.indexOf('Overpass server is busy') >= 0;
v.camera.moveEnd.raiseEvent();
await settle();
return { said: said, retried: buildingCalls().length, count: ents().length };
""")

scenario("before_init", r"""
setCamera(v, 8.58719, 12.00949, 465);
// The page applies a share link (bm=photoreal) before the modules start.
SDABuildings3D.setConfig({});
SDABuildings3D.setCityMode(true);
var early = buildingCalls().length;
SDABuildings3D.init(ctx);
await settle();
return { early: early, layer: SDABuildings3D.state().layer, count: ents().length };
""")

scenario("clamp", r"""
setCamera(v, 8.58719, 12.00949, 465);
var pin = v.entities.add({ id: 'pin', position: Cesium.Cartesian3.fromDegrees(8.587, 12.009, 0),
  point: { pixelSize: 6 } });
var plane = v.entities.add({ id: 'plane',
  position: Cesium.Cartesian3.fromDegrees(8.587, 12.009, 3000),
  point: { pixelSize: 6 } });
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
await SDABuildings3D.setCityMode(true);
var on = [pin.point.heightReference, plane.point.heightReference === undefined];
await SDABuildings3D.setCityMode(false);
return { on: on, off: pin.point.heightReference === undefined };
""")

scenario("tile_failed", r"""
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
await SDABuildings3D.setCityMode(true);
await settle();
var t = TILESETS[0];
// One stray failure, or two after a tile loaded, leave the mesh up.
failTile();
t.tileLoad.raiseEvent({});
failTile(); failTile();
await settle();
var held = { layer: SDABuildings3D.state().layer, globe: v.scene.globe.show,
  said: INTEL.indexOf(SDABuildings3D.FAIL_TEXT) >= 0 };
// A third failure with no tile loaded since: the session is gone, so OSM takes over.
failTile();
await settle();
var fell = { layer: SDABuildings3D.state().layer, globe: v.scene.globe.show,
  said: INTEL.indexOf(SDABuildings3D.FAIL_TEXT) >= 0,
  status: mount.textContent.indexOf(SDABuildings3D.FAIL_TEXT) >= 0,
  destroyed: t.destroyed, primitives: v.scene.primitives.list.length,
  creditsShown: !!v.creditDisplay.container.parentNode,
  buildings: ents().length, tileCalls: TILE_CALLS.length };
// The next entry asks Google again with a fresh tileset.
await SDABuildings3D.setCityMode(false);
await SDABuildings3D.setCityMode(true);
await settle();
var again = { layer: SDABuildings3D.state().layer, tileCalls: TILE_CALLS.length,
  primitives: v.scene.primitives.list.length, fresh: v.scene.primitives.list[0] !== t };
return { held: held, fell: fell, again: again };
""")

scenario("reenter", r"""
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
var seen = [];
for (var i = 0; i < 3; i++) {
  await SDABuildings3D.setCityMode(true);
  await settle();
  seen.push([SDABuildings3D.state().layer, v.scene.globe.show, TILESETS[0].show]);
  await SDABuildings3D.setCityMode(false);
  seen.push([SDABuildings3D.state().layer, v.scene.globe.show, TILESETS[0].show]);
}
return { seen: seen, tileCalls: TILE_CALLS.length, primitives: v.scene.primitives.list.length,
  destroyed: TILESETS[0].destroyed };
""")

scenario("config_same", r"""
setCamera(v, 8.58719, 12.00949, 465);
SDABuildings3D.init(ctx);
// A share link with bm=photoreal opens the city before /api/config has answered.
await SDABuildings3D.setCityMode(true);
await settle();
var first = buildingCalls().length;
await SDABuildings3D.setConfig({ cesium_ion_token: null, google_maps_api_key: null });
await settle();
return { first: first, after: buildingCalls().length, count: ents().length,
  layer: SDABuildings3D.state().layer };
""")

scenario("late_tileset", r"""
setCamera(v, 8.58719, 12.00949, 465);
TILES_DEFER = true;
SDABuildings3D.init(ctx);
SDABuildings3D.setConfig({ cesium_ion_token: 'free-token' });
// Passing through PHOTOREAL 3D before the tiles arrive: the late tileset is destroyed.
SDABuildings3D.setCityMode(true);
await SDABuildings3D.setCityMode(false);
RELEASE_TILES();
await settle();
var late = { destroyed: TILESETS[0].destroyed, primitives: v.scene.primitives.list.length,
  globe: v.scene.globe.show, layer: SDABuildings3D.state().layer };
// Leaving and coming back while the tiles are on their way waits for the same load.
SDABuildings3D.setCityMode(true);
await SDABuildings3D.setCityMode(false);
var back = SDABuildings3D.setCityMode(true);
RELEASE_TILES();
await back;
await settle();
return { late: late, tileCalls: TILE_CALLS.length, layer: SDABuildings3D.state().layer,
  kept: TILESETS[1].destroyed === false && v.scene.primitives.list[0] === TILESETS[1],
  globe: v.scene.globe.show };
""")


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_scenarios(tmp_path_factory.mktemp("buildings3d"), ["buildings3d.js"], SCENARIOS,
                         extra_prelude=PRELUDE)


def test_choose_city_layer(ran):
    assert value(ran, "choose") == ["google-key", "google-ion", "osm"]
    assert value(ran, "choose_odd") == ["osm", "osm", "osm", "google-ion"]


def test_buildings_show_below_3000_m(ran):
    assert value(ran, "visible") == [True, True, False, False, False, False, True]


def test_city_mode_names_hidden_layers(ran):
    assert value(ran, "hidden") == [NASA_LABEL, RADAR_LABEL]   # those two on, satellites on


def test_hidden_names_are_the_labels_the_panels_show():
    assert f"'{NASA_LABEL}'" in (JS_DIR / "nasaimagery.js").read_text(encoding="utf-8")
    assert f"'{RADAR_LABEL}'" in (JS_DIR / "weather.js").read_text(encoding="utf-8")


def test_google_tiles_failure_falls_back_to_osm(ran):
    assert value(ran, "fallback") == {"layer": "osm", "said": True}
    r = value(ran, "fallback_details")
    assert r["calls"] == [{"key": None, "ion": "free-token", "credits": True}]
    assert r["globe"] is True and r["primitives"] == 0
    assert r["buildings"] == 2 and r["status"] is True


def test_google_tiles_through_ion(ran):
    r = value(ran, "google_ion")
    on, off = r["on"], r["off"]
    assert on["layer"] == "google-ion"
    assert on["globe"] is False and on["primitives"] == ["google-tiles"]
    assert on["calls"] == [{"key": None, "ion": "free-token", "credits": True}]
    assert on["buildingRequests"] == 0
    assert "Google" in on["credit"] and "Cesium ion" in on["credit"]
    assert on["creditsShown"] is True
    assert len(on["hiddenSaid"]) == 1
    assert NASA_LABEL in on["hiddenSaid"][0] and RADAR_LABEL in on["hiddenSaid"][0]
    # Leaving hides the tileset and keeps it: the next entry costs no new root load.
    assert off == {"layer": None, "globe": True, "primitives": 1, "tilesShown": False,
                   "creditsShown": False}


def test_tile_failures_after_load_fall_back_to_osm(ran):
    r = value(ran, "tile_failed")
    assert r["held"] == {"layer": "google-ion", "globe": False, "said": False}
    fell = r["fell"]
    assert fell["layer"] == "osm" and fell["globe"] is True
    assert fell["said"] is True and fell["status"] is True
    assert fell["destroyed"] is True and fell["primitives"] == 0
    assert fell["creditsShown"] is False and fell["buildings"] == 2
    assert fell["tileCalls"] == 1
    # Google is tried again on the next entry, with a new tileset.
    assert r["again"] == {"layer": "google-ion", "tileCalls": 2, "primitives": 1, "fresh": True}


def test_reentering_photoreal_reuses_one_tileset(ran):
    r = value(ran, "reenter")
    assert r["tileCalls"] == 1 and r["primitives"] == 1 and r["destroyed"] is False
    assert r["seen"] == [["google-ion", False, True], [None, True, False]] * 3


def test_config_with_the_same_route_keeps_the_city(ran):
    r = value(ran, "config_same")
    assert r == {"first": 1, "after": 1, "count": 2, "layer": "osm"}


def test_a_tileset_arriving_after_leave_is_destroyed(ran):
    r = value(ran, "late_tileset")
    assert r["late"] == {"destroyed": True, "primitives": 0, "globe": True, "layer": None}
    assert r["tileCalls"] == 2
    assert r["layer"] == "google-ion" and r["kept"] is True and r["globe"] is False


def test_google_key_wins_over_ion(ran):
    r = value(ran, "google_key")
    assert r["layer"] == "google-key"
    assert [c["key"] for c in r["calls"]] == ["gkey"]


def test_osm_buildings_are_extruded_named_and_clickable(ran):
    r = value(ran, "osm")
    assert r["layer"] == "osm" and r["tileCalls"] == 0 and r["globe"] is True
    assert r["requests"] == 1 and r["inside"] is True
    assert 0 < r["spanLat"] <= 0.02 and 0 < r["spanLon"] <= 0.02
    assert r["count"] == 2
    assert r["name"] == "Kano Central Mosque" and r["plainName"] == "Building"
    assert r["type"] == "Building"
    rows = dict(r["rows"])
    assert rows["HEIGHT"] == "9.6 m (from 3 levels)"
    assert rows["LEVELS"] == "3"
    assert rows["OSM ID"] == "way/1"
    plain = dict(r["plainRows"])
    assert plain["HEIGHT"] == "8 m (not tagged, assumed)" and plain["LEVELS"] == "not tagged"
    assert r["extruded"] == 9.6 and r["hr"] == 1 and r["ehr"] == 2
    assert r["positions"] == 5
    assert r["colours"][0] != r["colours"][1]  # coloured by kind
    assert r["tokenNote"] is True
    assert "OpenStreetMap" in r["credit"]
    assert r["again"] == 1
    assert r["high"] == {"show": False, "count": 2, "requests": 1}
    assert r["off"] == {"layer": None, "count": 0}


def test_overpass_trouble_reaches_the_panel(ran):
    r = value(ran, "osm_error")
    assert r["said"] is True and r["count"] == 0
    assert r["retried"] == 2  # a failed box is asked for again on the next move


def test_city_mode_chosen_before_the_module_starts(ran):
    r = value(ran, "before_init")
    assert r["early"] == 0 and r["layer"] == "osm" and r["count"] == 2


def test_ground_marks_sit_on_the_mesh(ran):
    r = value(ran, "clamp")
    assert r["on"] == [1, True]  # the pin is clamped, the aircraft at 3 km is left alone
    assert r["off"] is True


def test_page_wiring():
    page = PAGE.read_text(encoding="utf-8")
    assert '<script src="static/js/buildings3d.js"></script>' in page
    assert "{ global: 'SDABuildings3D', mount: 'ext-buildings3d' }" in page
    assert '<div id="ext-buildings3d"></div>' in page
    assert 'data-basemap="photoreal"' in page
    config = page[page.index("async function checkConfig()"):page.index("// ===== PWA")]
    assert "SDABuildings3D.setConfig(config)" in config
    assert "tile.googleapis.com" not in config  # one loader, in buildings3d.js


def test_module_text_rules():
    text = MODULE.read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in text)
    assert ".innerHTML" not in text and "eval(" not in text
    assert "global.SDABuildings3D = " in text
