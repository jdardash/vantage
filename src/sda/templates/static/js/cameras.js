// Public cameras on the WorldView globe: about 190,000 road, city, beach, ski,
// airport and observatory cameras in 170+ countries, drawn as a density layer for
// the whole Earth and as single cameras once the view is close enough, with a
// floating viewer that plays each feed the way its operator publishes it.
//
// Data: the server's camera catalog (sda/cameras.py, imported from the Argus
// export, https://github.com/GoSlowPoke168/Argus, MIT, Copyright (c) 2026
// Jeremy Hou). Taken from Argus: the idea of a density view that hands over
// to single cameras, the "jump to a random camera" action, and the order of
// playback attempts (stream first, then a cache-busted still). The panel, the
// Cesium point layers, the viewer and "cameras under the satellite" are ours.
//
// Feeds are played straight from their operators. This file never frames a page
// it cannot vouch for: YouTube embeds go through youtube-nocookie.com, any other
// web page is a link that opens in a new tab. Every URL is checked to be plain
// http(s) before it reaches an attribute, and no text is ever set as HTML.
//
// hls.js (https://github.com/video-dev/hls.js, Apache-2.0) is loaded from
// jsDelivr with a pinned integrity hash only when an HLS feed is opened in a
// browser that cannot play HLS by itself.
//
// Frames: geodetic degrees only. The satellite's ground point comes from the
// point the globe already draws (Earth-fixed), never from orbital elements.
//
// Page wiring: the page loads this after the other modules and lists it in
// EXTENSIONS; the CCTV layer toggle calls SDACameras.setEnabled(on), and the
// page's click handler calls point.id.sdaPick() for anything that carries one.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Public cameras';
  var STYLE_ID = 'sda-cameras-style';

  // Below this camera height single cameras are drawn; above it, the density
  // grid. The grid cell shrinks as the view comes down.
  var DETAIL_ALT_M = 450000;
  var CELL_STEPS = [
    { above: 12000000, cell: 8 },
    { above: 6000000, cell: 4 },
    { above: 3000000, cell: 2 },
    { above: 1200000, cell: 1 },
    { above: 0, cell: 0.5 }
  ];
  var VIEW_LIMIT = 1500;
  var MOVE_DEBOUNCE_MS = 350;
  var MIN_IMAGE_REFRESH_S = 10;
  var NEAR_RADIUS_KM = 25;
  var SAT_MIN_ELEVATION_DEG = 30;
  var EARTH_RADIUS_KM = 6378.135; // WGS72 equatorial radius (sda.constants)

  var HLS_SRC = 'https://cdn.jsdelivr.net/npm/hls.js@1.7.4/dist/hls.min.js';
  var HLS_SRI = 'sha384-p9vgU/7IYJWd3I4STrtqxqR/2cAQlhIX7D/HrTnIMEjjcUox165b7se7sxmm3zDg';
  var YT_EMBED = 'https://www.youtube-nocookie.com/embed/';
  var YT_QUERY = '?autoplay=1&mute=1&playsinline=1&modestbranding=1&rel=0';
  var YT_ID = /^[A-Za-z0-9_-]{11}$/;

  var GROUPS = [
    { key: 'traffic', label: 'Traffic', color: '#ff9f1a' },
    { key: 'city', label: 'City', color: '#00d4ff' },
    { key: 'beach', label: 'Beach', color: '#ffd166' },
    { key: 'mountain', label: 'Mountain', color: '#e8e8ff' },
    { key: 'weather', label: 'Weather', color: '#7bdff2' },
    { key: 'aviation', label: 'Aviation', color: '#c77dff' },
    { key: 'nature', label: 'Nature', color: '#06d6a0' },
    { key: 'transport', label: 'Ports', color: '#ef476f' },
    { key: 'science', label: 'Science', color: '#b5ff3b' },
    { key: 'other', label: 'Other', color: '#aaaaaa' }
  ];

  // Pixels the floating viewer keeps clear of the page chrome (left column,
  // header, right column, time bar), as in isscam.js.
  var LAYOUT = { left: 262, top: 60, right: 322, bottom: 72 };

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function groupColor(key) {
    for (var i = 0; i < GROUPS.length; i++) if (GROUPS[i].key === key) return GROUPS[i].color;
    return '#aaaaaa';
  }

  function cellForHeight(heightM) {
    for (var i = 0; i < CELL_STEPS.length; i++) {
      if (heightM > CELL_STEPS[i].above) return CELL_STEPS[i].cell;
    }
    return CELL_STEPS[CELL_STEPS.length - 1].cell;
  }

  function modeForHeight(heightM) {
    return heightM <= DETAIL_ALT_M ? 'detail' : 'density';
  }

  // A plain http(s) URL, or '' for anything else (javascript:, data:, custom
  // schemes, relative paths, whitespace, absurd length).
  function safeHttpUrl(url) {
    if (typeof url !== 'string' || !url || url.length > 2000) return '';
    if (/\s/.test(url)) return '';
    if (!/^https?:\/\/[^\/?#]+/i.test(url)) return '';
    return url;
  }

  function youtubeEmbedUrl(id) {
    return (typeof id === 'string' && YT_ID.test(id)) ? YT_EMBED + id + YT_QUERY : '';
  }

  // The URL with a cache-busting parameter, so a refreshed still is not served
  // from the browser cache.
  function bustUrl(url, nowMs) {
    var safe = safeHttpUrl(url);
    if (!safe) return '';
    var hash = '';
    var h = safe.indexOf('#');
    if (h >= 0) { hash = safe.slice(h); safe = safe.slice(0, h); }
    return safe + (safe.indexOf('?') >= 0 ? '&' : '?') + 'sda_t=' + Math.floor(nowMs) + hash;
  }

  // How the viewer shows one camera: which element, from where, how often.
  //   { mode: 'img'|'video'|'hls'|'youtube'|'link'|'none', src, refreshS, link }
  function feedPlan(cam, api) {
    var none = { mode: 'none', src: '', refreshS: 0, link: '' };
    if (!cam || typeof cam !== 'object') return none;
    var link = safeHttpUrl(cam.page_url) || '';
    var refresh = isNum(cam.refresh_s) ? Math.max(MIN_IMAGE_REFRESH_S, cam.refresh_s) : 60;
    var url = cam.feed_url;
    switch (cam.feed) {
      case 'image':
        url = safeHttpUrl(url);
        return url ? { mode: 'img', src: url, refreshS: refresh, link: link || url } : none;
      case 'mjpeg':
        url = safeHttpUrl(url);
        return url ? { mode: 'img', src: url, refreshS: 0, link: link || url } : none;
      case 'txdot':
        if (!isNum(cam.rid)) return none;
        return {
          mode: 'img', src: (api || '') + '/api/cameras/' + cam.rid + '/snapshot',
          refreshS: Math.max(60, refresh), link: link
        };
      case 'mp4':
        url = safeHttpUrl(url);
        return url ? { mode: 'video', src: url, refreshS: 0, link: link || url } : none;
      case 'hls':
        url = safeHttpUrl(url);
        return url ? { mode: 'hls', src: url, refreshS: 0, link: link || url } : none;
      case 'youtube':
        var embed = youtubeEmbedUrl(url);
        return embed ? {
          mode: 'youtube', src: embed, refreshS: 0,
          link: 'https://www.youtube.com/watch?v=' + url
        } : none;
      case 'page':
        url = safeHttpUrl(url) || link;
        return url ? { mode: 'link', src: '', refreshS: 0, link: url } : none;
      default:
        return none;
    }
  }

  // Query string from [key, value] pairs, skipping null and empty values.
  function query(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i++) {
      var v = pairs[i][1];
      if (v === null || v === undefined || v === '') continue;
      parts.push(encodeURIComponent(pairs[i][0]) + '=' + encodeURIComponent(String(v)));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  function round3(x) { return Math.round(x * 1000) / 1000; }

  // A degree box for the request from a view rectangle in radians, padded a
  // little so a small pan does not empty the edges. Null when unusable.
  function boxFromRect(rect, padFrac) {
    if (!rect || !isNum(rect.west) || !isNum(rect.south) || !isNum(rect.east) ||
        !isNum(rect.north)) return null;
    var d = 180 / Math.PI;
    var w = rect.west * d, s = rect.south * d, e = rect.east * d, n = rect.north * d;
    var pad = isNum(padFrac) ? padFrac : 0.1;
    var lonSpan = e >= w ? e - w : e + 360 - w;
    var latSpan = n - s;
    if (lonSpan >= 359) { w = -180; e = 180; } else {
      w -= lonSpan * pad; e += lonSpan * pad;
      if (w < -180) w += 360;
      if (e > 180) e -= 360;
    }
    s = Math.max(-90, s - latSpan * pad);
    n = Math.min(90, n + latSpan * pad);
    return { west: round3(w), south: round3(s), east: round3(e), north: round3(n) };
  }

  // A degree box of half-width km around a point (used when the camera looks
  // past the horizon and Cesium has no view rectangle).
  function boxAround(lat, lon, km) {
    var dLat = km / 111.32;
    var dLon = km / (111.32 * Math.max(0.05, Math.cos(lat * Math.PI / 180)));
    var w = lon - dLon, e = lon + dLon;
    if (dLon >= 180) { w = -180; e = 180; } else {
      if (w < -180) w += 360;
      if (e > 180) e -= 360;
    }
    return {
      west: round3(w), south: round3(Math.max(-90, lat - dLat)),
      east: round3(e), north: round3(Math.min(90, lat + dLat))
    };
  }

  // Ground radius (km) inside which a satellite at altKm stands at least
  // minElDeg above the horizon.
  function footprintKm(altKm, minElDeg) {
    if (!isNum(altKm) || altKm <= 0) return 0;
    var el = (isNum(minElDeg) ? minElDeg : 0) * Math.PI / 180;
    var r = EARTH_RADIUS_KM;
    var lambda = Math.acos(r * Math.cos(el) / (r + altKm)) - el;
    return Math.max(0, lambda * r);
  }

  function densitySize(count) {
    if (!isNum(count) || count <= 0) return 0;
    return Math.min(30, 4 + 3.2 * Math.log(count + 1) / Math.LN10 * 2);
  }

  function formatCount(n) {
    if (!isNum(n)) return '0';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e4) return Math.round(n / 1e3) + 'k';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  function withCommas(n) {
    return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function textOr(v, fallback) {
    return (typeof v === 'string' && v) ? v : fallback;
  }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  function px(n) { return n + 'px'; }

  var STYLE_CSS = [
    '.cams-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 6px;',
    '  line-height: 1.4; }',
    '.cams-chips { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 6px; }',
    '.cams-chip { display: inline-flex; align-items: center; gap: 4px; background: none;',
    '  border: 1px solid rgba(255,176,0,0.25); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 8px; padding: 2px 5px; border-radius: 2px; cursor: pointer; }',
    '.cams-chip.off { opacity: 0.35; }',
    '.cams-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }',
    '.cams-row { display: flex; gap: 3px; margin-bottom: 5px; }',
    '.cams-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.cams-search { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.6);',
    '  border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000); font: inherit;',
    '  font-size: 9px; padding: 3px 5px; margin-bottom: 5px; }',
    '.cams-check { display: flex; align-items: center; gap: 5px; font-size: 9px;',
    '  margin-bottom: 5px; cursor: pointer; }',
    '.cams-view { font-size: 9px; color: var(--hud-dim, #7a5500); min-height: 11px; }',
    '.cams-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px; }',
    '.cams-list { max-height: 150px; overflow-y: auto; margin-top: 4px; }',
    '.cams-item { display: block; width: 100%; text-align: left; background: none; border: 0;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 3px 2px; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.cams-item:hover { background: rgba(255,176,0,0.08); }',
    '.cams-item .cams-dist { color: var(--hud-dim, #7a5500); margin-left: 4px; }',
    '.cams-frame { position: fixed; right: ' + px(LAYOUT.right) + '; bottom: ' +
      px(LAYOUT.bottom) + ';',
    '  z-index: 46; box-sizing: border-box; width: min(480px, calc(100vw - ' +
      px(LAYOUT.left + LAYOUT.right + 8) + '));',
    '  min-width: 240px; background: rgba(0,0,0,0.92);',
    '  border: 1px solid var(--hud-color, #ffb000); border-radius: 4px; overflow: hidden;',
    '  font-size: 10px; letter-spacing: 1px; color: var(--hud-color, #ffb000); }',
    '.cams-frame.cams-big { width: calc(100vw - ' + px(LAYOUT.left + LAYOUT.right + 8) + '); }',
    '.cams-head { display: flex; align-items: center; gap: 6px; height: 28px;',
    '  box-sizing: border-box; padding: 0 8px; background: rgba(0,0,0,0.6);',
    '  border-bottom: 1px solid rgba(255,176,0,0.15); }',
    '.cams-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;',
    '  white-space: nowrap; font-weight: 700; }',
    '.cams-btn { flex: 0 0 auto; background: none; border: 1px solid rgba(255,176,0,0.3);',
    '  color: var(--hud-color, #ffb000); padding: 2px 8px; font-size: 9px; font-family: inherit;',
    '  cursor: pointer; border-radius: 2px; }',
    '.cams-media { position: relative; width: 100%; aspect-ratio: 16 / 9; background: #000;',
    '  display: flex; align-items: center; justify-content: center; }',
    '.cams-media img, .cams-media video { max-width: 100%; max-height: 100%;',
    '  object-fit: contain; }',
    '.cams-media iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }',
    '.cams-msg { padding: 12px; text-align: center; color: var(--hud-dim, #7a5500); }',
    '.cams-foot { display: flex; align-items: center; gap: 8px; min-height: 26px;',
    '  box-sizing: border-box; padding: 3px 8px; border-top: 1px solid rgba(255,176,0,0.15); }',
    '.cams-meta { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;',
    '  white-space: nowrap; color: var(--hud-dim, #7a5500); }',
    '.cams-foot a { color: var(--hud-color, #ffb000); }'
  ].join('\n');

  function injectStyle() {
    var doc = global.document;
    if (doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    doc.head.appendChild(style);
  }

  function el(tag, className, text) {
    var node = global.document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function button(className, text, title) {
    var node = el('button', className, text);
    node.setAttribute('type', 'button');
    if (title) node.setAttribute('title', title);
    return node;
  }

  function outLink(href, text) {
    var safe = safeHttpUrl(href);
    if (!safe) return null;
    var a = el('a', null, text);
    a.setAttribute('href', safe);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  // ---------------------------------------------------------------------
  // hls.js, loaded once on demand
  // ---------------------------------------------------------------------

  var hlsLoading = null;

  function loadHls() {
    if (global.Hls) return Promise.resolve(global.Hls);
    if (hlsLoading) return hlsLoading;
    hlsLoading = new Promise(function (resolve, reject) {
      var s = global.document.createElement('script');
      s.src = HLS_SRC;
      s.integrity = HLS_SRI;
      s.crossOrigin = 'anonymous';
      s.onload = function () { global.Hls ? resolve(global.Hls) : reject(new Error('no Hls')); };
      s.onerror = function () { hlsLoading = null; reject(new Error('hls.js did not load')); };
      global.document.head.appendChild(s);
    });
    return hlsLoading;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, summary: null, groupsOn: {}, playableOnly: false,
      seq: 0, timer: null, mode: '', points: null, labels: null, density: null,
      frame: null, openRid: null, refreshTimer: null, hls: null, selectedSat: null,
      lastView: null
    };
    GROUPS.forEach(function (g) { m.groupsOn[g.key] = true; });

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    function activeGroups() {
      var on = [];
      var all = true;
      GROUPS.forEach(function (g) { if (m.groupsOn[g.key]) on.push(g.key); else all = false; });
      return all ? null : on.join(',');
    }

    function getJson(path) {
      return global.fetch(api + path).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    // -- panel ----------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.summaryEl = el('div', 'cams-summary', 'Turn on the CCTV layer [C] to load the catalog.');
    m.chips = el('div', 'cams-chips');
    m.search = el('input', 'cams-search');
    m.search.setAttribute('type', 'search');
    m.search.setAttribute('placeholder', 'Search camera or city...');
    m.search.setAttribute('maxlength', '100');
    var checkLabel = el('label', 'cams-check');
    m.playableBox = el('input');
    m.playableBox.setAttribute('type', 'checkbox');
    checkLabel.appendChild(m.playableBox);
    checkLabel.appendChild(el('span', null, 'Only feeds that play here'));
    var row = el('div', 'cams-row');
    m.randomBtn = button('mode-btn', 'RANDOM', 'Fly to a random live camera');
    m.nearBtn = button('mode-btn', 'NEAR VIEW', 'Live cameras nearest the centre of the view');
    m.satBtn = button('mode-btn', 'UNDER SAT',
      'Live cameras with the selected satellite at least 30 deg above their horizon');
    row.appendChild(m.randomBtn);
    row.appendChild(m.nearBtn);
    row.appendChild(m.satBtn);
    m.view = el('div', 'cams-view');
    m.view.setAttribute('aria-live', 'polite');
    m.status = el('div', 'cams-status');
    m.status.setAttribute('role', 'status');
    m.list = el('div', 'cams-list');
    body.appendChild(m.summaryEl);
    body.appendChild(m.chips);
    body.appendChild(m.search);
    body.appendChild(checkLabel);
    body.appendChild(m.view);
    body.appendChild(row);
    body.appendChild(m.status);
    body.appendChild(m.list);
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    // Two lines: what the layer shows (view) and the answer to the last button,
    // search or click (status), so a view refresh never hides that answer.
    function setStatus(msg) { m.status.textContent = msg; }
    function setView(msg) { m.view.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById('count-cctv');
        if (c) c.textContent = formatCount(n);
      } catch (e) { /* keep going */ }
    }

    function buildChips() {
      m.chips.replaceChildren();
      var counts = (m.summary && m.summary.groups) || {};
      GROUPS.forEach(function (g) {
        var n = counts[g.key] || 0;
        if (!n && m.summary) return;
        var chip = button('cams-chip' + (m.groupsOn[g.key] ? '' : ' off'));
        var dot = el('span', 'cams-dot');
        dot.style.background = g.color;
        chip.appendChild(dot);
        chip.appendChild(el('span', null, g.label + ' ' + formatCount(n)));
        chip.setAttribute('aria-pressed', m.groupsOn[g.key] ? 'true' : 'false');
        chip.addEventListener('click', function () {
          m.groupsOn[g.key] = !m.groupsOn[g.key];
          buildChips();
          refresh(true);
        });
        m.chips.appendChild(chip);
      });
    }

    function showList(items, emptyText) {
      m.list.replaceChildren();
      if (!items.length) { setStatus(emptyText || 'Nothing found.'); return; }
      items.forEach(function (cam) {
        var b = button('cams-item');
        var dot = el('span', 'cams-dot');
        dot.style.background = groupColor(cam.group);
        b.appendChild(dot);
        b.appendChild(el('span', null, ' ' + textOr(cam.name, 'Camera')));
        if (isNum(cam.distance_km)) {
          b.appendChild(el('span', 'cams-dist', cam.distance_km.toFixed(1) + ' km'));
        } else if (cam.city || cam.country) {
          b.appendChild(el('span', 'cams-dist', [cam.city, cam.country].filter(Boolean).join(', ')));
        }
        b.addEventListener('click', function () { flyTo(cam); openCamera(cam.rid); });
        m.list.appendChild(b);
      });
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.points) return;
      m.density = viewer.scene.primitives.add(new C.PointPrimitiveCollection());
      m.labels = viewer.scene.primitives.add(new C.LabelCollection());
      m.points = viewer.scene.primitives.add(new C.PointPrimitiveCollection());
    }

    function clearLayers() {
      if (!m.points) return;
      m.density.removeAll();
      m.labels.removeAll();
      m.points.removeAll();
    }

    function flyTo(cam, height) {
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(cam.lon, cam.lat, height || 4000),
          duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function drawDensity(cells, cell) {
      clearLayers();
      var color = C.Color.fromCssColorString('#ff9f1a');
      var labelled = 0;
      cells.sort(function (a, b) { return b.count - a.count; });
      cells.forEach(function (c) {
        var pos = C.Cartesian3.fromDegrees(c.lon, c.lat, 0);
        var pick = {
          type: 'camera-cluster',
          sdaPick: function () {
            flyTo(c, Math.max(150000, cellHeight(cell) / 2.5));
          }
        };
        m.density.add({
          position: pos, pixelSize: densitySize(c.count),
          color: color.withAlpha(c.playable ? 0.55 : 0.3),
          outlineColor: C.Color.BLACK.withAlpha(0.6), outlineWidth: 1, id: pick,
          disableDepthTestDistance: 0
        });
        if (c.count >= 50 && labelled < 150) {
          labelled++;
          m.labels.add({
            position: pos, text: formatCount(c.count), font: '10px monospace',
            fillColor: C.Color.WHITE, outlineColor: C.Color.BLACK, outlineWidth: 2,
            style: C.LabelStyle.FILL_AND_OUTLINE,
            horizontalOrigin: C.HorizontalOrigin.CENTER,
            verticalOrigin: C.VerticalOrigin.CENTER, id: pick
          });
        }
      });
    }

    function cellHeight(cell) {
      for (var i = 0; i < CELL_STEPS.length; i++) {
        if (CELL_STEPS[i].cell === cell) return Math.max(CELL_STEPS[i].above, 600000);
      }
      return 600000;
    }

    function drawCameras(cams) {
      clearLayers();
      cams.forEach(function (cam) {
        var col = C.Color.fromCssColorString(groupColor(cam.group));
        m.points.add({
          position: C.Cartesian3.fromDegrees(cam.lon, cam.lat, 15),
          pixelSize: cam.rid === m.openRid ? 13 : 8,
          color: col.withAlpha(cam.playable ? 0.95 : 0.4),
          outlineColor: cam.rid === m.openRid ? C.Color.WHITE : C.Color.BLACK,
          outlineWidth: cam.rid === m.openRid ? 2 : 1,
          scaleByDistance: new C.NearFarScalar(2000, 1.4, 400000, 0.7),
          id: { type: 'camera', rid: cam.rid, sdaPick: function () { openCamera(cam.rid); } }
        });
      });
    }

    function viewState() {
      var cam = viewer.camera;
      var carto = cam.positionCartographic;
      var height = carto ? carto.height : 2e7;
      var rect = null;
      try { rect = cam.computeViewRectangle(viewer.scene.globe.ellipsoid); } catch (e) { rect = null; }
      var box = boxFromRect(rect, 0.1);
      if (!box && carto) {
        box = boxAround(carto.latitude * 180 / Math.PI, carto.longitude * 180 / Math.PI,
          Math.min(3000, Math.max(50, height / 1000)));
      }
      return { height: height, box: box || { west: -180, south: -90, east: 180, north: 90 } };
    }

    function refresh(force) {
      if (!m.enabled || ctx.isStatic) return;
      var v = viewState();
      var mode = modeForHeight(v.height);
      var groups = activeGroups();
      var seq = ++m.seq;
      if (mode === 'density') {
        var cell = cellForHeight(v.height);
        var dbox = cell >= 4 ? { west: -180, south: -90, east: 180, north: 90 } : v.box;
        var key = 'd' + cell + JSON.stringify(dbox) + groups;
        if (!force && key === m.lastView) return;
        m.lastView = key;
        getJson('/api/cameras/density' + query([
          ['west', dbox.west], ['south', dbox.south], ['east', dbox.east], ['north', dbox.north],
          ['cell', cell], ['groups', groups]
        ])).then(function (data) {
          if (seq !== m.seq || !m.enabled) return;
          drawDensity(data.cells || [], data.cell_deg || cell);
          m.mode = 'density';
          setCount(data.total || 0);
          setView(withCommas(data.total || 0) + ' cameras in view. Zoom in or click a cluster.');
        }).catch(function () { if (seq === m.seq) setView('Camera density did not load.'); });
      } else {
        var b = v.box;
        var key2 = 'v' + JSON.stringify(b) + groups + m.playableOnly;
        if (!force && key2 === m.lastView) return;
        m.lastView = key2;
        getJson('/api/cameras' + query([
          ['west', b.west], ['south', b.south], ['east', b.east], ['north', b.north],
          ['limit', VIEW_LIMIT], ['groups', groups], ['playable_only', m.playableOnly ? 'true' : '']
        ])).then(function (data) {
          if (seq !== m.seq || !m.enabled) return;
          var cams = data.cameras || [];
          drawCameras(cams);
          m.mode = 'detail';
          m.visible = cams;
          setCount(data.total || 0);
          setView(data.capped
            ? 'Showing ' + withCommas(cams.length) + ' of ' + withCommas(data.total) +
              ' cameras here. Zoom in for the rest.'
            : withCommas(cams.length) + ' cameras in view. Click one to watch.');
        }).catch(function () { if (seq === m.seq) setView('Cameras did not load.'); });
      }
    }

    function scheduleRefresh() {
      if (!m.enabled) return;
      if (m.timer) global.clearTimeout(m.timer);
      m.timer = global.setTimeout(function () { m.timer = null; refresh(false); },
        MOVE_DEBOUNCE_MS);
    }

    // -- viewer -----------------------------------------------------------

    function stopMedia() {
      if (m.refreshTimer) { global.clearInterval(m.refreshTimer); m.refreshTimer = null; }
      if (m.hls) { try { m.hls.destroy(); } catch (e) { /* keep going */ } m.hls = null; }
    }

    function closeViewer() {
      stopMedia();
      if (m.frame && m.frame.parentNode) m.frame.parentNode.removeChild(m.frame);
      m.frame = null;
      var was = m.openRid;
      m.openRid = null;
      if (was !== null && m.mode === 'detail' && m.visible) drawCameras(m.visible);
    }

    function buildFrame(cam) {
      var frame = el('div', 'cams-frame');
      frame.setAttribute('role', 'dialog');
      frame.setAttribute('aria-label', 'Camera feed');
      var bar = el('div', 'cams-head');
      var dot = el('span', 'cams-dot');
      dot.style.background = groupColor(cam.group);
      bar.appendChild(dot);
      bar.appendChild(el('span', 'cams-title', textOr(cam.name, 'Camera')));
      var size = button('cams-btn', 'Enlarge');
      size.addEventListener('click', function () {
        var big = frame.classList.toggle('cams-big');
        size.textContent = big ? 'Shrink' : 'Enlarge';
      });
      var next = button('cams-btn', 'Next', 'The next live camera nearby');
      next.addEventListener('click', function () { nextNearby(cam); });
      var close = button('cams-btn', 'Close');
      close.addEventListener('click', closeViewer);
      bar.appendChild(next);
      bar.appendChild(size);
      bar.appendChild(close);
      var media = el('div', 'cams-media');
      var foot = el('div', 'cams-foot');
      var where = [cam.city, cam.country].filter(Boolean).join(', ');
      var meta = el('span', 'cams-meta',
        [where, cam.type, 'source ' + textOr(cam.source, '?')].filter(Boolean).join(' | '));
      foot.appendChild(meta);
      frame.appendChild(bar);
      frame.appendChild(media);
      frame.appendChild(foot);
      return { root: frame, media: media, foot: foot };
    }

    function showMessage(media, text) {
      media.replaceChildren(el('div', 'cams-msg', text));
    }

    function playInto(parts, cam) {
      var plan = feedPlan(cam, api);
      var media = parts.media;
      if (plan.link) {
        var a = outLink(plan.link, plan.mode === 'link' ? 'Open camera page' : 'Source');
        if (a) parts.foot.appendChild(a);
      }
      if (plan.mode === 'img') {
        var img = el('img');
        img.setAttribute('alt', textOr(cam.name, 'Camera'));
        img.setAttribute('referrerpolicy', 'no-referrer');
        var busted = plan.refreshS > 0;
        var failed = false;
        img.onerror = function () {
          if (busted) { busted = false; img.src = plan.src; return; } // signed URLs dislike extras
          failed = true;
          showMessage(media, 'The feed did not load here. Try the source link.');
        };
        img.src = busted ? bustUrl(plan.src, Date.now()) : plan.src;
        media.replaceChildren(img);
        if (plan.refreshS > 0) {
          m.refreshTimer = global.setInterval(function () {
            // A hidden tab gets no new stills: no point pulling an agency's
            // camera every few seconds for a page nobody is looking at.
            if (failed || (global.document && global.document.hidden)) return;
            img.src = busted ? bustUrl(plan.src, Date.now()) : plan.src;
          }, plan.refreshS * 1000);
        }
      } else if (plan.mode === 'video' || plan.mode === 'hls') {
        var video = el('video');
        video.muted = true;
        video.autoplay = true;
        video.playsInline = true;
        video.controls = true;
        video.setAttribute('muted', '');
        video.setAttribute('playsinline', '');
        if (plan.mode === 'video') video.loop = true;
        video.onerror = function () {
          showMessage(media, 'The stream did not play here. Try the source link.');
        };
        media.replaceChildren(video);
        if (plan.mode === 'video' || video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = plan.src;
        } else {
          loadHls().then(function (Hls) {
            // the viewer may have closed or moved on while hls.js loaded
            if (video.isConnected === false) return;
            if (!Hls.isSupported()) { video.src = plan.src; return; }
            stopMedia();
            var hls = new Hls({ lowLatencyMode: true, maxBufferLength: 10 });
            m.hls = hls;
            hls.on(Hls.Events.ERROR, function (_evt, data) {
              if (data && data.fatal) {
                showMessage(media, 'The stream did not play here (' +
                  textOr(data.details, 'error') + '). Try the source link.');
                stopMedia();
              }
            });
            hls.loadSource(plan.src);
            hls.attachMedia(video);
          }).catch(function () {
            showMessage(media, 'The HLS player did not load. Try the source link.');
          });
        }
      } else if (plan.mode === 'youtube') {
        var frame = el('iframe');
        frame.setAttribute('src', plan.src);
        frame.setAttribute('title', textOr(cam.name, 'Camera'));
        frame.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
        frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
        frame.setAttribute('allowfullscreen', '');
        media.replaceChildren(frame);
      } else if (plan.mode === 'link') {
        showMessage(media, 'This camera plays on its operator\'s page; use the link below.');
      } else {
        showMessage(media, 'No playable feed for this camera.');
      }
    }

    function openCamera(rid) {
      if (!isNum(rid)) return;
      getJson('/api/cameras/' + rid).then(function (cam) {
        closeViewer();
        m.openRid = cam.rid;
        var parts = buildFrame(cam);
        m.frame = parts.root;
        global.document.body.appendChild(parts.root);
        playInto(parts, cam);
        if (m.mode === 'detail' && m.visible) drawCameras(m.visible);
        note('Camera: ' + textOr(cam.name, 'unnamed') +
          (cam.city ? ' (' + cam.city + ')' : '') + ', ' + cam.feed + ' feed');
      }).catch(function () { setStatus('That camera did not load.'); });
    }

    function nextNearby(cam) {
      getJson('/api/cameras/nearest' + query([
        ['lat', cam.lat], ['lon', cam.lon], ['n', 8], ['radius_km', NEAR_RADIUS_KM * 4]
      ])).then(function (data) {
        var list = (data.cameras || []).filter(function (c) { return c.rid !== cam.rid; });
        if (!list.length) { setStatus('No other live camera within 100 km.'); return; }
        openCamera(list[0].rid);
      }).catch(function () { setStatus('Nearby cameras did not load.'); });
    }

    // -- actions ----------------------------------------------------------

    m.randomBtn.addEventListener('click', function () {
      if (!m.enabled) setEnabled(true, true);
      var groups = activeGroups();
      getJson('/api/cameras/random' + query([['groups', groups]])).then(function (cam) {
        flyTo(cam, 3000);
        openCamera(cam.rid);
      }).catch(function () { setStatus('No live camera matches the filters.'); });
    });

    m.nearBtn.addEventListener('click', function () {
      if (!m.enabled) setEnabled(true, true);
      var carto = null;
      try {
        var center = viewer.camera.pickEllipsoid(
          new C.Cartesian2(viewer.canvas.clientWidth / 2, viewer.canvas.clientHeight / 2),
          viewer.scene.globe.ellipsoid);
        if (center) carto = C.Cartographic.fromCartesian(center);
      } catch (e) { carto = null; }
      if (!carto) carto = viewer.camera.positionCartographic;
      var lat = carto.latitude * 180 / Math.PI;
      var lon = carto.longitude * 180 / Math.PI;
      setStatus('Looking for live cameras near the centre of the view...');
      getJson('/api/cameras/nearest' + query([
        ['lat', round3(lat)], ['lon', round3(lon)], ['n', 20], ['radius_km', 300],
        ['groups', activeGroups()]
      ])).then(function (data) {
        var list = data.cameras || [];
        showList(list, 'No live camera within 300 km of the centre of the view.');
        if (list.length) setStatus(list.length + ' live cameras nearest the view centre.');
      }).catch(function () { setStatus('Nearby cameras did not load.'); });
    });

    m.satBtn.addEventListener('click', function () {
      if (!m.enabled) setEnabled(true, true);
      var sat = selectedSatellite();
      if (!sat) { setStatus('Select a satellite on the globe first.'); return; }
      var carto = C.Cartographic.fromCartesian(sat.point.position);
      if (!carto) { setStatus('The satellite has no position yet.'); return; }
      var altKm = carto.height / 1000;
      var radius = Math.min(2000, Math.max(5, footprintKm(altKm, SAT_MIN_ELEVATION_DEG)));
      var lat = carto.latitude * 180 / Math.PI;
      var lon = carto.longitude * 180 / Math.PI;
      setStatus('Looking for live cameras under ' + textOr(sat.name, 'the satellite') + '...');
      getJson('/api/cameras/nearest' + query([
        ['lat', round3(lat)], ['lon', round3(lon)], ['n', 25], ['radius_km', Math.round(radius)],
        ['groups', activeGroups()]
      ])).then(function (data) {
        var list = data.cameras || [];
        showList(list, 'No live camera has ' + textOr(sat.name, 'it') + ' above ' +
          SAT_MIN_ELEVATION_DEG + ' deg right now (ground radius ' + Math.round(radius) +
          ' km). Try again as it moves.');
        if (list.length) {
          setStatus(list.length + ' live cameras have ' + textOr(sat.name, 'it') +
            ' above ' + SAT_MIN_ELEVATION_DEG + ' deg now (within ' + Math.round(radius) +
            ' km of its ground point).');
        }
      }).catch(function () { setStatus('Cameras under the satellite did not load.'); });
    });

    function selectedSatellite() {
      try {
        var eng = ctx.satEngine;
        var id = eng && eng.selectedId;
        var s = (id !== null && id !== undefined && eng.byNoradId) ? eng.byNoradId[id] : null;
        return (s && s.point && s.point.position) ? s : null;
      } catch (e) {
        return null;
      }
    }

    var searchTimer = null;
    m.search.addEventListener('input', function () {
      if (searchTimer) global.clearTimeout(searchTimer);
      var q = m.search.value.trim();
      if (q.length < 2) { m.list.replaceChildren(); return; }
      searchTimer = global.setTimeout(function () {
        getJson('/api/cameras/search' + query([['q', q], ['limit', 25]])).then(function (data) {
          var list = data.cameras || [];
          showList(list, 'No camera named like "' + q + '".');
          if (list.length) setStatus(list.length + ' cameras match "' + q + '".');
        }).catch(function () { setStatus('Search did not run.'); });
      }, 300);
    });

    m.playableBox.addEventListener('change', function () {
      m.playableOnly = !!m.playableBox.checked;
      refresh(true);
    });

    // -- enable / disable ---------------------------------------------------

    function loadSummary() {
      return getJson('/api/cameras/summary').then(function (s) {
        m.summary = s;
        if (!s.available) {
          m.summaryEl.textContent = 'No camera catalog yet. On the server run: ' +
            'python -m sda.cameras import';
        } else {
          m.summaryEl.textContent = withCommas(s.count) + ' public cameras in ' +
            s.country_count + ' countries (' + withCommas(s.playable) +
            ' play here), from ' + s.source_count + ' sources. ' + textOr(s.attribution, '');
        }
        buildChips();
        return s;
      });
    }

    function setEnabled(on, fromPanel) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'The camera catalog needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (fromPanel) {
        try {
          var box = global.document.getElementById('layer-cctv');
          if (box && !box.checked) {
            box.checked = true;
            if (typeof global.toggleLayer === 'function') { m.enabled = false; global.toggleLayer('cctv', true); return; }
          }
        } catch (e) { /* keep going */ }
      }
      if (on) {
        ensureLayers();
        m.density.show = m.labels.show = m.points.show = true;
        m.lastView = null;
        (m.summary ? Promise.resolve(m.summary) : loadSummary()).then(function (s) {
          if (s && s.available) {
            note('Cameras: ' + withCommas(s.count) + ' public cameras in ' + s.country_count +
              ' countries');
          }
          refresh(true);
        }).catch(function () { setStatus('Camera catalog did not load.'); });
      } else {
        if (m.timer) { global.clearTimeout(m.timer); m.timer = null; }
        m.seq++;
        if (m.points) { m.density.show = m.labels.show = m.points.show = false; }
        clearLayers();
        closeViewer();
        setCount(0);
        setView('');
        setStatus('');
      }
    }

    m.setEnabled = setEnabled;
    m.openCamera = openCamera;
    m.closeViewer = closeViewer;
    m.refresh = refresh;

    try {
      viewer.camera.moveEnd.addEventListener(scheduleRefresh);
    } catch (e) { /* keep going */ }

    buildChips();
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !global.Cesium) return false;
      injectStyle();
      current = createModule(ctx);
      ctx.mount.appendChild(current.root);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDACameras = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    open: function (rid) { if (current) current.openCamera(rid); },
    close: function () { if (current) current.closeViewer(); },
    isOpen: function () { return !!(current && current.frame); },
    GROUPS: GROUPS,
    LAYOUT: LAYOUT,
    DETAIL_ALT_M: DETAIL_ALT_M,
    HLS_SRC: HLS_SRC,
    HLS_SRI: HLS_SRI,
    cellForHeight: cellForHeight,
    modeForHeight: modeForHeight,
    safeHttpUrl: safeHttpUrl,
    youtubeEmbedUrl: youtubeEmbedUrl,
    bustUrl: bustUrl,
    feedPlan: feedPlan,
    query: query,
    boxFromRect: boxFromRect,
    boxAround: boxAround,
    footprintKm: footprintKm,
    densitySize: densitySize,
    formatCount: formatCount,
    groupColor: groupColor
  };
})(typeof window !== 'undefined' ? window : this);
