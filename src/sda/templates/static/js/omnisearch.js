// Universal search for the WorldView globe: one box for coordinates, places,
// satellites (name or NORAD id), aircraft (callsign, registration or ICAO
// hex), public cameras and named summits. Picking a result flies the globe
// there and opens what belongs to it (the satellite is selected, the camera
// viewer opens, the summit card opens).
//
// Where the answers come from:
//   GET /api/search            cameras, peaks, places, cached aircraft and the
//                              server's TLE catalog (routes/search.py)
//   SDASatSearch.rank          the satellites the globe is drawing right now,
//                              with their live positions (satsearch.js)
//   SDAMapTools.parseCoordinate decimal, DMS, UTM, MGRS, plus codes, map URLs
//   viewer.entities            aircraft on the globe (properties.type 'aircraft')
//
// While typing, the server is asked without places (places=0): the place
// geocoder is Nominatim behind /api/geocode, whose usage policy rules out
// search-as-you-type. Enter runs the full search (places=1, and one live
// adsb.lol lookup for an aircraft id, live=1); Enter again, or Enter on an
// exact hit, flies to the highlighted row. Without the server (the static
// demo) the local sources still answer and Enter hands places to map tools.
//
// The page's "/" box (#search-box) is adopted: its input is swapped for a copy
// (same id, same styling) so the page's own per-keystroke geocoder listener
// goes away and this one answers in its place. The mount gets a compact panel
// with the same search for touch screens and anyone who prefers the sidebar.
//
// Page wiring: list it in EXTENSIONS after satsearch, maptools, cameras,
// peaks and summit, with a mount div. The page's "/" key keeps calling
// openSearch(), which now focuses this box.

