"""Tests for the globe visual-quality module (static/js/visuals.js).

Runs under Node's `vm` with the DOM stub and fake Cesium from tests/globe_vm.py, plus
fakes for post-process stages and imagery layers. Shaders cannot compile here, so their
text is checked for the constructs the page's styles.js relies on and for the automatic
uniforms the night mask needs; the render-error path that removes them is exercised.
"""

from __future__ import annotations

import pytest

from tests.globe_vm import JS_DIR, NODE, run_scenarios, value

MODULE = JS_DIR / "visuals.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

EXTRA = r"""
function PostProcessStage(o) { var s = this; Object.keys(o).forEach(function (k) { s[k] = o[k]; });
  this.enabled = true; this.kind = 'stage'; }
function PostProcessStageComposite(o) { var s = this;
  Object.keys(o).forEach(function (k) { s[k] = o[k]; });
  if (this.inputPreviousStageTexture === undefined) this.inputPreviousStageTexture = true;
  this.enabled = true; this.kind = 'composite'; }
function ImageryLayer(provider) { this.provider = provider; this.alpha = 1; }
function UrlTemplateImageryProvider(o) { this.url = o.url; this.maximumLevel = o.maximumLevel; }
function Credit(t) { this.text = t; }
Cesium.PostProcessStage = PostProcessStage;
Cesium.PostProcessStageComposite = PostProcessStageComposite;
Cesium.ImageryLayer = ImageryLayer;
Cesium.UrlTemplateImageryProvider = UrlTemplateImageryProvider;
Cesium.Credit = Credit;
Cesium.SceneMode = { SCENE2D: 2, COLUMBUS_VIEW: 1, SCENE3D: 3 };
Cesium.DynamicAtmosphereLightingType = { NONE: 0, SCENE_LIGHT: 1, SUNLIGHT: 2 };

function Layers() { this.list = []; }
Layers.prototype.add = function (l, i) { if (i === undefined) this.list.push(l);
  else this.list.splice(i, 0, l); return l; };
Layers.prototype.remove = function (l) { var at = this.list.indexOf(l);
  if (at >= 0) this.list.splice(at, 1); return at >= 0; };
Object.defineProperty(Layers.prototype, 'length',
  { get: function () { return this.list.length; } });

var STORE = {};
var localStorage = { getItem: function (k) { return k in STORE ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); } };
this.localStorage = localStorage;

var NOW = 1000;
SDAVisuals._setClock(function () { return NOW; });
var BUDGETS = [];
window.SDAModels = { setBudget: function (b) { BUDGETS.push(b); } };
window.BASEMAPS = { earth: { lighting: false }, daynight: { lighting: true } };
window.currentBasemap = 'earth';
var v = makeViewer();
v.imageryLayers = new Layers();
var base1 = new ImageryLayer('blue'), base2 = new ImageryLayer('esri');
v.imageryLayers.add(base1); v.imageryLayers.add(base2);
window.basemapLayers = [base1, base2];
v.scene.skyAtmosphere.perFragmentAtmosphere = false;
v.scene.atmosphere = { dynamicLighting: 0 };
v.scene.globe.maximumScreenSpaceError = 2;
var ctx = { viewer: v, mount: new El('div') };
var g = v.scene.globe;
function glowStage() {
  return v.scene.postProcessStages.list.filter(function (s) {
    return s.name === 'sdaVisualsGlow'; })[0];
}
function frames(n, fps) {
  // n post-render callbacks spread over one 2 s window at the given rate.
  for (var i = 0; i < n; i++) { NOW += 1000 / fps; SDAVisuals._postRender(); }
}
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


scenario("adapt_pure", r"""
var a = SDAVisuals.adaptScale;
var s = { scale: 1.0 };
var out = [];
s = a(s, 20, 1.0); out.push([s.scale, s.changed]);
s = a(s, 20, 1.0); out.push([s.scale, s.changed]);
s = a(s, 40, 1.0); out.push([s.scale, s.changed]);
for (var i = 0; i < 3; i++) { s = a(s, 60, 1.0); }
out.push([s.scale, s.changed]);
s = a(a({ scale: 0.5, low: 1 }, 10, 1.0), 10, 1.0); out.push([s.scale]);
s = a({ scale: 1.0, high: 2 }, 60, 1.0); out.push([s.scale, s.changed]);
s = a({ scale: 0.8 }, NaN, 1.0); out.push([s.scale, s.changed]);
return out;
""")

scenario("status_text", r"""
return [SDAVisuals.statusText({ fps: 59.6, scale: 0.85, preset: 'medium', auto: true, note: '' }),
  SDAVisuals.statusText({ fps: 0, scale: 1, preset: 'high', auto: false,
    note: 'Glow switched off after a render error' })];
