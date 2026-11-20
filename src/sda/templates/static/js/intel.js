// Country intelligence on the WorldView globe: a card for any country you
// click, an instability heuristic per country (optional choropleth), WHO
// disease outbreaks, botnet C2 servers and internet outages per country, and
// convergence rings where three or more kinds of events meet within a day.
//
// Data: the server's /api/intel/* routes (sda/intel.py), which read Natural
// Earth borders (public domain), the World Bank (CC BY 4.0), WHO Disease
// Outbreak News, abuse.ch Feodo Tracker (CC0), IODA (Georgia Tech), the GDELT
// 2.0 event export and this application's own quake, fire, NWS and EONET feeds.
// The instability score is this application's heuristic; the card shows its
// formula and every term.
//
// Clicking: with the layer on, a click on empty globe asks the server which
// country is there (/api/intel/country-at) and opens its card. With the
// choropleth on, every country polygon carries its own sdaPick, which the
// page's click handler calls. Markers and rings carry sdaPick too.
//
// The card links to nearby public cameras (/api/cameras/nearest, opened with
// window.SDACameras.open when that module is loaded) and flies to the country.
//
// Static demo: there is no backend, so the panel says the live server is
// needed and nothing is fetched. Any endpoint that fails leaves its part empty.
//
// No text is ever set as HTML. Links are only followed when they are http(s).

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Country Intel';
  var STYLE_ID = 'sda-intel-style';
  var REFRESH_MS = 5 * 60 * 1000;
  var BORDER_TOL = 0.2;
  var CHORO_HEIGHT_M = 1500;
  var CHORO_ALPHA = 0.38;
  var RING_SEGMENTS = 48;
  var CAMERA_COUNT = 12;
  var SEARCH_LIMIT = 12;

  var LEVELS = [
    { from: 80, level: 'severe', color: '#d7191c' },
    { from: 60, level: 'high', color: '#fdae61' },
    { from: 40, level: 'elevated', color: '#ffe680' },
    { from: 20, level: 'guarded', color: '#abdda4' },
    { from: 0, level: 'low', color: '#2b83ba' }
  ];

  var LAYERS = [
    { key: 'choropleth', label: 'INSTABILITY', title: 'Colour countries by the instability heuristic', on: false },
    { key: 'outbreaks', label: 'OUTBREAKS', title: 'WHO Disease Outbreak News, last 90 days', on: true },
    { key: 'cyber', label: 'C2', title: 'Botnet C2 servers per country (abuse.ch Feodo Tracker)', on: true },
    { key: 'outages', label: 'OUTAGES', title: 'Internet outage alerts per country, 24 h (IODA)', on: true },
    { key: 'convergence', label: 'CONVERGE', title: 'Cells where three or more event kinds meet in 24 h', on: true }
  ];

  var COLORS = {
    outbreak: '#ff4fd8',
    cyber: '#4fd1ff',
    outageCritical: '#ff3b30',
    outageWarning: '#ffb000',
    ring: '#ff7a1a'
  };

  var KIND_SHORT = {
    quake: 'QUAKE', fire: 'FIRE', eonet: 'EONET', weather: 'WX', gdelt: 'NEWS',
    outage: 'OUTAGE', outbreak: 'OUTBREAK'
  };

  var TERM_LABELS = {
    unrest_volume: 'news unrest volume', unrest_share: 'news unrest share',
    news_tone: 'news tone', internet_outages: 'internet outages', earthquakes: 'earthquakes',
    fires: 'fires', weather_alerts: 'weather alerts', outbreaks: 'outbreaks'
  };

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

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

  function levelFor(score) {
    var s = isNum(score) ? score : 0;
    for (var i = 0; i < LEVELS.length; i++) {
      if (s >= LEVELS[i].from) return LEVELS[i];
    }
    return LEVELS[LEVELS.length - 1];
  }

  function withCommas(n) {
    return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  // "$1.04 trillion", "$415.2 billion", "$12.5 million", "$114,769"
  function formatUsd(v) {
    if (!isNum(v)) return 'n/a';
    var a = Math.abs(v);
    if (a >= 1e12) return '$' + (v / 1e12).toFixed(2) + ' trillion';
    if (a >= 1e9) return '$' + (v / 1e9).toFixed(1) + ' billion';
    if (a >= 1e6) return '$' + (v / 1e6).toFixed(1) + ' million';
    return '$' + withCommas(v);
  }

  function formatPeople(v) {
    if (!isNum(v)) return 'n/a';
    if (v >= 1e9) return (v / 1e9).toFixed(2) + ' billion';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + ' million';
    return withCommas(v);
  }

  function formatIndicator(ind) {
    if (!ind || !isNum(ind.value)) return 'n/a';
    if (ind.key === 'population') return formatPeople(ind.value);
    if (ind.key === 'inflation_pct') return ind.value.toFixed(1) + '%';
    return formatUsd(ind.value);
  }

  // Only http(s) links leave the page.
  function safeUrl(u) {
    return (typeof u === 'string' && /^https?:\/\/[^\s]+$/i.test(u)) ? u : '';
  }

  // A small circle as [lon, lat, ...] degrees, on a sphere (great-circle
  // destination from the centre at each bearing). Closed: last equals first.
  function ringPositions(lat, lon, radiusKm, segments) {
    var n = segments || RING_SEGMENTS;
    var R = 6371.0088;
    var d = radiusKm / R;
    var p1 = lat * Math.PI / 180;
    var l1 = lon * Math.PI / 180;
    var out = [];
    for (var i = 0; i <= n; i++) {
      var brg = 2 * Math.PI * (i % n) / n;
      var p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(brg));
      var l2 = l1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(p1),
        Math.cos(d) - Math.sin(p1) * Math.sin(p2));
      var lonDeg = ((l2 * 180 / Math.PI + 540) % 360) - 180;
      out.push(round3(lonDeg), round3(p2 * 180 / Math.PI));
    }
    return out;
  }

  function cellRadiusKm(cellDeg) {
    return Math.max(40, (isNum(cellDeg) ? cellDeg : 2) * 111.2 * 0.45);
  }

  function kindsText(kinds) {
    return (kinds || []).map(function (k) { return KIND_SHORT[k] || String(k).toUpperCase(); })
      .join(' + ');
  }

  // Non-zero terms, largest first: [[name, points], ...]
  function termList(terms) {
    var out = [];
    Object.keys(terms || {}).forEach(function (k) {
      if (isNum(terms[k]) && terms[k] > 0) out.push([k, terms[k]]);
    });
    out.sort(function (a, b) { return b[1] - a[1]; });
    return out;
  }

  function exchangeLine(ex) {
    if (!ex) return '';
    var state = ex.open ? 'OPEN' : String(ex.state || 'closed').toUpperCase();
    return ex.name + ': ' + state + ' (' + ex.local_time + ' local), ' + ex.next +
      ' [' + ex.sessions + ' ' + ex.days + ']';
  }

  function eventsLine(ev) {
    if (!ev) return '';
    return [
      'quakes ' + (ev.quakes || 0), 'fires ' + (ev.fires || 0),
      'NWS alerts ' + (ev.weather_alerts || 0), 'EONET ' + (ev.eonet || 0),
      'GDELT events ' + (ev.gdelt_events || 0) + ' (unrest ' + (ev.gdelt_unrest || 0) + ')',
      'outage sources ' + (ev.outage_sources || 0),
      'WHO outbreaks 30 d ' + (ev.outbreaks_30d || 0)
    ].join(' | ');
  }

  function localTimeLine(lt) {
    if (!lt) return 'unknown';
    return lt.time + ' ' + lt.weekday + ' ' + lt.date + ' (UTC' + lt.utc_offset + ', ' +
      lt.zone + ')';
  }

  function malwareText(malware) {
    return Object.keys(malware || {}).sort(function (a, b) { return malware[b] - malware[a]; })
      .map(function (k) { return k + ' ' + malware[k]; }).join(', ');
  }

  function outageColor(o) {
    return o && o.critical > 0 ? COLORS.outageCritical : COLORS.outageWarning;
  }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.intel-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 4px;',
    '  line-height: 1.4; }',
    '.intel-row { display: flex; flex-wrap: wrap; gap: 3px; margin-bottom: 5px; }',
    '.intel-row .mode-btn { flex: 1 1 30%; font-size: 8px; padding: 3px 2px; }',
    '.intel-search { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.6);',
    '  border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000); font: inherit;',
    '  font-size: 9px; padding: 3px 5px; margin-bottom: 5px; }',
    '.intel-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px; }',
    '.intel-notes { font-size: 8px; color: var(--hud-dim, #7a5500); line-height: 1.3; }',
    '.intel-list { max-height: 140px; overflow-y: auto; margin-top: 4px; }',
    '.intel-item { display: block; width: 100%; text-align: left; background: none; border: 0;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 3px 2px; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.intel-item:hover { background: rgba(255,176,0,0.08); }',
    '.intel-card { margin-top: 6px; border-top: 1px solid rgba(255,176,0,0.25);',
    '  padding-top: 5px; font-size: 9px; line-height: 1.45; color: var(--hud-color, #ffb000); }',
    '.intel-card h4 { margin: 0 0 3px 0; font-size: 11px; letter-spacing: 0.5px; }',
    '.intel-sec { margin-top: 5px; }',
    '.intel-sec-title { color: var(--hud-dim, #7a5500); font-size: 8px; letter-spacing: 1px; }',
    '.intel-score { font-size: 12px; font-weight: bold; }',
    '.intel-swatch { display: inline-block; width: 8px; height: 8px; margin-right: 4px;',
    '  border: 1px solid #000; vertical-align: middle; }',
    '.intel-legend { display: flex; flex-wrap: wrap; gap: 6px; font-size: 8px;',
    '  color: var(--hud-dim, #7a5500); margin-bottom: 4px; }',
    '.intel-formula { font-size: 8px; color: var(--hud-dim, #7a5500); white-space: pre-wrap; }',
    '.intel-card a { color: #8fd3ff; }'
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

  function link(text, url) {
    var safe = safeUrl(url);
    if (!safe) return el('span', null, text);
    var a = el('a', null, text);
    a.setAttribute('href', safe);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  function section(title) {
    var s = el('div', 'intel-sec');
    s.appendChild(el('div', 'intel-sec-title', title));
    return s;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, timer: null, seq: 0, show: {},
      countries: null, borders: null, scores: {}, cells: [], cellDeg: 2,
      outbreaks: [], cyber: [], outages: [], card: null, choro: null,
      points: null, labels: null, rings: null, handler: null, cardSeq: 0
    };
    LAYERS.forEach(function (l) { m.show[l.key] = l.on; });

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
    m.summaryEl = el('div', 'intel-summary',
      'Turn on the Country Intel layer, then click any country for its card.');
    var layerRow = el('div', 'intel-row');
    m.layerBtns = {};
    LAYERS.forEach(function (l) {
      var b = button('mode-btn' + (l.on ? ' active' : ''), l.label, l.title);
      b.setAttribute('aria-pressed', l.on ? 'true' : 'false');
      b.addEventListener('click', function () { setSublayer(l.key, !m.show[l.key]); });
      layerRow.appendChild(b);
      m.layerBtns[l.key] = b;
    });
    var legend = el('div', 'intel-legend');
    LEVELS.slice().reverse().forEach(function (lv) {
      var item = el('span');
      var sw = el('span', 'intel-swatch');
      sw.style.background = lv.color;
      item.appendChild(sw);
      item.appendChild(el('span', null, lv.level + ' ' + lv.from + '+'));
      legend.appendChild(item);
    });
    m.search = el('input', 'intel-search');
    m.search.setAttribute('type', 'search');
    m.search.setAttribute('placeholder', 'Find a country...');
    m.search.setAttribute('maxlength', '60');
    m.status = el('div', 'intel-status');
    m.status.setAttribute('role', 'status');
    m.notes = el('div', 'intel-notes');
    m.list = el('div', 'intel-list');
    m.cardEl = el('div', 'intel-card');
    [m.summaryEl, layerRow, legend, m.search, m.status, m.notes, m.list, m.cardEl]
      .forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.status.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById('count-intel');
        if (c) c.textContent = String(n);
      } catch (e) { /* keep going */ }
    }

    function showNotes(sources) {
      var lines = [];
      Object.keys(sources || {}).forEach(function (k) {
        var s = sources[k];
        if (s && !s.ok && s.note) lines.push(k + ': ' + s.note);
      });
      m.notes.textContent = lines.join('\n');
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.points) return;
      var prims = viewer.scene.primitives;
      m.rings = prims.add(new C.PolylineCollection());
      m.points = prims.add(new C.PointPrimitiveCollection());
      m.labels = prims.add(new C.LabelCollection());
    }

    function flyTo(lat, lon, height) {
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(lon, lat, height || 3000000),
          duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function countryPick(iso3, extra) {
      var pick = { type: 'intel-country', iso3: iso3,
        sdaPick: function () { openCard(iso3); } };
      if (extra) Object.keys(extra).forEach(function (k) { pick[k] = extra[k]; });
      return pick;
    }

    function addPoint(lat, lon, color, size, pick, text) {
      var pos = C.Cartesian3.fromDegrees(lon, lat, 0);
      m.points.add({
        position: pos, pixelSize: size, color: C.Color.fromCssColorString(color),
        outlineColor: C.Color.BLACK, outlineWidth: 1, id: pick
      });
      if (text) {
        m.labels.add({
          position: pos, text: text, font: '10px monospace',
          fillColor: C.Color.fromCssColorString(color), outlineColor: C.Color.BLACK,
          outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.LEFT,
          verticalOrigin: C.VerticalOrigin.CENTER,
          pixelOffset: new C.Cartesian2(8, 0), id: pick
        });
      }
    }

    function drawMarkers() {
      if (!m.points) return;
      m.points.removeAll();
      m.labels.removeAll();
      m.rings.removeAll();
      var n = 0;
      if (m.show.outbreaks) {
        var seen = {};
        m.outbreaks.forEach(function (o) {
          (o.countries || []).forEach(function (c) {
            if (seen[c.iso3] || !isNum(c.lat) || !isNum(c.lon)) return;
            seen[c.iso3] = true;
            n++;
            addPoint(c.lat + 0.6, c.lon, COLORS.outbreak, 10,
              countryPick(c.iso3, { kind: 'outbreak', title: o.title }),
              o.disease + ' ' + String(o.published || '').slice(0, 10));
          });
        });
      }
      if (m.show.cyber) {
        m.cyber.forEach(function (c) {
          if (!isNum(c.lat) || !isNum(c.lon)) return;
          n++;
          addPoint(c.lat, c.lon + 0.9, COLORS.cyber, Math.min(16, 6 + 2 * Math.sqrt(c.servers)),
            countryPick(c.iso3, { kind: 'cyber' }), null);
        });
      }
      if (m.show.outages) {
        m.outages.forEach(function (o) {
          if (!isNum(o.lat) || !isNum(o.lon)) return;
          n++;
          addPoint(o.lat - 0.6, o.lon, outageColor(o), 9,
            countryPick(o.iso3, { kind: 'outage' }),
            'OUTAGE ' + (o.critical ? o.critical + ' crit' : o.warning + ' warn'));
        });
      }
      if (m.show.convergence) {
        m.cells.forEach(function (cell) {
          if (!isNum(cell.lat) || !isNum(cell.lon)) return;
          n++;
          var pick = { type: 'intel-convergence', id: cell.id,
            sdaPick: function () { showCell(cell); } };
          var color = C.Color.fromCssColorString(COLORS.ring)
            .withAlpha(0.45 + 0.5 * Math.min(1, (cell.score || 0) / 100));
          m.rings.add({
            positions: C.Cartesian3.fromDegreesArray(
              ringPositions(cell.lat, cell.lon, cellRadiusKm(m.cellDeg))),
            width: 2 + Math.min(3, (cell.kinds || []).length - 2), material:
              C.Material.fromType('Color', { color: color }), id: pick
          });
          m.labels.add({
            position: C.Cartesian3.fromDegrees(cell.lon, cell.lat, 0),
            text: kindsText(cell.kinds), font: '10px monospace',
            fillColor: C.Color.fromCssColorString(COLORS.ring), outlineColor: C.Color.BLACK,
            outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
            horizontalOrigin: C.HorizontalOrigin.CENTER,
            verticalOrigin: C.VerticalOrigin.CENTER, id: pick
          });
        });
      }
      setCount(n);
    }

    function removeChoropleth() {
      if (m.choro) {
        try { viewer.scene.primitives.remove(m.choro); } catch (e) { /* keep going */ }
        m.choro = null;
      }
    }

    // One Primitive, one instance per polygon, coloured by level. Alpha stays
    // under 1 so the fill draws in the translucent pass with the markers.
    function drawChoropleth() {
      removeChoropleth();
      if (!m.enabled || !m.show.choropleth || !m.borders) return;
      if (!C.Primitive || !C.GeometryInstance || !C.PolygonGeometry || !C.PolygonHierarchy ||
          !C.PerInstanceColorAppearance || !C.ColorGeometryInstanceAttribute) return;
      var instances = [];
      (m.borders.countries || []).forEach(function (country) {
        var s = m.scores[country.iso3];
        var color = C.Color.fromCssColorString(levelFor(s ? s.score : 0).color)
          .withAlpha(s ? CHORO_ALPHA : 0.12);
        (country.polygons || []).forEach(function (rings) {
          if (!rings || !rings.length || rings[0].length < 6) return;
          try {
            var holes = rings.slice(1).filter(function (r) { return r.length >= 6; })
              .map(function (r) { return new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(r)); });
            instances.push(new C.GeometryInstance({
              geometry: new C.PolygonGeometry({
                polygonHierarchy: new C.PolygonHierarchy(
                  C.Cartesian3.fromDegreesArray(rings[0]), holes),
                height: CHORO_HEIGHT_M
              }),
              id: countryPick(country.iso3, { kind: 'country', name: country.name }),
              attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(color) }
            }));
          } catch (e) { /* skip a polygon Cesium will not take */ }
        });
      });
      if (!instances.length) return;
      m.choro = viewer.scene.primitives.add(new C.Primitive({
        geometryInstances: instances,
        appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }),
        asynchronous: true
      }));
    }

    // -- data -------------------------------------------------------------

    function loadAll() {
      if (!m.enabled || ctx.isStatic) return Promise.resolve(null);
      var seq = ++m.seq;
      setStatus('Loading country intel...');
      var jobs = [
        getJson('/api/intel/instability').then(function (d) {
          if (seq !== m.seq) return;
          m.scores = {};
          (d.countries || []).forEach(function (c) { m.scores[c.iso3] = c; });
          showNotes(d.sources);
          showTop(d.countries || []);
        }),
        getJson('/api/intel/outbreaks').then(function (d) {
          if (seq === m.seq) m.outbreaks = d.outbreaks || [];
        }),
        getJson('/api/intel/cyber').then(function (d) {
          if (seq === m.seq) m.cyber = d.countries || [];
        }),
        getJson('/api/intel/outages').then(function (d) {
          if (seq === m.seq) m.outages = d.countries || [];
        }),
        getJson('/api/intel/convergence' + query([['cell_deg', m.cellDeg]])).then(function (d) {
          if (seq === m.seq) m.cells = d.cells || [];
        })
      ];
      var settled = jobs.map(function (p) {
        return p.then(function () { return true; }, function () { return false; });
      });
      return Promise.all(settled).then(function (ok) {
        if (seq !== m.seq || !m.enabled) return null;
        var good = ok.filter(Boolean).length;
        drawMarkers();
        if (m.show.choropleth) loadBorders();
        setStatus(good ? m.outbreaks.length + ' outbreak reports, ' + m.outages.length +
          ' countries with outage alerts, convergence cells: ' + m.cells.length + '.'
          : 'Country intel did not load.');
        if (good) {
          note('Country intel: convergence cells ' + m.cells.length +
            ', countries with outage alerts ' + m.outages.length);
        }
        return good;
      });
    }

    function loadBorders() {
      if (m.borders) { drawChoropleth(); return Promise.resolve(m.borders); }
      return getJson('/api/intel/borders' + query([['tol', BORDER_TOL]])).then(function (b) {
        m.borders = b;
        drawChoropleth();
        return b;
      }).catch(function () { setStatus('Country borders did not load.'); });
    }

    function loadCountries() {
      if (m.countries) return Promise.resolve(m.countries);
      return getJson('/api/intel/countries').then(function (d) {
        m.countries = d.countries || [];
        return m.countries;
      });
    }

    function showTop(rows) {
      m.list.replaceChildren();
      rows.slice(0, 10).forEach(function (r) {
        if (!isNum(r.score) || r.score <= 0) return;
        var b = button('intel-item');
        var sw = el('span', 'intel-swatch');
        sw.style.background = levelFor(r.score).color;
        b.appendChild(sw);
        b.appendChild(el('span', null, r.name + '  ' + r.score.toFixed(1) + ' ' + r.level));
        b.addEventListener('click', function () { openCard(r.iso3); });
        m.list.appendChild(b);
      });
    }

    function showCell(cell) {
      m.cardSeq++;
      m.cardEl.replaceChildren();
      m.cardEl.appendChild(el('h4', null, 'Convergence: ' + kindsText(cell.kinds)));
      m.cardEl.appendChild(el('div', null, 'Score ' + cell.score + ' / 100, ' + cell.events +
        ' events within 24 h in this ' + m.cellDeg + ' degree cell.'));
      (cell.labels || []).forEach(function (lbl, i) {
        var kind = cell.kinds[i];
        var count = cell.counts ? cell.counts[kind] : 0;
        var line = lbl + (count ? ': ' + count : '');
        var samples = (cell.samples && cell.samples[kind]) || [];
        if (samples.length) line += ' (' + samples.join('; ') + ')';
        m.cardEl.appendChild(el('div', null, line));
      });
      (cell.countries || []).forEach(function (iso3) {
        var b = button('intel-item', 'Open ' + iso3 + ' country card');
        b.addEventListener('click', function () { openCard(iso3); });
        m.cardEl.appendChild(b);
      });
    }

    // -- the country card ---------------------------------------------------

    function openCard(code) {
      if (ctx.isStatic) { setStatus('Country intel needs the live server.'); return null; }
      var seq = ++m.cardSeq;
      setStatus('Loading ' + code + '...');
      return getJson('/api/intel/country/' + encodeURIComponent(code)).then(function (card) {
        if (seq !== m.cardSeq) return null;
        m.card = card;
        renderCard(card);
        setStatus('');
        note('Country: ' + card.name + ', instability ' + card.instability.score);
        return card;
      }).catch(function () {
        if (seq === m.cardSeq) setStatus('No card for ' + code + '.');
        return null;
      });
    }

    function renderCard(card) {
      var root = m.cardEl;
      root.replaceChildren();
      root.appendChild(el('h4', null, card.name + ' (' + card.iso3 + ')'));
      var actions = el('div', 'intel-row');
      var fly = button('mode-btn', 'FLY TO', 'Fly to ' + card.name);
      fly.addEventListener('click', function () {
        var v = card.view || {};
        flyTo(v.lat, v.lon, v.height_m);
      });
      var cams = button('mode-btn', 'CAMERAS', 'Public cameras nearest the capital');
      cams.addEventListener('click', function () { loadCameras(card); });
      actions.appendChild(fly);
      actions.appendChild(cams);
      root.appendChild(actions);
      root.appendChild(el('div', null, [card.continent, card.subregion, card.region,
        card.income_group].filter(Boolean).join(' | ')));
      var cap = card.capital ? 'Capital ' + card.capital.name + '. ' : '';
      root.appendChild(el('div', null, cap + 'Local time ' + localTimeLine(card.local_time)));

      var inst = card.instability || {};
      var lv = levelFor(inst.score);
      var sec = section('INSTABILITY (THIS APPLICATION\'S HEURISTIC)');
      var score = el('div', 'intel-score', (isNum(inst.score) ? inst.score.toFixed(1) : '0') +
        ' / 100 ' + String(inst.level || lv.level).toUpperCase());
      score.style.color = lv.color;
      sec.appendChild(score);
      var terms = termList(inst.terms);
      sec.appendChild(el('div', null, terms.length
        ? terms.map(function (t) { return (TERM_LABELS[t[0]] || t[0]) + ' ' + t[1]; }).join(', ')
        : 'No term above zero in the current feeds.'));
      var formulaBtn = button('mode-btn', 'FORMULA', 'Show how the score is computed');
      var formula = el('div', 'intel-formula', (inst.formula || []).join('\n') +
        (inst.note ? '\n' + inst.note : ''));
      formula.style.display = 'none';
      formulaBtn.addEventListener('click', function () {
        formula.style.display = formula.style.display === 'none' ? 'block' : 'none';
      });
      sec.appendChild(formulaBtn);
      sec.appendChild(formula);
      root.appendChild(sec);

      sec = section('LAST 24 HOURS (THIS APPLICATION\'S FEEDS)');
      sec.appendChild(el('div', null, eventsLine(card.events_24h)));
      root.appendChild(sec);

      sec = section('ECONOMY');
      (card.indicators || []).forEach(function (ind) {
        sec.appendChild(el('div', null, ind.label + ': ' + formatIndicator(ind) +
          (ind.year ? ' (' + ind.year + ')' : '')));
      });
      sec.appendChild(el('div', 'intel-notes', card.indicators_source || ''));
      root.appendChild(sec);

      sec = section('STOCK EXCHANGES');
      if (card.exchanges && card.exchanges.length) {
        card.exchanges.forEach(function (ex) { sec.appendChild(el('div', null, exchangeLine(ex))); });
        sec.appendChild(el('div', 'intel-notes', card.exchanges_note || ''));
      } else {
        sec.appendChild(el('div', null, 'No major exchange in the table for this country.'));
      }
      root.appendChild(sec);

      if (card.outbreaks && card.outbreaks.length) {
        sec = section('WHO DISEASE OUTBREAK NEWS');
        card.outbreaks.forEach(function (o) {
          var row = el('div');
          row.appendChild(el('span', null, String(o.published || '').slice(0, 10) + ' '));
          row.appendChild(link(o.title, o.url));
          sec.appendChild(row);
        });
        root.appendChild(sec);
      }
      if (card.outages) {
        sec = section('INTERNET OUTAGES (IODA, 24 H)');
        var o = card.outages;
        var row2 = el('div', null, o.critical + ' critical and ' + o.warning +
          ' warning data sources (' + (o.datasources || []).join(', ') + ')' +
          (o.ongoing ? ', still alerting. ' : ', recovered. '));
        row2.appendChild(link('IODA dashboard', o.url));
        sec.appendChild(row2);
        root.appendChild(sec);
      }
      if (card.cyber) {
        sec = section('BOTNET C2 SERVERS (FEODO TRACKER)');
        sec.appendChild(el('div', null, card.cyber.servers + ' listed, ' + card.cyber.online +
          ' online. ' + malwareText(card.cyber.malware)));
        root.appendChild(sec);
      }
      m.camList = el('div', 'intel-list');
      root.appendChild(m.camList);
      root.appendChild(el('div', 'intel-notes', (card.attribution || []).join('. ')));
    }

    function loadCameras(card) {
      var c = card.cameras || {};
      if (!isNum(c.lat) || !isNum(c.lon)) return null;
      setStatus('Looking for public cameras...');
      return getJson('/api/cameras/nearest' + query([
        ['lat', round3(c.lat)], ['lon', round3(c.lon)], ['n', CAMERA_COUNT],
        ['radius_km', c.radius_km]
      ])).then(function (d) {
        var list = d.cameras || [];
        m.camList.replaceChildren();
        if (!list.length) { setStatus('No public camera within ' + c.radius_km + ' km.'); return list; }
        setStatus(list.length + ' public cameras nearest ' + card.name + '.');
        list.forEach(function (cam) {
          var b = button('intel-item', cam.name + (cam.city ? ', ' + cam.city : '') +
            (isNum(cam.distance_km) ? ' (' + cam.distance_km.toFixed(0) + ' km)' : ''));
          b.addEventListener('click', function () {
            flyTo(cam.lat, cam.lon, 20000);
            var S = global.SDACameras;
            if (S && typeof S.open === 'function') {
              try { S.open(cam.rid); } catch (e) { /* keep going */ }
            }
          });
          m.camList.appendChild(b);
        });
        return list;
      }).catch(function () {
        setStatus('The camera catalog did not answer (python -m sda.cameras import builds it).');
        return null;
      });
    }

    // -- clicks on empty globe ------------------------------------------------

    function onGlobeClick(click) {
      if (!m.enabled || ctx.isStatic) return null;
      if (global.SDAMapTools && typeof global.SDAMapTools.isActive === 'function' &&
          global.SDAMapTools.isActive()) return null;
      var picked = null;
      try { picked = viewer.scene.pick(click.position); } catch (e) { picked = null; }
      if (picked && picked.id) return null; // something else owns this click
      var carto = null;
      try {
        var hit = viewer.camera.pickEllipsoid(click.position, viewer.scene.globe.ellipsoid);
        if (hit) carto = C.Cartographic.fromCartesian(hit);
      } catch (e) { carto = null; }
      if (!carto) return null;
      var lat = carto.latitude * 180 / Math.PI;
      var lon = carto.longitude * 180 / Math.PI;
      return getJson('/api/intel/country-at' + query([['lat', round3(lat)], ['lon', round3(lon)]]))
        .then(function (c) { return openCard(c.iso3); })
        .catch(function () { setStatus('No country there.'); return null; });
    }

    function wireClicks() {
      if (m.handler || !C.ScreenSpaceEventHandler) return;
      try {
        m.handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
        m.handler.setInputAction(onGlobeClick, C.ScreenSpaceEventType.LEFT_CLICK);
      } catch (e) { m.handler = null; }
    }

    // -- search -------------------------------------------------------------

    m.search.addEventListener('input', function () {
      var q = String(m.search.value || '').trim().toLowerCase();
      if (q.length < 2) { m.list.replaceChildren(); return; }
      if (ctx.isStatic) { setStatus('Country intel needs the live server.'); return; }
      loadCountries().then(function (all) {
        var hits = all.filter(function (c) {
          return c.name.toLowerCase().indexOf(q) === 0 || c.iso3.toLowerCase() === q ||
            (c.iso2 && c.iso2.toLowerCase() === q);
        }).slice(0, SEARCH_LIMIT);
        m.list.replaceChildren();
        if (!hits.length) { setStatus('No country named like "' + q + '".'); return; }
        hits.forEach(function (c) {
          var b = button('intel-item', c.name + ' (' + c.iso3 + ')');
          b.addEventListener('click', function () { openCard(c.iso3); });
          m.list.appendChild(b);
        });
      }).catch(function () { setStatus('The country list did not load.'); });
    });

    // -- enable / disable -------------------------------------------------------

    function setSublayer(key, on) {
      m.show[key] = !!on;
      var b = m.layerBtns[key];
      if (b) {
        b.className = 'mode-btn' + (on ? ' active' : '');
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      if (!m.enabled) return;
      if (key === 'choropleth') {
        if (on) loadBorders(); else removeChoropleth();
      } else {
        drawMarkers();
      }
    }

    function setEnabled(on) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'Country intel needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (on) {
        ensureLayers();
        wireClicks();
        m.points.show = m.labels.show = m.rings.show = true;
        loadAll();
        if (!m.timer) m.timer = global.setInterval(function () { loadAll(); }, REFRESH_MS);
      } else {
        if (m.timer) { global.clearInterval(m.timer); m.timer = null; }
        m.seq++;
        if (m.points) {
          m.points.removeAll(); m.labels.removeAll(); m.rings.removeAll();
          m.points.show = m.labels.show = m.rings.show = false;
        }
        removeChoropleth();
        setCount(0);
        setStatus('');
      }
    }

    m.setEnabled = setEnabled;
    m.loadAll = loadAll;
    m.openCard = openCard;
    m.onGlobeClick = onGlobeClick;
    m.setSublayer = setSublayer;
    m.loadCameras = loadCameras;
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

  global.SDAIntel = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { return current ? current.loadAll() : null; },
    openCountry: function (code) { return current ? current.openCard(code) : null; },
    setSublayer: function (key, on) { if (current) current.setSublayer(key, on); },
    _module: function () { return current; },
    LEVELS: LEVELS,
    LAYERS: LAYERS,
    query: query,
    levelFor: levelFor,
    formatUsd: formatUsd,
    formatPeople: formatPeople,
    formatIndicator: formatIndicator,
    safeUrl: safeUrl,
    ringPositions: ringPositions,
    cellRadiusKm: cellRadiusKm,
    kindsText: kindsText,
    termList: termList,
    exchangeLine: exchangeLine,
    eventsLine: eventsLine,
    localTimeLine: localTimeLine,
    malwareText: malwareText
  };
})(typeof window !== 'undefined' ? window : this);
