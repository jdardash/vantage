// Every mountain on Earth on the WorldView globe: about 12 million summits with
// at least 30 m (100 ft) of prominence, drawn as a density grid for the whole
// Earth and as single peaks once the view is close enough, the most prominent
// first, each coloured by its climbing verdict when the conditions module has one.
//
// Data: the server's peak catalog (sda/peaks.py): summits and prominence from
// the 2023 global prominence run of Andrew Kirmse and Jonathan de Ferranti on
// Copernicus GLO-30, names from GeoNames (CC BY 4.0). The look follows the
// Sierra Summit peak map: one marker per peak whose shape and colour both carry
// the verdict (circle GO, diamond MARG, square NO, grey when there is no
// forecast), names placed so they do not collide with higher peaks winning, a
// search box, and a drawer per peak. The drawer and the verdicts belong to the
// conditions module (window.SDASummit); this file only calls it:
//
//   SDASummit.open({ id, name, lat, lon, elev_m, prom_m })
//   SDASummit.verdicts([{ id, lat, lon, elev_m }, ...])   at most 50 points
//     -> Promise of { <id>: { verdict: 'GO'|'MARG'|'NO'|'?', day: 'YYYY-MM-DD' } }
//
// Verdicts are asked for the most prominent peaks in view, only for peaks not
// asked in the last 10 minutes, so a call happens when the view shows new peaks
// or the 10 minutes run out. Without SDASummit the layer works the same, with
// every marker neutral.
//
// Frames: geodetic degrees and metres above sea level only. No text is ever set
// as HTML; marker images come from the constant table below.
//
// Page wiring: the page loads this after the other modules and lists it in
// EXTENSIONS; the Peaks layer toggle calls SDAPeaks.setEnabled(on), and the
// page's click handler calls picked.id.sdaPick() for anything that carries one.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Mountains';
  var STYLE_ID = 'sda-peaks-style';

  // Below this camera height single peaks are drawn; above it, the density grid.
  var DETAIL_ALT_M = 1500000;
  // Server density cells, in degrees (sda.peaks.DENSITY_CELLS).
  var DENSITY_CELLS = [0.25, 0.5, 1, 2, 4, 8, 16];
  var VIEW_LIMIT = 300;
  var LABEL_MAX = 40;
  var DENSITY_TOPS = 20;
  var VERDICT_MAX = 50;
  var VERDICT_TTL_MS = 10 * 60 * 1000;
  var MOVE_DEBOUNCE_MS = 350;
  var SEARCH_DEBOUNCE_MS = 300;
  var NEAR_RADIUS_KM = 100;
  var MAX_REMEMBERED = 5000;
  var FT_PER_M = 1 / 0.3048;
  var PROM_STEPS = [
    { label: 'ALL', value: 0, title: 'Every summit, most prominent first' },
    { label: '300 M+', value: 300, title: 'Summits with at least 300 m (984 ft) of prominence' },
    { label: '1000 M+', value: 1000, title: 'Summits with at least 1000 m (3,281 ft) of prominence' }
  ];

  // Verdict -> marker. Shape and colour both carry the state, as on the peak map.
  var MARKERS = {
    none: { shape: 'triangle', color: '#d9d2bf', text: 'not checked' },
    GO: { shape: 'circle', color: '#0ca30c', text: 'GO' },
    MARG: { shape: 'diamond', color: '#fab219', text: 'MARG' },
    NO: { shape: 'square', color: '#d03b3b', text: 'NO' },
    '?': { shape: 'triangle', color: '#6b6a66', text: '?' }
  };
  var VERDICT_ORDER = ['GO', 'MARG', 'NO', '?', 'none'];

  var SHAPES = {
    triangle: '<path d="M12 3 L22 20 L2 20 Z"',
    circle: '<circle cx="12" cy="12" r="8"',
    diamond: '<path d="M12 2 L22 12 L12 22 L2 12 Z"',
    square: '<rect x="4" y="4" width="16" height="16"'
  };

  function iconUrl(shape, color) {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
      'viewBox="0 0 24 24">' + SHAPES[shape] + ' fill="' + color +
      '" stroke="#000000" stroke-width="1.6"/></svg>';
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }

  var ICONS = {};
  Object.keys(MARKERS).forEach(function (k) {
    ICONS[k] = iconUrl(MARKERS[k].shape, MARKERS[k].color);
  });

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function modeForHeight(heightM) {
    return heightM <= DETAIL_ALT_M ? 'detail' : 'density';
  }

  // Web-map zoom level for a camera height: 0 at 40,000 km, one level per halving.
  function zoomForHeight(heightM) {
    var h = isNum(heightM) && heightM > 1 ? heightM : 1;
    var z = Math.log(4e7 / h) / Math.LN2;
    return Math.max(0, Math.min(22, Math.round(z * 10) / 10));
  }

  // The density cell the server picks for a zoom (sda.peaks.cell_for_zoom).
  function cellForZoom(zoom) {
    var want = 16 / Math.pow(2, Math.max(0, zoom));
    for (var i = 0; i < DENSITY_CELLS.length; i++) {
      if (DENSITY_CELLS[i] >= want - 1e-9) return DENSITY_CELLS[i];
    }
    return DENSITY_CELLS[DENSITY_CELLS.length - 1];
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

  // A degree box from a view rectangle in radians, padded a little so a small
  // pan does not empty the edges. Null when unusable.
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

  // A degree box of half-width km around a point (when the camera looks past
  // the horizon and Cesium has no view rectangle).
  function boxAround(lat, lon, km) {
    var dLat = km / 111.195;
    var dLon = km / (111.195 * Math.max(0.05, Math.cos(lat * Math.PI / 180)));
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

  function bboxParam(box) {
    return [box.west, box.south, box.east, box.north].join(',');
  }

  function withCommas(n) {
    return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function formatCount(n) {
    if (!isNum(n)) return '0';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e4) return Math.round(n / 1e3) + 'k';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  // "4,421 m / 14,505 ft"
  function formatElev(m) {
    if (!isNum(m)) return 'height unknown';
    return withCommas(m) + ' m / ' + withCommas(m * FT_PER_M) + ' ft';
  }

  function formatProm(m) {
    if (!isNum(m)) return 'prominence unknown';
    return 'prom ' + withCommas(m) + ' m / ' + withCommas(m * FT_PER_M) + ' ft';
  }

  function peakName(p) {
    return (p && typeof p.name === 'string' && p.name) ? p.name : 'Unnamed peak';
  }

  function byRank(a, b) { return a.id - b.id; }

  function markerFor(verdict) {
    var key = (verdict && Object.prototype.hasOwnProperty.call(MARKERS, verdict.verdict))
      ? verdict.verdict : 'none';
    return { key: key, shape: MARKERS[key].shape, color: MARKERS[key].color, image: ICONS[key] };
  }

  // Map text on the globe: name, height in both units, and the verdict when known.
  function peakLabel(p, verdict) {
    var lines = [];
    if (p && typeof p.name === 'string' && p.name) lines.push(p.name);
    lines.push(formatElev(p ? p.elev_m : null));
    if (verdict && verdict.verdict) {
      lines.push(verdict.verdict + (verdict.day ? ' ' + verdict.day : ''));
    }
    return lines.join('\n');
  }

  // Which peaks get a label: the most prominent first, skipping any whose label
  // would sit on top of one already placed. Returns the ids, in rank order.
  function declutter(peaks, box, maxLabels) {
    if (!box || !peaks || !peaks.length) return [];
    var lonSpan = box.east >= box.west ? box.east - box.west : box.east + 360 - box.west;
    var latSpan = Math.max(0, box.north - box.south);
    var sepLon = Math.max(1e-4, lonSpan / 9);
    var sepLat = Math.max(1e-4, latSpan / 16);
    var placed = [];
    var sorted = peaks.slice().sort(byRank);
    for (var i = 0; i < sorted.length && placed.length < maxLabels; i++) {
      var p = sorted[i];
      if (!isNum(p.lat) || !isNum(p.lon)) continue;
      var clash = false;
      for (var j = 0; j < placed.length; j++) {
        var dLon = Math.abs(p.lon - placed[j].lon);
        if (dLon > 180) dLon = 360 - dLon;
        if (Math.abs(p.lat - placed[j].lat) < sepLat && dLon < sepLon) { clash = true; break; }
      }
      if (!clash) placed.push(p);
    }
    return placed.map(function (p) { return p.id; });
  }

  // The verdict answer, keeping only well-formed entries: { id: { verdict, day } }.
  function cleanVerdicts(res) {
    var out = {};
    if (!res || typeof res !== 'object') return out;
    Object.keys(res).forEach(function (k) {
      var id = Number(k);
      var v = res[k];
      if (!isFinite(id) || id <= 0 || Math.floor(id) !== id || !v || typeof v !== 'object') return;
      if (typeof v.verdict !== 'string' || v.verdict === 'none' ||
          !Object.prototype.hasOwnProperty.call(MARKERS, v.verdict)) return;
      var day = (typeof v.day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v.day)) ? v.day : '';
      out[id] = { verdict: v.verdict, day: day };
    });
    return out;
  }

  // The peaks to ask about: the VERDICT_MAX most prominent shown, minus those
  // asked within the last VERDICT_TTL_MS.
  function verdictPoints(shown, asked, nowMs) {
    var points = [];
    var top = (shown || []).slice().sort(byRank).slice(0, VERDICT_MAX);
    for (var i = 0; i < top.length; i++) {
      var p = top[i];
      var at = asked[p.id];
      if (isNum(at) && nowMs - at < VERDICT_TTL_MS) continue;
      points.push({ id: p.id, lat: p.lat, lon: p.lon, elev_m: isNum(p.elev_m) ? p.elev_m : null });
    }
    return points;
  }

  function densitySize(count) {
    if (!isNum(count) || count <= 0) return 0;
    return Math.min(28, 4 + 2.6 * Math.log(count + 1) / Math.LN10 * 2);
  }

  var clockFn = function () { return Date.now(); };

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.peaks-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 4px;',
    '  line-height: 1.4; }',
    '.peaks-attr { font-size: 8px; color: var(--hud-dim, #7a5500); margin-bottom: 6px;',
    '  line-height: 1.3; opacity: 0.8; }',
    '.peaks-search { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.6);',
    '  border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000); font: inherit;',
    '  font-size: 9px; padding: 3px 5px; margin-bottom: 5px; }',
    '.peaks-row { display: flex; gap: 3px; margin-bottom: 5px; }',
    '.peaks-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.peaks-legend { display: flex; flex-wrap: wrap; gap: 6px; font-size: 8px;',
    '  margin-bottom: 5px; color: var(--hud-dim, #7a5500); }',
    '.peaks-dot { width: 7px; height: 7px; display: inline-block; margin-right: 3px;',
    '  border: 1px solid #000; }',
    '.peaks-dot.circle { border-radius: 50%; }',
    '.peaks-dot.diamond { transform: rotate(45deg) scale(0.8); }',
    '.peaks-view { font-size: 9px; color: var(--hud-dim, #7a5500); min-height: 11px; }',
    '.peaks-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px; }',
    '.peaks-info { font-size: 9px; color: var(--hud-color, #ffb000); min-height: 11px;',
    '  margin-top: 3px; }',
    '.peaks-list { max-height: 150px; overflow-y: auto; margin-top: 4px; }',
    '.peaks-item { display: block; width: 100%; text-align: left; background: none; border: 0;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 3px 2px; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.peaks-item:hover { background: rgba(255,176,0,0.08); }',
    '.peaks-item .peaks-meta { color: var(--hud-dim, #7a5500); margin-left: 4px; }'
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

  function dot(key) {
    var d = el('span', 'peaks-dot ' + MARKERS[key].shape);
    d.style.background = MARKERS[key].color;
    return d;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, summary: null, available: false, minProm: 0,
      seq: 0, timer: null, verdictTimer: null, searchTimer: null, mode: '',
      density: null, labels: null, marks: null, lastView: null,
      shown: [], cells: [], cell: 0, box: null, selectedId: null,
      verdicts: Object.create(null), asked: Object.create(null), askedCount: 0
    };

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    function getJson(path) {
      if (ctx.isStatic) return Promise.reject(new Error('static build'));
      return global.fetch(api + path).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    // -- panel ----------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.summaryEl = el('div', 'peaks-summary', 'Turn on the Peaks layer [P] to load the catalog.');
    m.attrEl = el('div', 'peaks-attr');
    m.search = el('input', 'peaks-search');
    m.search.setAttribute('type', 'search');
    m.search.setAttribute('placeholder', 'Search a mountain...');
    m.search.setAttribute('maxlength', '100');
    var promRow = el('div', 'peaks-row');
    m.promBtns = PROM_STEPS.map(function (step) {
      var b = button('mode-btn' + (step.value === 0 ? ' active' : ''), step.label, step.title);
      b.setAttribute('aria-pressed', step.value === 0 ? 'true' : 'false');
      b.addEventListener('click', function () { setMinProm(step.value); });
      promRow.appendChild(b);
      return b;
    });
    var actRow = el('div', 'peaks-row');
    m.nearBtn = button('mode-btn', 'NEAR VIEW', 'Peaks nearest the centre of the view');
    actRow.appendChild(m.nearBtn);
    var legend = el('div', 'peaks-legend');
    VERDICT_ORDER.forEach(function (k) {
      var item = el('span');
      item.appendChild(dot(k));
      item.appendChild(el('span', null, MARKERS[k].text));
      legend.appendChild(item);
    });
    m.view = el('div', 'peaks-view');
    m.view.setAttribute('aria-live', 'polite');
    m.status = el('div', 'peaks-status');
    m.status.setAttribute('role', 'status');
    m.info = el('div', 'peaks-info');
    m.list = el('div', 'peaks-list');
    [m.summaryEl, m.attrEl, m.search, promRow, actRow, legend, m.view, m.status, m.info,
     m.list].forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.status.textContent = msg; }
    function setView(msg) { m.view.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById('count-peaks');
        if (c) c.textContent = formatCount(n);
      } catch (e) { /* keep going */ }
    }

    function verdictOf(id) { return m.verdicts[id] || null; }

    function showList(items, emptyText) {
      m.list.replaceChildren();
      if (!items.length) { setStatus(emptyText || 'Nothing found.'); return; }
      items.forEach(function (p) {
        var b = button('peaks-item');
        b.appendChild(dot(markerFor(verdictOf(p.id)).key));
        b.appendChild(el('span', null, peakName(p)));
        var meta = [isNum(p.elev_m) ? withCommas(p.elev_m) + ' m' : ''];
        if (isNum(p.distance_km)) meta.push(p.distance_km.toFixed(1) + ' km');
        else if (p.region || p.country) meta.push([p.region, p.country].filter(Boolean).join(', '));
        b.appendChild(el('span', 'peaks-meta', meta.filter(Boolean).join(' | ')));
        b.addEventListener('click', function () { flyTo(p, 30000); selectPeak(p); });
        m.list.appendChild(b);
      });
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.marks) return;
      m.density = viewer.scene.primitives.add(new C.PointPrimitiveCollection());
      m.marks = viewer.scene.primitives.add(new C.BillboardCollection());
      m.labels = viewer.scene.primitives.add(new C.LabelCollection());
    }

    function clearLayers() {
      if (!m.marks) return;
      m.density.removeAll();
      m.marks.removeAll();
      m.labels.removeAll();
    }

    function flyTo(p, height) {
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(p.lon, p.lat, height || 30000),
          duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function peakPick(p) {
      return { type: 'peak', id: p.id, sdaPick: function () { selectPeak(p); } };
    }

    // One marker, and its label when asked. Close views draw every marker in
    // front of terrain; the whole-Earth view lets the globe hide the far side.
    function addPeak(p, withLabel, close) {
      var v = verdictOf(p.id);
      var mk = markerFor(v);
      var selected = p.id === m.selectedId;
      var pos = C.Cartesian3.fromDegrees(p.lon, p.lat, (isNum(p.elev_m) ? p.elev_m : 0) + 30);
      var pick = peakPick(p);
      m.marks.add({
        position: pos, image: mk.image, width: selected ? 22 : 15, height: selected ? 22 : 15,
        verticalOrigin: C.VerticalOrigin.CENTER,
        scaleByDistance: new C.NearFarScalar(5000, 1.3, 2000000, 0.75),
        disableDepthTestDistance: close ? Number.POSITIVE_INFINITY : 0,
        id: pick
      });
      if (!withLabel) return;
      m.labels.add({
        position: pos, text: peakLabel(p, v), font: '11px monospace',
        fillColor: selected ? C.Color.fromCssColorString('#ffd166') : C.Color.WHITE,
        outlineColor: C.Color.BLACK, outlineWidth: 2,
        style: C.LabelStyle.FILL_AND_OUTLINE,
        horizontalOrigin: C.HorizontalOrigin.CENTER,
        verticalOrigin: C.VerticalOrigin.BOTTOM,
        pixelOffset: new C.Cartesian2(0, -11),
        disableDepthTestDistance: close ? Number.POSITIVE_INFINITY : 0,
        id: pick
      });
    }

    function drawPeaks(peaks, box) {
      clearLayers();
      var labelled = {};
      declutter(peaks, box, LABEL_MAX).forEach(function (id) { labelled[id] = true; });
      if (m.selectedId !== null) labelled[m.selectedId] = true;
      peaks.forEach(function (p) { addPeak(p, !!labelled[p.id], true); });
    }

    function drawDensity(cells, cell) {
      clearLayers();
      var color = C.Color.fromCssColorString('#d9d2bf');
      var tops = densityTops(cells);
      var topCell = {};
      tops.forEach(function (t) { topCell[t.cellIndex] = true; });
      var counted = 0;
      cells.forEach(function (c, i) {
        var pos = C.Cartesian3.fromDegrees(c.lon, c.lat, 0);
        var pick = {
          type: 'peak-cluster',
          sdaPick: function () { flyTo(c, cell >= 4 ? 4000000 : DETAIL_ALT_M * 0.7); }
        };
        m.density.add({
          position: pos, pixelSize: densitySize(c.count), color: color.withAlpha(0.35),
          outlineColor: C.Color.BLACK.withAlpha(0.6), outlineWidth: 1, id: pick,
          disableDepthTestDistance: 0
        });
        if (c.count >= 50 && counted < 150 && !topCell[i]) {
          counted++;
          m.labels.add({
            position: pos, text: formatCount(c.count), font: '10px monospace',
            fillColor: C.Color.WHITE.withAlpha(0.8), outlineColor: C.Color.BLACK,
            outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
            horizontalOrigin: C.HorizontalOrigin.CENTER,
            verticalOrigin: C.VerticalOrigin.CENTER, id: pick, disableDepthTestDistance: 0
          });
        }
      });
      tops.forEach(function (t) { addPeak(t.peak, true, false); });
    }

    // The highest peak of the DENSITY_TOPS clusters whose top peak ranks best.
    function densityTops(cells) {
      var tops = [];
      cells.forEach(function (c, i) {
        if (c && c.top && isNum(c.top.id)) tops.push({ cellIndex: i, peak: c.top });
      });
      tops.sort(function (a, b) { return a.peak.id - b.peak.id; });
      return tops.slice(0, DENSITY_TOPS);
    }

    function redraw() {
      if (!m.enabled) return;
      if (m.mode === 'detail') drawPeaks(m.shown, m.box);
      else if (m.mode === 'density') drawDensity(m.cells, m.cell);
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
      if (!m.enabled || ctx.isStatic || !m.available) return;
      var v = viewState();
      var seq;
      if (modeForHeight(v.height) === 'density') {
        var zoom = zoomForHeight(v.height);
        var cell = cellForZoom(zoom);
        var dbox = cell >= 4 ? { west: -180, south: -90, east: 180, north: 90 } : v.box;
        var key = 'd' + cell + bboxParam(dbox);
        if (!force && key === m.lastView) return;
        m.lastView = key;
        seq = ++m.seq;
        getJson('/api/peaks/density' + query([['bbox', bboxParam(dbox)], ['zoom', zoom]]))
          .then(function (data) {
            if (seq !== m.seq || !m.enabled) return;
            m.mode = 'density';
            m.cells = data.cells || [];
            m.cell = data.cell_deg || cell;
            m.shown = densityTops(m.cells).map(function (t) { return t.peak; });
            drawDensity(m.cells, m.cell);
            setCount(data.total || 0);
            setView(withCommas(data.total || 0) + ' summits in view. Zoom in for single ' +
              'peaks or click a cluster.');
            askVerdicts();
          }).catch(function () { if (seq === m.seq) setView('Peak density did not load.'); });
      } else {
        var b = v.box;
        var key2 = 'v' + bboxParam(b) + '|' + m.minProm;
        if (!force && key2 === m.lastView) return;
        m.lastView = key2;
        seq = ++m.seq;
        getJson('/api/peaks' + query([
          ['bbox', bboxParam(b)], ['limit', VIEW_LIMIT], ['min_prom', m.minProm || '']
        ])).then(function (data) {
          if (seq !== m.seq || !m.enabled) return;
          var peaks = data.peaks || [];
          m.mode = 'detail';
          m.shown = peaks;
          m.box = b;
          drawPeaks(peaks, b);
          setCount(data.total || 0);
          var total = withCommas(data.total || 0) + (data.total_capped ? '+' : '');
          setView(data.capped
            ? 'The ' + withCommas(peaks.length) + ' most prominent of ' + total +
              ' summits here. Zoom in for more.'
            : withCommas(peaks.length) + ' summits in view. Click one for conditions.');
          askVerdicts();
        }).catch(function () { if (seq === m.seq) setView('Peaks did not load.'); });
      }
    }

    function scheduleRefresh() {
      if (!m.enabled) return;
      if (m.timer) global.clearTimeout(m.timer);
      m.timer = global.setTimeout(function () { m.timer = null; refresh(false); },
        MOVE_DEBOUNCE_MS);
    }

    // -- conditions (window.SDASummit) -------------------------------------

    function askVerdicts() {
      var S = global.SDASummit;
      if (!m.enabled || !S || typeof S.verdicts !== 'function') return null;
      var t = clockFn();
      var points = verdictPoints(m.shown, m.asked, t);
      if (!points.length) return null;
      if (m.askedCount > MAX_REMEMBERED) { m.asked = Object.create(null); m.askedCount = 0; }
      points.forEach(function (p) { m.asked[p.id] = t; m.askedCount++; });
      var answer;
      try { answer = Promise.resolve(S.verdicts(points)); } catch (e) { answer = Promise.reject(e); }
      return answer.then(function (res) {
        var got = cleanVerdicts(res);
        var ids = Object.keys(got);
        if (Object.keys(m.verdicts).length + ids.length > MAX_REMEMBERED) {
          m.verdicts = Object.create(null);
        }
        ids.forEach(function (id) { m.verdicts[id] = got[id]; });
        if (ids.length) redraw();
      }).catch(function () { setStatus('Conditions did not load for the peaks in view.'); });
    }

    function selectPeak(p) {
      if (!p || !isNum(p.id)) return;
      m.selectedId = p.id;
      var where = [p.region, p.country].filter(Boolean).join(', ');
      m.info.textContent = [peakName(p), formatElev(p.elev_m), formatProm(p.prom_m), where]
        .filter(Boolean).join(' | ');
      redraw();
      note('Peak: ' + peakName(p) + ', ' + formatElev(p.elev_m));
      var S = global.SDASummit;
      if (!S || typeof S.open !== 'function') {
        setStatus('Mountain conditions are not loaded on this page.');
        return;
      }
      try {
        S.open({
          id: p.id, name: typeof p.name === 'string' ? p.name : '', lat: p.lat, lon: p.lon,
          elev_m: isNum(p.elev_m) ? p.elev_m : null, prom_m: isNum(p.prom_m) ? p.prom_m : null
        });
      } catch (e) {
        setStatus('Conditions did not open for ' + peakName(p) + '.');
      }
    }

    // -- actions ----------------------------------------------------------

    function setMinProm(value) {
      m.minProm = value;
      m.promBtns.forEach(function (b, i) {
        var on = PROM_STEPS[i].value === value;
        b.className = 'mode-btn' + (on ? ' active' : '');
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      if (!m.enabled) { setEnabled(true, true); return; }
      if (m.mode === 'density') setStatus('The prominence filter applies once you zoom in.');
      refresh(true);
    }

    m.nearBtn.addEventListener('click', function () {
      if (ctx.isStatic) { setStatus('The peak catalog needs the live server.'); return; }
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
      setStatus('Looking for peaks near the centre of the view...');
      getJson('/api/peaks/nearest' + query([
        ['lat', round3(lat)], ['lon', round3(lon)], ['n', 20], ['radius_km', NEAR_RADIUS_KM],
        ['min_prom', m.minProm || '']
      ])).then(function (data) {
        var list = data.peaks || [];
        showList(list, 'No peak within ' + NEAR_RADIUS_KM + ' km of the centre of the view.');
        if (list.length) setStatus(list.length + ' peaks nearest the view centre.');
      }).catch(function () { setStatus('Nearby peaks did not load.'); });
    });

    m.search.addEventListener('input', function () {
      if (m.searchTimer) global.clearTimeout(m.searchTimer);
      var q = String(m.search.value || '').trim();
      if (q.length < 2) { m.list.replaceChildren(); return; }
      if (ctx.isStatic) { setStatus('The peak catalog needs the live server.'); return; }
      m.searchTimer = global.setTimeout(function () {
        m.searchTimer = null;
        getJson('/api/peaks/search' + query([['q', q], ['limit', 20]])).then(function (data) {
          var list = data.peaks || [];
          showList(list, 'No mountain named like "' + q + '".');
          if (list.length) setStatus(list.length + ' mountains match "' + q + '".');
        }).catch(function () { setStatus('Search did not run.'); });
      }, SEARCH_DEBOUNCE_MS);
    });

    // -- enable / disable ---------------------------------------------------

    function loadSummary() {
      return getJson('/api/peaks/summary').then(function (s) {
        m.summary = s;
        m.available = !!s.available;
        if (!s.available) {
          m.summaryEl.textContent = 'No peak catalog yet. On the server run: ' +
            'python -m sda.peaks import';
          m.attrEl.textContent = '';
        } else {
          m.summaryEl.textContent = withCommas(s.count) + ' summits worldwide (' +
            withCommas(s.named) + ' named, ' + withCommas(s.with_prominence) +
            ' with prominence) in ' + s.country_count + ' countries.';
          m.attrEl.textContent = typeof s.attribution === 'string' ? s.attribution : '';
        }
        return s;
      });
    }

    function setEnabled(on, fromPanel) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'The peak catalog needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (fromPanel && on) {
        try {
          var box = global.document.getElementById('layer-peaks');
          if (box && !box.checked) {
            box.checked = true;
            if (typeof global.toggleLayer === 'function') {
              m.enabled = false;
              global.toggleLayer('peaks', true);
              return;
            }
          }
        } catch (e) { /* keep going */ }
      }
      if (on) {
        ensureLayers();
        m.density.show = m.labels.show = m.marks.show = true;
        m.lastView = null;
        if (!m.verdictTimer) {
          m.verdictTimer = global.setInterval(function () { askVerdicts(); }, VERDICT_TTL_MS);
        }
        (m.summary ? Promise.resolve(m.summary) : loadSummary()).then(function (s) {
          if (!m.enabled) return;
          if (s && s.available) {
            note('Peaks: ' + withCommas(s.count) + ' summits worldwide');
            refresh(true);
          }
        }).catch(function () { setStatus('Peak catalog did not load.'); });
      } else {
        if (m.timer) { global.clearTimeout(m.timer); m.timer = null; }
        if (m.searchTimer) { global.clearTimeout(m.searchTimer); m.searchTimer = null; }
        if (m.verdictTimer) { global.clearInterval(m.verdictTimer); m.verdictTimer = null; }
        m.seq++;
        if (m.marks) { m.density.show = m.labels.show = m.marks.show = false; }
        clearLayers();
        m.mode = '';
        m.shown = [];
        setCount(0);
        setView('');
        setStatus('');
      }
    }

    m.setEnabled = setEnabled;
    m.refresh = refresh;
    m.askVerdicts = askVerdicts;
    m.selectPeak = selectPeak;

    try {
      viewer.camera.moveEnd.addEventListener(scheduleRefresh);
    } catch (e) { /* keep going */ }

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

  global.SDAPeaks = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { if (current) current.refresh(true); },
    // Ask for verdicts now, as the 10-minute timer does; returns the Promise or null.
    recheck: function () { return current ? current.askVerdicts() : null; },
    select: function (peak) { if (current) current.selectPeak(peak); },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    DETAIL_ALT_M: DETAIL_ALT_M,
    VERDICT_MAX: VERDICT_MAX,
    VERDICT_TTL_MS: VERDICT_TTL_MS,
    MARKERS: MARKERS,
    ICONS: ICONS,
    modeForHeight: modeForHeight,
    zoomForHeight: zoomForHeight,
    cellForZoom: cellForZoom,
    query: query,
    boxFromRect: boxFromRect,
    boxAround: boxAround,
    formatElev: formatElev,
    formatProm: formatProm,
    formatCount: formatCount,
    markerFor: markerFor,
    peakLabel: peakLabel,
    declutter: declutter,
    cleanVerdicts: cleanVerdicts,
    verdictPoints: verdictPoints,
    densitySize: densitySize
  };
})(typeof window !== 'undefined' ? window : this);