""")

scenario("init_defaults", r"""
var ok = SDAVisuals.init(ctx);
var glow = glowStage();
return { ok: ok, lighting: g.enableLighting, ground: g.showGroundAtmosphere,
  dyn: g.dynamicAtmosphereLightingFromSun, atmo: v.scene.atmosphere.dynamicLighting,
  fog: v.scene.fog.enabled, fxaa: v.scene.postProcessStages.fxaa.enabled,
  builtInBloom: v.scene.postProcessStages.bloom.enabled,
  glow: !!glow && glow.enabled, sse: g.maximumScreenSpaceError, scale: v.resolutionScale,
  fades: [g.lightingFadeOutDistance, g.lightingFadeInDistance, g.nightFadeOutDistance,
    g.nightFadeInDistance],
  layers: v.imageryLayers.list.map(function (l) {
    return l === base1 ? 'blue' : l === base2 ? 'esri' : l.provider.url; }),
  night: { day: v.imageryLayers.list[2].dayAlpha, night: v.imageryLayers.list[2].nightAlpha },
  budgets: BUDGETS, title: byTag(ctx.mount, 'H3')[0].textContent,
  options: byTag(ctx.mount, 'OPTION').map(function (o) { return o.textContent; }),
  boxes: byTag(ctx.mount, 'INPUT').length };
""")

scenario("glow_structure", r"""
SDAVisuals.init(ctx);
var glow = glowStage();
var gen = glow.stages[0], combine = glow.stages[1];
return { names: [gen.name, combine.name].concat(gen.stages.map(function (s) { return s.name; })),
  input: glow.inputPreviousStageTexture, genInput: gen.inputPreviousStageTexture,
  glowTexture: combine.uniforms.glowTexture, scales: gen.stages.map(function (s) {
    return s.textureScale; }),
  dirs: [gen.stages[1].uniforms.direction.x, gen.stages[2].uniforms.direction.y],
  threshold: gen.stages[0].uniforms.threshold };
""")

scenario("basemap_changes_reassert", r"""
SDAVisuals.init(ctx);
var out = {};
window.currentBasemap = 'daynight';
g.enableLighting = true;
NOW += 1500; SDAVisuals._preUpdate();
out.daynightLayers = v.imageryLayers.length;
out.daynightNight = SDAVisuals.state().nightLayer;
window.currentBasemap = 'earth';
g.enableLighting = false;          // what setBasemap('earth') writes
NOW += 1500; SDAVisuals._preUpdate();
out.earthLighting = g.enableLighting;
out.earthNight = SDAVisuals.state().nightLayer;
g.enableLighting = false;          // something else turned it off
NOW += 300; SDAVisuals._preUpdate();
out.throttled = g.enableLighting;
NOW += 1000; SDAVisuals._preUpdate();
out.again = g.enableLighting;
return out;
""")

scenario("lighting_off_follows_basemap", r"""
SDAVisuals.init(ctx);
SDAVisuals.setOption('lighting', false);
var earth = { lighting: g.enableLighting, night: SDAVisuals.state().nightLayer,
  fade: g.lightingFadeOutDistance };
