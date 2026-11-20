// Visual quality for the Vantage globe: sun lighting from the simulation
// clock, city lights on the night side with a soft glow, ground atmosphere,
// fog, FXAA, and a LOW / MEDIUM / HIGH preset whose render scale follows the
// measured frame rate.
//
// The sun: Cesium places it from the clock it renders with, and on this page
// that is the simulation clock (simNow), so scrubbing or speeding time sweeps
// the terminator; nothing here computes a sun position of its own. Lighting
// is re-asserted after every basemap change, because setBasemap() writes
// globe.enableLighting from its registry entry.
//
// The glow is a post-process composite of our own (bright pass, two blur
// passes at half resolution, a screen blend), modelled on the shape of
// Cesium's own bloom stage (PostProcessStageLibrary.createBloomStage, Apache
// 2.0). Cesium's built-in bloom stays with the page's detection-glow control.
// The bright pass keeps warm, bright pixels on the night side of the globe
// only: each pixel's view ray is intersected with a sphere and the surface
// normal compared with the sun direction, so desert sand by day and amber
// labels do not glow. A render error removes the stages and says so.
//
// Shaders keep to constructs that compile under GLSL ES 1.00 and 3.00, as the
// page's styles.js does. Text is only ever set with textContent.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Visual quality';
  var STYLE_ID = 'sda-visuals-style';
  var STORE_KEY = 'sda.visuals.v1';
  var FPS_WINDOW_MS = 2000;
  var BASEMAP_CHECK_MS = 1000;
  var MIN_SCALE = 0.5;
  var STEP_DOWN = 0.15;
  var STEP_UP = 0.1;
  var LOW_FPS = 28;
  var HIGH_FPS = 50;
  var LOW_WINDOWS = 2;
  var HIGH_WINDOWS = 3;

  // scale: resolutionScale ceiling; native: draw at device pixels (retina).
  var PRESETS = {
    low: { label: 'LOW', scale: 0.75, native: false, fxaa: false, glow: false, sse: 3,
      perFragment: false, models: 'low' },
    medium: { label: 'MEDIUM', scale: 1.0, native: false, fxaa: true, glow: true, sse: 2,
      perFragment: false, models: 'medium' },
    high: { label: 'HIGH', scale: 1.0, native: true, fxaa: true, glow: true, sse: 1.5,
      perFragment: true, models: 'high' }
  };

  var DEFAULT_OPTS = { lighting: true, cityLights: true, atmosphere: true, fog: true,
    fxaa: true, glow: true, sunMoon: true };

  var OPTION_LABELS = [
    ['lighting', 'Sun lighting from the sim clock',
      'Day and night follow the simulation time; close in, the ground stays lit'],
    ['cityLights', 'City lights on the night side', 'NASA Black Marble (GIBS) under the dark side'],
    ['atmosphere', 'Atmosphere', 'Ground atmosphere lit from the sun'],
    ['fog', 'Fog', 'Haze toward the horizon; also skips far tiles'],
    ['fxaa', 'Anti-aliasing (FXAA)', 'Smooths edges of orbits, lines and models'],
    ['glow', 'City-light glow', 'Soft bloom on bright city lights at night'],
    ['sunMoon', 'Sun and Moon', 'Drawn where they are at the sim time; click either to see what it is']
  ];

  // Lighting fades out below these camera heights so a close view stays lit.
  var FADES = { lightingOut: 2.0e6, lightingIn: 6.0e6, nightOut: 2.0e6, nightIn: 6.0e6 };

  var BRIGHT_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform float threshold;',
    'uniform float nightOnly;',
    'in vec2 v_textureCoordinates;',
    'float sdaNightSide(vec2 uv) {',
    '  vec4 p = czm_inverseProjection * vec4(uv * 2.0 - 1.0, 1.0, 1.0);',
    '  vec3 dir = normalize(p.xyz / p.w);',
    '  vec3 c = (czm_view * vec4(0.0, 0.0, 0.0, 1.0)).xyz;',
    '  float b = dot(dir, c);',
    '  float disc = b * b - dot(c, c) + 6371000.0 * 6371000.0;',
    '  if (disc < 0.0) return 0.0;',
    '  float t = b - sqrt(disc);',
    '  if (t < 0.0) return 0.0;',
    '  vec3 n = normalize(dir * t - c);',
    '  return smoothstep(0.02, -0.12, dot(n, czm_sunDirectionEC));',
    '}',
    'void main() {',
    '  vec3 c = texture(colorTexture, v_textureCoordinates).rgb;',
    '  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));',
    '  float warm = smoothstep(0.05, 0.25, c.r - c.b);',
    '  float k = smoothstep(threshold, threshold + 0.22, l) * warm;',
    '  k *= mix(1.0, sdaNightSide(v_textureCoordinates), nightOnly);',
    '  out_FragColor = vec4(c * k, 1.0);',
    '}'
  ].join('\n');

  var BLUR_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform vec2 colorTextureDimensions;',
    'uniform vec2 direction;',
    'uniform float spread;',
    'in vec2 v_textureCoordinates;',
    'void main() {',
    '  vec2 st = direction * spread / colorTextureDimensions;',
    '  vec2 uv = v_textureCoordinates;',
    '  vec3 c = texture(colorTexture, uv).rgb * 0.227027;',
    '  c += texture(colorTexture, uv + st * 1.384615).rgb * 0.316216;',
    '  c += texture(colorTexture, uv - st * 1.384615).rgb * 0.316216;',
    '  c += texture(colorTexture, uv + st * 3.230769).rgb * 0.070270;',
    '  c += texture(colorTexture, uv - st * 3.230769).rgb * 0.070270;',
    '  out_FragColor = vec4(c, 1.0);',
    '}'
  ].join('\n');

  var COMBINE_SHADER = [
    'uniform sampler2D colorTexture;',
    'uniform sampler2D glowTexture;',
    'uniform float strength;',
    'in vec2 v_textureCoordinates;',
    'void main() {',
    '  vec4 c = texture(colorTexture, v_textureCoordinates);',
    '  vec3 g = clamp(texture(glowTexture, v_textureCoordinates).rgb * strength, 0.0, 1.0);',
    '  out_FragColor = vec4(1.0 - (1.0 - c.rgb) * (1.0 - g), c.a);',
    '}'
  ].join('\n');

  var clockFn = function () { return Date.now(); };

  // ---------------------------------------------------------------------
  // Pure helpers
  // ---------------------------------------------------------------------

  function presetFor(name) { return PRESETS[name] || PRESETS.medium; }

  // One frame-rate window in, the next render scale out. Two slow windows in
  // a row step down; three fast ones step back up toward the preset ceiling.
  function adaptScale(state, fps, ceiling) {
    var s = { scale: state.scale, low: state.low || 0, high: state.high || 0, changed: false };
    if (!(fps > 0)) return s;
    if (fps < LOW_FPS) {
      s.low += 1;
      s.high = 0;
      if (s.low >= LOW_WINDOWS && s.scale > MIN_SCALE) {
        s.scale = Math.max(MIN_SCALE, Math.round((s.scale - STEP_DOWN) * 100) / 100);
        s.low = 0;
        s.changed = true;
      }
    } else if (fps > HIGH_FPS) {
      s.high += 1;
      s.low = 0;
      if (s.high >= HIGH_WINDOWS && s.scale < ceiling) {
        s.scale = Math.min(ceiling, Math.round((s.scale + STEP_UP) * 100) / 100);
        s.high = 0;
        s.changed = true;
      }
    } else {
      s.low = 0;
      s.high = 0;
    }
    return s;
  }

  function statusText(st) {
    var parts = [];
    if (st.fps > 0) parts.push(Math.round(st.fps) + ' fps');
    parts.push('render scale ' + st.scale.toFixed(2));
    parts.push(presetFor(st.preset).label + (st.auto ? ' AUTO' : ''));
    var s = parts.join(' | ');
    if (st.note) s += '. ' + st.note;
    return s;
  }

  function loadStored() {
    try {
      var raw = global.localStorage && global.localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      var v = JSON.parse(raw);
      return v && typeof v === 'object' ? v : null;
    } catch (e) {
      return null;
    }
  }

  function saveStored(v) {
    try {
      if (global.localStorage) global.localStorage.setItem(STORE_KEY, JSON.stringify(v));
    } catch (e) { /* private mode: settings last for the session */ }
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var CSS = [
    '.sdavis-row { display: flex; align-items: center; gap: 6px; margin: 4px 0; font-size: 10px; }',
    '.sdavis-row label { display: flex; align-items: center; gap: 6px; cursor: pointer; }',
    '.sdavis-row select { background: rgba(0,0,0,0.6); color: inherit; border: 1px solid rgba(255,176,0,0.3); font: inherit; font-size: 10px; }',
    '.sdavis-status { font-size: 9px; color: var(--hud-dim, #7a5500); margin-top: 6px; min-height: 1.2em; }'
  ].join('\n');

  function el(doc, tag, className, text) {
    var n = doc.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var globe = scene.globe;
    var stored = loadStored() || {};
    var m = {
      enabled: true,
      preset: PRESETS[stored.preset] ? stored.preset : 'medium',
      auto: stored.auto !== false,
      opts: Object.assign({}, DEFAULT_OPTS, stored.opts || {}),
      scale: 1, low: 0, high: 0, fps: 0, frames: 0, winStart: 0,
      glow: null, glowBroken: false, night: null, lastBasemap: null, lastBasemapCheck: 0,
      note: ''
    };

    var pp = scene.postProcessStages;
    var original = {
      lighting: globe.enableLighting,
      groundAtmosphere: globe.showGroundAtmosphere,
      dynAtmo: globe.dynamicAtmosphereLighting,
      dynAtmoSun: globe.dynamicAtmosphereLightingFromSun,
      fog: scene.fog ? scene.fog.enabled : undefined,
      fxaa: pp && pp.fxaa ? pp.fxaa.enabled : undefined,
      resolutionScale: viewer.resolutionScale,
      browserRes: viewer.useBrowserRecommendedResolution,
      sse: globe.maximumScreenSpaceError,
      perFragment: scene.skyAtmosphere ? scene.skyAtmosphere.perFragmentAtmosphere : undefined,
      fades: [globe.lightingFadeOutDistance, globe.lightingFadeInDistance,
        globe.nightFadeOutDistance, globe.nightFadeInDistance],
      sun: scene.sun ? scene.sun.show : undefined,
      moon: scene.moon ? scene.moon.show : undefined
    };

    function basemapLit() {
      var maps = global.BASEMAPS;
      var cur = global.currentBasemap;
      if (maps && cur && maps[cur]) return !!maps[cur].lighting;
      return !!original.lighting;
    }

    // ---- lighting, atmosphere, night lights -----------------------------

    function blackMarbleProvider() {
      if (typeof global.blackMarble === 'function') {
        try { return global.blackMarble(); } catch (e) { /* build our own below */ }
      }
      return new C.UrlTemplateImageryProvider({
        url: 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_Black_Marble/default/' +
          '2016-01-01/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png',
        maximumLevel: 8,
        credit: new C.Credit('NASA Black Marble, GIBS')
      });
    }

    function syncNightLayer() {
      var layers = viewer.imageryLayers;
      if (!layers || typeof C.ImageryLayer !== 'function') return;
      // The DAY / NIGHT basemap carries its own Black Marble layer.
      var want = m.enabled && m.opts.lighting && m.opts.cityLights &&
        global.currentBasemap !== 'daynight';
      if (want && !m.night) {
        try {
          var layer = new C.ImageryLayer(blackMarbleProvider());
          layer.dayAlpha = 0.0;
          layer.nightAlpha = 1.0;
          layer.brightness = 2.0;
          var at = Array.isArray(global.basemapLayers) ? global.basemapLayers.length : 1;
          layers.add(layer, Math.min(at, layers.length));
          m.night = layer;
        } catch (e) { m.night = null; }
      } else if (!want && m.night) {
        try { layers.remove(m.night, true); } catch (e) { /* already gone */ }
        m.night = null;
      }
    }

    function applyLighting() {
      var on = m.enabled && m.opts.lighting;
      globe.enableLighting = on ? true : basemapLit();
      var atmo = m.enabled && m.opts.atmosphere;
      globe.showGroundAtmosphere = atmo ? true : original.groundAtmosphere;
      globe.dynamicAtmosphereLighting = (on || atmo) ? true : original.dynAtmo;
      globe.dynamicAtmosphereLightingFromSun = (on || atmo) ? true : original.dynAtmoSun;
      if (scene.atmosphere && C.DynamicAtmosphereLightingType) {
        scene.atmosphere.dynamicLighting = (on || atmo)
          ? C.DynamicAtmosphereLightingType.SUNLIGHT : C.DynamicAtmosphereLightingType.NONE;
      }
      if (on) {
        globe.lightingFadeOutDistance = FADES.lightingOut;
        globe.lightingFadeInDistance = FADES.lightingIn;
        globe.nightFadeOutDistance = FADES.nightOut;
        globe.nightFadeInDistance = FADES.nightIn;
      } else {
        globe.lightingFadeOutDistance = original.fades[0];
        globe.lightingFadeInDistance = original.fades[1];
        globe.nightFadeOutDistance = original.fades[2];
        globe.nightFadeInDistance = original.fades[3];
      }
      syncNightLayer();
      m.lastBasemap = global.currentBasemap;
    }

    // ---- the Sun and the Moon ---------------------------------------------
    // Cesium draws both where they are at the clock's time, so they agree with
    // the lit side of the globe. The sky view shows them whatever this says and
    // puts back what it found when it closes; the per-frame check below brings
    // them back to this setting a moment later.

    function skyOpen() {
      var sky = global.SDASkyView;
      return !!(sky && typeof sky.isActive === 'function' && sky.isActive());
    }

    // Off, or with the module off, each goes back to what the page set.
    function wantShown(pageValue) {
      return m.enabled && m.opts.sunMoon ? true : pageValue !== false;
    }

    function applySunMoon() {
      if (skyOpen()) return;
      if (scene.sun) scene.sun.show = wantShown(original.sun);
      if (scene.moon) scene.moon.show = wantShown(original.moon);
    }

    // ---- glow ------------------------------------------------------------

    function buildGlow() {
      if (m.glow || m.glowBroken || !pp || typeof pp.add !== 'function') return m.glow;
      if (typeof C.PostProcessStage !== 'function' ||
          typeof C.PostProcessStageComposite !== 'function') {
        m.glowBroken = true;
        m.note = 'Glow needs Cesium post-processing';
        return null;
      }
      try {
        var bright = new C.PostProcessStage({ name: 'sdaVisualsGlowBright',
          fragmentShader: BRIGHT_SHADER, uniforms: { threshold: 0.78, nightOnly: 1.0 },
          textureScale: 0.5 });
        var blurX = new C.PostProcessStage({ name: 'sdaVisualsGlowBlurX',
          fragmentShader: BLUR_SHADER, uniforms: { direction: new C.Cartesian2(1, 0), spread: 1.6 },
          textureScale: 0.5 });
        var blurY = new C.PostProcessStage({ name: 'sdaVisualsGlowBlurY',
          fragmentShader: BLUR_SHADER, uniforms: { direction: new C.Cartesian2(0, 1), spread: 1.6 },
          textureScale: 0.5 });
        var gen = new C.PostProcessStageComposite({ name: 'sdaVisualsGlowGenerate',
          stages: [bright, blurX, blurY] });
        var combine = new C.PostProcessStage({ name: 'sdaVisualsGlowCombine',
          fragmentShader: COMBINE_SHADER, uniforms: { glowTexture: gen.name, strength: 0.9 } });
        var glow = new C.PostProcessStageComposite({ name: 'sdaVisualsGlow',
          stages: [gen, combine], inputPreviousStageTexture: false });
        glow.enabled = false;
        pp.add(glow);
        m.glow = glow;
        m.glowParts = { bright: bright, combine: combine };
      } catch (err) {
        m.glow = null;
        m.glowBroken = true;
        m.note = 'Glow unavailable: ' + ((err && err.message) || 'stage rejected');
      }
      return m.glow;
    }

    function dropGlow(reason) {
      if (m.glow) {
        try { m.glow.enabled = false; } catch (e) { /* destroyed */ }
        try { pp.remove(m.glow); } catch (e) { /* already gone */ }
      }
      m.glow = null;
      m.glowBroken = true;
      m.note = reason;
      if (m.glowBox) m.glowBox.checked = false;
      setStatus();
    }

    function applyGlow() {
      var want = m.enabled && m.opts.glow && !m.glowBroken;
      if (want) buildGlow();
      if (!m.glow) return;
      var in3d = !C.SceneMode || scene.mode === undefined || scene.mode === C.SceneMode.SCENE3D;
      m.glow.enabled = want && in3d;
    }

    // ---- render scale, fxaa, fog, preset ----------------------------------

    function applyScale() {
      var p = presetFor(m.preset);
      viewer.useBrowserRecommendedResolution = (m.enabled && p.native) ? false
        : (original.browserRes === undefined ? true : original.browserRes);
      viewer.resolutionScale = m.enabled ? m.scale : original.resolutionScale;
    }

    function applyAll() {
      var p = presetFor(m.preset);
      applyLighting();
      if (scene.fog) scene.fog.enabled = m.enabled ? !!m.opts.fog : original.fog;
      if (pp && pp.fxaa) pp.fxaa.enabled = m.enabled ? !!m.opts.fxaa : original.fxaa;
      globe.maximumScreenSpaceError = m.enabled ? p.sse : original.sse;
      if (scene.skyAtmosphere && original.perFragment !== undefined) {
        scene.skyAtmosphere.perFragmentAtmosphere = m.enabled ? p.perFragment : original.perFragment;
      }
      applyScale();
      applyGlow();
      applySunMoon();
      if (m.enabled && global.SDAModels && typeof global.SDAModels.setBudget === 'function') {
        try { global.SDAModels.setBudget(p.models); } catch (e) { /* models module absent */ }
      }
      if (scene.requestRender) scene.requestRender();
      syncControls();
      setStatus();
    }

    function persist() {
      saveStored({ preset: m.preset, auto: m.auto, opts: m.opts });
    }

    function setPreset(name) {
      if (!PRESETS[name]) return;
      var p = PRESETS[name];
      m.preset = name;
      m.scale = p.scale;
      m.low = 0;
      m.high = 0;
      m.opts.fxaa = p.fxaa;
      m.opts.glow = p.glow;
      applyAll();
      persist();
    }

    function setOption(name, on) {
      if (!(name in DEFAULT_OPTS)) return;
      m.opts[name] = !!on;
      if (name === 'glow' && on) { m.glowBroken = false; m.note = ''; }
      applyAll();
      persist();
    }

    function setAuto(on) {
      m.auto = !!on;
      if (!m.auto) { m.scale = presetFor(m.preset).scale; applyScale(); }
      syncControls();
      setStatus();
      persist();
    }

    function setEnabled(on) {
      m.enabled = !!on;
      applyAll();
    }

    // ---- per-frame -------------------------------------------------------

    function onPostRender() {
      var t = clockFn();
      if (!m.winStart) { m.winStart = t; m.frames = 0; return; }
      m.frames += 1;
      var dt = t - m.winStart;
      if (dt < FPS_WINDOW_MS) return;
      m.fps = m.frames * 1000 / dt;
      m.frames = 0;
      m.winStart = t;
      if (m.enabled && m.auto && !(doc && doc.hidden)) {
        var s = adaptScale(m, m.fps, presetFor(m.preset).scale);
        m.scale = s.scale;
        m.low = s.low;
        m.high = s.high;
        if (s.changed) applyScale();
      }
      setStatus();
    }

    function onPreUpdate() {
      var t = clockFn();
      if (t - m.lastBasemapCheck < BASEMAP_CHECK_MS) return;
      m.lastBasemapCheck = t;
      if (global.currentBasemap !== m.lastBasemap) applyLighting();
      else if (m.enabled && m.opts.lighting && !globe.enableLighting) applyLighting();
      if (m.glow) applyGlow();
      if (scene.sun && scene.sun.show !== wantShown(original.sun)) applySunMoon();
    }

    // ---- panel ------------------------------------------------------------

    function setStatus() {
      if (!m.statusEl) return;
      m.statusEl.textContent = m.enabled ? statusText(m) : 'Off; the globe uses the page defaults.';
    }

    function syncControls() {
      if (m.presetSel) m.presetSel.value = m.preset;
      if (m.autoBox) m.autoBox.checked = m.auto;
      Object.keys(m.boxes || {}).forEach(function (k) { m.boxes[k].checked = !!m.opts[k]; });
    }

    function buildPanel() {
      var root = el(doc, 'div', 'panel sdavisuals');
      var h = el(doc, 'h3', '', PANEL_TITLE);
      root.appendChild(h);
      var body = el(doc, 'div', 'panel-body');
      root.appendChild(body);

      var row = el(doc, 'div', 'sdavis-row');
      row.appendChild(el(doc, 'span', '', 'Preset'));
      var sel = el(doc, 'select', '');
      Object.keys(PRESETS).forEach(function (k) {
        var o = el(doc, 'option', '', PRESETS[k].label);
        o.value = k;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { setPreset(sel.value); });
      m.presetSel = sel;
      row.appendChild(sel);
      body.appendChild(row);

      function check(label, value, onChange, title) {
        var r = el(doc, 'div', 'sdavis-row');
        var lab = el(doc, 'label', '');
        var box = el(doc, 'input', '');
        box.type = 'checkbox';
        box.checked = value;
        box.addEventListener('change', function () { onChange(!!box.checked); });
        lab.appendChild(box);
        lab.appendChild(doc.createTextNode(label));
        if (title) lab.title = title;
        r.appendChild(lab);
        body.appendChild(r);
        return box;
      }
      m.autoBox = check('Adapt render scale to frame rate', m.auto, setAuto,
        'Drops the render scale when the frame rate falls under ' + LOW_FPS +
        ' fps and raises it again above ' + HIGH_FPS);
      m.boxes = {};
      OPTION_LABELS.forEach(function (o) {
        m.boxes[o[0]] = check(o[1], !!m.opts[o[0]], function (on) { setOption(o[0], on); }, o[2]);
      });
      m.glowBox = m.boxes.glow;
      m.statusEl = el(doc, 'div', 'sdavis-status', '');
      body.appendChild(m.statusEl);
      h.addEventListener('click', function () { root.classList.toggle('collapsed'); });
      return root;
    }

    if (doc && !doc.getElementById(STYLE_ID)) {
      var s = doc.createElement('style');
      s.id = STYLE_ID;
      s.textContent = CSS;
      (doc.head || doc.body).appendChild(s);
    }
    m.root = buildPanel();
    m.scale = presetFor(m.preset).scale;
    if (scene.postRender && scene.postRender.addEventListener) {
      scene.postRender.addEventListener(onPostRender);
    }
    if (scene.preUpdate && scene.preUpdate.addEventListener) {
      scene.preUpdate.addEventListener(onPreUpdate);
    }
    if (scene.renderError && scene.renderError.addEventListener) {
      scene.renderError.addEventListener(function () {
        if (m.glow) dropGlow('Glow switched off after a render error');
      });
    }
    applyAll();

    m.setPreset = setPreset;
    m.setOption = setOption;
    m.setAuto = setAuto;
    m.setEnabled = setEnabled;
    m.onPostRender = onPostRender;
    m.onPreUpdate = onPreUpdate;
    m.state = function () {
      return { enabled: m.enabled, preset: m.preset, auto: m.auto, scale: m.scale, fps: m.fps,
        opts: Object.assign({}, m.opts), glow: !!(m.glow && m.glow.enabled),
        nightLayer: !!m.night, note: m.note };
    };
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !ctx.viewer.scene || !global.Cesium) return false;
      current = createModule(ctx);
      ctx.mount.appendChild(current.root);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAVisuals = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    setPreset: function (name) { if (current) current.setPreset(name); },
    setOption: function (name, on) { if (current) current.setOption(name, on); },
    setAuto: function (on) { if (current) current.setAuto(on); },
    state: function () { return current ? current.state() : null; },
    _postRender: function () { if (current) current.onPostRender(); },
    _preUpdate: function () { if (current) current.onPreUpdate(); },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    PRESETS: PRESETS,
    DEFAULT_OPTS: DEFAULT_OPTS,
    FADES: FADES,
    SHADERS: { bright: BRIGHT_SHADER, blur: BLUR_SHADER, combine: COMBINE_SHADER },
    adaptScale: adaptScale,
    statusText: statusText
  };
})(typeof window !== 'undefined' ? window : this);