(function (global) {
  'use strict';

  var STYLE_ID = 'sda-omnisearch-style';
  var DEFAULT_LIMIT = 12;
  var TYPE_DELAY_MS = 180;
  var CACHE_TTL_MS = 60000;
  var CACHE_MAX = 60;
  var KINDS = ['coordinate', 'satellite', 'aircraft', 'camera', 'peak', 'place'];
  var LABELS = {
    coordinate: 'COORD', satellite: 'SAT', aircraft: 'AIRCRAFT', camera: 'CAMERA',
    peak: 'PEAK', place: 'PLACE'
  };
  var TIER = { EXACT: 0, PREFIX: 1, WORD: 2, SUBSTRING: 3, OTHER: 4 };
  var ALPHA5 = /^[A-HJ-NP-Z]\d{4}$/;
  var HEX = /^[0-9a-f]{6}$/;
  var CALLSIGN = /^[A-Z]{2,3}\d[0-9A-Z]{0,4}$/;
  var REGISTRATION = /^(?:[A-Z0-9]{1,2}-[A-Z0-9]{2,5}|N\d[0-9A-Z]{0,4})$/;

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests); the match rules mirror
  // routes/search.py so the server and the page rank alike.
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  // Lower case, accents dropped, every run of punctuation one space.
  function fold(text) {
    if (typeof text !== 'string') return '';
    var s = text;
    try { s = s.normalize('NFKD').replace(/[\u0300-\u036f]/g, ''); } catch (e) { /* old engine */ }
    return s.toLowerCase().replace(/[^0-9a-z\u00c0-\uffff]+/g, ' ').trim();
  }

  function nameTier(name, q) {
    if (!name || !q) return -1;
    if (name === q) return TIER.EXACT;
    if (name.indexOf(q) === 0) return TIER.PREFIX;
    if (name.indexOf(' ' + q) >= 0) return TIER.WORD;
    if (name.indexOf(q) >= 0) return TIER.SUBSTRING;
    return -1;
  }

  function canonicalId(text) {
    return String(text == null ? '' : text).replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  }

  function queryShape(raw) {
    var text = String(raw == null ? '' : raw).trim();
    var upper = text.toUpperCase();
    return {
      digits: /^\d+$/.test(text),
      alpha5: ALPHA5.test(upper),
      hex: HEX.test(text.toLowerCase()) && /\d/.test(text),
      callsign: CALLSIGN.test(upper.replace(/ /g, '')),
      registration: REGISTRATION.test(upper)
    };
  }

  function kindOrder(raw) {
    var s = queryShape(raw);
    if (s.digits || s.alpha5) return ['coordinate', 'satellite', 'aircraft', 'camera', 'peak', 'place'];
    if (s.callsign || s.registration || s.hex) {
      return ['coordinate', 'aircraft', 'satellite', 'camera', 'peak', 'place'];
    }
    return ['coordinate', 'place', 'peak', 'camera', 'satellite', 'aircraft'];
  }

  // Hex exact, then callsign or registration exact, prefix, substring: the
  // order of rankContactMatch in gods-eye-view src/data/contactMatch.js (MIT),
  // as routes/search.py has it.
  function aircraftTier(rec, raw) {
    var q = canonicalId(raw);
    if (!q) return -1;
    var hex = String(rec.icao24 || '').trim().toLowerCase();
    if (hex && hex === String(raw).trim().toLowerCase()) return TIER.EXACT;
    var call = canonicalId(rec.callsign);
    var reg = canonicalId(rec.registration);
    var tests = [
      [TIER.EXACT, function (v) { return v === q; }],
      [TIER.PREFIX, function (v) { return v.indexOf(q) === 0; }],
      [TIER.SUBSTRING, function (v) { return q.length >= 3 && v.indexOf(q) >= 0; }]
    ];
    for (var i = 0; i < tests.length; i++) {
      if ((call && tests[i][1](call)) || (reg && tests[i][1](reg))) return tests[i][0];
    }
    return -1;
  }

  // One list: best tier first, then kind order for this query; every kind
  // with hits keeps a share of the limit, the way the server merges.
  function mergeResults(rows, raw, limit) {
    var order = kindOrder(raw);
    var max = isNum(limit) && limit > 0 ? Math.floor(limit) : DEFAULT_LIMIT;
    var pool = rows.map(function (r, i) {
      var k = order.indexOf(r.kind);
      return { tier: isNum(r.tier) ? r.tier : TIER.OTHER, kind: k < 0 ? order.length : k, i: i, r: r };
    });
    function cmp(a, b) { return (a.tier - b.tier) || (a.kind - b.kind) || (a.i - b.i); }
    pool.sort(cmp);
    var live = {};
    pool.forEach(function (p) { live[p.r.kind] = true; });
    var share = Math.max(2, Math.floor(max / Math.max(1, Object.keys(live).length)));
    var picked = [];
    var rest = [];
    var taken = {};
    pool.forEach(function (p) {
      var n = taken[p.r.kind] || 0;
      if (n < share && picked.length < max) { picked.push(p); taken[p.r.kind] = n + 1; } else rest.push(p);
    });
    for (var j = 0; j < rest.length && picked.length < max; j++) picked.push(rest[j]);
    picked.sort(cmp);
    return picked.map(function (p) { return p.r; });
  }

  function searchUrl(api, q, opts) {
    var o = opts || {};
    return (api || '') + '/api/search?q=' + encodeURIComponent(q) +
      '&limit=' + (isNum(o.limit) ? o.limit : DEFAULT_LIMIT) +
      '&places=' + (o.full ? 1 : 0) + '&live=' + (o.full ? 1 : 0);
  }

  function enough(q) {
    var t = String(q || '').trim();
    return /^\d+$/.test(t) ? t.length >= 1 : fold(t).length >= 2;
  }

  function standoffKm(altKm) { return Math.max(1200, Math.min(altKm * 0.6, 25000)); }

  // Cesium entity properties are PropertyBags in the page and plain values in
  // tests; read either.
  function propValue(p, when) {
    if (p && typeof p.getValue === 'function') {
      try { return p.getValue(when); } catch (e) { return undefined; }
    }
    return p;
  }

  // ---------------------------------------------------------------------
  // Local sources
  // ---------------------------------------------------------------------

  function satRows(sats, raw, limit) {
    var ss = global.SDASatSearch;
    if (!ss || typeof ss.rank !== 'function' || !sats || !sats.length) return null;
    var found = ss.rank(raw, sats, limit);
    var q = fold(raw);
    var digits = /^\d+$/.test(String(raw).trim());
    return found.results.map(function (s) {
      var id = s.noradId != null ? String(s.noradId) : '';
      var tier = nameTier(fold(String(s.name || '')), q);
      if (digits && id === String(raw).trim().replace(/^0+(?=\d)/, '')) tier = TIER.EXACT;
      else if (digits && tier < 0 && id.indexOf(String(raw).trim()) === 0) tier = TIER.PREFIX;
      // rank() matched a full alpha-5 id ("A0001") when the name did not match.
      else if (tier < 0 && queryShape(raw).alpha5) tier = TIER.EXACT;
      if (tier < 0) tier = TIER.OTHER;
      var placed = isNum(s.lastLat) && isNum(s.lastLon) && isNum(s.lastAlt);
      var parts = [];
      if (id) parts.push('NORAD ' + id);
      if (typeof s.regime === 'string' && s.regime) parts.push(s.regime);
      if (placed) parts.push(Math.round(s.lastAlt) + ' km');
      return {
        kind: 'satellite', id: id, name: String(s.name || ('NORAD ' + id)), detail: parts.join(' | '),
        tier: tier, lat: placed ? s.lastLat : null, lon: placed ? s.lastLon : null,
        alt_m: placed ? s.lastAlt * 1000 : null, extra: { norad_id: s.noradId, local: true }
      };
    });
  }

  function aircraftRows(viewer, raw, limit) {
    var C = global.Cesium;
    var list = viewer && viewer.entities && viewer.entities.values;
    if (!list || !list.length) return [];
    var when = viewer.clock ? viewer.clock.currentTime : undefined;
    var hits = [];
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      var props = e && e.properties;
      if (!props || propValue(props.type, when) !== 'aircraft') continue;
      var rec = {
        icao24: String(propValue(props.icao24, when) || '').toLowerCase(),
        callsign: String(e.name || propValue(props.callsign, when) || '').trim(),
        registration: String(propValue(props.registration, when) || '').trim()
      };
      var tier = aircraftTier(rec, raw);
      if (tier < 0) continue;
      var pos = null;
      try {
        var cart = e.position && typeof e.position.getValue === 'function' ? e.position.getValue(when) : null;
        var carto = cart && C ? C.Cartographic.fromCartesian(cart) : null;
        if (carto) pos = [carto.latitude * 180 / Math.PI, carto.longitude * 180 / Math.PI, carto.height];
      } catch (err) { pos = null; }
      if (!pos) continue;
      var parts = [];
      if (rec.registration) parts.push(rec.registration);
      if (rec.icao24) parts.push(rec.icao24.toUpperCase());
      var origin = propValue(props.origin_country, when);
      if (origin) parts.push(String(origin));
      hits.push({
        kind: 'aircraft', id: rec.icao24 || String(e.id), name: rec.callsign || rec.registration ||
          rec.icao24.toUpperCase(), detail: parts.join(' | '), tier: tier, lat: pos[0], lon: pos[1],
        alt_m: pos[2], extra: { icao24: rec.icao24, entity_id: e.id, local: true }
      });
    }
    hits.sort(function (a, b) { return (a.tier - b.tier) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0); });
    return hits.slice(0, limit);
  }

  function coordinateRow(raw) {
    var mt = global.SDAMapTools;
    if (!mt || typeof mt.parseCoordinate !== 'function') return null;
    var c = null;
    try { c = mt.parseCoordinate(String(raw)); } catch (e) { c = null; }
    if (!c || !isNum(c.lat) || !isNum(c.lon)) return null;
    var text = c.lat.toFixed(5) + ', ' + c.lon.toFixed(5);
    return {
      kind: 'coordinate', id: c.lat.toFixed(6) + ',' + c.lon.toFixed(6), name: c.label || text,
      detail: (c.kind ? String(c.kind).toUpperCase() + ' | ' : '') + text, tier: TIER.EXACT,
      lat: c.lat, lon: c.lon, alt_m: null, extra: { local: true }
    };
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.os-input { width: 100%; box-sizing: border-box; background: rgba(255,176,0,0.04);',
    '  border: 1px solid rgba(255,176,0,0.15); border-radius: 2px; padding: 6px 8px;',
    '  color: var(--hud-color, #ffb000); font-size: 11px; font-family: inherit; outline: none; }',
    '.os-list { max-height: 260px; overflow-y: auto; margin-top: 4px; }',
    '.os-list .search-result { padding: 4px 6px; font-size: 10px; }',
    '.search-result.os-active { background: rgba(255,176,0,0.16); }',
    '.search-result.os-more .sr-name { font-weight: 400; font-style: italic; }',
    '.os-status { font-size: 9px; min-height: 11px; margin-top: 3px; color: var(--hud-dim, #7a5500); }'
  ].join('\n');

  function injectStyle(doc) {
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

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var m = { serverGone: !!ctx.isStatic, cache: {}, cleanup: [], views: [] };

    function note(text) { try { ctx.addIntelItem(text); } catch (e) { /* page feed only */ } }

    function getServer(q, full) {
      if (m.serverGone || typeof global.fetch !== 'function') return Promise.resolve(null);
      var url = searchUrl(ctx.api, q, { full: full });
      var hit = m.cache[url];
      var now = Date.now();
      if (hit && now - hit.at < CACHE_TTL_MS) return Promise.resolve(hit.data);
      return global.fetch(url).then(function (r) {
        if (r.status === 404) { m.serverGone = true; return null; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (data) {
        if (!data) return null;
        var keys = Object.keys(m.cache);
        if (keys.length >= CACHE_MAX) delete m.cache[keys[0]];
        m.cache[url] = { at: now, data: data };
        return data;
      });
    }

    // Everything for one query: { query, full, results, server, failed }.
    function search(raw, opts) {
      var q = String(raw == null ? '' : raw).trim();
      var full = !!(opts && opts.full);
      var limit = (opts && isNum(opts.limit)) ? opts.limit : DEFAULT_LIMIT;
      if (!q) return Promise.resolve({ query: q, full: full, results: [], server: false, failed: false });
      var local = [];
      var coord = coordinateRow(q);
      if (coord) local.push(coord);
      var sats = enough(q) ? satRows(ctx.satEngine && ctx.satEngine.sats, q, limit) : null;
      if (sats) local = local.concat(sats);
      if (enough(q)) local = local.concat(aircraftRows(viewer, q, limit));
      var failed = false;
      return getServer(q, full).catch(function () { failed = true; return null; }).then(function (data) {
        var rows = local.slice();
        var seen = {};
        rows.forEach(function (r) { seen[r.kind + ':' + r.id] = true; });
        var serverRows = data && Array.isArray(data.results) ? data.results : [];
        serverRows.forEach(function (r) {
          if (!r || KINDS.indexOf(r.kind) < 0) return;
          // The globe's own satellites carry live positions; theirs win.
          if (r.kind === 'satellite' && sats) return;
          if (r.kind === 'coordinate' && coord) return;
          if (seen[r.kind + ':' + r.id]) return;
          seen[r.kind + ':' + r.id] = true;
          rows.push(r);
        });
        return {
          query: q, full: full, results: mergeResults(rows, q, limit), server: !!data,
          failed: failed
        };
      });
    }

    // -- flying and opening ---------------------------------------------

    function flyTo(lon, lat, heightM, pitchDeg) {
      if (!C || !isNum(lat) || !isNum(lon)) return false;
      try {
        if (typeof viewer.camera.cancelFlight === 'function') viewer.camera.cancelFlight();
        var opts = { destination: C.Cartesian3.fromDegrees(lon, lat, heightM), duration: 1.8 };
        if (isNum(pitchDeg) && C.Math && typeof C.Math.toRadians === 'function') {
          opts.orientation = { heading: 0, pitch: C.Math.toRadians(pitchDeg), roll: 0 };
        }
        viewer.camera.flyTo(opts);
        return true;
      } catch (e) { return false; }
    }

    function pick(r) {
      if (!r) return false;
      var flew = false;
      var extra = r.extra || {};
      if (r.kind === 'satellite') {
        var id = extra.norad_id != null ? extra.norad_id : Number(r.id);
        var eng = ctx.satEngine || {};
        var live = eng.byNoradId && eng.byNoradId[id];
        try { ctx.selectSatellite(id); } catch (e) { /* keep going */ }
        var lat = live && isNum(live.lastLat) ? live.lastLat : r.lat;
        var lon = live && isNum(live.lastLon) ? live.lastLon : r.lon;
        var altKm = live && isNum(live.lastAlt) ? live.lastAlt : (isNum(r.alt_m) ? r.alt_m / 1000 : null);
        if (isNum(altKm)) flew = flyTo(lon, lat, (altKm + standoffKm(altKm)) * 1000);
        note('Search: ' + r.name + ' (NORAD ' + id + ')' +
          (flew ? ', ' + Math.round(altKm) + ' km' : ', position not yet available'));
        return true;
      }
      if (r.kind === 'aircraft') {
        var ent = null;
        try {
          ent = viewer.entities.getById(extra.entity_id || ('flight-' + (extra.icao24 || r.id)));
        } catch (e) { ent = null; }
        if (ent) { try { viewer.selectedEntity = ent; } catch (e) { /* keep going */ } }
        flew = flyTo(r.lon, r.lat, (isNum(r.alt_m) ? r.alt_m : 0) + 40000, -50);
        note('Search: aircraft ' + r.name + (flew ? '' : ', no position'));
        return true;
      }
      if (r.kind === 'camera') {
        flew = flyTo(r.lon, r.lat, 3000);
        var cams = global.SDACameras;
        var rid = isNum(extra.rid) ? extra.rid : Number(r.id);
        if (cams && typeof cams.open === 'function' && isNum(rid)) cams.open(rid);
        note('Search: camera ' + r.name);
        return true;
      }
      if (r.kind === 'peak') {
        var elev = isNum(extra.elev_m) ? extra.elev_m : 0;
        flew = flyTo(r.lon, r.lat - 0.12, elev + 9000, -35);
        var summit = global.SDASummit;
        if (summit && typeof summit.open === 'function') {
          try {
            summit.open({ id: extra.peak_id != null ? extra.peak_id : r.id, name: r.name, lat: r.lat,
              lon: r.lon, elev_m: extra.elev_m, prom_m: extra.prom_m });
          } catch (e) { /* keep going */ }
        }
        note('Search: peak ' + r.name + (isNum(extra.elev_m) ? ' (' + Math.round(extra.elev_m) + ' m)' : ''));
        return true;
      }
      flew = flyTo(r.lon, r.lat, r.kind === 'coordinate' ? 20000 : 50000, -45);
      note('Search: flying to ' + r.name);
      return flew;
    }

    // -- one input, one result list ---------------------------------------

    function makeView(input, list, status, opts) {
      var v = {
        input: input, list: list, status: status, rows: [], active: 0, moved: false,
        seq: 0, timer: null, last: null, onClose: opts && opts.onClose
      };

      function say(text) { if (v.status) v.status.textContent = text || ''; }

      function paintActive() {
        Array.prototype.forEach.call(list.children, function (row, i) {
          row.classList.toggle('os-active', i === v.active);
          if (i === v.active && v.moved && typeof row.scrollIntoView === 'function') {
            row.scrollIntoView({ block: 'nearest' });
          }
        });
      }

      function render(res) {
        v.last = res;
        v.rows = res.results.slice();
        var canWiden = !res.full && res.query.length >= 2 && (!m.serverGone || mapToolsGoTo());
        if (canWiden) {
          v.rows.push({ kind: 'more', id: 'more', name: 'Search places for "' + res.query + '"',
            detail: m.serverGone ? 'Map tools place search' : 'Places, and a live aircraft lookup',
            tier: TIER.OTHER });
        }
        list.replaceChildren();
        v.rows.forEach(function (r, i) {
          var row = el('div', 'search-result' + (r.kind === 'more' ? ' os-more' : ''));
          row.setAttribute('role', 'option');
          row.appendChild(el('span', 'sr-name', r.name));
          if (r.kind !== 'more') row.appendChild(el('span', 'sr-type', LABELS[r.kind] || r.kind));
          if (r.detail) row.appendChild(el('span', 'sr-full', r.detail));
          row.addEventListener('click', function () { choose(i); });
          list.appendChild(row);
        });
        v.active = 0;
        v.moved = false;
        paintActive();
        var n = res.results.length;
        if (n) say(n + ' result' + (n === 1 ? '' : 's') + (res.failed ? '; the search service did not answer' : ''));
        else if (res.failed) say('The search service did not answer.');
        else say(res.full ? 'Nothing matched "' + res.query + '".' : 'No match yet; Enter searches places too.');
      }

      function run(full) {
        if (v.timer) { global.clearTimeout(v.timer); v.timer = null; }
        var q = String(input.value || '').trim();
        var seq = ++v.seq;
        if (!q || (!full && !enough(q))) {
          v.rows = []; v.last = null; list.replaceChildren(); say('');
          return Promise.resolve(null);
        }
        if (full && m.serverGone) {
          var mt = mapToolsGoTo();
          if (mt) { mt(q); close(); return Promise.resolve(null); }
        }
        if (full) say('Searching places for "' + q + '"...');
        return search(q, { full: full }).then(function (res) {
          if (seq !== v.seq) return null;
          render(res);
          return res;
        });
      }

      function choose(i) {
        var r = v.rows[i];
        if (!r) return false;
        if (r.kind === 'more') { run(true); return true; }
        pick(r);
        close();
        return true;
      }

      function close() {
        if (v.onClose) v.onClose();
      }

      function onKey(e) {
        try { e.stopPropagation(); } catch (err) { /* keep going */ }
        if (e.isComposing) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          if (!v.rows.length) return;
          e.preventDefault();
          v.active = (v.active + (e.key === 'ArrowDown' ? 1 : -1) + v.rows.length) % v.rows.length;
          v.moved = true;
          paintActive();
        } else if (e.key === 'Enter') {
          e.preventDefault();
          var q = String(input.value || '').trim();
          var fresh = v.last && v.last.query === q;
          var top = v.rows[v.active];
          // A row the user chose, an exact hit, or a list that already holds
          // places: fly. Otherwise widen the search first.
          if (fresh && top && (v.moved || v.last.full || (top.kind !== 'more' && top.tier === TIER.EXACT))) {
            choose(v.active);
          } else {
            run(true);
          }
        } else if (e.key === 'Escape') {
          e.preventDefault();
          if (String(input.value || '') !== '' && !(opts && opts.escapeCloses)) {
            input.value = '';
            run(false);
          } else {
            close();
          }
        }
      }

      input.addEventListener('keydown', onKey);
      input.addEventListener('input', function () {
        if (v.timer) global.clearTimeout(v.timer);
        v.timer = global.setTimeout(function () { v.timer = null; run(false); }, TYPE_DELAY_MS);
      });
      v.run = run;
      v.choose = choose;
      v.stop = function () { if (v.timer) { global.clearTimeout(v.timer); v.timer = null; } v.seq++; };
      return v;
    }

    function mapToolsGoTo() {
      var mt = global.SDAMapTools;
      try {
        if (mt && typeof mt.goTo === 'function' && typeof mt.instance === 'function' && mt.instance()) {
          return function (q) { return mt.goTo(q); };
        }
      } catch (e) { /* not running */ }
      return null;
    }

    // -- the page's "/" box ------------------------------------------------

    m.adopted = null;
    var oldInput = doc.getElementById('search-input');
    var box = doc.getElementById('search-box');
    var results = doc.getElementById('search-results');
    if (oldInput && box && results && oldInput.parentNode && typeof oldInput.cloneNode === 'function') {
      var fresh = oldInput.cloneNode(false);
      fresh.setAttribute('placeholder', 'Places, satellites, aircraft, cameras, peaks, coordinates (Esc closes)');
      fresh.setAttribute('autocomplete', 'off');
      fresh.setAttribute('spellcheck', 'false');
      fresh.setAttribute('aria-label', 'Search the globe');
      oldInput.parentNode.replaceChild(fresh, oldInput);
      var boxStatus = el('div', 'os-status os-box-status');
      boxStatus.setAttribute('role', 'status');
      boxStatus.style.padding = '0 20px 6px';
      box.appendChild(boxStatus);
      var boxView = makeView(fresh, results, boxStatus, {
        escapeCloses: true,
        onClose: function () { box.classList.remove('visible'); try { fresh.blur(); } catch (e) { /* gone */ } }
      });
      m.adopted = { old: oldInput, fresh: fresh, view: boxView, status: boxStatus };
      m.views.push(boxView);
    }

    // -- sidebar panel -------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, 'Search');
    var body = el('div', 'panel-body');
    var input = el('input', 'os-input');
    input.setAttribute('type', 'search');
    input.setAttribute('placeholder', 'Place, satellite, NORAD id, callsign, camera, peak');
    input.setAttribute('aria-label', 'Search places, satellites, aircraft, cameras and peaks');
    input.setAttribute('autocomplete', 'off');
    var list = el('div', 'os-list');
    list.setAttribute('role', 'listbox');
    var status = el('div', 'os-status');
    status.setAttribute('role', 'status');
    body.appendChild(input);
    body.appendChild(list);
    body.appendChild(status);
    m.root.appendChild(head);
    m.root.appendChild(body);
    // Taking the click first (and stopping it) gives one toggle whether init
    // runs before or after the page's own collapse binding.
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });
    var panelView = makeView(input, list, status, {
      onClose: function () { list.replaceChildren(); status.textContent = ''; }
    });
    m.views.push(panelView);

    function focus() {
      if (m.adopted && box) {
        box.classList.add('visible');
        m.adopted.fresh.value = '';
        results.replaceChildren();
        m.adopted.fresh.focus();
        return true;
      }
      m.root.classList.remove('collapsed');
      input.focus();
      return true;
    }

    function destroy() {
      m.views.forEach(function (v) { v.stop(); });
      if (m.adopted && m.adopted.fresh.parentNode) {
        // Hand the page its own input back, listeners and all.
        m.adopted.fresh.parentNode.replaceChild(m.adopted.old, m.adopted.fresh);
      }
      if (m.adopted && m.adopted.status.parentNode) {
        m.adopted.status.parentNode.removeChild(m.adopted.status);
      }
      m.adopted = null;
      if (m.root.parentNode) m.root.parentNode.removeChild(m.root);
    }

    return {
      root: m.root,
      search: search,
      pick: pick,
      focus: focus,
      query: function (text, full) {
        var v = m.adopted ? m.adopted.view : panelView;
        v.input.value = String(text == null ? '' : text);
        if (m.adopted && box) box.classList.add('visible');
        return v.run(!!full);
      },
      adopted: function () { return !!m.adopted; },
      serverGone: function () { return m.serverGone; },
      destroy: destroy
    };
  }

  var instance = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function' || !ctx.viewer ||
          !global.document) return false;
      if (instance) instance.destroy();
      injectStyle(global.document);
      instance = createModule(ctx);
      ctx.mount.appendChild(instance.root);
      return true;
    } catch (err) {
      instance = null;
      return false;
    }
  }

  global.SDAOmniSearch = {
    init: init,
    focus: function () { return instance ? instance.focus() : false; },
    search: function (q, opts) {
      return instance ? instance.search(q, opts) : Promise.resolve(null);
    },
    query: function (q, full) { return instance ? instance.query(q, full) : Promise.resolve(null); },
    pick: function (r) { return instance ? instance.pick(r) : false; },
    destroy: function () { if (instance) instance.destroy(); instance = null; },
    KINDS: KINDS.slice(),
    LABELS: LABELS,
    TIER: TIER,
    fold: fold,
    nameTier: nameTier,
    canonicalId: canonicalId,
    queryShape: queryShape,
    kindOrder: kindOrder,
    aircraftTier: aircraftTier,
    mergeResults: mergeResults,
    searchUrl: searchUrl,
    standoffKm: standoffKm
  };
})(typeof window !== 'undefined' ? window : this);