SDAVisuals.setOption('atmosphere', false);
SDAVisuals.setOption('fog', false);
return { earth: earth, ground: g.showGroundAtmosphere, fog: v.scene.fog.enabled,
  stored: JSON.parse(STORE['sda.visuals.v1']).opts };
""")

scenario("presets", r"""
SDAVisuals.init(ctx);
SDAVisuals.setPreset('low');
var low = { scale: v.resolutionScale, fxaa: v.scene.postProcessStages.fxaa.enabled,
  glow: glowStage().enabled, sse: g.maximumScreenSpaceError,
  native: v.useBrowserRecommendedResolution };
SDAVisuals.setPreset('high');
var high = { scale: v.resolutionScale, fxaa: v.scene.postProcessStages.fxaa.enabled,
  glow: glowStage().enabled, sse: g.maximumScreenSpaceError,
  native: v.useBrowserRecommendedResolution,
  perFragment: v.scene.skyAtmosphere.perFragmentAtmosphere };
SDAVisuals.setPreset('bogus');
return { low: low, high: high, budgets: BUDGETS, preset: SDAVisuals.state().preset,
  select: byTag(ctx.mount, 'SELECT')[0].value };
""")

scenario("auto_adapts_to_fps", r"""
SDAVisuals.init(ctx);
SDAVisuals._postRender();                 // opens the first window
frames(40, 20); frames(40, 20);           // two slow windows
var down = v.resolutionScale;
frames(40, 20); frames(40, 20);
var down2 = v.resolutionScale;
for (var i = 0; i < 12; i++) frames(120, 60);   // three steps of three fast windows
var up = v.resolutionScale;
SDAVisuals.setAuto(false);
frames(40, 20); frames(40, 20);
var manual = v.resolutionScale;
var status = byClass(ctx.mount, 'sdavis-status')[0].textContent;
return { down: down, down2: down2, up: up, manual: manual, status: status };
""")

scenario("hidden_tab_does_not_adapt", r"""
SDAVisuals.init(ctx);
document.hidden = true;
SDAVisuals._postRender();
frames(40, 10); frames(40, 10); frames(40, 10);
return v.resolutionScale;
""")

scenario("render_error_drops_glow", r"""
SDAVisuals.init(ctx);
v.scene.renderError.raiseEvent(v.scene, new Error('shader'));
var after = { stages: v.scene.postProcessStages.list.length, state: SDAVisuals.state(),
  box: byTag(ctx.mount, 'INPUT').filter(function (b) { return b.checked === false; }).length };
SDAVisuals.setOption('glow', true);
return { after: after, rebuilt: !!glowStage() && glowStage().enabled };
""")

scenario("no_post_processing", r"""
delete Cesium.PostProcessStageComposite;
var ok = SDAVisuals.init(ctx);
return { ok: ok, state: SDAVisuals.state() };
""")

scenario("glow_off_outside_3d", r"""
SDAVisuals.init(ctx);
v.scene.mode = Cesium.SceneMode.SCENE2D;
NOW += 1500; SDAVisuals._preUpdate();
var flat = glowStage().enabled;
v.scene.mode = Cesium.SceneMode.SCENE3D;
NOW += 1500; SDAVisuals._preUpdate();
return [flat, glowStage().enabled];
""")

scenario("disable_restores_page_defaults", r"""
g.lightingFadeOutDistance = 1e7; g.lightingFadeInDistance = 2e7;
SDAVisuals.init(ctx);
SDAVisuals.setPreset('high');
SDAVisuals.setEnabled(false);
return { lighting: g.enableLighting, ground: g.showGroundAtmosphere, fog: v.scene.fog.enabled,
  fxaa: v.scene.postProcessStages.fxaa.enabled, scale: v.resolutionScale,
  native: v.useBrowserRecommendedResolution, sse: g.maximumScreenSpaceError,
  glow: glowStage().enabled, layers: v.imageryLayers.length,
  fades: [g.lightingFadeOutDistance, g.lightingFadeInDistance],
  status: byClass(ctx.mount, 'sdavis-status')[0].textContent, on: SDAVisuals.isEnabled() };
