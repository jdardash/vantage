// Maritime intelligence on the WorldView globe: nine chokepoint monitors, live
// AIS vessels with sanctioned ones highlighted, and dark ships kept on their
// estimated course, drawn hollow and labelled as estimates.
//
// Data: the server's maritime routes (sda/maritime.py, sda/routes/maritime.py)
// over the shared AISStream subscription, and sda/sanctions.py for the OFAC SDN
// and UK lists:
//
//   GET /api/maritime/chokepoints   polygons, vessels inside now, transits today
//   GET /api/maritime/vessels       vessels in the view, plus dark estimates
//
// AIS needs AISSTREAM_API_KEY on the server (a free key from aisstream.io); it
// never reaches this file. Without it, and in the static demo with no server at
// all, the chokepoint polygons still draw from the copy in CHOKEPOINTS below
// (tests/test_globe_maritime.py checks it against sda.maritime).
//
// Colours: plain vessel blue, sanctioned (MMSI on a list) red and larger,
// name-only match amber, dark estimate a hollow violet ring joined to the last
// reported position by a thin line. A name match is a lead, never a listing.
//
// No text is ever set as HTML. Frames: geodetic degrees only.
//
// Page wiring: the page loads this after the other modules and lists it in
// EXTENSIONS with mount ext-maritime; the Maritime layer toggle calls
// SDAMaritime.setEnabled(on), and the page's click handler calls
// picked.id.sdaPick() for anything that carries one.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Maritime';
  var STYLE_ID = 'sda-maritime-style';
  var COUNT_ID = 'count-maritime';
  var LAYER_ID = 'layer-maritime';

  var VESSEL_POLL_MS = 20000;
  var CHOKE_POLL_MS = 60000;
  var MOVE_DEBOUNCE_MS = 400;
  var VESSEL_LIMIT = 3000;
  var LABEL_MAX = 60;
  var LIST_MAX = 30;
  var KEY_HINT = 'Live vessels need AISSTREAM_API_KEY on the server (a free key from ' +
    'aisstream.io). Chokepoints are shown without counts.';
  var STATIC_HINT = 'Static demo: chokepoints only. Vessels need the live server.';
  var MISSING_HINT = 'The maritime service is not on this server: chokepoints only.';

  var COLORS = {
    vessel: '#7fd1ff',
    listed: '#ff4040',
    lead: '#ffb000',
    dark: '#c58cff',
    choke: '#ffd166'
  };

  // Same polygons as sda.maritime.CHOKEPOINTS: (lon, lat) degrees, not closed.
  var CHOKEPOINTS = [
    { id: 'gibraltar', name: 'Strait of Gibraltar',
      polygon: [[-6.058, 36.056], [-5.308, 36.086], [-5.292, 35.834], [-6.042, 35.804]] },
    { id: 'hormuz', name: 'Strait of Hormuz',
      polygon: [[56.074, 26.698], [56.874, 26.618], [56.826, 26.222], [56.026, 26.302]] },
    { id: 'bab_el_mandeb', name: 'Bab-el-Mandeb',
      polygon: [[43.398, 12.247], [43.048, 12.747], [43.352, 12.953], [43.702, 12.453]] },
    { id: 'suez', name: 'Suez Canal',
      polygon: [[32.2, 31.4], [32.42, 31.4], [32.42, 30.5], [32.65, 30.05], [32.65, 29.85],
        [32.45, 29.85], [32.25, 30.3]] },
    { id: 'malacca', name: 'Strait of Malacca',
      polygon: [[101.831, 2.925], [102.131, 2.665], [101.369, 1.775], [101.069, 2.035]] },
    { id: 'panama', name: 'Panama Canal',
      polygon: [[-80.0, 9.4], [-79.84, 9.4], [-79.62, 9.1], [-79.5, 8.88], [-79.6, 8.84],
        [-79.78, 9.02], [-80.0, 9.2]] },
    { id: 'bosporus', name: 'Bosporus',
      polygon: [[28.96, 40.99], [29.06, 40.99], [29.1, 41.1], [29.18, 41.24], [29.07, 41.24],
        [29.02, 41.1]] },
    { id: 'dover', name: 'Strait of Dover',
      polygon: [[1.25, 51.072], [1.61, 51.272], [1.95, 51.028], [1.59, 50.828]] },
    { id: 'taiwan_strait', name: 'Taiwan Strait',
      polygon: [[119.487, 25.133], [119.787, 25.533], [121.013, 24.767], [120.713, 24.367]] }
  ];

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function str(x) { return typeof x === 'string' ? x : ''; }

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

  // The server's box: south, west, north, east in degrees. A view across the
  // antimeridian asks for every longitude in its latitude band.
  function viewBox(rect) {
    if (!rect || !isNum(rect.west) || !isNum(rect.south) || !isNum(rect.east) ||
        !isNum(rect.north)) return { south: -90, west: -180, north: 90, east: 180 };
    var d = 180 / Math.PI;
    var w = rect.west * d, s = rect.south * d, e = rect.east * d, n = rect.north * d;
    if (e <= w) { w = -180; e = 180; }
    return {
      south: round3(Math.max(-90, s)), west: round3(Math.max(-180, w)),
      north: round3(Math.min(90, n)), east: round3(Math.min(180, e))
    };
  }

  // Polygon ring as a flat lon, lat list, closed.
  function closedRing(polygon) {
    var flat = [];
    polygon.forEach(function (p) { flat.push(p[0], p[1]); });
    if (polygon.length) flat.push(polygon[0][0], polygon[0][1]);
    return flat;
  }

  function centroid(polygon) {
    var lon = 0, lat = 0;
    polygon.forEach(function (p) { lon += p[0]; lat += p[1]; });
    return { lon: lon / polygon.length, lat: lat / polygon.length };
  }

  function formatAge(seconds) {
    if (!isNum(seconds) || seconds < 0) return '';
    if (seconds < 90) return Math.round(seconds) + ' s';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min';
    return (seconds / 3600).toFixed(1) + ' h';
  }

  function withCommas(n) {
    return isNum(n) ? String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : '-';
  }

  // One vessel as drawn: which class, colour, size, hollow or filled.
  function vesselStyle(v) {
    if (v && v.estimate) return { key: 'dark', color: COLORS.dark, size: 9, hollow: true };
    if (v && v.sanction) return { key: 'listed', color: COLORS.listed, size: 9, hollow: false };
    if (v && v.name_match) return { key: 'lead', color: COLORS.lead, size: 7, hollow: false };
    return { key: 'vessel', color: COLORS.vessel, size: 5, hollow: false };
  }

  function vesselName(v) {
    var mmsi = isNum(v.mmsi) ? String(v.mmsi) : str(v.mmsi);
    return str(v.name) || (mmsi ? 'MMSI ' + mmsi : 'Unnamed vessel');
  }

  function vesselLabel(v) {
    if (v.estimate) return str(v.label) || ('EST ' + vesselName(v));
    if (v.sanction) return 'SANCTIONED ' + vesselName(v);
    return vesselName(v);
  }

  function chokepointById(id) {
    for (var i = 0; i < CHOKEPOINTS.length; i++) {
      if (CHOKEPOINTS[i].id === id) return CHOKEPOINTS[i];
    }
    return null;
  }

  // The card for one vessel: an array of lines, each { text, cls }.
  function vesselCard(v) {
    var lines = [{ text: vesselName(v), cls: 'title' }];
    var facts = ['MMSI ' + str(v.mmsi)];
    if (isNum(v.sog_kn)) facts.push(v.sog_kn.toFixed(1) + ' kn');
    if (isNum(v.cog_deg)) facts.push('course ' + Math.round(v.cog_deg) + ' deg');
    lines.push({ text: facts.join(' | '), cls: '' });
    if (v.estimate) {
      lines.push({ text: 'ESTIMATE: no AIS for ' + formatAge(v.silent_s) +
        '. Position dead-reckoned ' + (isNum(v.run_km) ? v.run_km.toFixed(1) + ' km ' : '') +
        'from the last course and speed. It may only be out of receiver range.',
        cls: 'warn' });
      if (isNum(v.last_lat) && isNum(v.last_lon)) {
        lines.push({ text: 'Last report at ' + v.last_lat.toFixed(3) + ', ' +
          v.last_lon.toFixed(3), cls: '' });
      }
    } else if (isNum(v.age_s)) {
      lines.push({ text: 'Last report ' + formatAge(v.age_s) + ' ago', cls: '' });
    }
    if (str(v.chokepoint)) {
      var cp = chokepointById(v.chokepoint);
      lines.push({ text: 'Inside ' + (cp ? cp.name : v.chokepoint), cls: '' });
    }
    var s = v.sanction;
    if (s) {
      lines.push({ text: 'SANCTIONED: ' + str(s.source) + ', ' + str(s.programme), cls: 'bad' });
      lines.push({ text: 'Listed as ' + str(s.name) + (str(s.imo) ? ', IMO ' + s.imo : '') +
        ' (MMSI match)', cls: '' });
      if (str(s.remarks)) lines.push({ text: s.remarks, cls: 'remark' });
    } else if (v.name_match) {
      var n = v.name_match;
      lines.push({ text: 'NAME MATCH, UNCONFIRMED: ' + str(n.source) + ', ' + str(n.programme),
        cls: 'lead' });
      lines.push({ text: str(n.note) || 'Name only; check the IMO number.', cls: '' });
      if (str(n.remarks)) lines.push({ text: n.remarks, cls: 'remark' });
    }
    return lines;
  }

  // Panel line for one monitor; counts are null without the AIS key.
  function chokeLine(cp) {
    if (!cp || !isNum(cp.inside_now)) return str(cp && cp.name);
    var parts = [cp.inside_now + ' in'];
    if (isNum(cp.transits_today)) parts.push(cp.transits_today + ' today');
    if (isNum(cp.transits_total)) parts.push(cp.transits_total + ' since start');
    if (isNum(cp.sanctioned_now) && cp.sanctioned_now > 0) {
      parts.push(cp.sanctioned_now + ' sanctioned');
    }
    return cp.name + ': ' + parts.join(' | ');
  }

  function chokeLabel(cp) {
    var short = str(cp.name).replace(/^Strait of /, '').toUpperCase();
    if (!isNum(cp.inside_now)) return short;
    return short + '  ' + cp.inside_now + ' IN / ' +
      (isNum(cp.transits_today) ? cp.transits_today : 0) + ' TODAY';
  }

  // Keep only rows with a position; the server already cleaned them.
  function cleanVessels(list) {
    if (!Array.isArray(list)) return [];
    return list.filter(function (v) {
      return v && isNum(v.lat) && isNum(v.lon) && Math.abs(v.lat) <= 90 &&
        Math.abs(v.lon) <= 180 && (typeof v.mmsi === 'string' || isNum(v.mmsi));
    }).map(function (v) { v.mmsi = String(v.mmsi); return v; });
  }

  function safeUrl(u) { return typeof u === 'string' && /^https:\/\/[^\s]+$/.test(u) ? u : ''; }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.mar-status { font-size: 9px; color: var(--hud-dim, #9a6c00); margin-bottom: 4px;',
    '  line-height: 1.4; }',
    '.mar-summary { font-size: 9px; color: var(--hud-color, #ffb000); margin-bottom: 4px; }',
    '.mar-legend { display: flex; flex-wrap: wrap; gap: 6px; font-size: 8px;',
    '  margin-bottom: 5px; color: var(--hud-dim, #9a6c00); }',
    '.mar-dot { width: 7px; height: 7px; display: inline-block; margin-right: 3px;',
    '  border-radius: 50%; border: 1px solid #000; }',
    '.mar-dot.hollow { background: transparent; border: 2px solid #c58cff; }',
    '.mar-head { font-size: 8px; letter-spacing: 1px; color: var(--hud-dim, #9a6c00);',
    '  margin: 5px 0 2px; text-transform: uppercase; }',
    '.mar-list { max-height: 140px; overflow-y: auto; }',
    '.mar-item { display: block; width: 100%; text-align: left; background: none; border: 0;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 3px 2px; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.mar-item:hover { background: rgba(255,176,0,0.08); }',
    '.mar-card { font-size: 9px; color: var(--hud-color, #ffb000); margin-top: 5px;',
    '  line-height: 1.4; }',
    '.mar-card .title { font-weight: 600; letter-spacing: 1px; }',
    '.mar-card .bad { color: #ff4040; font-weight: 600; }',
    '.mar-card .lead { color: #ffb000; font-weight: 600; }',
    '.mar-card .warn { color: #c58cff; }',
    '.mar-card .remark { color: var(--hud-dim, #9a6c00); max-height: 90px; overflow-y: auto; }',
    '.mar-card a { color: var(--hud-color, #ffb000); }',
    '.mar-rule { font-size: 8px; color: var(--hud-dim, #9a6c00); margin-top: 5px;',
    '  line-height: 1.3; opacity: 0.85; }'
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

  function legendItem(key, text) {
    var span = el('span');
    var d = el('span', 'mar-dot' + (key === 'dark' ? ' hollow' : ''));
    if (key !== 'dark') d.style.background = COLORS[key];
    span.appendChild(d);
    span.appendChild(el('span', null, text));
    return span;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      enabled: false, seq: 0, chokes: CHOKEPOINTS.slice(), keyConfigured: null,
      serverMissing: false, vessels: [], dark: [], selected: null, noted: {},
      vesselTimer: null, chokeTimer: null, moveTimer: null, layers: null,
      announced: false
    };

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    function getJson(path) {
      if (ctx.isStatic) return Promise.reject(new Error('static build'));
      return global.fetch(api + path).then(function (r) {
        if (!r.ok) { var err = new Error('HTTP ' + r.status); err.status = r.status; throw err; }
        return r.json();
      });
    }

    // -- panel ----------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.statusEl = el('div', 'mar-status', 'Turn on the Maritime layer to watch the chokepoints.');
    m.summaryEl = el('div', 'mar-summary');
    m.legend = el('div', 'mar-legend');
    m.legend.appendChild(legendItem('vessel', 'vessel'));
    m.legend.appendChild(legendItem('listed', 'sanctioned'));
    m.legend.appendChild(legendItem('lead', 'name match'));
    m.legend.appendChild(legendItem('dark', 'dark (estimate)'));
    m.chokeList = el('div', 'mar-list');
    m.listedHead = el('div', 'mar-head', 'Sanctioned in view');
    m.listedList = el('div', 'mar-list');
    m.darkHead = el('div', 'mar-head', 'Dark ships in view');
    m.darkList = el('div', 'mar-list');
    m.card = el('div', 'mar-card');
    m.rule = el('div', 'mar-rule');
    [m.statusEl, m.summaryEl, m.legend, el('div', 'mar-head', 'Chokepoints'), m.chokeList,
     m.listedHead, m.listedList, m.darkHead, m.darkList, m.card, m.rule]
      .forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      if (e && e.target && e.target.tagName === 'INPUT') return;
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.statusEl.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById(COUNT_ID);
        if (c) c.textContent = withCommas(n);
      } catch (e) { /* keep going */ }
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.layers) return;
      var prims = viewer.scene.primitives;
      m.layers = {
        chokeLines: prims.add(new C.PolylineCollection()),
        chokeLabels: prims.add(new C.LabelCollection()),
        trails: prims.add(new C.PolylineCollection()),
        points: prims.add(new C.PointPrimitiveCollection()),
        labels: prims.add(new C.LabelCollection()),
        fill: null
      };
    }

    function showLayers(on) {
      if (!m.layers) return;
      ['chokeLines', 'chokeLabels', 'trails', 'points', 'labels', 'fill'].forEach(function (k) {
        if (m.layers[k]) m.layers[k].show = on;
      });
    }

    function colorMaterial(css, alpha) {
      var color = C.Color.fromCssColorString(css).withAlpha(alpha);
      try { return C.Material.fromType('Color', { color: color }); } catch (e) { return undefined; }
    }

    function dashMaterial(css) {
      var color = C.Color.fromCssColorString(css).withAlpha(0.7);
      try { return C.Material.fromType('PolylineDash', { color: color, dashLength: 12 }); }
      catch (e) { return colorMaterial(css, 0.7); }
    }

    function flyTo(lon, lat, height) {
      try {
        viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(lon, lat, height),
          duration: 1.6 });
      } catch (e) { /* keep going */ }
    }

    function chokePick(cp) {
      return { type: 'chokepoint', id: cp.id, sdaPick: function () { selectChoke(cp); } };
    }

    // Translucent fill under the outlines, where the Cesium build has the
    // polygon primitive (a nicety: the outline carries the shape).
    function drawFill() {
      if (m.layers.fill || !C.Primitive || !C.GeometryInstance || !C.PolygonGeometry ||
          !C.PolygonHierarchy || !C.PerInstanceColorAppearance ||
          !C.ColorGeometryInstanceAttribute) return;
      try {
        var color = C.Color.fromCssColorString(COLORS.choke).withAlpha(0.12);
        var instances = CHOKEPOINTS.map(function (cp) {
          var flat = [];
          cp.polygon.forEach(function (p) { flat.push(p[0], p[1]); });
          return new C.GeometryInstance({
            geometry: new C.PolygonGeometry({
              polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)),
              height: 0
            }),
            id: chokePick(cp),
            attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(color) }
          });
        });
        m.layers.fill = viewer.scene.primitives.add(new C.Primitive({
          geometryInstances: instances,
          appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false }),
          asynchronous: true
        }));
      } catch (e) { m.layers.fill = null; }
    }

    function drawChokepoints() {
      ensureLayers();
      drawFill();
      var lines = m.layers.chokeLines;
      var labels = m.layers.chokeLabels;
      lines.removeAll();
      labels.removeAll();
      var material = colorMaterial(COLORS.choke, 0.9);
      m.chokes.forEach(function (cp) {
        var poly = cp.polygon || (chokepointById(cp.id) || {}).polygon;
        if (!poly || poly.length < 3) return;
        var pick = chokePick(cp);
        lines.add({ positions: C.Cartesian3.fromDegreesArray(closedRing(poly)), width: 2,
          material: material, id: pick });
        var c = centroid(poly);
        labels.add({
          position: C.Cartesian3.fromDegrees(c.lon, c.lat, 0), text: chokeLabel(cp),
          font: '10px monospace', fillColor: C.Color.fromCssColorString(COLORS.choke),
          outlineColor: C.Color.BLACK, outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.BOTTOM,
          pixelOffset: new C.Cartesian2(0, -6), id: pick
        });
      });
      renderChokeList();
    }

    function vesselPick(v) {
      return { type: 'vessel', id: v.mmsi, sdaPick: function () { selectVessel(v); } };
    }

    function drawVessels() {
      ensureLayers();
      var L = m.layers;
      L.points.removeAll();
      L.labels.removeAll();
      L.trails.removeAll();
      var labelled = 0;
      var trailMaterial = dashMaterial(COLORS.dark);
      function addOne(v) {
        var st = vesselStyle(v);
        var color = C.Color.fromCssColorString(st.color);
        var pos = C.Cartesian3.fromDegrees(v.lon, v.lat, 10);
        var pick = vesselPick(v);
        var selected = m.selected === v.mmsi;
        L.points.add({
          position: pos, pixelSize: st.size + (selected ? 4 : 0),
          color: st.hollow ? color.withAlpha(0) : color,
          outlineColor: st.hollow ? color : C.Color.BLACK,
          outlineWidth: st.hollow ? 2 : (st.key === 'vessel' ? 0 : 1),
          id: pick
        });
        if (v.estimate && isNum(v.last_lat) && isNum(v.last_lon)) {
          L.trails.add({ positions: C.Cartesian3.fromDegreesArray([v.last_lon, v.last_lat,
            v.lon, v.lat]), width: 1, material: trailMaterial, id: pick });
        }
        if ((st.key === 'vessel' && !selected) || labelled >= LABEL_MAX) return;
        labelled++;
        L.labels.add({
          position: pos, text: vesselLabel(v), font: '10px monospace', fillColor: color,
          outlineColor: C.Color.BLACK, outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.BOTTOM,
          pixelOffset: new C.Cartesian2(0, -9), id: pick
        });
      }
      // Listed and dark first, so they win the label budget.
      m.vessels.filter(function (v) { return v.sanction; }).forEach(addOne);
      m.dark.forEach(addOne);
      m.vessels.filter(function (v) { return !v.sanction; }).forEach(addOne);
    }

    // -- lists and cards ------------------------------------------------

    function renderChokeList() {
      m.chokeList.replaceChildren();
      m.chokes.forEach(function (cp) {
        var b = button('mar-item', chokeLine(cp), cp.note || cp.name);
        b.addEventListener('click', function () { selectChoke(cp); });
        m.chokeList.appendChild(b);
      });
    }

    function renderVesselList(target, list, emptyText) {
      target.replaceChildren();
      if (!list.length) { target.appendChild(el('div', 'mar-status', emptyText)); return; }
      list.slice(0, LIST_MAX).forEach(function (v) {
        var meta = v.estimate ? 'no AIS ' + formatAge(v.silent_s) :
          (v.sanction ? str(v.sanction.programme) : '');
        var b = button('mar-item', vesselName(v) + (meta ? ' | ' + meta : ''));
        b.addEventListener('click', function () {
          flyTo(v.lon, v.lat, 200000);
          selectVessel(v);
        });
        target.appendChild(b);
      });
    }

    function showCard(lines, link) {
      m.card.replaceChildren();
      lines.forEach(function (ln) { m.card.appendChild(el('div', ln.cls || null, ln.text)); });
      var url = safeUrl(link);
      if (url) {
        var a = el('a', null, 'Check the listing');
        a.setAttribute('href', url);
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        m.card.appendChild(a);
      }
    }

    function selectVessel(v) {
      m.selected = v.mmsi;
      var flag = v.sanction || v.name_match;
      showCard(vesselCard(v), flag ? flag.url : '');
      note('Vessel: ' + vesselName(v) + (v.sanction ? ' (sanctioned)' :
        (v.estimate ? ' (dark, estimated)' : '')));
      if (m.enabled) drawVessels();
    }

    function selectChoke(cp) {
      var poly = cp.polygon || (chokepointById(cp.id) || {}).polygon || [];
      if (poly.length) { var c = centroid(poly); flyTo(c.lon, c.lat, 400000); }
      var lines = [{ text: cp.name, cls: 'title' }];
      if (str(cp.note)) lines.push({ text: cp.note, cls: '' });
      if (isNum(cp.inside_now)) {
        lines.push({ text: chokeLine(cp).replace(cp.name + ': ', ''), cls: '' });
        var days = cp.by_day && typeof cp.by_day === 'object' ? Object.keys(cp.by_day) : [];
        days.sort().slice(-7).forEach(function (d) {
          if (isNum(cp.by_day[d])) {
            lines.push({ text: d + ': ' + cp.by_day[d] + ' transits', cls: '' });
          }
        });
      } else {
        lines.push({ text: m.keyConfigured === false ? KEY_HINT :
          (ctx.isStatic ? STATIC_HINT : 'No live counts yet.'), cls: '' });
      }
      showCard(lines, '');
    }

    // -- data -------------------------------------------------------------

    function applyChokes(data) {
      if (data && Array.isArray(data.chokepoints) && data.chokepoints.length) {
        m.chokes = data.chokepoints.filter(function (cp) {
          return cp && typeof cp.id === 'string' && Array.isArray(cp.polygon);
        });
      }
      if (data && data.rules && str(data.rules.transit_rule)) {
        m.rule.textContent = 'Transits: ' + data.rules.transit_rule + ' Dark ships: ' +
          str(data.rules.dark_rule);
      }
      drawChokepoints();
    }

    function loadChokes() {
      var seq = m.seq;
      return getJson('/api/maritime/chokepoints').then(function (data) {
        if (seq !== m.seq || !m.enabled) return;
        m.keyConfigured = !!data.key_configured;
        applyChokes(data);
        if (!data.key_configured) {
          setStatus(KEY_HINT);
        } else if (data.error) {
          setStatus('AIS: ' + str(data.error));
        } else {
          setStatus(data.connected ? 'AISStream connected. Counting since ' +
            (str(data.counting_since) || str(data.server_started)) + '.' :
            'Connecting to AISStream; vessels appear over the next minutes.');
        }
        if (!m.announced) {
          m.announced = true;
          note('Maritime: ' + m.chokes.length + ' chokepoint monitors' +
            (data.key_configured ? '' : ' (no AIS key on the server)'));
        }
        return data;
      }).catch(function (err) {
        if (seq !== m.seq || !m.enabled) return;
        m.serverMissing = !!(err && err.status === 404);
        setStatus(m.serverMissing ? MISSING_HINT : 'Chokepoint counts did not load.');
        drawChokepoints();
      });
    }

    function viewState() {
      var rect = null;
      try { rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid); }
      catch (e) { rect = null; }
      return viewBox(rect);
    }

    function applyVessels(data) {
      if (!data.key_configured) {
        m.keyConfigured = false;
        setStatus(KEY_HINT);
        m.vessels = []; m.dark = [];
      } else {
        m.vessels = cleanVessels(data.vessels);
        m.dark = cleanVessels(data.dark).filter(function (v) { return v.estimate; });
      }
      var listed = m.vessels.filter(function (v) { return v.sanction; })
        .concat(m.dark.filter(function (v) { return v.sanction; }));
      listed.forEach(function (v) {
        if (m.noted[v.mmsi]) return;
        m.noted[v.mmsi] = true;
        note('Sanctioned vessel in view: ' + vesselName(v) + ' (' + str(v.sanction.source) +
          ', ' + str(v.sanction.programme) + ')');
      });
      m.summaryEl.textContent = withCommas(m.vessels.length) + ' vessels in view | ' +
        listed.length + ' sanctioned | ' + m.dark.length + ' dark (estimated)' +
        (data.capped ? ' | capped' : '');
      renderVesselList(m.listedList, listed, 'No sanctioned vessel in view.');
      renderVesselList(m.darkList, m.dark, 'No dark ship in view.');
      setCount(m.vessels.length + m.dark.length);
      drawVessels();
      if (!m.selected) return;
      var again = m.vessels.concat(m.dark).filter(function (v) {
        return v.mmsi === m.selected;
      })[0];
      if (again) {
        var flag = again.sanction || again.name_match;
        showCard(vesselCard(again), flag ? flag.url : '');
      }
    }

    function loadVessels() {
      if (!m.enabled || ctx.isStatic || m.serverMissing || m.keyConfigured === false) {
        return Promise.resolve(null);
      }
      var seq = m.seq;
      var box = viewState();
      return getJson('/api/maritime/vessels' + query([
        ['south', box.south], ['west', box.west], ['north', box.north], ['east', box.east],
        ['limit', VESSEL_LIMIT]
      ])).then(function (data) {
        if (seq !== m.seq || !m.enabled) return null;
        applyVessels(data);
        return data;
      }).catch(function (err) {
        if (seq !== m.seq || !m.enabled) return null;
        if (err && err.status === 404) { m.serverMissing = true; setStatus(MISSING_HINT); }
        else m.summaryEl.textContent = 'Vessels did not load.';
        return null;
      });
    }

    function scheduleVessels() {
      if (!m.enabled) return;
      if (m.moveTimer) global.clearTimeout(m.moveTimer);
      m.moveTimer = global.setTimeout(function () { m.moveTimer = null; loadVessels(); },
        MOVE_DEBOUNCE_MS);
    }

    function stopTimers() {
      ['vesselTimer', 'chokeTimer'].forEach(function (k) {
        if (m[k]) { global.clearInterval(m[k]); m[k] = null; }
      });
      if (m.moveTimer) { global.clearTimeout(m.moveTimer); m.moveTimer = null; }
    }

    function setEnabled(on) {
      on = !!on;
      if (on === m.enabled) return;
      m.enabled = on;
      m.seq++;
      stopTimers();
      if (!on) {
        if (m.layers) {
          showLayers(false);
          m.layers.points.removeAll();
          m.layers.labels.removeAll();
          m.layers.trails.removeAll();
        }
        m.vessels = []; m.dark = [];
        setCount(0);
        m.summaryEl.textContent = '';
        return;
      }
      ensureLayers();
      showLayers(true);
      drawChokepoints();
      if (ctx.isStatic) { setStatus(STATIC_HINT); return; }
      loadChokes().then(function () { return loadVessels(); });
      m.chokeTimer = global.setInterval(function () {
        if (m.keyConfigured !== false && !m.serverMissing) loadChokes();
      }, CHOKE_POLL_MS);
      m.vesselTimer = global.setInterval(function () { loadVessels(); }, VESSEL_POLL_MS);
    }

    m.setEnabled = setEnabled;
    m.loadVessels = loadVessels;
    m.loadChokes = loadChokes;
    m.selectVessel = selectVessel;
    m.selectChoke = selectChoke;

    try { viewer.camera.moveEnd.addEventListener(scheduleVessels); } catch (e) { /* keep going */ }

    renderChokeList();
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

  global.SDAMaritime = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { return current ? current.loadVessels() : null; },
    LAYER_ID: LAYER_ID,
    COUNT_ID: COUNT_ID,
    KEY_HINT: KEY_HINT,
    COLORS: COLORS,
    CHOKEPOINTS: CHOKEPOINTS,
    query: query,
    viewBox: viewBox,
    closedRing: closedRing,
    centroid: centroid,
    formatAge: formatAge,
    vesselStyle: vesselStyle,
    vesselLabel: vesselLabel,
    vesselCard: vesselCard,
    chokeLine: chokeLine,
    chokeLabel: chokeLabel,
    cleanVessels: cleanVessels,
    safeUrl: safeUrl
  };
})(typeof window !== 'undefined' ? window : this);
