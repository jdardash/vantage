// Presentation for the Vantage globe: side panels that fade out when the
// pointer and keyboard go quiet, a bottom-sheet layout on narrow screens, a
// loading screen that waits for the first imagery instead of a fixed delay,
// and a "share view" PNG with a legend and an attribution line.
//
// Body classes (CSS lives here, so worldview.html needs no edit for them):
//   sda-idle     panels faded after the idle delay (wide screens only)
//   sda-touring  set by static/js/tour.js; panels faded the same way
//   sda-narrow   viewport at most 700 px wide: #controls becomes a sheet
//   sda-sheet-open  the sheet is pulled up
//
// whenSettled(maxMs) works before init too: the page can swap
// `setTimeout(hideLoading, 600)` for `SDAPresent.whenSettled(6000).then(hideLoading)`.
// The PNG is copied inside scene.postRender because the WebGL buffer is not
// preserved between frames (the same approach as maptools.js screenshot()).
// Text is only ever set with textContent.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Presentation';
  var STYLE_ID = 'sda-present-style';
  var STORE_KEY = 'sda.present.v1';
  var NARROW_QUERY = '(max-width: 700px)';
  var DEFAULTS = { idleHide: true, idleS: 10 };
  var IDLE_CHOICES = [5, 10, 20, 60];
  var ACTIVITY = ['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchstart'];
  // Elements whose hover or focus keeps the panels up.
  var PANEL_IDS = ['controls', 'info-panel', 'intel-feed', 'time-control', 'timeline-strip',
    'search-box', 'sda-ride'];
  var timing = { checkMs: 1000, pollMs: 250, minSettleMs: 400, skipAfterMs: 3000,
    noViewerMs: 600 };
  var clockFn = function () { return Date.now(); };

  // What the share card lists for each visible layer: label and data source.
  // Layers that only re-draw other data (orbits, trails, covariance) carry no source.
  var LAYERS = {
    satellites: ['Satellites', 'CelesTrak'],
    orbits: ['Orbit paths', ''],
    flights: ['Aircraft', 'OpenSky Network'],
    military: ['Military aircraft', 'adsb.lol'],
    quakes: ['Earthquakes', 'USGS'],
    traffic: ['Roads', 'OpenStreetMap contributors'],
    cctv: ['Public cameras', 'camera operators'],
    aurora: ['Aurora', 'NOAA SWPC'],
    infra: ['Infrastructure', 'OpenStreetMap contributors'],
    aviation: ['Aviation alerts', 'adsb.lol'],
    intel: ['Country intel', ''],
    maritime: ['Shipping', 'aisstream.io'],
    peaks: ['Peaks', 'Kirmse and de Ferranti, GeoNames'],
    weather: ['Weather', 'Open-Meteo, NOAA'],
    eonet: ['Natural events', 'NASA EONET'],
    conjunctions: ['Conjunctions', ''],
    covariance: ['Covariance', ''],
    sensors: ['Sensors', ''],
    trails: ['Trails', '']
  };

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  // ---------------------------------------------------------------------
  // Pure helpers
  // ---------------------------------------------------------------------

  function layerLegend(keys) {
    var out = [];
    (Array.isArray(keys) ? keys : []).forEach(function (k) {
      var row = LAYERS[k];
      if (row) out.push({ key: k, label: row[0], source: row[1] });
    });
    return out;
  }

  // Non-empty parts, in order, as one line; a part already contained in an
  // earlier one (the Cesium credit repeating the basemap credit) is dropped.
  function attributionText(parts) {
    var out = [];
    (parts || []).forEach(function (p) {
      var t = String(p || '').replace(/\s+/g, ' ').trim();
      if (!t) return;
      var low = t.toLowerCase();
      for (var i = 0; i < out.length; i++) {
        if (out[i].toLowerCase().indexOf(low) >= 0) return;
      }
      out.push(t);
    });
    return out.join('. ');
  }

  // Greedy word wrap with a width function. When the text needs more than
  // maxLines, the last line is cut and ends in '...'.
  function wrapText(measure, text, maxW, maxLines) {
    var words = String(text || '').split(/\s+/).filter(Boolean);
    var lines = [];
    var line = '';
    var i = 0;
    for (; i < words.length; i++) {
      var next = line ? line + ' ' + words[i] : words[i];
      if (!line || measure(next) <= maxW) { line = next; continue; }
      lines.push(line);
      line = words[i];
      if (lines.length === maxLines) break;
    }
    var cut = i < words.length;
    if (!cut && line) lines.push(line);
    var last = lines.length - 1;
    if (last >= 0 && (cut || measure(lines[last]) > maxW)) {
      var t = lines[last];
      while (t.length > 1 && measure(t + '...') > maxW) t = t.slice(0, -1);
      lines[last] = t + '...';
    }
    return lines;
  }

  function formatCamera(latDeg, lonDeg, heightM) {
    if (!finite(latDeg) || !finite(lonDeg)) return '';
    var t = Math.abs(latDeg).toFixed(2) + (latDeg >= 0 ? 'N ' : 'S ') +
      Math.abs(lonDeg).toFixed(2) + (lonDeg >= 0 ? 'E' : 'W');
    if (finite(heightM)) {
      t += ', alt ' + (heightM >= 10000 ? Math.round(heightM / 1000) + ' km'
        : Math.round(heightM) + ' m');
    }
    return t;
  }

  function utcStamp(d) {
    if (!(d instanceof Date) || !isFinite(d.getTime())) return '';
    return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
  }

  function fileStamp(d) {
    return d.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  }

  // ---------------------------------------------------------------------
  // CSS
  // ---------------------------------------------------------------------

  var FADE = ['#controls', '#intel-feed', '#timeline-strip', '#time-control'];
  var DIM = ['#header', '#status-bar'];
  function sel(prefix, list) { return list.map(function (s) { return prefix + ' ' + s; }).join(', '); }

  var CSS = [
    FADE.concat(DIM).join(', ') + ' { transition: opacity 0.6s ease, transform 0.3s ease; }',
    sel('body.sda-idle', FADE) + ', ' + sel('body.sda-touring', FADE.concat(['#info-panel'])) +
      ' { opacity: 0 !important; pointer-events: none !important; }',
    sel('body.sda-idle', DIM) + ', ' + sel('body.sda-touring', DIM) + ' { opacity: 0.35; }',
    '.sda-sheet-handle { display: none; }',
    'body.sda-narrow #controls { top: auto; bottom: 64px; left: 0; right: 0; width: auto;',
    '  max-height: 46vh; padding: 0 10px 10px; font-size: 11px; z-index: 30;',
    '  background: rgba(0,0,0,0.9); border-top: 1px solid rgba(255,176,0,0.3);',
    '  border-radius: 10px 10px 0 0; transform: translateY(calc(100% - 40px)); }',
    'body.sda-narrow.sda-sheet-open #controls { transform: none; }',
    'body.sda-narrow #controls .sda-sheet-handle { display: block; position: sticky; top: 0;',
    '  z-index: 1; width: 100%; height: 40px; margin: 0 0 6px; border: 0;',
    '  background: rgba(0,0,0,0.95); color: var(--hud-color, #ffb000);',
    '  font: 600 10px "JetBrains Mono", monospace; letter-spacing: 2px; cursor: pointer; }',
    'body.sda-narrow #controls .sda-sheet-handle::before { content: ""; display: block;',
    '  width: 36px; height: 3px; margin: 0 auto 6px; border-radius: 2px;',
    '  background: rgba(255,176,0,0.5); }',
    'body.sda-narrow #info-panel { top: 46px; left: 8px; right: 8px; width: auto;',
    '  max-height: 40vh; overflow-y: auto; }',
    'body.sda-narrow #info-panel.hidden { transform: translateY(-120%); }',
    'body.sda-narrow #intel-feed, body.sda-narrow .header-right,',
    '  body.sda-narrow .classification { display: none; }',
    'body.sda-narrow #header { padding: 0 10px; }',
    'body.sda-narrow #time-control { padding: 0 8px; overflow-x: auto; }',
    'body.sda-narrow #timeline-strip { left: 8px; right: 8px; }',
    '#sda-loading-detail { margin-top: 8px; font-size: 9px; letter-spacing: 2px; color: #7a5500;',
    '  min-height: 12px; font-family: "JetBrains Mono", monospace; }',
    '#sda-loading-tiles { width: 200px; height: 1px; margin-top: 6px; background: rgba(255,176,0,0.1); }',
    '#sda-loading-tiles > div { height: 100%; width: 0%; background: #7a5500; transition: width 0.25s; }',
    '#sda-loading-skip { margin-top: 14px; background: none; color: #7a5500; cursor: pointer;',
    '  border: 1px solid rgba(255,176,0,0.3); font: 9px "JetBrains Mono", monospace;',
    '  letter-spacing: 2px; padding: 4px 10px; visibility: hidden; }',
    '#sda-loading-skip.on { visibility: visible; }',
    '.sdapresent-row { display: flex; gap: 6px; align-items: center; margin: 4px 0; }',
    '.sdapresent-row select, .sdapresent-row button { background: rgba(0,0,0,0.6); color: inherit;',
    '  border: 1px solid rgba(255,176,0,0.35); font: inherit; font-size: 10px; padding: 2px 6px; }',
    '.sdapresent-row button { cursor: pointer; }',
    '.sdapresent-status { font-size: 9px; color: var(--hud-dim, #7a5500); margin-top: 6px; }'
  ].join('\n');

  function el(doc, tag, className, text) {
    var n = doc.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function ensureStyle(doc) {
    if (doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    (doc.head || doc.body).appendChild(s);
  }

  function loadSettings() {
    var out = { idleHide: DEFAULTS.idleHide, idleS: DEFAULTS.idleS };
    try {
      var raw = global.localStorage && global.localStorage.getItem(STORE_KEY);
      var j = raw ? JSON.parse(raw) : null;
      if (j && typeof j.idleHide === 'boolean') out.idleHide = j.idleHide;
      if (j && IDLE_CHOICES.indexOf(j.idleS) >= 0) out.idleS = j.idleS;
    } catch (e) { /* private mode or bad JSON: defaults */ }
    return out;
  }

  function saveSettings(s) {
    try {
      if (global.localStorage) global.localStorage.setItem(STORE_KEY, JSON.stringify(s));
    } catch (e) { /* storage full or blocked */ }
  }

  // ---------------------------------------------------------------------
  // Loading: wait for the first imagery, with a tile line and a skip button
  // ---------------------------------------------------------------------

  var settleWaits = [];

  function whenSettled(maxMs, viewerArg) {
    var doc = global.document;
    var viewer = viewerArg || global.viewer || null;
    var limit = finite(maxMs) && maxMs > 0 ? maxMs : 6000;
    return new Promise(function (resolve) {
      var done = false;
      var started = clockFn();
      var peak = 0;
      var poll = null;
      var cap = null;
      var removeProgress = null;
      var overlay = doc && doc.getElementById('loading-overlay');
      var detail = null;
      var fill = null;
      var skip = null;

      function finish(why) {
        if (done) return;
        done = true;
        if (poll) global.clearInterval(poll);
        if (cap) global.clearTimeout(cap);
        if (typeof removeProgress === 'function') removeProgress();
        var at = settleWaits.indexOf(finish);
        if (at >= 0) settleWaits.splice(at, 1);
        if (detail) detail.textContent = why === 'tiles' ? 'IMAGERY READY' : '';
        resolve(why);
      }
      settleWaits.push(finish);

      var globe = viewer && viewer.scene && viewer.scene.globe;
      if (!globe) {
        cap = global.setTimeout(function () { finish('no viewer'); }, timing.noViewerMs);
        return;
      }
      if (overlay && doc) {
        ensureStyle(doc);
        detail = doc.getElementById('sda-loading-detail');
        if (detail) {
          var oldBar = doc.getElementById('sda-loading-tiles');
          fill = oldBar && oldBar.children ? oldBar.children[0] : null;
          skip = doc.getElementById('sda-loading-skip');
        } else {
          detail = el(doc, 'div', '', 'LOADING IMAGERY');
          detail.id = 'sda-loading-detail';
          var bar = el(doc, 'div', '');
          bar.id = 'sda-loading-tiles';
          fill = el(doc, 'div', '');
          bar.appendChild(fill);
          skip = el(doc, 'button', '', 'CONTINUE');
          skip.id = 'sda-loading-skip';
          skip.type = 'button';
          // Settles every pending wait, so a second whenSettled() shares it.
          skip.addEventListener('click', function () {
            settleWaits.slice().forEach(function (f) { f('skipped'); });
          });
          overlay.appendChild(detail);
          overlay.appendChild(bar);
          overlay.appendChild(skip);
        }
        if (skip) skip.classList.remove('on');
      }

      function show(queued) {
        if (!detail || done) return;
        if (queued > peak) peak = queued;
        detail.textContent = queued > 0
          ? 'LOADING IMAGERY: ' + queued + (queued === 1 ? ' TILE' : ' TILES')
          : 'LOADING IMAGERY';
        if (fill) fill.style.width = (peak > 0 ? Math.round(100 * (1 - queued / peak)) : 0) + '%';
        if (skip && clockFn() - started >= timing.skipAfterMs) skip.classList.add('on');
      }

      if (globe.tileLoadProgressEvent && globe.tileLoadProgressEvent.addEventListener) {
        removeProgress = globe.tileLoadProgressEvent.addEventListener(function (queued) {
          if (finite(queued)) show(queued);
        });
      }
      poll = global.setInterval(function () {
        if (viewer.scene && typeof viewer.scene.requestRender === 'function') {
          viewer.scene.requestRender();     // requestRenderMode: keep the tiles coming
        }
        if (skip && clockFn() - started >= timing.skipAfterMs) skip.classList.add('on');
        if (globe.tilesLoaded === true && clockFn() - started >= timing.minSettleMs) {
          finish('tiles');
        }
      }, timing.pollMs);
      cap = global.setTimeout(function () { finish('timeout'); }, limit);
    });
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var body = doc.body;
    var m = { settings: loadSettings(), lastActive: clockFn(), overPanel: false, narrow: false,
      mql: null, timer: null, statusEl: null, sharing: false };

    // ---- idle -------------------------------------------------------------

    function inPanel(target) {
      if (!target) return false;
      for (var i = 0; i < PANEL_IDS.length; i++) {
        var p = doc.getElementById(PANEL_IDS[i]);
        if (p && (p === target || (p.contains && p.contains(target)))) return true;
      }
      return false;
    }

    function typing() {
      var a = doc.activeElement;
      if (!a || !a.tagName) return false;
      var t = String(a.tagName).toUpperCase();
      return (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || a.isContentEditable === true);
    }

    function setIdle(on) {
      if (body.classList.contains('sda-idle') === on) return;
      body.classList.toggle('sda-idle', on);
    }

    function onActivity(evt) {
      m.lastActive = clockFn();
      if (evt && evt.type === 'pointermove') m.overPanel = inPanel(evt.target);
      setIdle(false);
    }

    function check() {
      if (!m.settings.idleHide || m.narrow || m.overPanel || typing() ||
          doc.getElementById('loading-overlay')) {
        setIdle(false);
        return;
      }
      if (clockFn() - m.lastActive >= m.settings.idleS * 1000) setIdle(true);
    }

    // ---- narrow screens ------------------------------------------------------

    function handle() {
      var controls = doc.getElementById('controls');
      if (!controls) return null;
      var h = null;
      for (var i = 0; i < (controls.children || []).length; i++) {
        if (controls.children[i].className === 'sda-sheet-handle') h = controls.children[i];
      }
      if (h) return h;
      h = el(doc, 'button', 'sda-sheet-handle', 'LAYERS AND TOOLS');
      h.type = 'button';
      h.setAttribute('aria-expanded', 'false');
      h.addEventListener('click', function (evt) {
        if (evt && evt.stopPropagation) evt.stopPropagation();
        setSheet(!body.classList.contains('sda-sheet-open'));
      });
      var first = controls.firstChild || (controls.children && controls.children[0]) || null;
      controls.insertBefore(h, first);
      return h;
    }

    function setSheet(open) {
      body.classList.toggle('sda-sheet-open', !!open);
      var h = handle();
      if (h) {
        h.setAttribute('aria-expanded', open ? 'true' : 'false');
        h.textContent = open ? 'HIDE LAYERS AND TOOLS' : 'LAYERS AND TOOLS';
      }
    }

    function applyNarrow() {
      var q = m.mql;
      var narrow = q ? !!q.matches : (global.innerWidth || 1e4) <= 700;
      m.narrow = narrow;
      body.classList.toggle('sda-narrow', narrow);
      if (narrow) { handle(); setIdle(false); } else { setSheet(false); }
    }

    // ---- share image -------------------------------------------------------------

    function cameraText() {
      var c = viewer.camera && viewer.camera.positionCartographic;
      if (!c || !C.Math) return '';
      return formatCamera(C.Math.toDegrees(c.latitude), C.Math.toDegrees(c.longitude), c.height);
    }

    function simDate() {
      try {
        if (typeof ctx.simNow === 'function') return ctx.simNow();
      } catch (e) { /* fall back to the wall clock */ }
      return new Date(clockFn());
    }

    function regimeChips() {
      var box = doc.getElementById('orbit-legend');
      var out = [];
      if (!box || !box.children) return out;
      Array.prototype.forEach.call(box.children, function (span) {
        var sw = span.children && span.children[0];
        var colour = sw && sw.style ? (sw.style.background || sw.style.backgroundColor) : '';
        var label = String(span.textContent || '').trim();
        if (label) out.push({ colour: colour || '#ffb000', label: label });
      });
      return out;
    }

    function cloudCredits() {
      var cl = global.SDAClouds;
      var st = cl && typeof cl.state === 'function' ? cl.state() : null;
      if (!st || !st.enabled || !cl.SOURCES || !cl.SOURCES[st.source]) return [];
      var out = [];
      var src = cl.SOURCES[st.source];
      Object.keys(src).forEach(function (k) {
        if (Array.isArray(src[k])) {
          src[k].forEach(function (x) { if (x && x.credit) out.push(String(x.credit)); });
        }
      });
      return out;
    }

    function shareParts() {
      var keys = typeof ctx.getVisibleLayers === 'function' ? ctx.getVisibleLayers() : [];
      var legend = layerLegend(keys);
      var chips = keys.indexOf('satellites') >= 0 ? regimeChips() : [];
      var credits = [];
      var bc = doc.getElementById('basemap-credit');
      if (bc && bc.textContent) credits.push('Imagery: ' + bc.textContent);
      var cw = viewer.cesiumWidget && viewer.cesiumWidget.creditContainer;
      if (cw && cw.textContent) credits.push(cw.textContent);
      cloudCredits().forEach(function (c) { credits.push('Clouds: ' + c); });
      var sources = [];
      legend.forEach(function (l) { if (l.source && sources.indexOf(l.source) < 0) sources.push(l.source); });
      if (sources.length) credits.push('Data: ' + sources.join(', '));
      credits.push('Rendered with CesiumJS');
      var link = '';
      try { if (typeof global.shareUrl === 'function') link = String(global.shareUrl()); } catch (e) { link = ''; }
      return {
        title: 'VANTAGE',
        when: utcStamp(simDate()),
        camera: cameraText(),
        layers: legend.map(function (l) { return l.label; }),
        chips: chips,
        attribution: attributionText(credits),
        link: link.length > 300 ? '' : link
      };
    }

    function drawCard(src, parts) {
      var W = src.width;
      var H = src.height;
      var out = doc.createElement('canvas');
      var g0 = out.getContext && out.getContext('2d');
      if (!g0) throw new Error('2D canvas is not available');
      var pad = 12;
      var measureAt = function (font) {
        return function (t) { g0.font = font; return g0.measureText(t).width; };
      };
      var small = measureAt('11px monospace');
      var attr = wrapText(small, parts.attribution, W - 2 * pad, 2);
      // Legend rows: chips first, then the layer names as one wrapped line.
      var chipRows = [];
      var row = [];
      var x = pad;
      var mid = measureAt('12px monospace');
      parts.chips.forEach(function (c) {
        var w = 14 + mid(c.label) + 18;
        if (row.length && x + w > W - pad) { chipRows.push(row); row = []; x = pad; }
        row.push({ c: c, x: x });
        x += w;
      });
      if (row.length) chipRows.push(row);
      var layerLines = parts.layers.length
        ? wrapText(mid, 'Layers: ' + parts.layers.join(', '), W - 2 * pad, 2) : [];
      var band = pad + 20 + (chipRows.length + layerLines.length) * 17 + attr.length * 14 +
        (parts.link ? 14 : 0) + pad;
      out.width = W;
      out.height = H + band;
      var g = out.getContext('2d');
      g.drawImage(src, 0, 0);
      g.fillStyle = '#050505';
      g.fillRect(0, H, W, band);
      g.fillStyle = '#ffb000';
      g.fillRect(0, H, W, 2);
      g.textBaseline = 'top';
      var y = H + pad;
      g.font = 'bold 15px monospace';
      g.fillText(parts.title, pad, y);
      var tw = g.measureText(parts.title).width;
      g.font = '12px monospace';
      g.fillStyle = '#f2e6cc';
      g.fillText([parts.when, parts.camera].filter(Boolean).join('   '), pad + tw + 16, y + 2);
      y += 20;
      chipRows.forEach(function (r) {
        r.forEach(function (it) {
          g.fillStyle = it.c.colour;
          g.fillRect(it.x, y + 2, 10, 10);
          g.fillStyle = '#f2e6cc';
          g.font = '12px monospace';
          g.fillText(it.c.label, it.x + 14, y);
        });
        y += 17;
      });
      g.fillStyle = '#f2e6cc';
      g.font = '12px monospace';
      layerLines.forEach(function (l) { g.fillText(l, pad, y); y += 17; });
      g.fillStyle = '#a89a7c';
      g.font = '11px monospace';
      attr.forEach(function (l) { g.fillText(l, pad, y); y += 14; });
      if (parts.link) {
        g.fillStyle = '#7a5500';
        g.fillText(wrapText(small, parts.link, W - 2 * pad, 1)[0] || '', pad, y);
      }
      return out;
    }

    function download(url) {
      var a = doc.createElement('a');
      a.href = url;
      a.download = 'vantage-' + fileStamp(new Date(clockFn())) + '.png';
      a.style.display = 'none';
      body.appendChild(a);
      if (typeof a.click === 'function') a.click();
      if (a.remove) a.remove();
    }

    function shareImage(opts) {
      opts = opts || {};
      if (m.sharing) return Promise.reject(new Error('A share image is already being made'));
      m.sharing = true;
      setStatus('Making the share image...');
      var scene = viewer.scene;
      return new Promise(function (resolve, reject) {
        var remove = null;
        var done = false;
        function grab() {
          if (done) return;
          done = true;
          if (typeof remove === 'function') remove();
          try {
            var card = drawCard(scene.canvas || viewer.canvas, shareParts());
            var url = card.toDataURL('image/png');
            if (opts.download !== false) download(url);
            setStatus('Share image saved (' + card.width + ' x ' + card.height + ').');
            m.sharing = false;
            resolve(url);
          } catch (err) {
            setStatus('Share image failed: ' + String((err && err.message) || err));
            m.sharing = false;
            reject(err);
          }
        }
        remove = scene.postRender.addEventListener(grab);
        if (typeof scene.requestRender === 'function') scene.requestRender();
      });
    }

    // ---- panel ----------------------------------------------------------------

    function setStatus(text) { if (m.statusEl) m.statusEl.textContent = text; }

    function statusLine() {
      var s = m.settings;
      return (s.idleHide ? 'Panels fade after ' + s.idleS + ' s without input' : 'Panels stay up') +
        (m.narrow ? '; narrow screen: panels in the bottom sheet.' : '.');
    }

    function buildPanel() {
      var root = el(doc, 'div', 'panel collapsed sdapresent');
      var h = el(doc, 'h3', '', PANEL_TITLE);
      root.appendChild(h);
      var bodyEl = el(doc, 'div', 'panel-body');
      root.appendChild(bodyEl);

      var lab = el(doc, 'label', 'layer-toggle');
      var cb = el(doc, 'input');
      cb.type = 'checkbox';
      cb.checked = m.settings.idleHide;
      cb.addEventListener('change', function () { api.setIdleHide(!!cb.checked); });
      lab.appendChild(cb);
      lab.appendChild(el(doc, 'span', '', 'Hide panels when idle'));
      bodyEl.appendChild(lab);
      m.idleBox = cb;

      var row = el(doc, 'div', 'sdapresent-row');
      row.appendChild(el(doc, 'span', '', 'After'));
      var pick = el(doc, 'select');
      IDLE_CHOICES.forEach(function (s) {
        var o = el(doc, 'option', '', s + ' s');
        o.value = String(s);
        pick.appendChild(o);
      });
      pick.value = String(m.settings.idleS);
      pick.addEventListener('change', function () { api.setIdleDelay(Number(pick.value)); });
      row.appendChild(pick);
      bodyEl.appendChild(row);
      m.idlePick = pick;

      var row2 = el(doc, 'div', 'sdapresent-row');
      var share = el(doc, 'button', '', 'SHARE VIEW (PNG)');
      share.type = 'button';
      share.title = 'Save the current view with a legend and the data credits';
      share.addEventListener('click', function () { shareImage().then(null, function () {}); });
      row2.appendChild(share);
      bodyEl.appendChild(row2);

      m.statusEl = el(doc, 'div', 'sdapresent-status', statusLine());
      bodyEl.appendChild(m.statusEl);
      h.addEventListener('click', function () { root.classList.toggle('collapsed'); });
      return root;
    }

    var api = {
      setIdleHide: function (on) {
        m.settings.idleHide = !!on;
        if (m.idleBox) m.idleBox.checked = !!on;
        saveSettings(m.settings);
        if (!on) setIdle(false);
        setStatus(statusLine());
      },
      setIdleDelay: function (s) {
        if (IDLE_CHOICES.indexOf(s) < 0) return false;
        m.settings.idleS = s;
        if (m.idlePick) m.idlePick.value = String(s);
        saveSettings(m.settings);
        setStatus(statusLine());
        return true;
      }
    };

    // ---- wire up -------------------------------------------------------------

    ensureStyle(doc);
    m.root = buildPanel();
    ACTIVITY.forEach(function (t) { doc.addEventListener(t, onActivity, true); });
    if (typeof global.matchMedia === 'function') {
      try { m.mql = global.matchMedia(NARROW_QUERY); } catch (e) { m.mql = null; }
    }
    if (m.mql && m.mql.addEventListener) {
      m.mql.addEventListener('change', applyNarrow);
    } else if (m.mql && m.mql.addListener) {
      m.mql.addListener(applyNarrow);
    }
    if (global.addEventListener) {
      global.addEventListener('resize', function () {
        if (typeof global.matchMedia === 'function') {
          try { m.mql = global.matchMedia(NARROW_QUERY); } catch (e) { /* keep the old query */ }
        }
        applyNarrow();
        if (m.statusEl) setStatus(statusLine());
      });
    }
    applyNarrow();
    m.statusEl.textContent = statusLine();
    m.timer = global.setInterval(check, timing.checkMs);

    m.api = api;
    m.viewer = viewer;
    m.check = check;
    m.shareImage = shareImage;
    m.shareParts = shareParts;
    m.setSheet = setSheet;
    m.applyNarrow = applyNarrow;
    m.state = function () {
      return { idle: body.classList.contains('sda-idle'), narrow: m.narrow,
        sheetOpen: body.classList.contains('sda-sheet-open'), overPanel: m.overPanel,
        idleHide: m.settings.idleHide, idleS: m.settings.idleS };
    };
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !global.Cesium || !global.document || !global.document.body) return false;
      if (current && current.timer) global.clearInterval(current.timer);
      current = createModule(ctx);
      ctx.mount.appendChild(current.root);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAPresent = {
    init: init,
    // No layer of its own; off means panels stay up.
    setEnabled: function (on) { if (current) current.api.setIdleHide(!!on); },
    setIdleHide: function (on) { if (current) current.api.setIdleHide(on); },
    setIdleDelay: function (s) { return current ? current.api.setIdleDelay(s) : false; },
    setSheet: function (open) { if (current) current.setSheet(open); },
    whenSettled: function (maxMs) {
      return whenSettled(maxMs, current ? current.viewer : null);
    },
    shareImage: function (opts) {
      return current ? current.shareImage(opts) : Promise.reject(new Error('not ready'));
    },
    shareParts: function () { return current ? current.shareParts() : null; },
    state: function () { return current ? current.state() : null; },
    _check: function () { if (current) current.check(); },
    _applyNarrow: function () { if (current) current.applyNarrow(); },
    _settleNow: function (why) { settleWaits.slice().forEach(function (f) { f(why || 'test'); }); },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    _setTiming: function (t) { Object.keys(t || {}).forEach(function (k) { if (k in timing) timing[k] = t[k]; }); },
    LAYERS: LAYERS,
    IDLE_CHOICES: IDLE_CHOICES,
    layerLegend: layerLegend,
    attributionText: attributionText,
    wrapText: wrapText,
    formatCamera: formatCamera,
    utcStamp: utcStamp
  };
})(typeof window !== 'undefined' ? window : this);