""")

scenario("sun_and_moon", r"""
// The page hides both before the modules start.
v.scene.sun.show = false; v.scene.moon.show = false;
SDAVisuals.init(ctx);
var shown = function () { return [v.scene.sun.show, v.scene.moon.show]; };
var out = { init: shown() };
SDAVisuals.setOption('sunMoon', false); out.off = shown();
SDAVisuals.setOption('sunMoon', true); out.on = shown();
// The sky view owns them while it is open and restores its own copy on exit.
window.SDASkyView = { isActive: function () { return true; } };
SDAVisuals.setOption('sunMoon', false); out.skyOpen = shown();
window.SDASkyView = { isActive: function () { return false; } };
NOW += 1500; SDAVisuals._preUpdate(); out.afterSky = shown();
SDAVisuals.setOption('sunMoon', true);
SDAVisuals.setEnabled(false); out.disabled = shown();
out.stored = JSON.parse(STORE['sda.visuals.v1']).opts.sunMoon;
return out;
""")

scenario("stored_settings_load", r"""
STORE['sda.visuals.v1'] = JSON.stringify({ preset: 'low', auto: false,
  opts: { glow: false, fog: false } });
SDAVisuals.init(ctx);
var s = SDAVisuals.state();
return { preset: s.preset, auto: s.auto, glow: s.opts.glow, fog: s.opts.fog,
  lighting: s.opts.lighting, fogOn: v.scene.fog.enabled };
