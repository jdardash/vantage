// Space weather chip on the WorldView globe: the newest planetary Kp coloured by
// level, the NOAA G scale now and for today and the next two days, the observed
// F10.7, and one line in the intel feed whenever a G1 or stronger geomagnetic
// storm is in progress or forecast. Refreshed every fifteen minutes.
//
// Data: GET /api/space-weather/summary (sda/routes/space.py over
// sda/spaceweather.py), which fetches the keyless NOAA SWPC products at most
// once per fifteen minutes and serves the last good copy, flagged stale, when
// SWPC does not answer. The static demo build has no server, so there the panel
// says so and no request is made.
//
// Why it is on the globe: storm-time heating raises thermospheric density, so
// drag and the in-track error of TLE predictions grow for objects below about
// 600 km; the conjunction storm check is GET /api/space-weather/conjunction.
//
// Text is only ever set with textContent. Every value from the server is checked
// (numbers finite and in range, scale labels matched against G0..G5 and the like)
// before it is shown.
//
// Page wiring: list it in EXTENSIONS ({ global: 'SDASpaceWeather', mount:
// 'ext-spaceweather' }); it builds a small panel in ctx.mount when one is given
// and posts storm lines with ctx.addIntelItem.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Space weather';
  var PATH = '/api/space-weather/summary';
  var REFRESH_MS = 15 * 60 * 1000;
  var STORM_G = 1;

  // Kp colour by level. Kp comes in thirds (5- = 4.67) and NOAA counts the
  // whole band, minus included: quiet below 4-, active from 4-, storm (G1-G2)
  // from 5-, severe (G3 and up) from 7-. Same thresholds as sda.spaceweather.
  var KP_LEVELS = [
    { min: 6.66, color: '#ff4d4d', label: 'severe storm' },
    { min: 4.66, color: '#ff9f1a', label: 'storm' },
    { min: 3.66, color: '#ffd84d', label: 'active' },
    { min: 0, color: '#4dd27a', label: 'quiet' }
  ];
  var UNKNOWN_COLOR = '#8899aa';

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the tests)
  // ---------------------------------------------------------------------

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function kpValue(kp) { return finite(kp) && kp >= 0 && kp <= 9 ? kp : null; }

  function kpLevel(kp) {
    var v = kpValue(kp);
    if (v === null) return null;
    for (var i = 0; i < KP_LEVELS.length; i++) {
      if (v >= KP_LEVELS[i].min) return { color: KP_LEVELS[i].color, label: KP_LEVELS[i].label };
    }
    return null;
  }

  function kpColor(kp) {
    var level = kpLevel(kp);
    return level ? level.color : UNKNOWN_COLOR;
  }

  function kpText(kp) {
    var v = kpValue(kp);
    if (v === null) return '-';
    return Math.round(v) === v ? v.toFixed(1) : (Math.round(v * 100) / 100).toString();
  }

  // 'G2' -> 2; anything that is not G0..G5 -> null. Same for S and R.
  function scaleLevel(label, prefix) {
    if (typeof label !== 'string') return null;
    var m = /^([GSR])([0-5])$/.exec(label.trim());
    if (!m || m[1] !== (prefix || 'G')) return null;
    return Number(m[2]);
  }

  function scaleText(label, prefix) {
    var n = scaleLevel(label, prefix);
    return n === null ? '-' : (prefix || 'G') + n;
  }

  function summaryUrl(api) {
    var base = typeof api === 'string' ? api.replace(/\/+$/, '') : '';
    return base + PATH;
  }

  // '2026-11-19T18:00:00+00:00' -> '18:00 UTC'
  function shortTime(iso) {
    if (typeof iso !== 'string') return '';
    var m = /T(\d{2}):(\d{2})/.exec(iso);
    return m ? m[1] + ':' + m[2] + ' UTC' : '';
  }

  // '2026-11-20' -> '11-20'
  function shortDate(iso) {
    if (typeof iso !== 'string') return '';
    var m = /^\d{4}-(\d{2})-(\d{2})/.exec(iso.trim());
    return m ? m[1] + '-' + m[2] : '';
  }

  function flux(v) { return finite(v) && v >= 50 && v <= 400 ? v : null; }

  function days(data) {
    var list = data && Array.isArray(data.next_3_days) ? data.next_3_days : [];
    var out = [];
    list.forEach(function (d) {
      if (!d || typeof d !== 'object') return;
      var date = shortDate(d.date);
      if (!date) return;
      out.push({ date: date, g: scaleLevel(d.g_scale, 'G') });
    });
    return out.slice(0, 3);
  }

  // Storm level now: the stronger of the NOAA current G scale and the G level of
  // the newest Kp. null when neither is known.
  function stormNow(data) {
    if (!data || typeof data !== 'object') return null;
    var a = scaleLevel(data.g_scale_now, 'G');
    var b = scaleLevel(data.kp_g_scale, 'G');
    if (a === null) return b;
    if (b === null) return a;
    return Math.max(a, b);
  }

  function stormAhead(data) {
    var best = null;
    days(data).forEach(function (d) {
      if (d.g !== null && (best === null || d.g > best)) best = d.g;
    });
    var max = data ? scaleLevel(data.g_scale_next_3_days_max, 'G') : null;
    if (max !== null && (best === null || max > best)) best = max;
    return best;
  }

  // The banner for the intel feed, or '' when no G1+ storm is current or forecast.
  function stormLine(data) {
    var now = stormNow(data);
    var ahead = stormAhead(data);
    var tail = ': drag and TLE in-track error rise for objects below 600 km.';
    if (now !== null && now >= STORM_G) {
      var kp = kpValue(data.kp_now);
      var line = 'Geomagnetic storm G' + now + ' in progress (NOAA SWPC' +
        (kp !== null ? ', Kp ' + kpText(kp) : '') + ')';
      if (ahead !== null && ahead >= STORM_G) line += ', up to G' + ahead + ' forecast';
      return line + tail;
    }
    if (ahead !== null && ahead >= STORM_G) {
      var when = days(data).filter(function (d) { return d.g !== null && d.g >= STORM_G; })
        .map(function (d) { return d.date + ' G' + d.g; });
      return 'Geomagnetic storm forecast by NOAA SWPC, up to G' + ahead +
        (when.length ? ' (' + when.join(', ') + ')' : '') + tail;
    }
    return '';
  }

  function panelText(data) {
    var kp = data ? kpValue(data.kp_now) : null;
    var level = kpLevel(kp);
    var t = data ? shortTime(data.kp_time) : '';
    var gNow = data ? scaleText(data.g_scale_now, 'G') : '-';
    var ahead = days(data).map(function (d) {
      return d.date + ' ' + (d.g === null ? '-' : 'G' + d.g);
    });
    var f = data ? flux(data.f107_observed) : null;
    var mean = data ? flux(data.f107_90day_mean) : null;
    return {
      kp: 'Kp ' + kpText(kp),
      kpNote: (level ? ' ' + level.label : '') + (t ? ', ' + t : ''),
      kpColor: kpColor(kp),
      g: gNow + ' now; next 3 days: ' + (ahead.length ? ahead.join(', ') : '-') +
        ' | ' + scaleText(data && data.s_scale_now, 'S') + ' ' +
        scaleText(data && data.r_scale_now, 'R'),
      f107: 'F10.7 ' + (f === null ? '-' : Math.round(f) + ' SFU') +
        (mean === null ? '' : ' (90-day mean ' + Math.round(mean) + ')'),
      status: 'NOAA SWPC' + (data && data.stale ? ', stale: last good copy' : '') +
        ', refreshed every 15 min'
    };
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  function el(tag, cls, text) {
    var e = global.document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var api = ctx.api || '';
    var m = {
      timer: null, seq: 0, stopped: false, lastBanner: '', failed: false, data: null,
      root: null, kpEl: null, kpNoteEl: null, gEl: null, f107El: null, statusEl: null
    };

    function note(msg) {
      try { if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem(msg); } catch (e) {
        /* keep going */
      }
    }

    function setStatus(msg) { if (m.statusEl) m.statusEl.textContent = msg; }

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      m.root = el('div', 'panel');
      var body = el('div', 'panel-body');
      var kpRow = el('div', 'sw-kp');
      m.kpEl = el('span', 'sw-kp-value', 'Kp -');
      m.kpEl.style.fontWeight = 'bold';
      m.kpEl.style.color = UNKNOWN_COLOR;
      m.kpNoteEl = el('span', 'sw-kp-note', '');
      kpRow.appendChild(m.kpEl);
      kpRow.appendChild(m.kpNoteEl);
      m.gEl = el('div', 'sw-g', '');
      m.f107El = el('div', 'sw-f107', '');
      m.statusEl = el('div', 'sw-status', ctx.isStatic
        ? 'Space weather needs the live server (NOAA SWPC).'
        : 'Loading NOAA SWPC space weather...');
      m.statusEl.style.fontSize = '10px';
      m.statusEl.style.opacity = '0.75';
      body.appendChild(kpRow);
      body.appendChild(m.gEl);
      body.appendChild(m.f107El);
      body.appendChild(m.statusEl);
      m.root.appendChild(el('h3', null, PANEL_TITLE));
      m.root.appendChild(body);
      ctx.mount.appendChild(m.root);
    }

    function render(data) {
      var p = panelText(data);
      if (m.kpEl) {
        m.kpEl.textContent = p.kp;
        m.kpEl.style.color = p.kpColor;
      }
      if (m.kpNoteEl) m.kpNoteEl.textContent = p.kpNote;
      if (m.gEl) m.gEl.textContent = p.g;
      if (m.f107El) m.f107El.textContent = p.f107;
      setStatus(p.status);
    }

    function load() {
      if (ctx.isStatic || m.stopped) return Promise.resolve(null);
      var seq = ++m.seq;
      return global.fetch(summaryUrl(api)).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (data) {
        if (seq !== m.seq || m.stopped) return null;
        if (!data || typeof data !== 'object' || Array.isArray(data)) {
          throw new Error('not a summary');
        }
        m.data = data;
        m.failed = false;
        render(data);
        var line = stormLine(data);
        if (line && line !== m.lastBanner) note(line);
        m.lastBanner = line;
        return data;
      }).catch(function () {
        if (seq !== m.seq || m.stopped) return null;
        m.failed = true;
        setStatus('NOAA SWPC space weather did not load; retrying in 15 minutes.');
        return null;
      });
    }

    function start() {
      if (ctx.isStatic) return;
      load();
      m.timer = global.setInterval(load, REFRESH_MS);
    }

    function stop() {
      m.stopped = true;
      m.seq++;
      if (m.timer) {
        global.clearInterval(m.timer);
        m.timer = null;
      }
    }

    buildPanel();
    m.start = start;
    m.stop = stop;
    m.load = load;
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || typeof ctx !== 'object') return false;
      if (current) current.stop();
      current = createModule(ctx);
      current.start();
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDASpaceWeather = {
    init: init,
    refresh: function () { return current ? current.load() : Promise.resolve(null); },
    stop: function () { if (current) current.stop(); },
    data: function () { return current ? current.data : null; },
    REFRESH_MS: REFRESH_MS,
    PATH: PATH,
    kpLevel: kpLevel,
    kpColor: kpColor,
    kpText: kpText,
    scaleLevel: scaleLevel,
    summaryUrl: summaryUrl,
    shortTime: shortTime,
    shortDate: shortDate,
    stormNow: stormNow,
    stormAhead: stormAhead,
    stormLine: stormLine,
    panelText: panelText
  };
})(typeof window !== 'undefined' ? window : this);
