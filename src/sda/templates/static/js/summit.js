// Summit conditions on the WorldView globe: PeakWatch's day-by-day screen for any
// mountain on Earth. A drawer shows seven days of GO / MARG / NO over the summit
// hours (10:00 to 14:00 local), the reasons and numbers behind each day, a
// meteogram, and how old every source is. It is a screen, not a go/no-go, and the
// drawer says so on every open.
//
// Data: GET /api/summit/conditions for one point and POST /api/summit/verdicts for
// up to 50 (sda/routes/summit.py, engine sda/summit.py, ported from PeakWatch in
// the trip-runbook project, MIT). The static demo build has no server, so there the
// drawer says so and no request is made.
//
// Public API (window.SDASummit):
//   open({id, name, lat, lon, elev_m, prom_m})  open the drawer for one summit
//   verdicts(points)  Promise of {id: {verdict, day, when, top_reason}}, batched by
//                     50 and cached for ten minutes in the page
//   close(), isOpen(), setRightClick(on)
//   init(ctx)         the EXTENSIONS hook: a small panel with "Conditions at view
//                     centre" and a right-click on the globe that opens the drawer
// A peaks layer calls open() and verdicts() behind `if (window.SDASummit)`; both
// work before init() too.
//
// Elevation: a given elev_m wins. Otherwise the globe's terrain height is used when
// the page has real terrain loaded, and with none (the plain ellipsoid) elev_m is
// left out so the server reads Open-Meteo's 90 m elevation model instead.
//
// Frames: geodetic degrees only. Text is only ever set with textContent. ASCII only.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Summit conditions';
  var STYLE_ID = 'summit-style';
  var MAX_BATCH = 50;
  var VERDICT_TTL_MS = 10 * 60 * 1000;
  var DAYS = 7;
  var SCREEN_LINE = 'A screen, not a go/no-go: which peak, which day, and what the risk ' +
    'is. Read the forecasts it names and judge the mountain yourself before any attempt.';
  var STATIC_LINE = 'Summit conditions need the live server.';
  var VERDICT_COLORS = { GO: '#3ddc84', MARG: '#ffb000', NO: '#ff4d4d', '?': '#888888' };
  var VERDICT_WORDS = { GO: 'GO', MARG: 'MARG', NO: 'NO', '?': '?' };
  var LEVEL_TAGS = { fail: 'NO', warn: 'MARG', info: 'note' };
  var LAYOUT = { left: 262, top: 60, right: 322, bottom: 72 };
  var CANVAS_W = 480;
  var CANVAS_H = 300;

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the tests)
  // ---------------------------------------------------------------------

  function num(v) {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function wrapLon(lon) {
    if (lon >= -180 && lon <= 180) return lon;  // in range: untouched, no float drift
    var x = ((lon + 180) % 360 + 360) % 360 - 180;
    return x === -180 && lon > 0 ? 180 : x;
  }

  // A summit as the API takes it, or null when the position is not usable.
  function cleanPoint(p) {
    if (!p || typeof p !== 'object') return null;
    var lat = num(p.lat);
    var lon = num(p.lon);
    if (lat === null || lon === null || lat < -90 || lat > 90) return null;
    lon = wrapLon(lon);
    var elev = num(p.elev_m);
    if (elev !== null && (elev < -500 || elev > 9000)) elev = null;
    var prom = num(p.prom_m);
    var id = p.id === undefined || p.id === null ? '' : String(p.id).slice(0, 80);
    var name = typeof p.name === 'string' ? p.name.slice(0, 120) : '';
    return { id: id, name: name, lat: lat, lon: lon, elev_m: elev, prom_m: prom };
  }

  function round(v, nd) {
    var f = Math.pow(10, nd);
    return Math.round(v * f) / f;
  }

  function conditionsUrl(api, p, days) {
    var q = 'lat=' + round(p.lat, 5) + '&lon=' + round(p.lon, 5);
    if (p.elev_m !== null && p.elev_m !== undefined) q += '&elev_m=' + Math.round(p.elev_m);
    if (p.name) q += '&name=' + encodeURIComponent(p.name);
    var d = num(days);
    q += '&days=' + (d === null ? DAYS : Math.max(1, Math.min(DAYS, Math.round(d))));
    return (api || '') + '/api/summit/conditions?' + q;
  }

  function verdictsUrl(api) { return (api || '') + '/api/summit/verdicts'; }

  // The POST body: cleaned, one entry per id (the last one wins), ids kept as given.
  function verdictPoints(points) {
    var byId = {};
    var order = [];
    (Array.isArray(points) ? points : []).forEach(function (raw) {
      var p = cleanPoint(raw);
      if (!p || !p.id) return;
      if (!(p.id in byId)) order.push(p.id);
      byId[p.id] = { id: p.id, lat: p.lat, lon: p.lon, elev_m: p.elev_m };
    });
    return order.map(function (id) { return byId[id]; });
  }

  function chunk(list, n) {
    var out = [];
    for (var i = 0; i < list.length; i += n) out.push(list.slice(i, i + n));
    return out;
  }

  function cacheKey(p) {
    return p.id + '|' + p.lat.toFixed(3) + '|' + p.lon.toFixed(3) + '|' +
      (p.elev_m === null || p.elev_m === undefined ? '' : Math.round(p.elev_m));
  }

  function verdictColor(v) {
    return Object.prototype.hasOwnProperty.call(VERDICT_COLORS, v) ? VERDICT_COLORS[v]
      : VERDICT_COLORS['?'];
  }

  function withCommas(n) {
    var s = String(Math.round(Math.abs(n)));
    var out = '';
    while (s.length > 3) { out = ',' + s.slice(-3) + out; s = s.slice(0, -3); }
    return (n < 0 ? '-' : '') + s + out;
  }

  function ageText(min) {
    var m = num(min);
    if (m === null) return 'age unknown';
    if (m < 1) return 'just now';
    if (m < 60) return Math.round(m) + ' min old';
    if (m < 48 * 60) {
      var h = Math.floor(m / 60);
      var r = Math.round(m - h * 60);
      return h + ' h' + (r ? ' ' + r + ' min' : '') + ' old';
    }
    return Math.round(m / 1440) + ' d old';
  }

  // One line per source: name, read or not, age, and why when it was not fresh.
  function sourceLine(s) {
    if (!s || typeof s !== 'object') return '';
    var name = String(s.name || s.id || 'source');
    var state = String(s.state || '');
    var tag = s.required ? ' (required)' : '';
    if (state === 'skipped') return name + ': not checked' + (s.note ? ' (' + s.note + ')' : '');
    if (!s.read) {
      return name + tag + ': NOT READ' + (s.error ? ' (' + s.error + ')' : '');
    }
    var line = name + tag + ': read, ' + ageText(s.age_min);
    if (state === 'stale') line += ' (stale copy' + (s.error ? ': ' + s.error : '') + ')';
    if (s.run) line += ', model run ' + String(s.run).replace('T', ' ').slice(0, 16) + ' UTC';
    if (s.note) line += ' (' + s.note + ')';
    return line;
  }

  function stripDays(data) {
    var days = data && Array.isArray(data.days) ? data.days : [];
    return days.map(function (d) {
      var v = d && typeof d.verdict === 'string' && VERDICT_COLORS[d.verdict] ? d.verdict : '?';
      return { date: String((d && d.date) || ''), label: String((d && d.label) || ''),
               verdict: v, color: verdictColor(v), top: String((d && d.top_reason) || '') };
    });
  }

  function fmt(v, nd, unit) {
    var n = num(v);
    if (n === null) return null;
    return (nd ? n.toFixed(nd) : String(Math.round(n))) + (unit || '');
  }

  // The numbers behind one day, as short "label value" strings.
  function numberLines(day) {
    var n = (day && day.numbers) || {};
    var out = [];
    function add(label, value) { if (value !== null && value !== undefined && value !== '')
      out.push(label + ' ' + value); }
    add('wind', fmt(n.wind_mph, 0, ' mph'));
    if (n.wind_by_model && typeof n.wind_by_model === 'object') {
      var parts = Object.keys(n.wind_by_model).map(function (k) {
        var w = fmt(n.wind_by_model[k], 0, '');
        return w === null ? null : k + ' ' + w;
      }).filter(Boolean);
      if (parts.length) out.push('by model ' + parts.join(', '));
    }
    add('chill', fmt(n.chill_f, 0, ' F'));
    add('temp', fmt(n.temp_f, 0, ' F'));
    add('snow', fmt(n.snow_in, 1, ' in'));
    add('precip', fmt(n.precip_in, 2, ' in'));
    add('72 h snow before', fmt(n.snow72_in, 0, ' in'));
    add('cloud', fmt(n.cloud_h, 1, ' h'));
    add('CAPE', fmt(n.cape, 0, ' J/kg'));
    var fz = num(n.freezing_level_ft);
    if (fz !== null) out.push('freezing level ' + withCommas(fz) + ' ft');
    add('surface gust', fmt(n.gust_surface_mph, 0, ' mph'));
    add('ensemble p', fmt(n.ens_p, 2, ''));
    add('wind odds', fmt(n.wind_odds, 2, ''));
    add('summit odds', fmt(n.summit_odds, 2, ''));
    var aqi = fmt(n.aqi, 0, '');
    if (aqi !== null) out.push((n.aqi_scale || 'AQI') + ' ' + aqi);
    if (n.avalanche) out.push('avalanche ' + n.avalanche);
    if (n.nearest_fire) out.push('nearest fire ' + n.nearest_fire);
    if (n.nws_day) out.push('NWS: ' + n.nws_day);
    return out;
  }

  function niceRange(lo, hi) {
    if (lo === null || hi === null) return [0, 1];
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    var span = hi - lo;
    var step = Math.pow(10, Math.floor(Math.log(span / 4) / Math.LN10));
    [1, 2, 5, 10].some(function (k) {
      if (span / (step * k) <= 5) { step = step * k; return true; }
      return false;
    });
    return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
  }

  function seriesOf(hr, key) {
    var v = hr && Array.isArray(hr[key]) ? hr[key] : [];
    return v.map(num);
  }

  function extent(lists, extra) {
    var lo = null;
    var hi = null;
    lists.concat([extra || []]).forEach(function (list) {
      list.forEach(function (v) {
        if (v === null) return;
        if (lo === null || v < lo) lo = v;
        if (hi === null || v > hi) hi = v;
      });
    });
    return [lo, hi];
  }

  function isoSeconds(s) {
    var t = Date.parse(String(s || ''));
    return isFinite(t) ? t / 1000 : null;
  }

  // Everything the meteogram draws, as plain numbers, so a test can check it
  // without a canvas: the time axis, four panels with their ranges and series,
  // the shaded summit hours of each day and the limit lines.
  function meteogramModel(data, width, height, selected) {
    var hr = data && data.hourly;
    var t = seriesOf(hr, 't');
    var first = null;
    var last = null;
    t.forEach(function (x) {
      if (x === null) return;
      if (first === null || x < first) first = x;
      if (last === null || x > last) last = x;
    });
    if (first === null || last === null || last <= first) return null;
    var W = num(width) || CANVAS_W;
    var H = num(height) || CANVAS_H;
    var L = (data && data.limits) || {};
    var elevFt = num(data && data.elev_ft);
    var left = 36;
    var right = W - 6;
    var gap = 14;
    var top = 4;
    var ph = (H - top - 4 * gap) / 4;
    var specs = [
      { key: 'wind', title: 'Summit wind, mph (median, model range, surface gust)',
        series: [{ key: 'wind', color: '#ffb000', width: 1.5 },
                 { key: 'gust', color: '#7a5500', width: 1, dash: [3, 3] }],
        band: ['wind_min', 'wind_max'], floor: 0,
        lines: [num(L.wind_warn_mph), num(L.wind_fail_mph)] },
      { key: 'temp', title: 'Summit temp and wind chill, F',
        series: [{ key: 'temp', color: '#ffd27a', width: 1.5 },
                 { key: 'chill', color: '#66b3ff', width: 1.5 }],
        lines: [32, num(L.chill_warn_f), num(L.chill_fail_f)] },
      { key: 'wet', title: 'Cloud at summit % (line), precip in/h (bars)',
        series: [{ key: 'cloud', color: '#bbbbbb', width: 1 }], bars: 'precip',
        fixed: [0, 100], lines: [num(L.cloud_pct)] },
      { key: 'fz', title: 'Freezing level, ft (summit line)',
        series: [{ key: 'freezing_ft', color: '#66b3ff', width: 1.5 }], floor: 0,
        lines: [elevFt] }
    ];
    var panels = specs.map(function (sp, i) {
      var pt = top + i * (ph + gap) + gap - 4;
      var lists = sp.series.map(function (s) { return seriesOf(hr, s.key); });
      if (sp.band) { lists.push(seriesOf(hr, sp.band[0])); lists.push(seriesOf(hr, sp.band[1])); }
      var lines = sp.lines.filter(function (v) { return v !== null; });
      var range;
      if (sp.fixed) {
        range = sp.fixed.slice();
      } else {
        var ex = extent(lists, lines);
        if (sp.floor !== undefined && ex[0] !== null) ex[0] = Math.min(ex[0], sp.floor);
        range = niceRange(ex[0], ex[1]);
      }
      var bars = sp.bars ? seriesOf(hr, sp.bars) : null;
      var barMax = 0;
      if (bars) bars.forEach(function (v) { if (v !== null && v > barMax) barMax = v; });
      return {
        key: sp.key, title: sp.title, top: pt, bottom: pt + ph, ymin: range[0], ymax: range[1],
        series: sp.series.map(function (s) {
          return { key: s.key, color: s.color, width: s.width, dash: s.dash || null,
                   values: seriesOf(hr, s.key) };
        }),
        band: sp.band ? { lo: seriesOf(hr, sp.band[0]), hi: seriesOf(hr, sp.band[1]) } : null,
        bars: bars, barMax: barMax > 0 ? barMax : null,
        lines: lines.filter(function (v) { return v >= range[0] && v <= range[1]; })
      };
    });
    var days = data && Array.isArray(data.days) ? data.days : [];
    var shades = [];
    var ticks = [];
    days.forEach(function (d, i) {
      var sh = d && d.summit_hours;
      var a = isoSeconds(sh && sh.start);
      var b = isoSeconds(sh && sh.end);
      if (a === null || b === null || b <= first || a >= last) return;
      shades.push({ t0: Math.max(a, first), t1: Math.min(b, last), day: i,
                    selected: i === selected, verdict: String(d.verdict || '?') });
      ticks.push({ t: (a + b) / 2, label: String(d.label || '') });
    });
    return { width: W, height: H, left: left, right: right, t0: first, t1: last,
             t: t, panels: panels, shades: shades, ticks: ticks };
  }

  function xOf(model, t) {
    return model.left + (t - model.t0) / (model.t1 - model.t0) * (model.right - model.left);
  }

  function yOf(panel, v) {
    var f = (v - panel.ymin) / ((panel.ymax - panel.ymin) || 1);
    return panel.bottom - Math.max(0, Math.min(1, f)) * (panel.bottom - panel.top);
  }

  function intelLine(data) {
    var strip = stripDays(data).map(function (d) {
      return (d.label.split(' ')[0] || d.date) + ' ' + d.verdict;
    }).join(', ');
    var elev = num(data && data.elev_m);
    return 'Summit conditions: ' + String((data && data.name) || 'point') +
      (elev !== null ? ' ' + withCommas(elev) + ' m' : '') + ': ' + strip;
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  function px(n) { return n + 'px'; }

  var STYLE_CSS = [
    '.sumt-frame { position: fixed; right: ' + px(LAYOUT.right) + '; top: ' + px(LAYOUT.top) +
      '; bottom: ' + px(LAYOUT.bottom) + ';',
    '  z-index: 46; box-sizing: border-box; width: min(520px, calc(100vw - ' +
      px(LAYOUT.left + LAYOUT.right + 8) + '));',
    '  min-width: 260px; display: flex; flex-direction: column; background: rgba(0,0,0,0.92);',
    '  border: 1px solid var(--hud-color, #ffb000); border-radius: 4px; overflow: hidden;',
    '  font-size: 10px; letter-spacing: 1px; color: var(--hud-color, #ffb000); }',
    '.sumt-head { display: flex; align-items: center; gap: 6px; min-height: 28px;',
    '  box-sizing: border-box; padding: 0 8px; background: rgba(0,0,0,0.6);',
    '  border-bottom: 1px solid rgba(255,176,0,0.15); }',
    '.sumt-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;',
    '  white-space: nowrap; font-weight: 700; }',
    '.sumt-btn { flex: 0 0 auto; background: none; border: 1px solid rgba(255,176,0,0.3);',
    '  color: var(--hud-color, #ffb000); padding: 2px 8px; font-size: 9px; font-family: inherit;',
    '  cursor: pointer; border-radius: 2px; }',
    '.sumt-body { flex: 1; overflow-y: auto; padding: 6px 8px; }',
    '.sumt-sub { color: var(--hud-dim, #7a5500); font-size: 9px; margin-bottom: 4px; }',
    '.sumt-screen { border: 1px solid rgba(255,176,0,0.35); padding: 4px 6px; margin: 4px 0 6px;',
    '  font-size: 9px; line-height: 1.4; }',
    '.sumt-status { font-size: 9px; min-height: 11px; margin-bottom: 4px; }',
    '.sumt-strip { display: flex; gap: 3px; margin-bottom: 6px; }',
    '.sumt-day { flex: 1; min-width: 0; background: none; border: 1px solid rgba(255,176,0,0.2);',
    '  color: inherit; font: inherit; font-size: 9px; padding: 3px 1px; cursor: pointer;',
    '  border-radius: 2px; text-align: center; }',
    '.sumt-day.sel { border-color: var(--hud-color, #ffb000); background: rgba(255,176,0,0.1); }',
    '.sumt-verdict { display: block; font-weight: 700; font-size: 11px; margin-top: 2px; }',
    '.sumt-reasons { margin: 0 0 6px; padding: 0; list-style: none; }',
    '.sumt-reasons li { font-size: 9px; line-height: 1.45; }',
    '.sumt-tag { display: inline-block; min-width: 34px; font-weight: 700; }',
    '.sumt-nums { font-size: 9px; color: var(--hud-dim, #7a5500); line-height: 1.45;',
    '  margin-bottom: 6px; }',
    '.sumt-canvas { width: 100%; height: auto; display: block; margin: 2px 0 6px;',
    '  background: rgba(255,255,255,0.02); }',
    '.sumt-h { font-weight: 700; font-size: 9px; margin: 6px 0 2px; }',
    '.sumt-src { font-size: 9px; line-height: 1.45; color: var(--hud-dim, #7a5500); }',
    '.sumt-src.bad { color: #ff4d4d; }',
    '.sumt-panel-row { display: flex; gap: 4px; margin: 4px 0; }',
    '.sumt-panel-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.sumt-check { display: flex; align-items: center; gap: 5px; font-size: 9px; cursor: pointer; }'
  ].join('\n');

  function el(tag, cls, text) {
    var e = global.document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function button(cls, text, title) {
    var b = el('button', cls, text);
    b.setAttribute('type', 'button');
    if (title) b.setAttribute('title', title);
    return b;
  }

  function injectStyle() {
    var doc = global.document;
    if (!doc || !doc.head || (doc.getElementById && doc.getElementById(STYLE_ID))) return;
    var style = doc.createElement('style');
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    doc.head.appendChild(style);
  }

  // ---------------------------------------------------------------------
  // Canvas
  // ---------------------------------------------------------------------

  function drawMeteogram(canvas, model) {
    var g = canvas && canvas.getContext ? canvas.getContext('2d') : null;
    if (!g) return false;
    g.clearRect(0, 0, canvas.width, canvas.height);
    if (!model) {
      g.fillStyle = '#7a5500';
      g.font = '10px monospace';
      g.fillText('No hourly forecast to draw.', 10, 20);
      return false;
    }
    g.font = '9px monospace';
    model.panels.forEach(function (p) {
      model.shades.forEach(function (s) {
        g.fillStyle = s.selected ? 'rgba(255,176,0,0.22)' : 'rgba(255,176,0,0.09)';
        var x0 = xOf(model, s.t0);
        g.fillRect(x0, p.top, Math.max(1, xOf(model, s.t1) - x0), p.bottom - p.top);
      });
      g.strokeStyle = 'rgba(255,176,0,0.25)';
      g.lineWidth = 1;
      g.setLineDash([]);
      g.strokeRect(model.left, p.top, model.right - model.left, p.bottom - p.top);
      g.fillStyle = '#7a5500';
      g.fillText(p.title, model.left + 2, p.top - 2);
      g.fillText(String(Math.round(p.ymax)), 2, p.top + 8);
      g.fillText(String(Math.round(p.ymin)), 2, p.bottom);
      if (p.bars && p.barMax) {
        g.fillStyle = 'rgba(102,179,255,0.6)';
        var bw = Math.max(1, (model.right - model.left) / model.t.length);
        p.bars.forEach(function (v, i) {
          var t = model.t[i];
          if (v === null || v <= 0 || t === null) return;
          var hgt = v / p.barMax * (p.bottom - p.top) * 0.9;
          g.fillRect(xOf(model, t) - bw / 2, p.bottom - hgt, bw, hgt);
        });
        g.fillStyle = '#7a5500';
        g.fillText('bars to ' + p.barMax.toFixed(2) + ' in', model.right - 80, p.top + 8);
      }
      if (p.band) {
        g.fillStyle = 'rgba(255,176,0,0.18)';
        g.beginPath();
        var open = false;
        var back = [];
        p.band.hi.forEach(function (hi, i) {
          var lo = p.band.lo[i];
          var t = model.t[i];
          if (hi === null || lo === null || t === null) return;
          var x = xOf(model, t);
          if (!open) { g.moveTo(x, yOf(p, hi)); open = true; } else g.lineTo(x, yOf(p, hi));
          back.push([x, yOf(p, lo)]);
        });
        for (var k = back.length - 1; k >= 0; k--) g.lineTo(back[k][0], back[k][1]);
        if (open) { g.closePath(); g.fill(); }
      }
      p.lines.forEach(function (v) {
        g.strokeStyle = 'rgba(255,77,77,0.7)';
        g.setLineDash([4, 3]);
        g.beginPath();
        g.moveTo(model.left, yOf(p, v));
        g.lineTo(model.right, yOf(p, v));
        g.stroke();
      });
      g.setLineDash([]);
      p.series.forEach(function (s) {
        g.strokeStyle = s.color;
        g.lineWidth = s.width;
        g.setLineDash(s.dash || []);
        g.beginPath();
        var pen = false;
        s.values.forEach(function (v, i) {
          var t = model.t[i];
          if (v === null || t === null) { pen = false; return; }
          var x = xOf(model, t);
          var y = yOf(p, v);
          if (pen) g.lineTo(x, y); else g.moveTo(x, y);
          pen = true;
        });
        g.stroke();
      });
      g.setLineDash([]);
    });
    g.fillStyle = '#ffb000';
    model.ticks.forEach(function (tk) {
      g.fillText(tk.label, xOf(model, tk.t) - 12, model.height - 1);
    });
    return true;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer || null;
    var m = {
      api: ctx.api || '', isStatic: !!ctx.isStatic, seq: 0, frame: null, data: null,
      point: null, selected: 0, statusEl: null, panelStatus: null,
      // With the map tools loaded, right-click opens their point popup, which
      // carries a CONDITIONS button; a second right-click handler here would
      // open both at once. The panel checkbox still turns it back on.
      rightClick: !global.SDAMapTools, rightBox: null,
      handler: null, cache: {}, inflight: {}, notedStatic: false, notedFail: false
    };

    function note(msg) {
      try { if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem(msg); } catch (e) {
        /* keep going */
      }
    }

    function fetchJson(url, opts) {
      if (typeof global.fetch !== 'function') return Promise.reject(new Error('no fetch'));
      return global.fetch(url, opts).then(function (res) {
        if (!res || !res.ok) {
          var err = new Error('HTTP ' + (res ? res.status : '?'));
          err.status = res ? res.status : 0;
          throw err;
        }
        return res.json();
      });
    }

    // -- globe picks ----------------------------------------------------

    function terrainHeight(lat, lon) {
      try {
        if (!viewer || !C || !viewer.scene || !viewer.scene.globe) return null;
        var tp = viewer.terrainProvider || viewer.scene.terrainProvider;
        if (tp && C.EllipsoidTerrainProvider && tp instanceof C.EllipsoidTerrainProvider) {
          return null;
        }
        var h = viewer.scene.globe.getHeight(C.Cartographic.fromDegrees(lon, lat));
        return typeof h === 'number' && isFinite(h) && h > 1 ? h : null;
      } catch (e) {
        return null;
      }
    }

    function pickAt(pos) {
      try {
        if (!viewer || !C || !pos) return null;
        var scene = viewer.scene;
        var cart = null;
        if (scene.globe && typeof scene.globe.pick === 'function' && viewer.camera.getPickRay) {
          var ray = viewer.camera.getPickRay(pos);
          if (ray) cart = scene.globe.pick(ray, scene);
        }
        if (!cart) cart = viewer.camera.pickEllipsoid(pos, scene.globe && scene.globe.ellipsoid);
        if (!cart) return null;
        var c = C.Cartographic.fromCartesian(cart);
        if (!c) return null;
        var lat = C.Math.toDegrees(c.latitude);
        var lon = C.Math.toDegrees(c.longitude);
        return { lat: lat, lon: lon };
      } catch (e) {
        return null;
      }
    }

    function openAtScreen(pos) {
      var at = pickAt(pos);
      if (!at) {
        setPanelStatus('That point is off the globe.');
        return null;
      }
      return open({ lat: at.lat, lon: at.lon });
    }

    function viewCentre() {
      var canvas = viewer && viewer.scene && viewer.scene.canvas;
      if (!canvas || !C) return null;
      var w = canvas.clientWidth || canvas.width;
      var h = canvas.clientHeight || canvas.height;
      return openAtScreen(new C.Cartesian2(w / 2, h / 2));
    }

    function setRightClick(on) {
      m.rightClick = !!on;
      if (m.rightBox) m.rightBox.checked = m.rightClick;
      if (m.handler) {
        try { m.handler.destroy(); } catch (e) { /* already gone */ }
        m.handler = null;
      }
      if (!m.rightClick || !viewer || !C || !C.ScreenSpaceEventHandler || !viewer.scene) return;
      m.handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
      m.handler.setInputAction(function (ev) { openAtScreen(ev && ev.position); },
        C.ScreenSpaceEventType.RIGHT_CLICK);
    }

    // -- panel ----------------------------------------------------------

    function setPanelStatus(msg) { if (m.panelStatus) m.panelStatus.textContent = msg; }

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      var root = el('div', 'panel');
      var body = el('div', 'panel-body');
      m.panelStatus = el('div', 'sumt-status', m.isStatic ? STATIC_LINE
        : (m.rightClick ? 'Right-click the globe, or use the button, for the next seven days at that point.'
          : 'Right-click the globe and press CONDITIONS, or use the button, for the next seven days at that point.'));
      var row = el('div', 'sumt-panel-row');
      var here = button('mode-btn', 'Conditions at view centre',
        'Summit conditions for the point at the middle of the view');
      here.addEventListener('click', function () { viewCentre(); });
      row.appendChild(here);
      var label = el('label', 'sumt-check');
      var box = el('input');
      box.setAttribute('type', 'checkbox');
      box.checked = m.rightClick;
      m.rightBox = box;
      box.addEventListener('change', function () { setRightClick(box.checked); });
      label.appendChild(box);
      label.appendChild(el('span', null, 'Right-click the globe for conditions'));
      body.appendChild(m.panelStatus);
      body.appendChild(row);
      body.appendChild(label);
      root.appendChild(el('h3', null, PANEL_TITLE));
      root.appendChild(body);
      ctx.mount.appendChild(root);
    }

    // -- drawer ---------------------------------------------------------

    function close() {
      m.seq++;
      if (m.frame && m.frame.parentNode) m.frame.parentNode.removeChild(m.frame);
      m.frame = null;
      m.data = null;
    }

    function titleOf(p) {
      var name = p.name || (Math.abs(p.lat).toFixed(3) + (p.lat >= 0 ? ' N ' : ' S ') +
        Math.abs(p.lon).toFixed(3) + (p.lon >= 0 ? ' E' : ' W'));
      var bits = [name];
      if (p.elev_m !== null) bits.push(withCommas(p.elev_m) + ' m');
      if (p.prom_m !== null) bits.push('prominence ' + withCommas(p.prom_m) + ' m');
      return bits.join(', ');
    }

    function buildFrame(p) {
      injectStyle();
      if (m.frame && m.frame.parentNode) m.frame.parentNode.removeChild(m.frame);
      var f = el('div', 'sumt-frame');
      var head = el('div', 'sumt-head');
      m.titleEl = el('div', 'sumt-title', titleOf(p));
      var shut = button('sumt-btn', 'Close', 'Close the conditions drawer');
      shut.addEventListener('click', close);
      head.appendChild(m.titleEl);
      head.appendChild(shut);
      var body = el('div', 'sumt-body');
      m.subEl = el('div', 'sumt-sub', p.lat.toFixed(4) + ', ' + p.lon.toFixed(4));
      m.screenEl = el('div', 'sumt-screen', SCREEN_LINE);
      m.statusEl = el('div', 'sumt-status', '');
      m.stripEl = el('div', 'sumt-strip');
      m.dayEl = el('div', 'sumt-dayview');
      m.canvas = el('canvas', 'sumt-canvas');
      m.canvas.width = CANVAS_W;
      m.canvas.height = CANVAS_H;
      m.srcEl = el('div', 'sumt-sources');
      m.metaEl = el('div', 'sumt-sub');
      [m.subEl, m.screenEl, m.statusEl, m.stripEl, m.dayEl, m.canvas, m.srcEl, m.metaEl]
        .forEach(function (x) { body.appendChild(x); });
      f.appendChild(head);
      f.appendChild(body);
      global.document.body.appendChild(f);
      m.frame = f;
    }

    function setStatus(msg) { if (m.statusEl) m.statusEl.textContent = msg; }

    function renderDay() {
      var d = m.data && m.data.days ? m.data.days[m.selected] : null;
      m.dayEl.textContent = '';
      if (!d) return;
      var head = el('div', 'sumt-h', (d.label || d.date) + ': ' +
        (VERDICT_WORDS[d.verdict] || '?') + ' over the summit hours ' +
        String(m.data.summit_hours_local || '10:00-14:00') + ' local' +
        (d.sunrise && d.sunset ? '; sun ' + d.sunrise + '-' + d.sunset
          : d.polar ? '; ' + d.polar : ''));
      var list = el('ul', 'sumt-reasons');
      (Array.isArray(d.reasons) ? d.reasons : []).forEach(function (r) {
        var li = el('li');
        var tag = el('span', 'sumt-tag', LEVEL_TAGS[r.level] || 'note');
        tag.style.color = r.level === 'fail' ? VERDICT_COLORS.NO
          : r.level === 'warn' ? VERDICT_COLORS.MARG : '#888888';
        li.appendChild(tag);
        li.appendChild(el('span', null, String(r.text || '')));
        list.appendChild(li);
      });
      if (!list.children.length) list.appendChild(el('li', null, 'No limit crossed.'));
      m.dayEl.appendChild(head);
      m.dayEl.appendChild(list);
      var nums = numberLines(d);
      if (nums.length) m.dayEl.appendChild(el('div', 'sumt-nums', nums.join('; ')));
    }

    function renderStrip() {
      m.stripEl.textContent = '';
      stripDays(m.data).forEach(function (d, i) {
        var b = button('sumt-day' + (i === m.selected ? ' sel' : ''), null, d.top);
        b.appendChild(el('span', null, d.label));
        var v = el('span', 'sumt-verdict', VERDICT_WORDS[d.verdict]);
        v.style.color = d.color;
        b.appendChild(v);
        b.addEventListener('click', function () { selectDay(i); });
        m.stripEl.appendChild(b);
      });
    }

    function renderSources() {
      m.srcEl.textContent = '';
      m.srcEl.appendChild(el('div', 'sumt-h', 'Sources'));
      (Array.isArray(m.data.sources) ? m.data.sources : []).forEach(function (s) {
        var bad = s && s.state !== 'skipped' && !s.read;
        m.srcEl.appendChild(el('div', 'sumt-src' + (bad ? ' bad' : ''), sourceLine(s)));
      });
      var meta = [];
      var d = m.data;
      if (d.timezone) meta.push('time zone ' + d.timezone + (d.tz_exact ? '' : ' (fixed offset)'));
      if (d.elev_source) meta.push('height from ' + d.elev_source);
      if (Array.isArray(d.levels_hpa)) meta.push('levels ' + d.levels_hpa.join('/') + ' hPa');
      if (d.calibration && d.calibration.note) meta.push(d.calibration.note);
      if (d.calibration && d.calibration.applied) {
        meta.push('wind odds calibrated for ' + d.calibration.region);
      }
      if (d.exposed_rule) meta.push('exposed rule: ' + d.exposed_rule);
      if (d.tz_note) meta.push(d.tz_note);
      if (d.generated) meta.push('scored ' + String(d.generated).replace('T', ' ').slice(0, 16));
      m.metaEl.textContent = meta.join('; ');
    }

    function draw() {
      drawMeteogram(m.canvas, meteogramModel(m.data, CANVAS_W, CANVAS_H, m.selected));
    }

    function selectDay(i) {
      if (!m.data || !m.data.days || !m.data.days[i]) return;
      m.selected = i;
      renderStrip();
      renderDay();
      draw();
    }

    function render(data) {
      m.data = data;
      var days = Array.isArray(data.days) ? data.days : [];
      var first = 0;
      while (first < days.length - 1 && days[first].summit_hours &&
             days[first].summit_hours.passed) first++;
      m.selected = first;
      if (data.screen_note) m.screenEl.textContent = String(data.screen_note);
      var elev = num(data.elev_m);
      if (elev !== null && m.point.elev_m === null) {
        m.point.elev_m = elev;
        m.titleEl.textContent = titleOf(m.point);
      }
      setStatus(days.length ? '' : 'No days in the answer.');
      renderStrip();
      renderDay();
      draw();
      renderSources();
    }

    function open(raw) {
      var p = cleanPoint(raw);
      if (!p) return Promise.resolve(null);
      if (p.elev_m === null) p.elev_m = terrainHeight(p.lat, p.lon);
      m.point = p;
      var seq = ++m.seq;
      buildFrame(p);
      if (m.isStatic) {
        setStatus(STATIC_LINE);
        if (!m.notedStatic) { m.notedStatic = true; note(STATIC_LINE); }
        return Promise.resolve(null);
      }
      setStatus('Loading seven days of summit conditions...');
      return fetchJson(conditionsUrl(m.api, p, DAYS)).then(function (data) {
        if (seq !== m.seq) return null;
        if (!data || typeof data !== 'object') throw new Error('empty answer');
        render(data);
        note(intelLine(data));
        return data;
      }).catch(function (err) {
        if (seq !== m.seq) return null;
        setStatus('Conditions did not load (' + String((err && err.message) || err) + ').');
        return null;
      });
    }

    // -- verdicts ---------------------------------------------------------

    function verdicts(points) {
      var list = verdictPoints(points);
      var out = {};
      if (m.isStatic) {
        if (!m.notedStatic) { m.notedStatic = true; note(STATIC_LINE); }
        return Promise.resolve(out);
      }
      var now = Date.now();
      var need = [];
      var waits = [];
      list.forEach(function (p) {
        var key = cacheKey(p);
        var hit = m.cache[key];
        if (hit && now - hit.at < VERDICT_TTL_MS) {
          out[p.id] = hit.value;
        } else if (m.inflight[key]) {
          waits.push(m.inflight[key].then(function (res) {
            if (res && res[p.id]) out[p.id] = res[p.id];
          }));
        } else {
          need.push(p);
        }
      });
      chunk(need, MAX_BATCH).forEach(function (part) {
        var req = fetchJson(verdictsUrl(m.api), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(part)
        }).then(function (res) {
          var at = Date.now();
          part.forEach(function (p) {
            if (res && res[p.id]) m.cache[cacheKey(p)] = { at: at, value: res[p.id] };
          });
          return res || {};
        }).catch(function (err) {
          if (!m.notedFail) {
            m.notedFail = true;
            note('Summit verdicts did not load (' + String((err && err.message) || err) + ')');
          }
          return {};
        });
        part.forEach(function (p) { m.inflight[cacheKey(p)] = req; });
        waits.push(req.then(function (res) {
          part.forEach(function (p) {
            delete m.inflight[cacheKey(p)];
            if (res[p.id]) out[p.id] = res[p.id];
          });
        }));
      });
      return Promise.all(waits).then(function () { return out; });
    }

    buildPanel();
    if (viewer) setRightClick(m.rightClick);
    m.open = open;
    m.close = close;
    m.verdicts = verdicts;
    m.setRightClick = setRightClick;
    m.viewCentre = viewCentre;
    m.openAtScreen = openAtScreen;
    m.destroy = function () {
      close();
      if (m.handler) { try { m.handler.destroy(); } catch (e) { /* gone */ } m.handler = null; }
    };
    return m;
  }

  var current = null;

  // open() and verdicts() work before init(): the page's API base and static flag
  // are read from the globals worldview.html defines.
  function ensure() {
    if (!current) {
      current = createModule({
        viewer: null,
        api: typeof global.API === 'string' ? global.API : '',
        isStatic: !!global.STATIC_DEMO,
        addIntelItem: typeof global.addIntelItem === 'function' ? global.addIntelItem : null
      });
    }
    return current;
  }

  function init(ctx) {
    try {
      if (!ctx) return false;
      if (current) current.destroy();
      current = createModule(ctx);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDASummit = {
    init: init,
    open: function (p) { return ensure().open(p); },
    close: function () { if (current) current.close(); },
    isOpen: function () { return !!(current && current.frame); },
    verdicts: function (points) { return ensure().verdicts(points); },
    setRightClick: function (on) { if (current) current.setRightClick(on); },
    viewCentre: function () { return current ? current.viewCentre() : null; },
    MAX_BATCH: MAX_BATCH,
    VERDICT_TTL_MS: VERDICT_TTL_MS,
    VERDICT_COLORS: VERDICT_COLORS,
    SCREEN_LINE: SCREEN_LINE,
    STATIC_LINE: STATIC_LINE,
    cleanPoint: cleanPoint,
    conditionsUrl: conditionsUrl,
    verdictsUrl: verdictsUrl,
    verdictPoints: verdictPoints,
    chunk: chunk,
    verdictColor: verdictColor,
    ageText: ageText,
    sourceLine: sourceLine,
    stripDays: stripDays,
    numberLines: numberLines,
    niceRange: niceRange,
    meteogramModel: meteogramModel,
    drawMeteogram: drawMeteogram,
    intelLine: intelLine,
    wrapLon: wrapLon
  };
})(typeof window !== 'undefined' ? window : this);