""")


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    # EXTRA calls SDAVisuals._setClock, so it runs after the module loads: each
    # scenario body starts with it.
    bodies = {k: EXTRA + body for k, body in SCENARIOS.items()}
    return run_scenarios(tmp_path_factory.mktemp("visuals"), ["visuals.js"], bodies)


@needs_node
def test_adapt_scale(ran):
    assert value(ran, "adapt_pure") == [
        [1.0, False], [0.85, True], [0.85, False], [0.95, True], [0.5], [1.0, False],
        [0.8, False]]


@needs_node
def test_status_text(ran):
    assert value(ran, "status_text") == [
        "60 fps | render scale 0.85 | MEDIUM AUTO",
        "render scale 1.00 | HIGH. Glow switched off after a render error"]


@needs_node
def test_defaults_on_init(ran):
    r = value(ran, "init_defaults")
    assert r["ok"] and r["lighting"] and r["ground"] and r["dyn"] and r["atmo"] == 2
    assert r["fog"] and r["fxaa"] and r["glow"]
    assert r["builtInBloom"] is False          # the page's detection glow is left alone
    assert r["sse"] == 2 and r["scale"] == 1.0
    assert r["fades"] == [2.0e6, 6.0e6, 2.0e6, 6.0e6]
    assert r["layers"][:2] == ["blue", "esri"]
    assert "VIIRS_Black_Marble" in r["layers"][2] and len(r["layers"]) == 3
    assert r["night"] == {"day": 0, "night": 1}
    assert r["budgets"] == ["medium"]
    assert r["title"] == "Visual quality" and r["options"] == ["LOW", "MEDIUM", "HIGH"]
    assert r["boxes"] == 8


@needs_node
def test_glow_is_a_two_level_composite(ran):
    r = value(ran, "glow_structure")
    assert r["names"] == ["sdaVisualsGlowGenerate", "sdaVisualsGlowCombine",
                          "sdaVisualsGlowBright", "sdaVisualsGlowBlurX", "sdaVisualsGlowBlurY"]
    assert r["input"] is False and r["genInput"] is True
    assert r["glowTexture"] == "sdaVisualsGlowGenerate"
    assert r["scales"] == [0.5, 0.5, 0.5] and r["dirs"] == [1, 1]
    assert r["threshold"] == pytest.approx(0.78)


def test_shader_text():
    text = MODULE.read_text(encoding="utf-8")
    for needed in ("czm_sunDirectionEC", "czm_inverseProjection", "czm_view", "out_FragColor",
                   "in vec2 v_textureCoordinates", "uniform sampler2D glowTexture"):
        assert needed in text
    for banned in ("#version", "texture2D", "gl_FragColor", "innerHTML"):
        assert banned not in text
    assert text.isascii()


@needs_node
def test_basemap_switch_keeps_lighting(ran):
    r = value(ran, "basemap_changes_reassert")
    assert r == {"daynightLayers": 2, "daynightNight": False, "earthLighting": True,
                 "earthNight": True, "throttled": False, "again": True}


@needs_node
def test_options_turn_off_cleanly(ran):
    r = value(ran, "lighting_off_follows_basemap")
    assert r["earth"] == {"lighting": False, "night": False, "fade": 1e7}
    assert r["ground"] is False and r["fog"] is False
    assert r["stored"]["lighting"] is False and r["stored"]["fog"] is False


@needs_node
def test_presets(ran):
    r = value(ran, "presets")
    assert r["low"] == {"scale": 0.75, "fxaa": False, "glow": False, "sse": 3, "native": True}
    assert r["high"] == {"scale": 1.0, "fxaa": True, "glow": True, "sse": 1.5, "native": False,
                         "perFragment": True}
    assert r["budgets"] == ["medium", "low", "high"]
    assert r["preset"] == "high" and r["select"] == "high"


@needs_node
def test_auto_scale(ran):
    r = value(ran, "auto_adapts_to_fps")
    assert r["down"] == pytest.approx(0.85) and r["down2"] == pytest.approx(0.7)
    assert r["up"] == pytest.approx(1.0)
    assert r["manual"] == pytest.approx(1.0)
    assert r["status"].endswith("| MEDIUM")
    assert value(ran, "hidden_tab_does_not_adapt") == 1.0


@needs_node
def test_render_error_removes_glow(ran):
    r = value(ran, "render_error_drops_glow")
    assert r["after"]["stages"] == 0
    assert r["after"]["state"]["glow"] is False
    assert r["after"]["state"]["note"] == "Glow switched off after a render error"
    assert r["after"]["box"] == 1
    assert r["rebuilt"] is True


@needs_node
def test_without_post_processing(ran):
    r = value(ran, "no_post_processing")
    assert r["ok"] is True and r["state"]["glow"] is False
    assert r["state"]["note"] == "Glow needs Cesium post-processing"


@needs_node
def test_glow_only_in_3d(ran):
    assert value(ran, "glow_off_outside_3d") == [False, True]


@needs_node
def test_disable_restores(ran):
    r = value(ran, "disable_restores_page_defaults")
    assert r["lighting"] is False and r["ground"] is False and r["fog"] is False
    assert r["fxaa"] is False and r["scale"] == 1.0 and r["native"] is True
    assert r["sse"] == 2 and r["glow"] is False and r["layers"] == 2
    assert r["fades"] == [1e7, 2e7]
    assert r["status"].startswith("Off") and r["on"] is False


@needs_node
def test_stored_settings(ran):
    assert value(ran, "stored_settings_load") == {"preset": "low", "auto": False, "glow": False,
                                                  "fog": False, "lighting": True,
                                                  "fogOn": False}


@needs_node
def test_the_sun_and_moon_show_on_the_globe(ran):
    # Both are drawn by default so a click on either can say what it is; the
    # checkbox and switching the module off give the page's hidden state back,
    # and the sky view is left alone while it is open.
    assert value(ran, "sun_and_moon") == {
        "init": [True, True], "off": [False, False], "on": [True, True],
        "skyOpen": [True, True], "afterSky": [False, False],
        "disabled": [False, False], "stored": True}
