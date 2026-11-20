// View mode for the WorldView globe: 3D globe, 2D map or Columbus view (a flat
// map that keeps height), switched from a small control over the globe and
// remembered in localStorage, so the globe opens the way it was left.
//
// Map tools (maptools.js) already has 3D / 2D / CV buttons in its sidebar
// panel. This module does not repeat that morph: when SDAMapTools is running it
// calls SDAMapTools.setSceneMode, so both sets of buttons agree, and it saves the
// mode on every scene.morphComplete, so a switch made from either place, or by
// any other module, is what the next visit restores. Without map tools it calls
// scene.morphTo3D / morphTo2D / morphToColumbusView itself.
//
// Restoring at start-up morphs with duration 0 so the globe does not animate
// on load. The saved value is one of '3d', '2d', 'cv'; anything else stored
// under the key is ignored.
//
// Page wiring: list it in EXTENSIONS with a mount div (the div stays empty; the
// switch floats over the globe, top right). A key handler can call
// SDAViewMode.cycle().

(function (global) {
  'use strict';

  var STORE_KEY = 'sda.viewmode.v1';
  var STYLE_ID = 'sda-viewmode-style';
  var MODES = ['3d', '2d', 'cv'];
  var LABELS = { '3d': '3D', '2d': '2D', cv: 'CV' };
  var TITLES = {
    '3d': '3D globe',
    '2d': '2D map',
    cv: 'Columbus view: a flat map that keeps height'
  };
  var NAMES = { '3d': '3D globe', '2d': '2D map', cv: 'Columbus view' };
  var MORPH_S = 1.2;
  var ALIASES = {
    '3d': '3d', globe: '3d', scene3d: '3d', '3': '3d',
    '2d': '2d', map: '2d', flat: '2d', scene2d: '2d', '2': '2d',
    cv: 'cv', columbus: 'cv', columbusview: 'cv', columbus_view: 'cv', '2.5d': 'cv'
  };

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  // '3d' | '2d' | 'cv' for any spelling the page or a user might use, else null.
  function normalizeMode(value) {
    if (typeof value !== 'string') return null;
    var key = value.trim().toLowerCase().replace(/[\s-]+/g, '');
    return Object.prototype.hasOwnProperty.call(ALIASES, key) ? ALIASES[key] : null;
  }

  // The mode a Cesium scene is in, or null while it is morphing.
  function modeOfScene(sceneMode, SceneMode) {
    var M = SceneMode || {};
    if (sceneMode === M.SCENE3D) return '3d';
    if (sceneMode === M.SCENE2D) return '2d';
    if (sceneMode === M.COLUMBUS_VIEW) return 'cv';
    return null;
  }

  function nextMode(mode) {
    var i = MODES.indexOf(normalizeMode(mode) || '3d');
    return MODES[(i + 1) % MODES.length];
  }

  function memoryStorage() {
    var data = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; }
    };
  }

  // localStorage when it works (private windows and sandboxed frames can throw
  // on access), else a memory store that lasts for the page.
  function pickStorage() {
    try {
      var s = global.localStorage;
      if (s) {
        var probe = STORE_KEY + '.probe';
        s.setItem(probe, '1');
        s.removeItem(probe);
        return s;
      }
    } catch (err) { /* fall through */ }
    return memoryStorage();
  }

  function readSaved(storage) {
    try { return normalizeMode(storage.getItem(STORE_KEY)); } catch (err) { return null; }
  }

  function writeSaved(storage, mode) {
    var m = normalizeMode(mode);
    if (!m) return false;
    try { storage.setItem(STORE_KEY, m); return true; } catch (err) { return false; }
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.vm-float { position: fixed; top: 52px; right: 12px; z-index: 11; display: flex;',
    '  border: 1px solid rgba(255,176,0,0.35); border-radius: 3px; overflow: hidden;',
    '  background: rgba(0,0,0,0.72); font-family: "JetBrains Mono", monospace; }',
    '#info-panel:not(.hidden) ~ .vm-float { right: 302px; }',
    '.vm-btn { background: none; border: 0; border-right: 1px solid rgba(255,176,0,0.2);',
    '  color: var(--hud-dim, #7a5500); font: inherit; font-size: 10px; letter-spacing: 1px;',
    '  padding: 4px 9px; cursor: pointer; }',
    '.vm-btn:last-child { border-right: 0; }',
    '.vm-btn:hover { color: var(--hud-color, #ffb000); }',
    '.vm-btn.vm-on { color: #000; background: var(--hud-color, #ffb000); }',
    '@media (max-width: 760px) { .vm-float { top: auto; bottom: 112px; right: 10px; } }'
  ].join('\n');

  function injectStyle(doc) {
    if (doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    doc.head.appendChild(style);
  }

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var scene = ctx.viewer.scene;
    var storage = ctx.storage || pickStorage();
    var m = { buttons: {}, cleanup: [], mode: null, pending: null };

    function note(text) { try { ctx.addIntelItem(text); } catch (err) { /* page feed only */ } }

    function current() { return modeOfScene(scene.mode, C && C.SceneMode); }

    function mapTools() {
      var mt = global.SDAMapTools;
      try {
        if (mt && typeof mt.setSceneMode === 'function' && typeof mt.instance === 'function' &&
            mt.instance()) return mt;
      } catch (err) { /* not running */ }
      return null;
    }

    function paint(mode) {
      MODES.forEach(function (k) {
        var b = m.buttons[k];
        if (!b) return;
        b.className = 'vm-btn' + (k === mode ? ' vm-on' : '');
        b.setAttribute('aria-pressed', k === mode ? 'true' : 'false');
      });
    }

    function morph(mode, seconds) {
      if (seconds > 0) {
        var mt = mapTools();
        if (mt) { mt.setSceneMode(mode); return; }
      }
      if (mode === '2d' && typeof scene.morphTo2D === 'function') scene.morphTo2D(seconds);
      else if (mode === 'cv' && typeof scene.morphToColumbusView === 'function') {
        scene.morphToColumbusView(seconds);
      } else if (mode === '3d' && typeof scene.morphTo3D === 'function') scene.morphTo3D(seconds);
    }

    // Switch to a mode and save it. Returns the mode, or null for a bad name.
    function setMode(mode, opts) {
      var mm = normalizeMode(mode);
      if (!mm) return null;
      var seconds = opts && typeof opts.duration === 'number' ? opts.duration : MORPH_S;
      writeSaved(storage, mm);
      m.mode = mm;
      m.pending = mm;
      paint(mm);
      if (current() !== mm) morph(mm, seconds);
      if (!(opts && opts.quiet)) note('View: ' + NAMES[mm]);
      return mm;
    }

    function cycle() { return setMode(nextMode(m.mode || current() || '3d')); }

    // Whatever changed the mode (these buttons, map tools, another module),
    // the settled mode is the one to save.
    function onMorphComplete() {
      var mode = current();
      if (!mode) return;
      m.mode = mode;
      m.pending = null;
      writeSaved(storage, mode);
      paint(mode);
    }

    var root = doc.createElement('div');
    root.className = 'vm-float';
    root.setAttribute('role', 'group');
    root.setAttribute('aria-label', 'View mode');
    MODES.forEach(function (k) {
      var b = doc.createElement('button');
      b.setAttribute('type', 'button');
      b.className = 'vm-btn';
      b.textContent = LABELS[k];
      b.setAttribute('title', TITLES[k]);
      b.addEventListener('click', function () { setMode(k); });
      m.buttons[k] = b;
      root.appendChild(b);
    });
    if (doc.body) doc.body.appendChild(root);

    try {
      if (scene.morphComplete && typeof scene.morphComplete.addEventListener === 'function') {
        var off = scene.morphComplete.addEventListener(onMorphComplete);
        m.cleanup.push(function () {
          if (typeof off === 'function') off();
          else if (typeof scene.morphComplete.removeEventListener === 'function') {
            scene.morphComplete.removeEventListener(onMorphComplete);
          }
        });
      }
    } catch (err) { /* no event: the buttons still save */ }

    var saved = readSaved(storage);
    var now = current() || '3d';
    if (saved && saved !== now) setMode(saved, { duration: 0, quiet: true });
    else { m.mode = now; paint(now); }

    function destroy() {
      m.cleanup.splice(0).forEach(function (fn) { try { fn(); } catch (err) { /* gone */ } });
      if (root.parentNode) root.parentNode.removeChild(root);
    }

    return {
      root: root,
      setMode: setMode,
      cycle: cycle,
      mode: function () { return current() || m.pending || m.mode; },
      saved: function () { return readSaved(storage); },
      destroy: destroy
    };
  }

  var instance = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.viewer || !ctx.viewer.scene || !global.Cesium || !global.document) {
        return false;
      }
      if (instance) instance.destroy();
      injectStyle(global.document);
      instance = createModule(ctx);
      return true;
    } catch (err) {
      instance = null;
      return false;
    }
  }

  global.SDAViewMode = {
    init: init,
    setMode: function (mode) { return instance ? instance.setMode(mode) : null; },
    cycle: function () { return instance ? instance.cycle() : null; },
    mode: function () { return instance ? instance.mode() : null; },
    saved: function () { return instance ? instance.saved() : null; },
    destroy: function () { if (instance) instance.destroy(); instance = null; },
    STORE_KEY: STORE_KEY,
    MODES: MODES.slice(),
    normalizeMode: normalizeMode,
    modeOfScene: modeOfScene,
    nextMode: nextMode,
    readSaved: readSaved,
    writeSaved: writeSaved,
    memoryStorage: memoryStorage
  };
})(typeof window !== 'undefined' ? window : this);
