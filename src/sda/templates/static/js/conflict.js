// Armed conflict on the WorldView globe: front lines and control of territory,
// events verified against photo or video, and the historical record.
//
// Data (all from the server, sda/conflict.py, which caches and credits each):
//   Russia-Ukraine   DeepStateMap areas and a line of contact derived from them,
//                    any day back to April 2022 (date picker).
//   Other theatres   Control of settlements from the Wikipedia "detailed map"
//                    modules (Sudan, Myanmar, Sahel, Yemen, Syria, Israel and
//                    Gaza, Lebanon, Somalia, Ethiopia, CAR, Libya, Nigeria,
//                    Mozambique), CC BY-SA 4.0, each with its revision date.
//   Verified events  GeoConfirmed, 90 days, every point linking to its sources.
//   History          UCDP Georeferenced Event Dataset (CC BY 4.0) as a density
//                    grid with a year filter, single events when close.
//   ACLED            Only when the server holds an ACLED account.
//
// Every item drawn carries its source link and date, shown when it is clicked.
// No text is ever set as HTML, and links are only ever http(s) URLs.
// Frames: geodetic degrees only.
//
// Page wiring: listed in EXTENSIONS as { global: 'SDAConflict', mount: 'ext-conflict' };
// the Conflict layer toggle calls SDAConflict.setEnabled(on), and the page's click
// handler calls picked.id.sdaPick() for the points this module draws. Areas and
// lines are entities, which the page's own entity panel describes by name.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Armed conflict';
  var STYLE_ID = 'sda-conflict-style';
  var COUNT_ID = 'count-conflict';
  var LAYER_ID = 'layer-conflict';

  // Below this camera height UCDP single events are drawn; above it, the grid.
  var HISTORY_DETAIL_ALT_M = 700000;
  var HISTORY_LIMIT = 800;
  var MOVE_DEBOUNCE_MS = 400;
  var FIRST_UKRAINE_DAY = '2022-04-03';

  var UKRAINE_STYLE = {
    held_2014: { fill: '#880e4f', alpha: 0.34 },
    held: { fill: '#a52714', alpha: 0.34 },
    contested: { fill: '#bcaaa4', alpha: 0.42 },
    retaken: { fill: '#0f9d58', alpha: 0.22 }
  };
  var CONTACT_LINE = '#ff3b1f';

  var UCDP_TYPES = [
    { id: 1, label: 'STATE', title: 'State-based conflict', color: '#e4572e' },
    { id: 2, label: 'NON-STATE', title: 'Non-state conflict', color: '#f6c026' },
    { id: 3, label: 'ONE-SIDED', title: 'One-sided violence against civilians', color: '#c2189b' }
  ];

  var ACLED_COLORS = {
    'Battles': '#e4572e',
    'Explosions/Remote violence': '#ff9f1c',
    'Violence against civilians': '#c2189b',
    'Protests': '#2ec4b6',
    'Riots': '#3a86ff',
    'Strategic developments': '#8d99ae'
  };

  var DAY_CHOICES = [7, 30, 90];

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

  function bboxParam(box) { return [box.west, box.south, box.east, box.north].join(','); }

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

  // The URL when it is an absolute http(s) link with no spaces or quotes, else ''.
  function safeHref(url) {
    return (typeof url === 'string' && /^https?:\/\/[^\s<>"'`]+$/i.test(url)) ? url : '';
  }

  // "x.com" from "https://x.com/a/status/1".
  function hostOf(url) {
    var m = /^https?:\/\/([^\/?#:]+)/i.exec(str(url));
    return m ? m[1].replace(/^www\./i, '').toLowerCase() : '';
  }

  // [[lon, lat], ...] -> [lon, lat, lon, lat, ...], skipping bad positions.
  function flatPositions(ring) {
    var out = [];
    (ring || []).forEach(function (p) {
      if (p && isNum(p[0]) && isNum(p[1])) out.push(p[0], p[1]);
    });
    return out;
  }

  // UCDP grid cell (degrees) for a camera height, as the server snaps it.
  function historyCell(heightM) {
    if (!isNum(heightM) || heightM > 9e6) return 4;
    if (heightM > 4.5e6) return 2;
    if (heightM > 2.2e6) return 1;
    if (heightM > 1.2e6) return 0.5;
    return 0.25;
  }

  function historyMode(heightM) {
    return isNum(heightM) && heightM <= HISTORY_DETAIL_ALT_M ? 'detail' : 'density';
  }

  // Yellow for a handful of deaths to deep red for thousands.
  function deathColor(deaths) {
    var t = Math.max(0, Math.min(1, Math.log((deaths || 0) + 1) / Math.log(5001)));
    var r = 255;
    var g = Math.round(214 - 214 * t);
    var b = Math.round(102 - 80 * t);
    if (t > 0.75) r = Math.round(255 - (t - 0.75) * 260);
    var hex = function (v) { var h = v.toString(16); return h.length < 2 ? '0' + h : h; };
    return '#' + hex(r) + hex(g) + hex(b);
  }

  function densitySize(events) {
    if (!isNum(events) || events <= 0) return 0;
    return Math.min(30, 3 + 3.2 * Math.log(events + 1) / Math.LN10);
  }

  function deathSize(deaths) {
    return Math.min(18, 4 + 2.4 * Math.log((deaths || 0) + 1) / Math.LN10);
  }

  // A year range inside [min, max], in order.
  function clampYears(from, to, min, max) {
    var lo = isNum(from) ? Math.round(from) : min;
    var hi = isNum(to) ? Math.round(to) : max;
    lo = Math.max(min, Math.min(max, lo));
    hi = Math.max(min, Math.min(max, hi));
    if (lo > hi) { var t = lo; lo = hi; hi = t; }
    return { from: lo, to: hi };
  }

  // "1,3" for the selected UCDP types; '' when all or none are selected.
  function typesParam(selected) {
    var on = UCDP_TYPES.filter(function (t) { return selected[t.id]; })
      .map(function (t) { return t.id; });
    return (on.length === 0 || on.length === UCDP_TYPES.length) ? '' : on.join(',');
  }

  function isoDay(d) {
    return d.getUTCFullYear() + '-' + ('0' + (d.getUTCMonth() + 1)).slice(-2) + '-' +
      ('0' + d.getUTCDate()).slice(-2);
  }

  function daysBefore(iso, days) {
    var t = Date.parse(iso + 'T00:00:00Z');
    if (!isFinite(t)) return '';
    return isoDay(new Date(t - days * 86400000));
  }

  // Events newer than `days` before `today`, of one conflict when given.
  function recentEvents(events, days, today, conflict) {
    var since = daysBefore(today, days);
    return (events || []).filter(function (e) {
      return e && str(e.date) >= since && (!conflict || e.conflict === conflict);
    });
  }

  // The parties with the most marks across the loaded theatres, for the legend.
  function partyLegend(maps, limit) {
    var rows = [];
    (maps || []).forEach(function (cm) {
      (cm && cm.legend || []).forEach(function (p) {
        if (p && p.count > 0) {
          rows.push({ party: str(p.party), hex: str(p.hex) || '#b0b0b0', count: p.count,
            theatre: str(cm.name) });
        }
      });
    });
    rows.sort(function (a, b) { return b.count - a.count; });
    return rows.slice(0, limit || 14);
  }

  function shortDate(iso) { return str(iso).slice(0, 10); }

  function eventLines(e) {
    var lines = [];
    lines.push([shortDate(e.date), str(e.conflict_name) || str(e.conflict)].filter(Boolean)
      .join(' | '));
    var who = [str(e.faction), str(e.media) ? 'evidence: ' + e.media : ''].filter(Boolean);
    if (who.length) lines.push(who.join(' | '));
    if (str(e.place)) lines.push(e.place);
    if (str(e.text)) lines.push(e.text);
    if (str(e.equipment)) lines.push('Equipment: ' + e.equipment);
    if (str(e.units)) lines.push('Units: ' + e.units);
    return lines;
  }

  function eventLinks(e) {
    var links = [];
    var n = 0;
    (e.sources || []).forEach(function (u) {
      if (!safeHref(u)) return;
      n += 1;
      links.push({ href: u, text: 'Source ' + n + ' (' + hostOf(u) + ')' });
    });
    (e.geolocation || []).forEach(function (u) {
      if (safeHref(u)) links.push({ href: u, text: 'Geolocation (' + hostOf(u) + ')' });
    });
    if (safeHref(e.url)) links.push({ href: e.url, text: 'GeoConfirmed' });
    return links;
  }

  function ucdpLines(e) {
    var lines = [];
    lines.push([shortDate(e.date) + (e.date_end && e.date_end !== e.date ? ' to ' +
      shortDate(e.date_end) : ''), str(e.type_name)].filter(Boolean).join(' | '));
    if (str(e.conflict)) lines.push(e.conflict);
    if (str(e.side_a) || str(e.side_b)) lines.push(str(e.side_a) + ' vs ' + str(e.side_b));
    lines.push([str(e.place), str(e.country)].filter(Boolean).join(', '));
    lines.push('Deaths, best estimate ' + withCommas(e.best) + ' (range ' + withCommas(e.low) +
      ' to ' + withCommas(e.high) + ')' + (e.civilian_deaths ? ', civilians ' +
      withCommas(e.civilian_deaths) : ''));
    if (str(e.source)) lines.push('Reported by: ' + e.source);
    if (str(e.dataset)) lines.push(e.dataset);
    return lines.filter(Boolean);
  }

  function acledLines(e) {
    var lines = [];
    lines.push([shortDate(e.date), str(e.type), str(e.sub_type)].filter(Boolean).join(' | '));
    var actors = [str(e.actor1), str(e.actor2)].filter(Boolean).join(' vs ');
    if (actors) lines.push(actors);
    lines.push([str(e.place), str(e.country)].filter(Boolean).join(', '));
    if (isNum(e.fatalities)) lines.push('Reported fatalities: ' + e.fatalities);
    if (str(e.text)) lines.push(e.text);
    if (str(e.source)) lines.push('Sources: ' + e.source);
    return lines.filter(Boolean);
  }

  var MARK_KIND_TEXT = {
    control: 'held by', presence: 'rural presence of', hill: 'height held by',
    contested: 'contested between', mixed: 'shared control of'
  };

  function markLines(mk, cm) {
    var parties = (mk.p || []).map(function (i) {
      var p = cm.legend && cm.legend[i];
      return p ? str(p.party) : '';
    }).filter(Boolean);
    return [
      (MARK_KIND_TEXT[mk.kind] || 'control:') + ' ' + parties.join(' and '),
      str(cm.name) + ', per Wikipedia revision of ' + shortDate(cm.revised_at)
    ];
  }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.conflict-summary, .conflict-attr, .conflict-view { font-size: 9px; line-height: 1.4;',
    '  color: var(--hud-dim, #7a5500); }',
    '.conflict-attr { font-size: 8px; opacity: 0.85; margin-top: 4px; }',
    '.conflict-sec { margin-top: 6px; border-top: 1px solid rgba(255,176,0,0.15);',
    '  padding-top: 4px; }',
    '.conflict-sec-title { font-size: 9px; letter-spacing: 1px; color: var(--hud-color, #ffb000);',
    '  margin-bottom: 3px; }',
    '.conflict-row { display: flex; gap: 4px; align-items: center; margin-bottom: 4px;',
    '  font-size: 9px; flex-wrap: wrap; }',
    '.conflict-row select, .conflict-row input[type=date], .conflict-row input[type=number] {',
    '  background: rgba(0,0,0,0.6); border: 1px solid rgba(255,176,0,0.3);',
    '  color: var(--hud-color, #ffb000); font: inherit; font-size: 9px; padding: 2px 3px; }',
    '.conflict-row input[type=number] { width: 52px; }',
    '.conflict-row select { max-width: 100%; flex: 1; }',
    '.conflict-row .mode-btn { font-size: 8px; padding: 2px 4px; }',
    '.conflict-legend { display: flex; flex-wrap: wrap; gap: 3px 8px; font-size: 8px;',
    '  color: var(--hud-dim, #7a5500); margin-bottom: 3px; }',
    '.conflict-swatch { width: 8px; height: 8px; display: inline-block; margin-right: 3px;',
    '  border: 1px solid #000; vertical-align: middle; }',
    '.conflict-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px;',
    '  margin-top: 4px; }',
    '.conflict-info { font-size: 9px; color: var(--hud-color, #ffb000); margin-top: 3px;',
    '  max-height: 180px; overflow-y: auto; line-height: 1.35; }',
    '.conflict-info .conflict-info-title { font-weight: bold; }',
    '.conflict-info a { color: #7fd4ff; margin-right: 8px; display: inline-block; }'
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

  function checkbox(labelText, checked) {
    var label = el('label');
    var box = el('input');
    box.setAttribute('type', 'checkbox');
    box.checked = !!checked;
    label.appendChild(box);
    label.appendChild(el('span', null, ' ' + labelText));
    return { label: label, box: box };
  }

  function option(value, text) {
    var o = el('option', null, text);
    o.setAttribute('value', value);
    o.value = value;
    return o;
  }

  function swatch(hex) {
    var s = el('span', 'conflict-swatch');
    s.style.background = hex;
    return s;
  }

  function link(href, text) {
    var a = el('a', null, text);
    a.setAttribute('href', href);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, summary: null, seq: { front: 0, events: 0, history: 0,
        acled: 0 }, timer: null,
      sub: { front: true, events: true, history: false, acled: false },
      theatre: 'all', ukraineDate: '', ukraine: null, control: {}, controlOrder: [],
      gc: null, gcDays: 90, gcConflict: '', history: null, historyMode: '',
      historyKey: null, years: null, types: { 1: true, 2: true, 3: true }, acled: null,
      ds: null, points: null, counts: { front: 0, control: 0, events: 0, history: 0, acled: 0 }
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
    m.summaryEl = el('div', 'conflict-summary',
      'Turn on the Conflict layer to load front lines, verified events and history.');

    // Front lines and control
    var secFront = el('div', 'conflict-sec');
    secFront.appendChild(el('div', 'conflict-sec-title', 'FRONT LINES AND CONTROL'));
    var frontRow = el('div', 'conflict-row');
    m.frontBox = checkbox('Show', true);
    m.theatreSel = el('select');
    m.theatreSel.setAttribute('aria-label', 'Theatre');
    m.theatreSel.appendChild(option('all', 'All theatres'));
    m.theatreSel.appendChild(option('ukraine', 'Russia-Ukraine'));
    m.theatreSel.value = 'all';
    frontRow.appendChild(m.frontBox.label);
    frontRow.appendChild(m.theatreSel);
    var dateRow = el('div', 'conflict-row');
    dateRow.appendChild(el('span', null, 'Ukraine map of'));
    m.dateInput = el('input');
    m.dateInput.setAttribute('type', 'date');
    m.dateInput.setAttribute('min', FIRST_UKRAINE_DAY);
    m.dateInput.setAttribute('max', isoDay(new Date()));
    m.dateInput.setAttribute('aria-label', 'Date of the Russia-Ukraine map');
    m.latestBtn = button('mode-btn active', 'LATEST', 'The newest DeepStateMap update');
    dateRow.appendChild(m.dateInput);
    dateRow.appendChild(m.latestBtn);
    m.frontInfo = el('div', 'conflict-view');
    m.legendEl = el('div', 'conflict-legend');
    [frontRow, dateRow, m.frontInfo, m.legendEl].forEach(function (n) { secFront.appendChild(n); });

    // Verified events
    var secEvents = el('div', 'conflict-sec');
    secEvents.appendChild(el('div', 'conflict-sec-title', 'VERIFIED EVENTS (PHOTO OR VIDEO)'));
    var evRow = el('div', 'conflict-row');
    m.eventsBox = checkbox('Show', true);
    m.daysSel = el('select');
    m.daysSel.setAttribute('aria-label', 'Days of events');
    DAY_CHOICES.forEach(function (d) { m.daysSel.appendChild(option(String(d), d + ' days')); });
    m.daysSel.value = '90';
    m.conflictSel = el('select');
    m.conflictSel.setAttribute('aria-label', 'Conflict');
    m.conflictSel.appendChild(option('', 'All conflicts'));
    m.conflictSel.value = '';
    [m.eventsBox.label, m.daysSel, m.conflictSel].forEach(function (n) { evRow.appendChild(n); });
    m.eventsInfo = el('div', 'conflict-view');
    secEvents.appendChild(evRow);
    secEvents.appendChild(m.eventsInfo);

    // History
    var secHist = el('div', 'conflict-sec');
    secHist.appendChild(el('div', 'conflict-sec-title', 'HISTORY (UCDP, SINCE 1989)'));
    var hRow = el('div', 'conflict-row');
    m.historyBox = checkbox('Show', false);
    m.yearFrom = el('input');
    m.yearFrom.setAttribute('type', 'number');
    m.yearFrom.setAttribute('aria-label', 'From year');
    m.yearTo = el('input');
    m.yearTo.setAttribute('type', 'number');
    m.yearTo.setAttribute('aria-label', 'To year');
    [m.historyBox.label, el('span', null, 'from'), m.yearFrom, el('span', null, 'to'),
     m.yearTo].forEach(function (n) { hRow.appendChild(n); });
    var tRow = el('div', 'conflict-row');
    m.typeBtns = UCDP_TYPES.map(function (t) {
      var b = button('mode-btn active', t.label, t.title);
      b.setAttribute('aria-pressed', 'true');
      b.addEventListener('click', function () { toggleType(t.id); });
      tRow.appendChild(b);
      return b;
    });
    m.historyInfo = el('div', 'conflict-view');
    [hRow, tRow, m.historyInfo].forEach(function (n) { secHist.appendChild(n); });

    // ACLED
    var secAcled = el('div', 'conflict-sec');
    secAcled.appendChild(el('div', 'conflict-sec-title', 'ACLED (ACCOUNT NEEDED)'));
    var aRow = el('div', 'conflict-row');
    m.acledBox = checkbox('Show the last 30 days', false);
    aRow.appendChild(m.acledBox.label);
    m.acledInfo = el('div', 'conflict-view');
    secAcled.appendChild(aRow);
    secAcled.appendChild(m.acledInfo);

    m.status = el('div', 'conflict-status');
    m.status.setAttribute('role', 'status');
    m.info = el('div', 'conflict-info');
    m.attrEl = el('div', 'conflict-attr');
    [m.summaryEl, secFront, secEvents, secHist, secAcled, m.status, m.info, m.attrEl]
      .forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.status.textContent = msg; }

    function setCount() {
      var c = m.counts;
      var n = c.front + c.control + c.events + c.history + c.acled;
      try {
        var node = global.document.getElementById(COUNT_ID);
        if (node) node.textContent = formatCount(n);
      } catch (e) { /* keep going */ }
      return n;
    }

    function showInfo(title, lines, links) {
      m.info.replaceChildren();
      m.info.appendChild(el('div', 'conflict-info-title', title));
      (lines || []).forEach(function (t) { if (t) m.info.appendChild(el('div', null, t)); });
      var row = el('div');
      (links || []).forEach(function (l) {
        var href = safeHref(l.href);
        if (href) row.appendChild(link(href, l.text));
      });
      if (row.children.length) m.info.appendChild(row);
    }

    function updateAttribution() {
      var s = m.summary && m.summary.attribution || {};
      var parts = [];
      if (m.sub.front) {
        parts.push(str(s.frontline));
        var mods = m.controlOrder.map(function (k) { return m.control[k]; })
          .filter(function (cm) { return cm && cm.attribution; });
        if (mods.length) {
          parts.push('Control of settlements: Wikipedia contributors, ' + mods.length +
            ' detailed-map modules (CC BY-SA 4.0); each mark links to its revision.');
        }
      }
      if (m.sub.events) parts.push(str(s.events));
      if (m.sub.history) parts.push(str(s.history));
      if (m.sub.acled && m.acled && m.acled.configured) parts.push(str(s.acled));
      m.attrEl.textContent = parts.filter(Boolean).join(' ');
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.points) return;
      m.points = {
        control: viewer.scene.primitives.add(new C.PointPrimitiveCollection()),
        events: viewer.scene.primitives.add(new C.PointPrimitiveCollection()),
        history: viewer.scene.primitives.add(new C.PointPrimitiveCollection()),
        acled: viewer.scene.primitives.add(new C.PointPrimitiveCollection())
      };
      m.ds = new C.CustomDataSource('conflict-front');
      try { viewer.dataSources.add(m.ds); } catch (e) { /* headless */ }
    }

    function showLayers(on) {
      if (!m.points) return;
      Object.keys(m.points).forEach(function (k) { m.points[k].show = on; });
      m.ds.show = on;
    }

    function flyTo(lat, lon, height) {
      try {
        viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(lon, lat, height),
          duration: 1.8 });
      } catch (e) { /* keep going */ }
    }

    // Russia-Ukraine areas and the line of contact, as entities.
    function drawUkraine() {
      if (!m.ds) return;
      m.ds.entities.removeAll();
      m.counts.front = 0;
      var u = m.ukraine;
      if (!m.enabled || !m.sub.front || !u || (m.theatre !== 'all' && m.theatre !== 'ukraine')) {
        return;
      }
      var day = shortDate(u.snapshot_at);
      (u.areas || []).forEach(function (a) {
        var st = UKRAINE_STYLE[a.kind];
        var rings = a.rings || [];
        if (!st || !rings.length) return;
        var outer = flatPositions(rings[0]);
        if (outer.length < 6) return;
        var holes = rings.slice(1).map(function (r) {
          return new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flatPositions(r)));
        });
        var legend = (u.legend && u.legend[a.kind]) || a.kind;
        m.ds.entities.add({
          name: legend + ' (DeepStateMap, ' + day + (a.note ? ', ' + a.note : '') + ')',
          polygon: {
            hierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(outer), holes),
            material: C.Color.fromCssColorString(st.fill).withAlpha(st.alpha)
          },
          properties: {
            type: 'conflict area', source: 'DeepStateMap', date: day,
            detail_rows: [['CONTROL', String(legend)], ['AS OF', day]]
              .concat(a.note ? [['NOTE', String(a.note)]] : [])
              .concat([['SOURCE', 'DeepStateMap, volunteer analysts, redrawn daily']])
          }
        });
        m.counts.front++;
      });
      var lines = u.lines || [];
      if (u.line_method === 'outline' || !lines.length) {
        lines = [];
        (u.areas || []).forEach(function (a) {
          if (a.kind === 'held' && a.rings && a.rings[0]) lines.push(a.rings[0]);
        });
      }
      lines.forEach(function (ln) {
        var flat = flatPositions(ln);
        if (flat.length < 4) return;
        m.ds.entities.add({
          name: 'Line of contact (derived from DeepStateMap, ' + day + ')',
          polyline: {
            positions: C.Cartesian3.fromDegreesArray(flat), width: 3, clampToGround: true,
            material: C.Color.fromCssColorString(CONTACT_LINE)
          },
          properties: {
            type: 'line of contact', source: 'DeepStateMap', date: day,
            detail_rows: [
              ['MEANING', 'Edge of the ground held by Russian forces'], ['AS OF', day],
              ['SOURCE', u.line_method === 'outline' || !(u.lines || []).length
                ? 'Outline of the DeepStateMap occupied area' : 'DeepStateMap front line']
            ]
          }
        });
      });
    }

    function markPick(mk, cm) {
      return {
        type: 'conflict-control',
        sdaPick: function () {
          var links = [];
          if (mk.wiki) {
            links.push({ href: 'https://en.wikipedia.org/wiki/' +
              encodeURIComponent(mk.wiki.replace(/ /g, '_')), text: 'Place on Wikipedia' });
          }
          links.push({ href: cm.url, text: 'Map module revision' });
          links.push({ href: cm.history_url, text: 'Edit history' });
          showInfo(mk.name || 'Unnamed place', markLines(mk, cm), links);
        }
      };
    }

    function drawControl() {
      var coll = m.points && m.points.control;
      if (!coll) return;
      coll.removeAll();
      m.counts.control = 0;
      if (!m.enabled || !m.sub.front) return;
      m.controlOrder.forEach(function (slug) {
        if (m.theatre !== 'all' && m.theatre !== slug) return;
        var cm = m.control[slug];
        if (!cm || !cm.marks) return;
        var colors = (cm.legend || []).map(function (p) {
          return C.Color.fromCssColorString(str(p.hex) || '#b0b0b0');
        });
        cm.marks.forEach(function (mk) {
          var p = mk.p || [];
          var fill = colors[p[0]];
          if (!fill || !isNum(mk.lat) || !isNum(mk.lon)) return;
          var two = p.length > 1 && colors[p[1]];
          var size = mk.kind === 'presence' ? 4 : mk.kind === 'hill' ? 5 : 7;
          coll.add({
            position: C.Cartesian3.fromDegrees(mk.lon, mk.lat, 0),
            pixelSize: two ? 8 : size, color: fill,
            outlineColor: two ? colors[p[1]] : C.Color.BLACK.withAlpha(0.7),
            outlineWidth: two ? 3 : 1,
            scaleByDistance: new C.NearFarScalar(2e5, 1.4, 8e6, 0.55),
            id: markPick(mk, cm)
          });
          m.counts.control++;
        });
      });
    }

    function drawLegend() {
      m.legendEl.replaceChildren();
      if (!m.sub.front) return;
      var items = [];
      if (m.ukraine && (m.theatre === 'all' || m.theatre === 'ukraine')) {
        Object.keys(UKRAINE_STYLE).forEach(function (k) {
          items.push({ hex: UKRAINE_STYLE[k].fill, text: (m.ukraine.legend || {})[k] || k });
        });
        items.push({ hex: CONTACT_LINE, text: 'Line of contact (derived)' });
      }
      var maps = m.controlOrder.filter(function (k) {
        return m.theatre === 'all' || m.theatre === k;
      }).map(function (k) { return m.control[k]; });
      partyLegend(maps, m.theatre === 'all' ? 10 : 16).forEach(function (p) {
        items.push({ hex: p.hex, text: p.party + (m.theatre === 'all' ? ' (' + p.theatre + ')'
          : '') });
      });
      items.forEach(function (it) {
        var span = el('span');
        span.appendChild(swatch(it.hex));
        span.appendChild(el('span', null, it.text));
        m.legendEl.appendChild(span);
      });
    }

    function frontSummary() {
      var parts = [];
      var u = m.ukraine;
      if (u && (m.theatre === 'all' || m.theatre === 'ukraine')) {
        if (u.error) parts.push('Russia-Ukraine: ' + u.error + '.');
        else {
          parts.push('Russia-Ukraine as of ' + shortDate(u.snapshot_at) + ' (DeepStateMap' +
            (u.stale ? ', cached copy' : '') + ').');
        }
      }
      var loaded = m.controlOrder.filter(function (k) {
        return (m.theatre === 'all' || m.theatre === k) && m.control[k] && !m.control[k].error;
      });
      if (loaded.length === 1) {
        var cm = m.control[loaded[0]];
        parts.push(cm.name + ': ' + withCommas(m.counts.control) +
          ' places, Wikipedia revision of ' + shortDate(cm.revised_at) + '.');
      } else if (loaded.length > 1) {
        parts.push(loaded.length + ' other theatres, ' + withCommas(m.counts.control) +
          ' places.');
      }
      m.frontInfo.textContent = parts.join(' ');
    }

    function redrawFront() {
      drawUkraine();
      drawControl();
      drawLegend();
      frontSummary();
      updateAttribution();
      setCount();
    }

    function eventPick(e) {
      return {
        type: 'conflict-event', id: e.id,
        sdaPick: function () { openEvent(e); }
      };
    }

    function openEvent(e) {
      showInfo('Verified event', [shortDate(e.date) + ' | ' + str(e.faction)], []);
      return getJson('/api/conflict/events/' + encodeURIComponent(e.id)).then(function (full) {
        var title = str(full.title) || str(full.place) || 'Verified event';
        showInfo(title, eventLines(full), eventLinks(full));
        note('Conflict: ' + title + ', ' + shortDate(full.date));
      }).catch(function () {
        showInfo('Verified event', [shortDate(e.date) + ' | ' + str(e.faction),
          'Details did not load.'], []);
      });
    }

    function drawEvents() {
      var coll = m.points && m.points.events;
      if (!coll) return;
      coll.removeAll();
      m.counts.events = 0;
      if (!m.enabled || !m.sub.events || !m.gc) return;
      var list = recentEvents(m.gc.events, m.gcDays, isoDay(new Date()), m.gcConflict);
      var cache = {};
      list.forEach(function (e) {
        if (!isNum(e.lat) || !isNum(e.lon)) return;
        var css = str(e.color) || '#999999';
        var col = cache[css] || (cache[css] = C.Color.fromCssColorString(css));
        coll.add({
          position: C.Cartesian3.fromDegrees(e.lon, e.lat, 0), pixelSize: 6, color: col,
          outlineColor: C.Color.WHITE.withAlpha(0.75), outlineWidth: 1,
          scaleByDistance: new C.NearFarScalar(2e5, 1.5, 8e6, 0.6),
          id: eventPick(e)
        });
        m.counts.events++;
      });
      var label = m.gcConflict ? ((m.gc.conflict_names || {})[m.gcConflict] || m.gcConflict) :
        'all conflicts';
      m.eventsInfo.textContent = m.gc.error && !list.length
        ? 'GeoConfirmed: ' + m.gc.error + '.'
        : withCommas(m.counts.events) + ' events in the last ' + m.gcDays + ' days, ' + label +
          (m.gc.fetched_at ? ', fetched ' + str(m.gc.fetched_at).slice(0, 16).replace('T', ' ') +
          ' UTC' : '') + (m.gc.stale ? ' (cached copy)' : '') + '.';
      setCount();
    }

    function fillConflicts() {
      var keep = m.gcConflict;
      m.conflictSel.replaceChildren();
      m.conflictSel.appendChild(option('', 'All conflicts'));
      var counts = (m.gc && m.gc.conflicts) || {};
      var names = (m.gc && m.gc.conflict_names) || {};
      Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; })
        .forEach(function (k) {
          m.conflictSel.appendChild(option(k, (names[k] || k) + ' (' + counts[k] + ')'));
        });
      m.conflictSel.value = counts[keep] ? keep : '';
      m.gcConflict = m.conflictSel.value;
    }

    function historyPick(e) {
      return {
        type: 'conflict-history', id: e.id,
        sdaPick: function () {
          showInfo(str(e.dyad) || str(e.conflict) || 'UCDP event', ucdpLines(e),
            [{ href: e.url, text: 'UCDP conflict page' }]);
        }
      };
    }

    function cellPick(c) {
      return {
        type: 'conflict-history-cell',
        sdaPick: function () {
          showInfo('UCDP events here, ' + m.years.from + ' to ' + m.years.to,
            [withCommas(c.events) + ' events, ' + withCommas(c.deaths) +
             ' deaths (best estimate). Zoom in for single events.'], []);
          flyTo(c.lat, c.lon, HISTORY_DETAIL_ALT_M * 0.8);
        }
      };
    }

    function drawHistory() {
      var coll = m.points && m.points.history;
      if (!coll) return;
      coll.removeAll();
      m.counts.history = 0;
      var h = m.history;
      if (!m.enabled || !m.sub.history || !h) return;
      var cache = {};
      function color(css) { return cache[css] || (cache[css] = C.Color.fromCssColorString(css)); }
      if (m.historyMode === 'density') {
        (h.cells || []).forEach(function (c) {
          coll.add({
            position: C.Cartesian3.fromDegrees(c.lon, c.lat, 0), pixelSize: densitySize(c.events),
            color: color(deathColor(c.deaths)).withAlpha(0.6),
            outlineColor: C.Color.BLACK.withAlpha(0.5), outlineWidth: 1, id: cellPick(c)
          });
        });
        m.counts.history = h.events || 0;
        m.historyInfo.textContent = withCommas(h.events) + ' events, ' + withCommas(h.deaths) +
          ' deaths in view, ' + h.year_from + ' to ' + h.year_to + ' (' + h.cell_deg +
          ' degree cells). Zoom in for single events.';
      } else {
        var typeColor = {};
        UCDP_TYPES.forEach(function (t) { typeColor[t.id] = color(t.color); });
        (h.events || []).forEach(function (e) {
          coll.add({
            position: C.Cartesian3.fromDegrees(e.lon, e.lat, 0), pixelSize: deathSize(e.best),
            color: (typeColor[e.type] || color('#999999')).withAlpha(0.85),
            outlineColor: C.Color.BLACK.withAlpha(0.6), outlineWidth: 1, id: historyPick(e)
          });
        });
        m.counts.history = (h.events || []).length;
        m.historyInfo.textContent = h.capped
          ? 'The ' + withCommas(h.count) + ' deadliest of ' + withCommas(h.total) +
            (h.total_capped ? '+' : '') + ' events in view.'
          : withCommas(h.count) + ' events in view.';
      }
      setCount();
    }

    function acledPick(e) {
      return {
        type: 'conflict-acled', id: e.id,
        sdaPick: function () {
          getJson('/api/conflict/acled/' + encodeURIComponent(e.id)).then(function (full) {
            showInfo('ACLED ' + str(full.id), acledLines(full),
              [{ href: full.url, text: 'ACLED' }]);
          }).catch(function () { showInfo('ACLED event', ['Details did not load.'], []); });
        }
      };
    }

    function drawAcled() {
      var coll = m.points && m.points.acled;
      if (!coll) return;
      coll.removeAll();
      m.counts.acled = 0;
      var a = m.acled;
      if (!m.enabled || !m.sub.acled || !a) return;
      if (!a.configured || a.error) {
        m.acledInfo.textContent = str(a.error) || 'ACLED is not configured on the server.';
        setCount();
        return;
      }
      var cache = {};
      (a.events || []).forEach(function (e) {
        var css = ACLED_COLORS[e.type] || '#b0b0b0';
        coll.add({
          position: C.Cartesian3.fromDegrees(e.lon, e.lat, 0), pixelSize: 5,
          color: cache[css] || (cache[css] = C.Color.fromCssColorString(css)),
          outlineColor: C.Color.BLACK.withAlpha(0.6), outlineWidth: 1, id: acledPick(e)
        });
        m.counts.acled++;
      });
      m.acledInfo.textContent = withCommas(m.counts.acled) + ' ACLED events in the last ' +
        a.days + ' days.';
      setCount();
    }

    // -- loading ------------------------------------------------------------

    function loadUkraine() {
      var seq = ++m.seq.front;
      var path = '/api/conflict/frontline/ukraine' + query([['date', m.ukraineDate]]);
      setStatus('Loading the Russia-Ukraine map...');
      return getJson(path).then(function (u) {
        if (seq !== m.seq.front || !m.enabled) return;
        m.ukraine = u;
        redrawFront();
        setStatus(u.error ? 'Russia-Ukraine: ' + u.error + '.' : '');
        if (!u.error) {
          note('Conflict: Russia-Ukraine map of ' + shortDate(u.snapshot_at) + ', ' +
            (u.lines || []).length + ' line of contact segments');
        }
      }).catch(function () {
        if (seq === m.seq.front) setStatus('The Russia-Ukraine map did not load.');
      });
    }

    // Wikipedia theatres one after another (the server spaces its requests).
    function loadControl(slugs) {
      var seq = m.seq.front;
      var chain = Promise.resolve();
      slugs.forEach(function (slug) {
        chain = chain.then(function () {
          if (seq !== m.seq.front || !m.enabled || m.control[slug]) return null;
          return getJson('/api/conflict/control/' + encodeURIComponent(slug)).then(function (cm) {
            if (!m.enabled) return;
            m.control[slug] = cm;
            if (m.controlOrder.indexOf(slug) < 0) m.controlOrder.push(slug);
            redrawFront();
          }).catch(function () { setStatus('Control map for ' + slug + ' did not load.'); });
        });
      });
      return chain;
    }

    function wikiSlugs() {
      var list = (m.summary && m.summary.theatres) || [];
      return list.filter(function (t) { return t.slug !== 'ukraine'; })
        .map(function (t) { return t.slug; });
    }

    function loadFront() {
      if (!m.sub.front) { redrawFront(); return Promise.resolve(); }
      var jobs = [];
      if (m.theatre === 'all' || m.theatre === 'ukraine') jobs.push(loadUkraine());
      var slugs = m.theatre === 'all' ? wikiSlugs() : (m.theatre === 'ukraine' ? [] :
        [m.theatre]);
      jobs.push(loadControl(slugs));
      redrawFront();
      return Promise.all(jobs);
    }

    function loadEvents() {
      if (!m.sub.events) { drawEvents(); return Promise.resolve(); }
      var seq = ++m.seq.events;
      return getJson('/api/conflict/events' + query([['days', 90]])).then(function (gc) {
        if (seq !== m.seq.events || !m.enabled) return;
        m.gc = gc;
        fillConflicts();
        drawEvents();
        updateAttribution();
      }).catch(function () {
        if (seq === m.seq.events) m.eventsInfo.textContent = 'Verified events did not load.';
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

    function readYears() {
      var hs = m.summary && m.summary.history;
      if (!hs || !hs.available) return null;
      m.years = clampYears(Number(m.yearFrom.value), Number(m.yearTo.value), hs.year_min,
        hs.year_max);
      m.yearFrom.value = String(m.years.from);
      m.yearTo.value = String(m.years.to);
      return m.years;
    }

    function loadHistory(force) {
      if (!m.sub.history) { drawHistory(); return Promise.resolve(); }
      var hs = m.summary && m.summary.history;
      if (!hs || !hs.available) {
        m.historyInfo.textContent = (hs && hs.hint) ||
          'No UCDP history on the server. Run: python -m sda.conflict import';
        return Promise.resolve();
      }
      var years = readYears();
      var v = viewState();
      var mode = historyMode(v.height);
      var types = typesParam(m.types);
      var path;
      if (mode === 'density') {
        var cell = historyCell(v.height);
        var box = cell >= 2 ? { west: -180, south: -90, east: 180, north: 90 } : v.box;
        path = '/api/conflict/history/density' + query([['bbox', bboxParam(box)],
          ['cell', cell], ['year_from', years.from], ['year_to', years.to], ['types', types]]);
      } else {
        path = '/api/conflict/history/events' + query([['bbox', bboxParam(v.box)],
          ['year_from', years.from], ['year_to', years.to], ['types', types],
          ['limit', HISTORY_LIMIT]]);
      }
      if (!force && path === m.historyKey) return Promise.resolve();
      m.historyKey = path;
      var seq = ++m.seq.history;
      return getJson(path).then(function (h) {
        if (seq !== m.seq.history || !m.enabled) return;
        m.history = h;
        m.historyMode = mode;
        drawHistory();
      }).catch(function () {
        if (seq === m.seq.history) m.historyInfo.textContent = 'UCDP history did not load.';
      });
    }

    function loadAcled() {
      if (!m.sub.acled) { drawAcled(); return Promise.resolve(); }
      var seq = ++m.seq.acled;
      return getJson('/api/conflict/acled' + query([['days', 30]])).then(function (a) {
        if (seq !== m.seq.acled || !m.enabled) return;
        m.acled = a;
        drawAcled();
        updateAttribution();
      }).catch(function () {
        if (seq === m.seq.acled) m.acledInfo.textContent = 'ACLED did not load.';
      });
    }

    function applySummary(s) {
      m.summary = s;
      var keep = m.theatre;
      m.theatreSel.replaceChildren();
      m.theatreSel.appendChild(option('all', 'All theatres (' + (s.theatres || []).length + ')'));
      (s.theatres || []).forEach(function (t) {
        m.theatreSel.appendChild(option(t.slug, t.name));
      });
      m.theatreSel.value = keep;
      var hs = s.history || {};
      if (hs.available) {
        m.yearFrom.setAttribute('min', String(hs.year_min));
        m.yearFrom.setAttribute('max', String(hs.year_max));
        m.yearTo.setAttribute('min', String(hs.year_min));
        m.yearTo.setAttribute('max', String(hs.year_max));
        if (!m.yearFrom.value) m.yearFrom.value = String(hs.year_min);
        if (!m.yearTo.value) m.yearTo.value = String(hs.year_max);
        m.summaryEl.textContent = (s.theatres || []).length + ' front-line theatres, verified ' +
          'events for 90 days, and ' + withCommas(hs.events) + ' UCDP events ' + hs.year_min +
          ' to ' + hs.year_max + '.';
      } else {
        m.summaryEl.textContent = (s.theatres || []).length + ' front-line theatres and ' +
          'verified events for 90 days. History: ' + (str(hs.hint) ||
          'run python -m sda.conflict import on the server') + '.';
      }
      m.acledInfo.textContent = s.acled_configured ? '' : str(s.acled_hint);
      updateAttribution();
    }

    function loadAll() {
      return getJson('/api/conflict/summary').then(function (s) {
        if (!m.enabled) return null;
        applySummary(s);
        note('Conflict layer: ' + (s.theatres || []).length + ' theatres');
        return Promise.all([loadFront(), loadEvents(), loadHistory(true), loadAcled()]);
      }).catch(function () {
        setStatus('The conflict layer is not available on this server.');
      });
    }

    // -- controls -------------------------------------------------------------

    function setSub(key, on) {
      m.sub[key] = !!on;
      if (!m.enabled) { if (on) setEnabled(true, true); return; }
      if (key === 'front') loadFront();
      else if (key === 'events') { if (on && !m.gc) loadEvents(); else drawEvents(); }
      else if (key === 'history') loadHistory(true);
      else if (key === 'acled') { if (on && !m.acled) loadAcled(); else drawAcled(); }
      if (!on) {
        if (key === 'events') m.eventsInfo.textContent = '';
        if (key === 'history') m.historyInfo.textContent = '';
      }
      updateAttribution();
      setCount();
    }

    m.frontBox.box.addEventListener('change', function () { setSub('front', m.frontBox.box.checked); });
    m.eventsBox.box.addEventListener('change', function () { setSub('events', m.eventsBox.box.checked); });
    m.historyBox.box.addEventListener('change', function () { setSub('history', m.historyBox.box.checked); });
    m.acledBox.box.addEventListener('change', function () { setSub('acled', m.acledBox.box.checked); });

    m.theatreSel.addEventListener('change', function () {
      m.theatre = String(m.theatreSel.value || 'all');
      var list = (m.summary && m.summary.theatres) || [];
      var t = list.filter(function (x) { return x.slug === m.theatre; })[0];
      if (t && isNum(t.lat) && isNum(t.lon)) flyTo(t.lat, t.lon, (t.view_km || 1500) * 1000);
      if (!m.enabled) { setEnabled(true, true); return; }
      loadFront();
    });

    m.dateInput.addEventListener('change', function () {
      var v = String(m.dateInput.value || '');
      if (v && !/^\d{4}-\d{2}-\d{2}$/.test(v)) return;
      m.ukraineDate = v;
      m.latestBtn.className = 'mode-btn' + (v ? '' : ' active');
      if (m.theatre !== 'all' && m.theatre !== 'ukraine') {
        m.theatre = 'ukraine';
        m.theatreSel.value = 'ukraine';
      }
      if (m.enabled && m.sub.front) loadUkraine();
    });

    m.latestBtn.addEventListener('click', function () {
      m.dateInput.value = '';
      m.ukraineDate = '';
      m.latestBtn.className = 'mode-btn active';
      if (m.enabled && m.sub.front) loadUkraine();
    });

    m.daysSel.addEventListener('change', function () {
      var d = Number(m.daysSel.value);
      m.gcDays = DAY_CHOICES.indexOf(d) >= 0 ? d : 90;
      drawEvents();
    });

    m.conflictSel.addEventListener('change', function () {
      m.gcConflict = String(m.conflictSel.value || '');
      drawEvents();
    });

    [m.yearFrom, m.yearTo].forEach(function (inp) {
      inp.addEventListener('change', function () { if (m.enabled) loadHistory(true); });
    });

    function toggleType(id) {
      m.types[id] = !m.types[id];
      if (!m.types[1] && !m.types[2] && !m.types[3]) m.types[id] = true;
      UCDP_TYPES.forEach(function (t, i) {
        m.typeBtns[i].className = 'mode-btn' + (m.types[t.id] ? ' active' : '');
        m.typeBtns[i].setAttribute('aria-pressed', m.types[t.id] ? 'true' : 'false');
      });
      if (m.enabled && m.sub.history) loadHistory(true);
    }

    function scheduleRefresh() {
      if (!m.enabled || !m.sub.history) return;
      if (m.timer) global.clearTimeout(m.timer);
      m.timer = global.setTimeout(function () { m.timer = null; loadHistory(false); },
        MOVE_DEBOUNCE_MS);
    }

    // -- enable / disable ---------------------------------------------------

    function setEnabled(on, fromPanel) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'The conflict layer needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (fromPanel && on) {
        try {
          var box = global.document.getElementById(LAYER_ID);
          if (box && !box.checked) {
            box.checked = true;
            if (typeof global.toggleLayer === 'function') {
              m.enabled = false;
              global.toggleLayer('conflict', true);
              return;
            }
          }
        } catch (e) { /* keep going */ }
      }
      if (on) {
        ensureLayers();
        showLayers(true);
        m.historyKey = null;
        loadAll();
      } else {
        if (m.timer) { global.clearTimeout(m.timer); m.timer = null; }
        Object.keys(m.seq).forEach(function (k) { m.seq[k]++; });
        showLayers(false);
        if (m.points) Object.keys(m.points).forEach(function (k) { m.points[k].removeAll(); });
        if (m.ds) m.ds.entities.removeAll();
        Object.keys(m.counts).forEach(function (k) { m.counts[k] = 0; });
        setCount();
        setStatus('');
        m.info.replaceChildren();
      }
    }

    m.setEnabled = setEnabled;
    m.refresh = function () {
      if (!m.enabled) return Promise.resolve();
      m.control = {};
      m.controlOrder = [];
      m.gc = null;
      m.acled = null;
      return loadAll();
    };
    m.openEvent = openEvent;

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

  global.SDAConflict = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { return current ? current.refresh() : Promise.resolve(); },
    HISTORY_DETAIL_ALT_M: HISTORY_DETAIL_ALT_M,
    UKRAINE_STYLE: UKRAINE_STYLE,
    UCDP_TYPES: UCDP_TYPES,
    query: query,
    boxFromRect: boxFromRect,
    boxAround: boxAround,
    safeHref: safeHref,
    hostOf: hostOf,
    flatPositions: flatPositions,
    historyCell: historyCell,
    historyMode: historyMode,
    deathColor: deathColor,
    densitySize: densitySize,
    clampYears: clampYears,
    typesParam: typesParam,
    daysBefore: daysBefore,
    recentEvents: recentEvents,
    partyLegend: partyLegend,
    eventLines: eventLines,
    eventLinks: eventLinks,
    ucdpLines: ucdpLines,
    acledLines: acledLines,
    formatCount: formatCount
  };
})(typeof window !== 'undefined' ? window : this);
