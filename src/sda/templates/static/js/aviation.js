// Aviation intelligence on the WorldView globe, drawn over the page's own
// aircraft layers (it never touches the flight-* or mil-* entities):
//
//   - military aircraft from adsb.lol /v2/mil as green chevrons, with the
//     nearest mapped military site (OpenStreetMap) on the card;
//   - emergency squawks 7500, 7600 and 7700 in red, with a banner across the top
//     of the page while any are airborne;
//   - an aircraft card: registration, type, operator, callsign, altitude, speed,
//     the scheduled route for the callsign (VRS standing data) with LiveATC links
//     for both airports, and a sanctions check (/api/sanctions/aircraft, shown
//     only when it answers);
//   - dead reckoning: an aircraft whose last position is older than 2 minutes
//     is carried along its last track at its last speed for up to 20 minutes,
//     drawn hollow and labelled EST, never as a report. A civil aircraft whose
//     card was opened gets the same treatment if it drops out of the page's
//     Aircraft layer (only opened ones: that layer keeps an arbitrary 2,000 of
//     the OpenSky states, so membership churns and a vanished aircraft there
//     is usually not a lost one);
//   - GPS interference inferred from aircraft reports (GPSJam method: the share
//     of aircraft with NIC below 7 or NACp below 8 per hex over the last hour),
//     drawn as a coloured hex grid with a legend.
//
// Server: sda/routes/aviation.py. adsb.lol allows 500 requests a day per IP
// without a key, so the server paces and caches every call; this module polls
// the server every 30 s and the server decides when to ask upstream.
//
// Frames: geodetic degrees, metres, knots. No text is ever set as HTML. Links
// are only ever set to the LiveATC search URL checked below.
//
// Page wiring: listed in EXTENSIONS with a mount div; the layer toggle calls
// SDAAviation.setEnabled(on); clicks on its own markers come back through the
// page's picked.id.sdaPick() path. Other modules may call SDAAviation.openCard.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Aviation intel';
  var STYLE_ID = 'sda-aviation-style';
  var BANNER_ID = 'aviation-emergency-banner';
  var COUNT_ID = 'count-aviation';

  var POLL_MS = 30000;
  var TICK_MS = 5000;
  var SANCTIONS_TTL_MS = 3600000;
  var REPORT_FRESH_S = 120;
  var DEAD_RECKON_MAX_S = 1200;
  var DEAD_RECKON_MIN_KT = 30;
  var MAX_PINNED = 8;
  var EARTH_R_KM = 6371.0088;
  var NM_KM = 1.852;
  var KT_PER_MS = 1 / 0.514444;
  var FT_PER_M = 1 / 0.3048;
  var LIVEATC_PREFIX = 'https://www.liveatc.net/search/?icao=';

  var SQUAWKS = {
    '7700': 'general emergency',
    '7600': 'radio failure',
    '7500': 'unlawful interference'
  };
  var SQUAWK_ORDER = ['7700', '7600', '7500'];

  var COLORS = { mil: '#9be36b', emerg: '#ff3030', civ: '#44aaff', sanction: '#ff40ff' };
  var LEVELS = {
    low: { color: '#2fbf4a', alpha: 0.22, text: 'under 2%' },
    medium: { color: '#f2c12e', alpha: 0.42, text: '2 to 10%' },
    high: { color: '#e8352b', alpha: 0.5, text: 'over 10%' }
  };
  var LEVEL_ORDER = ['low', 'medium', 'high'];

  var TOGGLES = [
    { key: 'mil', label: 'MIL', title: 'Military aircraft (adsb.lol /v2/mil)' },
    { key: 'emerg', label: 'SQUAWK', title: 'Emergency squawks 7500, 7600 and 7700' },
    { key: 'est', label: 'EST', title: 'Carry lost aircraft on their last track for 20 minutes' },
    { key: 'gps', label: 'GPS', title: 'GPS interference inferred from aircraft reports' }
  ];

  // Chevron pointing up (north before rotation); hollow for estimates.
  function iconUrl(color, hollow) {
    var path = '<path d="M12 2 L20 21 L12 16.5 L4 21 Z"';
    var paint = hollow
      ? ' fill="none" stroke="' + color + '" stroke-width="2.2" stroke-linejoin="round"/>'
      : ' fill="' + color + '" stroke="#000000" stroke-width="1.3" stroke-linejoin="round"/>';
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
      'viewBox="0 0 24 24">' + path + paint + '</svg>';
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }

  function ringUrl(color) {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" ' +
      'viewBox="0 0 32 32"><circle cx="16" cy="16" r="13" fill="none" stroke="' + color +
      '" stroke-width="2.5" stroke-dasharray="5 3"/></svg>';
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }

  var ICONS = {};
  ['mil', 'emerg', 'civ'].forEach(function (k) {
    ICONS[k] = iconUrl(COLORS[k], false);
    ICONS[k + '-est'] = iconUrl(COLORS[k], true);
  });
  ICONS.sanction = ringUrl(COLORS.sanction);

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function withCommas(n) {
    return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function wrapLon(lon) { return ((lon + 540) % 360) - 180; }

  // Great-circle position after dtS seconds along trackDeg at speedKt.
  function deadReckon(lat, lon, trackDeg, speedKt, dtS) {
    var d = speedKt * NM_KM * dtS / 3600 / EARTH_R_KM;
    var r = Math.PI / 180;
    var p1 = lat * r, l1 = lon * r, th = trackDeg * r;
    var sinP2 = Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(th);
    var p2 = Math.asin(Math.max(-1, Math.min(1, sinP2)));
    var l2 = l1 + Math.atan2(Math.sin(th) * Math.sin(d) * Math.cos(p1),
      Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    return { lat: p2 / r, lon: wrapLon(l2 / r) };
  }

  // Where to draw a track at nowS: reported, estimated (dead reckoned), or lost.
  function positionAt(t, nowS) {
    if (!t || !isNum(t.lat) || !isNum(t.lon) || !isNum(t.seen_at)) return { state: 'lost' };
    var age = Math.max(0, nowS - t.seen_at);
    if (age <= REPORT_FRESH_S) return { state: 'reported', lat: t.lat, lon: t.lon, age_s: age };
    if (age > DEAD_RECKON_MAX_S || t.on_ground || !isNum(t.track) || !isNum(t.gs_kt) ||
        t.gs_kt < DEAD_RECKON_MIN_KT) {
      return { state: 'lost', age_s: age };
    }
    var p = deadReckon(t.lat, t.lon, t.track, t.gs_kt, age);
    return { state: 'estimated', lat: p.lat, lon: p.lon, age_s: age };
  }

  function fmtAge(s) {
    if (!isNum(s)) return '';
    if (s < 90) return Math.round(s) + ' s';
    return Math.round(s / 60) + ' min';
  }

  function fmtAlt(m, onGround) {
    if (onGround) return 'on the ground';
    if (!isNum(m)) return 'unknown';
    return withCommas(m * FT_PER_M) + ' ft (' + withCommas(m) + ' m)';
  }

  function fmtSpeed(kt) {
    if (!isNum(kt)) return 'unknown';
    return Math.round(kt) + ' kt (' + withCommas(kt * NM_KM) + ' km/h)';
  }

  function normReg(s) {
    return typeof s === 'string' ? s.toUpperCase().replace(/[^A-Z0-9]/g, '') : '';
  }

  function safeLiveAtc(url) {
    return (typeof url === 'string' && url.indexOf(LIVEATC_PREFIX) === 0 &&
      /^[A-Z0-9]{4}$/.test(url.slice(LIVEATC_PREFIX.length))) ? url : null;
  }

  function query(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i++) {
      var v = pairs[i][1];
      if (v === null || v === undefined || v === '') continue;
      parts.push(encodeURIComponent(pairs[i][0]) + '=' + encodeURIComponent(String(v)));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  function trackName(t) {
    if (!t) return '';
    if (t.private) return 'PRIVATE';
    return t.callsign || t.registration || String(t.hex || '').toUpperCase();
  }

  function trackKind(a) {
    if (a && SQUAWKS[a.squawk] && a.inEmerg !== false) return 'emerg';
    if (a && a.military) return 'mil';
    return 'civ';
  }

  // Map label: name, then EST and the age for estimates.
  function labelFor(t, pos) {
    var name = trackName(t);
    if (t.kind === 'emerg' && SQUAWKS[t.squawk]) name += ' ' + t.squawk;
    if (pos && pos.state === 'estimated') name += ' EST ' + fmtAge(pos.age_s);
    if (t.sanctioned) name += ' SANCTIONS LIST';
    return name;
  }

  // Banner text for the airborne emergency tracks, most serious code first.
  function bannerText(list) {
    if (!list || !list.length) return '';
    var sorted = list.slice().sort(function (a, b) {
      return SQUAWK_ORDER.indexOf(a.t.squawk) - SQUAWK_ORDER.indexOf(b.t.squawk);
    });
    var parts = sorted.slice(0, 4).map(function (e) {
      var s = e.t.squawk + ' ' + SQUAWKS[e.t.squawk] + ': ' + trackName(e.t);
      if (e.t.type) s += ' (' + e.t.type + ')';
      if (e.pos.state === 'estimated') s += ', estimated, last report ' + fmtAge(e.pos.age_s) + ' ago';
      return s;
    });
    var more = sorted.length > 4 ? ' and ' + (sorted.length - 4) + ' more' : '';
    return 'EMERGENCY SQUAWK ' + sorted.length + ' airborne. ' + parts.join(' | ') + more;
  }

  function levelStyle(level) {
    return LEVELS[level] || LEVELS.low;
  }

  var clockFn = function () { return Date.now(); };

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.avi-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 4px;',
    '  line-height: 1.4; }',
    '.avi-row { display: flex; gap: 3px; margin-bottom: 5px; }',
    '.avi-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.avi-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px; }',
    '.avi-legend { font-size: 8px; color: var(--hud-dim, #7a5500); margin: 4px 0; }',
    '.avi-legend .avi-swatch { width: 9px; height: 9px; display: inline-block;',
    '  margin: 0 3px 0 6px; vertical-align: middle; border: 1px solid #000; }',
    '.avi-method { font-size: 8px; color: var(--hud-dim, #7a5500); line-height: 1.35;',
    '  margin-bottom: 4px; }',
    '.avi-card { font-size: 9px; color: var(--hud-color, #ffb000); margin-top: 5px;',
    '  border-top: 1px solid rgba(255,176,0,0.2); padding-top: 4px; }',
    '.avi-card:empty { display: none; }',
    '.avi-card-row { display: flex; gap: 6px; padding: 1px 0; }',
    '.avi-card-row .avi-k { flex: 0 0 74px; color: var(--hud-dim, #7a5500); }',
    '.avi-card-row .avi-v { flex: 1; word-break: break-word; }',
    '.avi-est { color: #ffd166; }',
    '.avi-alert { color: #ff6060; font-weight: 600; }',
    '.avi-card a { color: #8fd3ff; margin-right: 8px; }',
    '.avi-card .mode-btn { font-size: 8px; padding: 2px 6px; margin-top: 4px; }',
    '#' + BANNER_ID + ' { position: fixed; top: 6px; left: 50%; transform: translateX(-50%);',
    '  z-index: 60; max-width: 70vw; background: rgba(150,0,0,0.92); color: #fff;',
    '  border: 1px solid #ff6060; font: 600 11px monospace; letter-spacing: 0.5px;',
    '  padding: 5px 12px; cursor: pointer; display: none; text-align: center; }'
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

  function prop(p) {
    if (p && typeof p.getValue === 'function') {
      try { return p.getValue(); } catch (e) { return undefined; }
    }
    return p;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, pollTimer: null, tickTimer: null, offsetS: 0,
      toggles: { mil: true, emerg: true, est: true, gps: true },
      tracks: Object.create(null), civ: Object.create(null), status: null,
      icons: null, labels: null, hexPrim: null, hexes: [], gps: null,
      sanctions: null, sanctionsAt: 0, cardHex: null, cardSeq: 0, pollSeq: 0,
      bannerList: [], clickHandler: null
    };

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    function getJson(path) {
      if (ctx.isStatic) return Promise.reject(new Error('static build'));
      return global.fetch(api + path).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    function nowS() { return clockFn() / 1000 + m.offsetS; }

    function syncClock(data) {
      if (data && isNum(data.server_now)) m.offsetS = data.server_now - clockFn() / 1000;
    }

    // -- panel ----------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.summaryEl = el('div', 'avi-summary',
      'Turn on the Aviation intel layer for military aircraft, emergency squawks, ' +
      'estimated tracks and GPS interference.');
    var row = el('div', 'avi-row');
    m.toggleBtns = {};
    TOGGLES.forEach(function (tg) {
      var b = button('mode-btn active', tg.label, tg.title);
      b.setAttribute('aria-pressed', 'true');
      b.addEventListener('click', function () { setToggle(tg.key, !m.toggles[tg.key]); });
      m.toggleBtns[tg.key] = b;
      row.appendChild(b);
    });
    m.statusEl = el('div', 'avi-status');
    m.statusEl.setAttribute('role', 'status');
    m.budgetEl = el('div', 'avi-summary');
    m.legend = el('div', 'avi-legend');
    m.legend.appendChild(el('span', null, 'Low-integrity share:'));
    LEVEL_ORDER.forEach(function (k) {
      var sw = el('span', 'avi-swatch');
      sw.style.background = LEVELS[k].color;
      m.legend.appendChild(sw);
      m.legend.appendChild(el('span', null, LEVELS[k].text));
    });
    m.methodEl = el('div', 'avi-method',
      'GPS interference is inferred from aircraft reports, not measured: the share of ' +
      'aircraft per hex over the last hour whose own position report had NIC below 7 or ' +
      'NACp below 8, or whose GPS adsb.lol marked as lost (the GPSJam method). Only ' +
      'areas that were sampled appear.');
    m.gpsEl = el('div', 'avi-summary');
    m.card = el('div', 'avi-card');
    [m.summaryEl, row, m.statusEl, m.budgetEl, m.legend, m.methodEl, m.gpsEl, m.card]
      .forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.statusEl.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById(COUNT_ID);
        if (c) c.textContent = String(n);
      } catch (e) { /* keep going */ }
    }

    // -- banner -----------------------------------------------------------

    function banner() {
      var doc = global.document;
      var b = doc.getElementById(BANNER_ID);
      if (!b) {
        b = el('div');
        b.id = BANNER_ID;
        b.setAttribute('role', 'alert');
        b.setAttribute('title', 'Click to fly to the aircraft');
        b.addEventListener('click', function () {
          var first = m.bannerList[0];
          if (first) { flyTo(first.pos, 120000); openCard(first.t.hex, first.t); }
        });
        doc.body.appendChild(b);
      }
      return b;
    }

    function updateBanner(list) {
      m.bannerList = list;
      var text = (m.enabled && m.toggles.emerg) ? bannerText(list) : '';
      var b = global.document.getElementById(BANNER_ID);
      if (!text) {
        if (b) { b.textContent = ''; b.style.display = 'none'; }
        return;
      }
      b = banner();
      b.textContent = text;
      b.style.display = 'block';
    }

    // -- globe ------------------------------------------------------------

    function ensureLayers() {
      if (m.icons) return;
      m.icons = viewer.scene.primitives.add(new C.BillboardCollection());
      m.labels = viewer.scene.primitives.add(new C.LabelCollection());
    }

    function clearHexes() {
      if (m.hexPrim) {
        try { viewer.scene.primitives.remove(m.hexPrim); } catch (e) { /* keep going */ }
        m.hexPrim = null;
      }
    }

    function flyTo(p, height) {
      if (!p || !isNum(p.lat) || !isNum(p.lon)) return;
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(p.lon, p.lat, height || 150000),
          duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function pickFor(t) {
      return {
        type: 'aviation', hex: t.hex,
        sdaPick: function () { openCard(t.hex, t); }
      };
    }

    // Every drawn track: reported ones solid, estimates hollow and labelled EST.
    function redraw() {
      if (!m.enabled || !m.icons) return;
      m.icons.removeAll();
      m.labels.removeAll();
      var t = nowS();
      var airborneEmerg = [];
      var drawn = 0;
      Object.keys(m.tracks).forEach(function (hex) {
        var tr = m.tracks[hex];
        var pos = positionAt(tr, t);
        if (pos.state === 'lost') { delete m.tracks[hex]; return; }
        if (tr.kind === 'emerg' && !tr.on_ground) airborneEmerg.push({ t: tr, pos: pos });
        if (tr.kind === 'emerg' ? !m.toggles.emerg : !m.toggles.mil) return;
        if (pos.state === 'estimated' && !m.toggles.est) return;
        drawTrack(tr, pos);
        drawn++;
      });
      if (m.toggles.est) {
        Object.keys(m.civ).forEach(function (icao) {
          var tr = m.civ[icao];
          if (tr.present) return;  // the page's Aircraft layer draws it
          var pos = positionAt(tr, t);
          if (pos.state !== 'estimated') return;
          drawTrack(tr, pos);
          drawn++;
        });
      }
      updateBanner(airborneEmerg);
      setCount(drawn);
    }

    function drawTrack(tr, pos) {
      var est = pos.state === 'estimated';
      var kind = tr.kind || 'civ';
      var position = C.Cartesian3.fromDegrees(pos.lon, pos.lat,
        isNum(tr.alt_m) ? tr.alt_m : 0);
      var pick = pickFor(tr);
      m.icons.add({
        position: position, image: ICONS[kind + (est ? '-est' : '')],
        width: kind === 'emerg' ? 22 : 16, height: kind === 'emerg' ? 22 : 16,
        rotation: isNum(tr.track) ? -tr.track * Math.PI / 180 : 0,
        alignedAxis: C.Cartesian3.UNIT_Z,
        verticalOrigin: C.VerticalOrigin.CENTER,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        id: pick
      });
      if (tr.sanctioned) {
        m.icons.add({
          position: position, image: ICONS.sanction, width: 30, height: 30,
          verticalOrigin: C.VerticalOrigin.CENTER,
          disableDepthTestDistance: Number.POSITIVE_INFINITY, id: pick
        });
      }
      m.labels.add({
        position: position, text: labelFor(tr, pos), font: '10px monospace',
        fillColor: C.Color.fromCssColorString(est ? '#ffd166' : COLORS[kind]),
        outlineColor: C.Color.BLACK, outlineWidth: 2,
        style: C.LabelStyle.FILL_AND_OUTLINE,
        horizontalOrigin: C.HorizontalOrigin.LEFT,
        verticalOrigin: C.VerticalOrigin.CENTER,
        pixelOffset: new C.Cartesian2(12, 0),
        translucencyByDistance: new C.NearFarScalar(2e5, 1, 8e6, 0.15),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        id: pick
      });
    }

    function drawHexes() {
      clearHexes();
      if (!m.enabled || !m.toggles.gps || !m.hexes.length) return;
      var instances = [];
      m.hexes.forEach(function (h) {
        if (!h || !h.boundary || h.boundary.length < 3) return;
        var flat = [];
        h.boundary.forEach(function (c) { flat.push(c[0], c[1]); });
        var style = levelStyle(h.level);
        instances.push(new C.GeometryInstance({
          geometry: new C.PolygonGeometry({
            polygonHierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)),
            height: 0
          }),
          attributes: {
            color: C.ColorGeometryInstanceAttribute.fromColor(
              C.Color.fromCssColorString(style.color).withAlpha(style.alpha))
          },
          id: { type: 'gps-hex', sdaPick: function () { showHex(h); } }
        }));
      });
      if (!instances.length) return;
      m.hexPrim = viewer.scene.primitives.add(new C.Primitive({
        geometryInstances: instances,
        appearance: new C.PerInstanceColorAppearance({ flat: true, translucent: true }),
        asynchronous: true
      }));
    }

    function showHex(h) {
      m.cardHex = null;
      m.card.replaceChildren();
      m.card.appendChild(el('div', 'avi-alert', 'GPS interference (inferred)'));
      cardRow('Share', h.pct + '% of ' + h.aircraft + ' aircraft (' + h.degraded +
        ' with low integrity), level ' + h.level);
      cardRow('Centre', h.lat.toFixed(2) + ', ' + h.lon.toFixed(2));
      cardRow('Method', 'GPSJam: 100 x (degraded - 1) / aircraft over the last hour. ' +
        'From aircraft reports; not a measurement.');
    }

    // -- feeds --------------------------------------------------------------

    function applyFeed(list, flag) {
      var seen = Object.create(null);
      (list || []).forEach(function (a) {
        if (!a || typeof a.hex !== 'string' || !isNum(a.lat) || !isNum(a.lon)) return;
        seen[a.hex] = true;
        var tr = m.tracks[a.hex] || { hex: a.hex };
        // Never let an older report (the other feed's copy) overwrite a newer one.
        var older = isNum(tr.seen_at) && isNum(a.seen_at) && a.seen_at < tr.seen_at;
        if (!older) {
          ['callsign', 'registration', 'type', 'operator', 'military', 'private', 'lat', 'lon',
           'alt_m', 'on_ground', 'gs_kt', 'track', 'squawk', 'seen_at', 'gps']
            .forEach(function (k) { if (a[k] !== undefined) tr[k] = a[k]; });
        }
        delete m.civ[a.hex];
        tr[flag] = true;
        tr.kind = trackKind(tr);
        tr.sanctioned = isSanctioned(tr);
        m.tracks[a.hex] = tr;
      });
      // An aircraft gone from the latest squawk answer is no longer an emergency.
      Object.keys(m.tracks).forEach(function (hex) {
        var tr = m.tracks[hex];
        if (!tr[flag] || seen[hex]) return;
        tr[flag] = false;
        if (flag === 'inEmerg') tr.kind = trackKind(tr);
        if (!tr.inMil && !tr.inEmerg && tr.kind !== 'emerg' && !tr.military) delete m.tracks[hex];
      });
    }

    function feedNote(data) {
      if (!data || !data.feeds) return '';
      var waits = Object.keys(data.feeds).map(function (k) { return data.feeds[k]; })
        .filter(function (f) { return f && isNum(f.next_refresh_s); })
        .map(function (f) { return f.next_refresh_s; });
      if (!waits.length) return '';
      return 'next adsb.lol refresh in ' + fmtAge(Math.min.apply(null, waits));
    }

    function poll() {
      if (!m.enabled || ctx.isStatic) return Promise.resolve();
      var seq = ++m.pollSeq;
      var jobs = [];
      var notes = [];
      if (m.toggles.mil) {
        jobs.push(getJson('/api/aviation/military').then(function (d) {
          if (seq !== m.pollSeq || !m.enabled) return;
          syncClock(d);
          applyFeed(d.aircraft, 'inMil');
          notes.push((d.count || 0) + ' military' + (d.error ? ' (' + d.error + ')' : ''));
          var n = feedNote(d);
          if (n) notes.push(n);
        }).catch(function () { notes.push('military feed did not load'); }));
      }
      if (m.toggles.emerg) {
        jobs.push(getJson('/api/aviation/emergencies').then(function (d) {
          if (seq !== m.pollSeq || !m.enabled) return;
          syncClock(d);
          var before = Object.keys(m.tracks).filter(function (h) {
            return m.tracks[h].kind === 'emerg';
          });
          applyFeed(d.aircraft, 'inEmerg');
          (d.aircraft || []).forEach(function (a) {
            if (before.indexOf(a.hex) < 0 && SQUAWKS[a.squawk] && !a.on_ground) {
              note('Squawk ' + a.squawk + ' (' + SQUAWKS[a.squawk] + '): ' +
                trackName(a) + (a.type ? ' ' + a.type : ''));
            }
          });
          notes.push((d.count || 0) + ' squawking 7500/7600/7700');
        }).catch(function () { notes.push('squawk feed did not load'); }));
      }
      if (m.toggles.gps) jobs.push(loadGps(seq));
      if (Date.now() - m.sanctionsAt > SANCTIONS_TTL_MS) jobs.push(loadSanctions());
      return Promise.all(jobs).then(function () {
        if (seq !== m.pollSeq || !m.enabled) return;
        setStatus(notes.join(', ') + '.');
        redraw();
      });
    }

    function viewCentre() {
      var carto = null;
      try {
        var centre = viewer.camera.pickEllipsoid(
          new C.Cartesian2(viewer.canvas.clientWidth / 2, viewer.canvas.clientHeight / 2),
          viewer.scene.globe.ellipsoid);
        if (centre) carto = C.Cartographic.fromCartesian(centre);
      } catch (e) { carto = null; }
      if (!carto) carto = viewer.camera.positionCartographic;
      if (!carto) return null;
      return {
        lat: Math.round(carto.latitude * 180 / Math.PI * 100) / 100,
        lon: Math.round(wrapLon(carto.longitude * 180 / Math.PI) * 100) / 100
      };
    }

    function loadGps(seq) {
      var c = viewCentre();
      return getJson('/api/aviation/gps-interference' +
        query([['lat', c ? c.lat : null], ['lon', c ? c.lon : null]]))
        .then(function (d) {
          if (seq !== m.pollSeq || !m.enabled) return;
          m.gps = d;
          m.hexes = d.hexes || [];
          var high = m.hexes.filter(function (h) { return h.level === 'high'; }).length;
          m.gpsEl.textContent = m.hexes.length + ' hexes from ' + (d.aircraft_counted || 0) +
            ' aircraft in the last hour' + (high ? ', ' + high + ' over 10%' : '') + '.';
          drawHexes();
        }).catch(function () { m.gpsEl.textContent = 'GPS interference did not load.'; });
    }

    function loadSanctions() {
      m.sanctionsAt = Date.now();
      return getJson('/api/sanctions/index?kind=aircraft').then(function (d) {
        if (!d || !d.list_loaded) { m.sanctionsAt = 0; return; }
        var regs = Object.create(null);
        var hexes = Object.create(null);
        (d.regs || []).forEach(function (r) { var n = normReg(r); if (n) regs[n] = true; });
        (d.hex || []).forEach(function (h) {
          if (typeof h === 'string') hexes[h.toUpperCase()] = true;
        });
        m.sanctions = { regs: regs, hex: hexes };
        Object.keys(m.tracks).forEach(function (h) {
          m.tracks[h].sanctioned = isSanctioned(m.tracks[h]);
        });
      }).catch(function () { /* no sanctions service: show nothing */ });
    }

    function isSanctioned(tr) {
      var s = m.sanctions;
      if (!s || !tr || tr.private) return false;
      var reg = normReg(tr.registration);
      return !!((reg && s.regs[reg]) || (tr.hex && s.hex[String(tr.hex).toUpperCase()]));
    }

    // -- the page's Aircraft layer: carry dropped aircraft --------------------

    function entityLatLon(ent) {
      try {
        var p = ent.position && ent.position.getValue
          ? ent.position.getValue(C.JulianDate.now()) : null;
        if (!p) return null;
        var c = C.Cartographic.fromCartesian(p);
        return { lat: c.latitude * 180 / Math.PI, lon: c.longitude * 180 / Math.PI,
          h: c.height };
      } catch (e) { return null; }
    }

    // Follow a civil aircraft from the page's Aircraft layer once its card is
    // opened, so it can be carried on as an estimate if it drops out.
    function pinCivil(hex) {
      var fe = global.flightEntities;
      if (m.tracks[hex] || !fe || !fe['flight-' + hex]) return;
      if (!m.civ[hex]) {
        var keys = Object.keys(m.civ);
        if (keys.length >= MAX_PINNED) delete m.civ[keys[0]];
        m.civ[hex] = { hex: hex, kind: 'civ', civil: true, present: true };
      }
      watchCivil(nowS());
    }

    function watchCivil(t) {
      var fe = global.flightEntities;
      var ls = global.layerState;
      if (!m.toggles.est || !fe || !ls || !ls.flights) {
        m.civ = Object.create(null);
        return;
      }
      Object.keys(m.civ).forEach(function (icao) {
        var tr = m.civ[icao];
        var ent = fe['flight-' + icao];
        var p = ent ? entityLatLon(ent) : null;
        tr.present = !!p;
        if (!p) {
          if (positionAt(tr, t).state === 'lost') delete m.civ[icao];
          return;
        }
        if (tr.lat !== p.lat || tr.lon !== p.lon) {
          tr.lat = p.lat; tr.lon = p.lon; tr.alt_m = p.h; tr.seen_at = t;
        }
        var props = ent.properties || {};
        var v = prop(props.velocity);
        var hd = prop(props.heading);
        tr.callsign = typeof ent.name === 'string' ? ent.name.trim() : '';
        tr.gs_kt = isNum(v) ? v * KT_PER_MS : null;
        tr.track = isNum(hd) ? hd : null;
      });
    }

    function tick() {
      if (!m.enabled) return;
      watchCivil(nowS());
      redraw();
    }

    // -- the card -----------------------------------------------------------

    function cardRow(k, v, cls) {
      var r = el('div', 'avi-card-row');
      r.appendChild(el('span', 'avi-k', k));
      var val = el('span', 'avi-v' + (cls ? ' ' + cls : ''), v);
      r.appendChild(val);
      m.card.appendChild(r);
      return val;
    }

    function link(parent, url, text) {
      var safe = safeLiveAtc(url);
      if (!safe) return;
      var a = el('a', null, text);
      a.setAttribute('href', safe);
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
      parent.appendChild(a);
    }

    function airportText(ap) {
      if (!ap) return '';
      return ap.icao + (ap.iata ? '/' + ap.iata : '') + (ap.name ? ' ' + ap.name : '') +
        (ap.location ? ', ' + ap.location : '');
    }

    function positionText(card) {
      var p = card.position || {};
      if (p.state === 'reported') return 'Reported ' + fmtAge(p.age_s) + ' ago';
      if (p.state === 'estimated') {
        return 'ESTIMATED, not a report: last position ' + fmtAge(p.age_s) +
          ' ago, carried along track ' + Math.round(card.track) + ' at ' +
          Math.round(card.gs_kt) + ' kt';
      }
      return isNum(p.age_s) ? 'No usable position (last ' + fmtAge(p.age_s) + ' ago)'
        : 'No position known';
    }

    function renderCard(card, hints) {
      m.card.replaceChildren();
      var title = trackName(card) || String(card.hex).toUpperCase();
      m.card.appendChild(el('div', card.squawk && SQUAWKS[card.squawk] ? 'avi-alert' : null,
        title + (card.military ? ' (military)' : '')));
      if (card.private) {
        cardRow('Privacy', card.note || 'Identity withheld at the owner\'s request.');
      }
      cardRow('ICAO hex', String(card.hex).toUpperCase());
      if (!card.private) {
        cardRow('Callsign', card.callsign || '--');
        cardRow('Registration', card.registration || '--');
      }
      cardRow('Type', (card.type || '--') + (card.description ? ', ' + card.description : ''));
      if (!card.private) {
        cardRow('Operator', card.operator
          ? card.operator + (card.operator_source ? ' (' + card.operator_source + ')' : '')
          : '--');
      }
      if (card.squawk) {
        cardRow('Squawk', card.squawk + (SQUAWKS[card.squawk] ? ', ' + SQUAWKS[card.squawk] : ''),
          SQUAWKS[card.squawk] ? 'avi-alert' : null);
      }
      if (card.found) {
        cardRow('Altitude', fmtAlt(card.alt_m, card.on_ground));
        cardRow('Speed', fmtSpeed(card.gs_kt));
        if (isNum(card.track)) cardRow('Track', Math.round(card.track) + ' deg true');
      }
      cardRow('Position', positionText(card),
        card.position && card.position.state === 'estimated' ? 'avi-est' : null);
      if (isNum(card.nic) || isNum(card.nac_p)) {
        cardRow('GPS integrity', 'NIC ' + (isNum(card.nic) ? card.nic : '-') + ', NACp ' +
          (isNum(card.nac_p) ? card.nac_p : '-') +
          (card.gps === 'bad' ? ' (low: possible interference)' : card.gps === 'good' ? ' (normal)' : ''),
          card.gps === 'bad' ? 'avi-alert' : null);
      }
      var rt = card.route;
      if (rt && rt.origin && rt.destination) {
        cardRow('From', airportText(rt.origin));
        cardRow('To', airportText(rt.destination));
        var links = cardRow('LiveATC', '');
        link(links, rt.origin.liveatc, rt.origin.icao);
        link(links, rt.destination.liveatc, rt.destination.icao);
        cardRow('Route note', (rt.note || '') +
          (rt.plausible === false ? ' It does not match the current position.' : ''));
      } else if (!card.private) {
        cardRow('Route', 'No route on file for this callsign.');
      }
      m.sanctionEl = el('div');
      m.card.appendChild(m.sanctionEl);
      m.siteEl = el('div');
      m.card.appendChild(m.siteEl);
      var fly = button('mode-btn', 'FLY TO', 'Fly the camera to this aircraft');
      fly.addEventListener('click', function () {
        var p = card.position && isNum(card.position.lat) ? card.position : hints;
        flyTo(p, 120000);
      });
      m.card.appendChild(fly);
      if (card.sources && card.sources.length) {
        m.card.appendChild(el('div', 'avi-method', 'Sources: ' + card.sources.join('; ')));
      }
    }

    function checkSanctions(card, seq) {
      if (card.private) return Promise.resolve();
      var q = card.registration ? [['reg', card.registration]] : [['hex', card.hex]];
      return getJson('/api/sanctions/aircraft' + query(q)).then(function (s) {
        if (seq !== m.cardSeq || !s || !s.list_loaded) return;
        var box = m.sanctionEl;
        if (s.listed) {
          var r = el('div', 'avi-card-row');
          r.appendChild(el('span', 'avi-k', 'Sanctions'));
          r.appendChild(el('span', 'avi-v avi-alert', 'LISTED' +
            (s.programme ? ', ' + s.programme : '') + (s.name ? ': ' + s.name : '') +
            (s.source ? ' (' + s.source + ')' : '')));
          box.appendChild(r);
        } else if (s.possible_matches && s.possible_matches.length) {
          var r2 = el('div', 'avi-card-row');
          r2.appendChild(el('span', 'avi-k', 'Sanctions'));
          r2.appendChild(el('span', 'avi-v avi-est', 'Possible match on a previous ' +
            'registration, a lead only (' + s.possible_matches.length + ')'));
          box.appendChild(r2);
        }
      }).catch(function () { /* 404 or error: show nothing */ });
    }

    function nearestSite(card, seq) {
      var p = card.position && isNum(card.position.lat) ? card.position : null;
      if (!card.military || !p) return Promise.resolve();
      m.siteEl.textContent = 'Nearest military site: looking...';
      return getJson('/api/aviation/nearest-site' + query([['lat', p.lat.toFixed(3)],
        ['lon', p.lon.toFixed(3)]])).then(function (d) {
        if (seq !== m.cardSeq) return;
        var n = d && d.nearest;
        if (!n || !n.site) {
          m.siteEl.textContent = d && d.error ? 'Nearest military site did not load.'
            : 'No mapped military site within about 250 km.';
          return;
        }
        m.siteEl.textContent = 'Nearest mapped military site: ' + n.site.name +
          (n.site.kind ? ' (' + n.site.kind + ')' : '') + ', ' + n.distance_km + ' km, bearing ' +
          n.bearing_deg + ' deg. OpenStreetMap.';
      }).catch(function () {
        if (seq === m.cardSeq) m.siteEl.textContent = 'Nearest military site did not load.';
      });
    }

    function openCard(hex, hints) {
      if (typeof hex !== 'string' || !/^~?[0-9a-fA-F]{6}$/.test(hex.trim())) return null;
      hex = hex.trim().toLowerCase();
      hints = hints || {};
      if (ctx.isStatic) {
        m.card.replaceChildren();
        m.card.appendChild(el('div', null, 'Aircraft cards need the live server.'));
        return null;
      }
      var seq = ++m.cardSeq;
      m.cardHex = hex;
      if (m.enabled) pinCivil(hex);
      m.card.replaceChildren();
      m.card.appendChild(el('div', null, 'Loading ' + hex.toUpperCase() + '...'));
      var path = '/api/aviation/aircraft/' + encodeURIComponent(hex) + query([
        ['callsign', hints.private ? '' : (hints.callsign || '')],
        ['lat', isNum(hints.lat) ? hints.lat.toFixed(4) : null],
        ['lon', isNum(hints.lon) ? hints.lon.toFixed(4) : null]
      ]);
      return getJson(path).then(function (card) {
        if (seq !== m.cardSeq) return;
        syncClock(card);
        renderCard(card, hints);
        return Promise.all([checkSanctions(card, seq), nearestSite(card, seq)]);
      }).catch(function () {
        if (seq !== m.cardSeq) return;
        m.card.replaceChildren();
        m.card.appendChild(el('div', null, 'Aircraft card did not load.'));
      });
    }

    // Clicks on the page's own aircraft entities open the card too.
    function onClick(click) {
      if (!m.enabled) return;
      var picked;
      try { picked = viewer.scene.pick(click.position); } catch (e) { return; }
      var ent = picked && picked.id;
      if (!ent || typeof ent.sdaPick === 'function' || !ent.properties) return;
      var type = prop(ent.properties.type);
      if (type !== 'aircraft' && type !== 'military') return;
      var hex = prop(ent.properties.icao24);
      var p = entityLatLon(ent);
      openCard(String(hex || ''), {
        callsign: typeof ent.name === 'string' ? ent.name.trim() : '',
        lat: p ? p.lat : null, lon: p ? p.lon : null
      });
    }

    // -- toggles and enable ------------------------------------------------

    function setToggle(key, on) {
      m.toggles[key] = !!on;
      var b = m.toggleBtns[key];
      b.className = 'mode-btn' + (on ? ' active' : '');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (!m.enabled) return;
      if (key === 'gps') {
        if (!on) clearHexes();
        else poll();
      } else if (on && (key === 'mil' || key === 'emerg')) {
        poll();
      } else {
        redraw();
      }
    }

    function loadStatus() {
      return getJson('/api/aviation/status').then(function (s) {
        m.status = s;
        m.budgetEl.textContent = s.note || '';
      }).catch(function () {
        m.budgetEl.textContent = 'Aviation service did not answer.';
      });
    }

    function setEnabled(on) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'Aviation intel needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (on) {
        ensureLayers();
        m.icons.show = m.labels.show = true;
        note('Aviation intel enabled (adsb.lol)');
        loadStatus();
        poll();
        m.pollTimer = global.setInterval(poll, POLL_MS);
        m.tickTimer = global.setInterval(tick, TICK_MS);
      } else {
        if (m.pollTimer) { global.clearInterval(m.pollTimer); m.pollTimer = null; }
        if (m.tickTimer) { global.clearInterval(m.tickTimer); m.tickTimer = null; }
        m.pollSeq++;
        if (m.icons) {
          m.icons.removeAll();
          m.labels.removeAll();
          m.icons.show = m.labels.show = false;
        }
        clearHexes();
        m.tracks = Object.create(null);
        m.civ = Object.create(null);
        m.hexes = [];
        updateBanner([]);
        setCount(0);
        setStatus('');
        m.gpsEl.textContent = '';
      }
    }

    m.setEnabled = setEnabled;
    m.openCard = openCard;
    m.poll = poll;
    m.tick = tick;
    m.redraw = redraw;

    try {
      m.clickHandler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
      m.clickHandler.setInputAction(onClick, C.ScreenSpaceEventType.LEFT_CLICK);
    } catch (e) { m.clickHandler = null; }

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
      if (ctx.isStatic) current.summaryEl.textContent = 'Aviation intel needs the live server.';
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAAviation = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { return current ? current.poll() : null; },
    openCard: function (hex, hints) { return current ? current.openCard(hex, hints) : null; },
    _tick: function () { if (current) current.tick(); },
    _setClock: function (fn) {
      clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); };
    },
    REPORT_FRESH_S: REPORT_FRESH_S,
    DEAD_RECKON_MAX_S: DEAD_RECKON_MAX_S,
    SQUAWKS: SQUAWKS,
    LEVELS: LEVELS,
    ICONS: ICONS,
    deadReckon: deadReckon,
    positionAt: positionAt,
    fmtAge: fmtAge,
    fmtAlt: fmtAlt,
    fmtSpeed: fmtSpeed,
    normReg: normReg,
    safeLiveAtc: safeLiveAtc,
    query: query,
    trackName: trackName,
    trackKind: trackKind,
    labelFor: labelFor,
    bannerText: bannerText
  };
})(typeof window !== 'undefined' ? window : this);
