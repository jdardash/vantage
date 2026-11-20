// "What's here?" for the WorldView globe: right-click a point (or press and
// hold on a touch screen) for one card about it:
//
//   place            reverse geocoded (OpenStreetMap Nominatim, through map
//                    tools' polite cached lookup, else /api/geocode/reverse)
//   coordinates      decimal degrees, hemisphere form and MGRS (map tools)
//   elevation        Open-Meteo's 90 m digital elevation model at the point
//   local time       the UTC offset Open-Meteo reports for the point's zone
//   weather now      Open-Meteo current conditions (keyless, CC BY 4.0)
//   cameras          the nearest public cameras (/api/cameras/nearest)
//   peaks            the nearest named summits (/api/peaks/nearest)
//   satellites       how many of the globe's satellites are at least 10 deg
//                    above the horizon there, at the globe's clock time
//
// The card docks at the bottom centre of the window, so it does not cover the
// map tools popup that opens at the cursor on the same right-click; the two
// complement each other. The panel can switch right-click off.
//
// Open-Meteo is asked straight from the browser, once per point: answers are
// kept for ten minutes per 0.001 deg (about the 90 m elevation grid), so
// clicking the same spot again costs nothing. Its free tier allows 10,000
// calls a day per address for non-commercial use.
//
// The press-and-hold gesture (600 ms, 10 px of finger drift allowed, touch
// pointers only, cancelled by a second finger) follows the idea of
// long-press.ts in worldmonitor; that file is AGPL-3.0 and none of its code
// is used here.
//
// Page wiring: list it in EXTENSIONS with a mount div. A key handler can call
// SDAWhatsHere.openAtCentre(); other modules can call SDAWhatsHere.open({lat, lon}).

(function (global) {
  'use strict';

  var STYLE_ID = 'sda-whatshere-style';
  var STORE_KEY = 'sda.whatshere.v1.rightclick';
  var OPEN_METEO = 'https://api.open-meteo.com/v1/forecast';
  var CURRENT_VARS = ['temperature_2m', 'apparent_temperature', 'relative_humidity_2m',
    'precipitation', 'weather_code', 'cloud_cover', 'wind_speed_10m', 'wind_direction_10m', 'is_day'];
  var WEATHER_TTL_MS = 10 * 60 * 1000;
  var WEATHER_CACHE_MAX = 200;
  var EARTH_RADIUS_KM = 6371.0088; // mean radius, display geometry only
  var MIN_ELEVATION_DEG = 10;
  var NEAR_N = 5;
  var NEAR_RADIUS_KM = 50;
  var LONG_PRESS_MS = 600;
  var LONG_PRESS_SLOP_PX = 10;
  var D2R = Math.PI / 180;
  var R2D = 180 / Math.PI;

  // WMO weather interpretation codes as Open-Meteo reports them, in plain words.
  var WMO = {
    0: 'Clear sky', 1: 'Mostly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Fog', 48: 'Fog with rime frost',
    51: 'Light drizzle', 53: 'Drizzle', 55: 'Heavy drizzle',
    56: 'Light freezing drizzle', 57: 'Freezing drizzle',
    61: 'Light rain', 63: 'Rain', 65: 'Heavy rain',
    66: 'Light freezing rain', 67: 'Freezing rain',
    71: 'Light snow', 73: 'Snow', 75: 'Heavy snow', 77: 'Snow grains',
    80: 'Light showers', 81: 'Showers', 82: 'Violent showers',
    85: 'Light snow showers', 86: 'Heavy snow showers',
    95: 'Thunderstorm', 96: 'Thunderstorm with light hail', 99: 'Thunderstorm with heavy hail'
  };
  var COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function validPoint(lat, lon) {
    return isNum(lat) && isNum(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
  }

  function wmoText(code) {
    return Object.prototype.hasOwnProperty.call(WMO, code) ? WMO[code] : 'Weather code ' + code;
  }

  function compass(deg) {
    if (!isNum(deg)) return '';
    return COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // "UTC+05:30" style, from seconds east of UTC.
  function formatOffset(sec) {
    if (!isNum(sec)) return '';
    var sign = sec < 0 ? '-' : '+';
    var m = Math.round(Math.abs(sec) / 60);
    return 'UTC' + sign + pad2(Math.floor(m / 60)) + ':' + pad2(m % 60);
  }

  // "14:05 Thu" at a UTC offset.
  function localTime(nowMs, offsetSec) {
    var d = new Date(nowMs + offsetSec * 1000);
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ' ' + DAYS[d.getUTCDay()];
  }

  function weatherKey(lat, lon) { return lat.toFixed(3) + ',' + lon.toFixed(3); }

  function weatherUrl(lat, lon) {
    return OPEN_METEO + '?latitude=' + lat.toFixed(4) + '&longitude=' + lon.toFixed(4) +
      '&current=' + CURRENT_VARS.join(',') + '&wind_speed_unit=ms&timezone=auto';
  }

  // Elevation angle (deg) of a satellite at (satLat, satLon, altKm) seen from
  // the ground at (lat, lon), on a spherical Earth: enough to count what is
  // overhead for display, not for pointing an antenna.
  function elevationDeg(lat, lon, satLat, satLon, altKm) {
    var p1 = lat * D2R;
    var p2 = satLat * D2R;
    var dl = (satLon - lon) * D2R;
    var cosG = Math.sin(p1) * Math.sin(p2) + Math.cos(p1) * Math.cos(p2) * Math.cos(dl);
    cosG = Math.max(-1, Math.min(1, cosG));
    var g = Math.acos(cosG);
    var ratio = EARTH_RADIUS_KM / (EARTH_RADIUS_KM + altKm);
    return Math.atan2(cosG - ratio, Math.sin(g)) * R2D;
  }

  // { count, top: [{ noradId, name, el }] } for satellites at or above minEl.
  function overhead(sats, lat, lon, minEl, topN) {
    var min = isNum(minEl) ? minEl : MIN_ELEVATION_DEG;
    var hits = [];
    (sats || []).forEach(function (s) {
      if (!s || !isNum(s.lastLat) || !isNum(s.lastLon) || !isNum(s.lastAlt) || s.lastAlt <= 0) return;
      var el = elevationDeg(lat, lon, s.lastLat, s.lastLon, s.lastAlt);
      if (el >= min) hits.push({ noradId: s.noradId, name: String(s.name || ''), el: el });
    });
    hits.sort(function (a, b) { return b.el - a.el; });
    return { count: hits.length, top: hits.slice(0, isNum(topN) ? topN : 3) };
  }

  // Lines for the card from an Open-Meteo answer, or null when it has nothing usable.
  function describeWeather(json) {
    var c = json && json.current;
    if (!c || typeof c !== 'object') return null;
    var parts = [];
    if (isNum(c.weather_code)) parts.push(wmoText(c.weather_code));
    if (isNum(c.temperature_2m)) {
      var t = c.temperature_2m.toFixed(1) + ' C';
      if (isNum(c.apparent_temperature) && Math.abs(c.apparent_temperature - c.temperature_2m) >= 1) {
        t += ' (feels ' + c.apparent_temperature.toFixed(1) + ' C)';
      }
      parts.push(t);
    }
    var second = [];
    if (isNum(c.wind_speed_10m)) {
      second.push('wind ' + c.wind_speed_10m.toFixed(1) + ' m/s' +
        (isNum(c.wind_direction_10m) && c.wind_speed_10m > 0 ? ' from ' + compass(c.wind_direction_10m) : ''));
    }
    if (isNum(c.relative_humidity_2m)) second.push('humidity ' + Math.round(c.relative_humidity_2m) + '%');
    if (isNum(c.cloud_cover)) second.push('cloud ' + Math.round(c.cloud_cover) + '%');
    if (isNum(c.precipitation) && c.precipitation > 0) second.push('precip ' + c.precipitation.toFixed(1) + ' mm');
    if (!parts.length && !second.length) return null;
    return { summary: parts.join(', '), detail: second.join(', '), isDay: c.is_day !== 0 };
  }

  function round3(x) { return Math.round(x * 1000) / 1000; }

  function formatKm(km) {
    if (!isNum(km)) return '';
    return km < 10 ? km.toFixed(1) + ' km' : Math.round(km) + ' km';
  }

  // Press and hold on a touch screen. Calls onPress({ x, y }) with client
  // coordinates after holdMs of a single finger that has not drifted more
  // than slopPx. Returns a function that removes the listeners.
  function attachLongPress(target, onPress, opts) {
    var hold = opts && isNum(opts.holdMs) ? opts.holdMs : LONG_PRESS_MS;
    var slop = opts && isNum(opts.slopPx) ? opts.slopPx : LONG_PRESS_SLOP_PX;
    var timer = null;
    var start = null;
    var down = {};
    function stop() { if (timer) { global.clearTimeout(timer); timer = null; } start = null; }
    function onDown(e) {
      if (e.pointerType !== 'touch') return;
      down[e.pointerId] = true;
      if (Object.keys(down).length > 1) { stop(); return; } // a pinch, not a press
      start = { x: e.clientX, y: e.clientY, id: e.pointerId };
      timer = global.setTimeout(function () {
        timer = null;
        var s = start;
        start = null;
        if (!s) return;
        try { onPress({ x: s.x, y: s.y }); } catch (err) { /* the globe must keep working */ }
      }, hold);
    }
    function onMove(e) {
      if (!start || e.pointerId !== start.id) return;
      var dx = e.clientX - start.x;
      var dy = e.clientY - start.y;
      if (dx * dx + dy * dy > slop * slop) stop();
    }
    function onUp(e) { delete down[e.pointerId]; stop(); }
    target.addEventListener('pointerdown', onDown);
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', onUp);
    target.addEventListener('pointercancel', onUp);
    target.addEventListener('pointerleave', onUp);
    return function detach() {
      stop();
      target.removeEventListener('pointerdown', onDown);
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', onUp);
      target.removeEventListener('pointercancel', onUp);
      target.removeEventListener('pointerleave', onUp);
    };
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.wh-card { position: fixed; left: 50%; bottom: 140px; transform: translateX(-50%); z-index: 46;',
    '  width: 360px; max-width: 92vw; max-height: 52vh; overflow-y: auto; box-sizing: border-box;',
    '  background: rgba(0,0,0,0.92); border: 1px solid var(--hud-color, #ffb000); border-radius: 4px;',
    '  font-family: "JetBrains Mono", monospace; color: var(--hud-text, #ffb000); font-size: 10px;',
    '  padding: 6px 8px 8px; }',
    '.wh-head { display: flex; align-items: center; gap: 6px; font-weight: 700; letter-spacing: 1px;',
    '  margin-bottom: 4px; color: var(--hud-color, #ffb000); }',
    '.wh-head span { flex: 1; }',
    '.wh-btn { background: none; border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 1px 6px; cursor: pointer; border-radius: 2px; }',
    '.wh-place { font-size: 11px; margin-bottom: 3px; line-height: 1.35; }',
    '.wh-row { margin: 2px 0; line-height: 1.4; }',
    '.wh-label { color: var(--hud-dim, #7a5500); margin-right: 4px; }',
    '.wh-sec { margin-top: 5px; border-top: 1px solid rgba(255,176,0,0.12); padding-top: 3px; }',
    '.wh-sec-title { color: var(--hud-dim, #7a5500); font-size: 9px; letter-spacing: 1px; }',
    '.wh-link { display: block; width: 100%; text-align: left; background: none; border: 0; padding: 1px 0;',
    '  color: var(--hud-color, #ffb000); font: inherit; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.wh-link:hover { text-decoration: underline; }',
    '.wh-muted { color: var(--hud-dim, #7a5500); }',
    '.wh-foot { margin-top: 5px; font-size: 8px; color: var(--hud-dim, #7a5500); }',
    '.wh-help { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 6px; line-height: 1.4; }',
    '.wh-check { display: flex; gap: 5px; align-items: center; font-size: 9px; margin-bottom: 5px; }',
    '@media (max-width: 760px) { .wh-card { bottom: 110px; } }'
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

  function button(className, text, title) {
    var b = el('button', className, text);
    b.setAttribute('type', 'button');
    if (title) b.setAttribute('title', title);
    return b;
  }

  function memoryStorage() {
    var data = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; }
    };
  }

  function pickStorage() {
    try {
      var s = global.localStorage;
      if (s) { s.setItem(STORE_KEY + '.probe', '1'); s.removeItem(STORE_KEY + '.probe'); return s; }
    } catch (e) { /* fall through */ }
    return memoryStorage();
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var storage = ctx.storage || pickStorage();
    var m = { card: null, seq: 0, weather: {}, handler: null, cleanup: [], point: null };
    var stored = null;
    try { stored = storage.getItem(STORE_KEY); } catch (e) { stored = null; }
    m.rightClick = stored !== '0';

    function note(text) { try { ctx.addIntelItem(text); } catch (e) { /* page feed only */ } }

    function getJson(url) {
      if (typeof global.fetch !== 'function') return Promise.reject(new Error('no fetch'));
      return global.fetch(url).then(function (r) {
        if (!r.ok) { var err = new Error('HTTP ' + r.status); err.status = r.status; throw err; }
        return r.json();
      });
    }

    function mapTools() {
      var mt = global.SDAMapTools;
      try { return mt && typeof mt.instance === 'function' ? mt.instance() : null; } catch (e) { return null; }
    }

    // -- point picking --------------------------------------------------

    function pickAt(x, y) {
      if (!C || !viewer || !viewer.camera) return null;
      try {
        var cart = viewer.camera.pickEllipsoid(new C.Cartesian2(x, y), viewer.scene.globe.ellipsoid);
        if (!cart) return null;
        var carto = C.Cartographic.fromCartesian(cart);
        if (!carto) return null;
        return { lat: carto.latitude * R2D, lon: carto.longitude * R2D };
      } catch (e) { return null; }
    }

    function centre() {
      var canvas = viewer && viewer.scene && viewer.scene.canvas;
      if (!canvas) return null;
      var w = canvas.clientWidth || canvas.width;
      var h = canvas.clientHeight || canvas.height;
      if (!w || !h) {
        try {
          var r = canvas.getBoundingClientRect();
          w = r.width; h = r.height;
        } catch (e) { return null; }
      }
      return pickAt(w / 2, h / 2);
    }

    // -- data -------------------------------------------------------------

    function weather(lat, lon) {
      var key = weatherKey(lat, lon);
      var hit = m.weather[key];
      var now = Date.now();
      if (hit && now - hit.at < WEATHER_TTL_MS) return hit.promise;
      var promise = getJson(weatherUrl(lat, lon));
      var keys = Object.keys(m.weather);
      if (keys.length >= WEATHER_CACHE_MAX) delete m.weather[keys[0]];
      m.weather[key] = { at: now, promise: promise };
      promise.catch(function () {
        if (m.weather[key] && m.weather[key].promise === promise) delete m.weather[key];
      });
      return promise;
    }

    function placeName(lat, lon) {
      var mt = mapTools();
      if (mt && typeof mt.reverseGeocode === 'function') {
        try { return Promise.resolve(mt.reverseGeocode(lat, lon)); } catch (e) { /* fall through */ }
      }
      if (ctx.isStatic) return Promise.resolve(null);
      return getJson(api + '/api/geocode/reverse?lat=' + lat.toFixed(6) + '&lon=' + lon.toFixed(6))
        .then(function (j) { return j && typeof j.display_name === 'string' ? j.display_name : ''; });
    }

    function coordsText(lat, lon) {
      var MT = global.SDAMapTools;
      var lines = [lat.toFixed(5) + ', ' + lon.toFixed(5)];
      try {
        if (MT && typeof MT.toMGRS === 'function') {
          var g = MT.toMGRS(lat, lon, 5);
          if (g) lines.push('MGRS ' + g);
        }
      } catch (e) { /* decimal is enough */ }
      return lines.join('  |  ');
    }

    // -- card -------------------------------------------------------------

    function close() {
      m.seq++;
      if (m.card && m.card.parentNode) m.card.parentNode.removeChild(m.card);
      m.card = null;
      m.point = null;
    }

    function flyTo(lat, lon, heightM) {
      if (!C) return;
      try {
        viewer.camera.flyTo({ destination: C.Cartesian3.fromDegrees(lon, lat, heightM), duration: 1.6 });
      } catch (e) { /* keep going */ }
    }

    function section(card, title) {
      var sec = el('div', 'wh-sec');
      sec.appendChild(el('div', 'wh-sec-title', title));
      var body = el('div', 'wh-muted', 'Loading...');
      sec.appendChild(body);
      card.appendChild(sec);
      return { root: sec, body: body };
    }

    function row(label, text) {
      var r = el('div', 'wh-row');
      r.appendChild(el('span', 'wh-label', label));
      r.appendChild(el('span', 'wh-muted', text));
      return r;
    }

    function setText(r, text, muted) {
      var v = r.children[1];
      v.textContent = text;
      v.className = muted ? 'wh-muted' : '';
    }

    function fillList(body, list, empty, label, onPick) {
      body.replaceChildren();
      if (!list.length) { body.className = 'wh-muted'; body.textContent = empty; return; }
      body.className = '';
      list.forEach(function (item) {
        var b = button('wh-link', label(item));
        b.addEventListener('click', function () { onPick(item); });
        body.appendChild(b);
      });
    }

    function open(p, opts) {
      var lat = p && Number(p.lat);
      var lon = p && Number(p.lon);
      if (!validPoint(lat, lon)) return false;
      close();
      var seq = m.seq;
      var live = function () { return seq === m.seq; };
      m.point = { lat: lat, lon: lon };
      var nowMs = Date.now();

      var card = el('div', 'wh-card');
      card.setAttribute('role', 'dialog');
      card.setAttribute('aria-label', "What's here");
      var head = el('div', 'wh-head');
      head.appendChild(el('span', null, "WHAT'S HERE"));
      var shut = button('wh-btn', 'X', 'Close (Esc)');
      shut.setAttribute('aria-label', 'Close');
      shut.addEventListener('click', close);
      head.appendChild(shut);
      card.appendChild(head);
      var place = el('div', 'wh-place wh-muted', 'Finding the place...');
      card.appendChild(place);
      var coordRow = row('Coords', coordsText(lat, lon));
      coordRow.children[1].className = '';
      card.appendChild(coordRow);
      var elevRow = row('Elevation', '...');
      var timeRow = row('Local time', '...');
      var wxRow = row('Weather', '...');
      card.appendChild(elevRow);
      card.appendChild(timeRow);
      card.appendChild(wxRow);
      var sats = section(card, 'SATELLITES OVERHEAD');
      var cams = section(card, 'NEAREST CAMERAS');
      var peaks = section(card, 'NEAREST PEAKS');
      card.appendChild(el('div', 'wh-foot',
        'Weather and elevation: Open-Meteo (CC BY 4.0). Place: OpenStreetMap Nominatim (ODbL).'));
      doc.body.appendChild(card);
      m.card = card;
      if (!(opts && opts.quiet)) note("What's here: " + lat.toFixed(3) + ', ' + lon.toFixed(3));

      function guessTime() {
        var off = Math.round(lon / 15) * 3600;
        return 'about ' + localTime(nowMs, off) + ' (' + formatOffset(off) + ' by longitude)';
      }

      // Place
      placeName(lat, lon).then(function (name) {
        if (!live()) return;
        if (name === null) {
          place.textContent = 'Place lookup needs the live server or map tools.';
          return;
        }
        place.textContent = name || 'No named place here (open water or remote land).';
        place.className = name ? 'wh-place' : 'wh-place wh-muted';
      }, function () {
        if (live()) place.textContent = 'The place lookup did not answer.';
      });

      // Weather, elevation and local time: one Open-Meteo call.
      weather(lat, lon).then(function (j) {
        if (!live()) return;
        if (isNum(j.elevation)) setText(elevRow, Math.round(j.elevation) + ' m (90 m elevation model)');
        else setText(elevRow, 'not known', true);
        if (isNum(j.utc_offset_seconds)) {
          var zone = typeof j.timezone === 'string' && j.timezone && j.timezone !== 'GMT'
            ? j.timezone + ', ' : '';
          setText(timeRow, localTime(nowMs, j.utc_offset_seconds) + '  (' + zone +
            formatOffset(j.utc_offset_seconds) + ')');
        } else {
          setText(timeRow, guessTime(), true);
        }
        var wx = describeWeather(j);
        if (wx) {
          setText(wxRow, wx.summary + (wx.detail ? '; ' + wx.detail : '') + (wx.isDay ? '' : ' (night)'));
        } else {
          setText(wxRow, 'no current conditions', true);
        }
      }, function () {
        if (!live()) return;
        setText(elevRow, 'not available', true);
        setText(timeRow, guessTime(), true);
        setText(wxRow, 'Open-Meteo did not answer', true);
      });

      // Satellites above 10 deg, from the positions the globe already holds.
      var eng = ctx.satEngine;
      sats.body.replaceChildren();
      if (!eng || !eng.sats || !eng.sats.length) {
        sats.body.textContent = 'No satellite catalog loaded yet.';
      } else {
        var over = overhead(eng.sats, lat, lon, MIN_ELEVATION_DEG, 3);
        sats.body.className = '';
        sats.body.appendChild(el('div', null, over.count + ' of ' + eng.sats.length +
          ' satellites are ' + MIN_ELEVATION_DEG + ' deg or more above the horizon (globe clock).'));
        over.top.forEach(function (s) {
          var b = button('wh-link', (s.name || 'NORAD ' + s.noradId) + '  ' + Math.round(s.el) + ' deg',
            'Select this satellite');
          b.addEventListener('click', function () {
            try { ctx.selectSatellite(s.noradId); } catch (e) { /* keep going */ }
          });
          sats.body.appendChild(b);
        });
      }

      // Nearest cameras and peaks (server catalogs).
      if (ctx.isStatic) {
        cams.body.textContent = 'Needs the live server (camera catalog).';
        peaks.body.textContent = 'Needs the live server (peak catalog).';
        return true;
      }
      var q = '?lat=' + round3(lat) + '&lon=' + round3(lon) + '&n=' + NEAR_N +
        '&radius_km=' + NEAR_RADIUS_KM;
      getJson(api + '/api/cameras/nearest' + q).then(function (j) {
        if (!live()) return;
        fillList(cams.body, (j && j.cameras) || [], 'No live camera within ' + NEAR_RADIUS_KM + ' km.',
          function (c) {
            return (c.name || 'Camera') + (c.city ? ', ' + c.city : '') + '  ' + formatKm(c.distance_km);
          }, function (c) {
            flyTo(c.lat, c.lon, 3000);
            var mod = global.SDACameras;
            if (mod && typeof mod.open === 'function') mod.open(c.rid);
          });
      }, function (err) {
        if (!live()) return;
        cams.body.textContent = err && err.status === 503 ? 'The camera catalog is not imported.'
          : 'Nearby cameras did not load.';
      });
      getJson(api + '/api/peaks/nearest' + q).then(function (j) {
        if (!live()) return;
        fillList(peaks.body, (j && j.peaks) || [], 'No catalogued summit within ' + NEAR_RADIUS_KM + ' km.',
          function (pk) {
            return (pk.name || 'Unnamed summit') +
              (isNum(pk.elev_m) ? ' ' + Math.round(pk.elev_m) + ' m' : '') + '  ' + formatKm(pk.distance_km);
          }, function (pk) {
            flyTo(pk.lat - 0.12, pk.lon, (isNum(pk.elev_m) ? pk.elev_m : 0) + 9000);
            var s = global.SDASummit;
            if (s && typeof s.open === 'function') {
              try {
                s.open({ id: pk.id, name: pk.name, lat: pk.lat, lon: pk.lon, elev_m: pk.elev_m,
                  prom_m: pk.prom_m });
              } catch (e) { /* keep going */ }
            }
          });
      }, function (err) {
        if (!live()) return;
        peaks.body.textContent = err && err.status === 503 ? 'The peak catalog is not imported.'
          : 'Nearby peaks did not load.';
      });
      return true;
    }

    // -- input --------------------------------------------------------------

    function bindRightClick() {
      if (m.handler || !m.rightClick || !C || !C.ScreenSpaceEventHandler || !viewer.scene) return;
      try {
        m.handler = new C.ScreenSpaceEventHandler(viewer.scene.canvas);
        m.handler.setInputAction(function (ev) {
          var p = ev && ev.position ? pickAt(ev.position.x, ev.position.y) : null;
          if (p) open(p);
        }, C.ScreenSpaceEventType.RIGHT_CLICK);
      } catch (e) { m.handler = null; }
    }

    function unbindRightClick() {
      if (!m.handler) return;
      try { m.handler.destroy(); } catch (e) { /* gone */ }
      m.handler = null;
    }

    function setEnabled(on) {
      m.rightClick = !!on;
      try { storage.setItem(STORE_KEY, m.rightClick ? '1' : '0'); } catch (e) { /* memory only */ }
      if (m.rightBox) m.rightBox.checked = m.rightClick;
      if (m.rightClick) bindRightClick(); else unbindRightClick();
    }

    var canvas = viewer.scene && viewer.scene.canvas;
    if (canvas && typeof canvas.addEventListener === 'function') {
      m.cleanup.push(attachLongPress(canvas, function (pt) {
        if (!m.rightClick) return;
        var x = pt.x;
        var y = pt.y;
        try {
          var r = canvas.getBoundingClientRect();
          x -= r.left; y -= r.top;
        } catch (e) { /* client coordinates then */ }
        var p = pickAt(x, y);
        if (p) open(p);
      }));
    }

    function onKey(e) {
      if (!m.card || e.key !== 'Escape') return;
      var t = e.target && e.target.tagName;
      if (t === 'INPUT' || t === 'TEXTAREA') return;
      close();
      try { e.preventDefault(); e.stopPropagation(); } catch (err) { /* keep going */ }
    }
    doc.addEventListener('keydown', onKey, true);
    m.cleanup.push(function () { doc.removeEventListener('keydown', onKey, true); });

    // -- panel --------------------------------------------------------------

    m.root = el('div', 'panel');
    var headEl = el('h3', null, "What's here");
    var body = el('div', 'panel-body');
    body.appendChild(el('div', 'wh-help',
      'Right-click the globe, or press and hold on a touch screen, for the place, elevation, local ' +
      'time, weather, nearest cameras and peaks, and the satellites overhead.'));
    var label = el('label', 'wh-check');
    m.rightBox = el('input');
    m.rightBox.setAttribute('type', 'checkbox');
    m.rightBox.checked = m.rightClick;
    m.rightBox.addEventListener('change', function () { setEnabled(m.rightBox.checked); });
    label.appendChild(m.rightBox);
    label.appendChild(el('span', null, 'Right-click and long press open the card'));
    var centreBtn = button('mode-btn', 'VIEW CENTRE', "What's at the centre of the view");
    body.appendChild(label);
    body.appendChild(centreBtn);
    m.root.appendChild(headEl);
    m.root.appendChild(body);
    // Taking the click first (and stopping it) gives one toggle whether init
    // runs before or after the page's own collapse binding.
    headEl.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function openAtCentre() {
      var p = centre();
      return p ? open(p) : false;
    }
    centreBtn.addEventListener('click', function () { openAtCentre(); });

    bindRightClick();

    function destroy() {
      close();
      unbindRightClick();
      m.cleanup.splice(0).forEach(function (fn) { try { fn(); } catch (e) { /* gone */ } });
      if (m.root.parentNode) m.root.parentNode.removeChild(m.root);
    }

    return {
      root: m.root,
      open: open,
      openAtCentre: openAtCentre,
      close: close,
      isOpen: function () { return !!m.card; },
      point: function () { return m.point; },
      setEnabled: setEnabled,
      enabled: function () { return m.rightClick; },
      destroy: destroy
    };
  }

  var instance = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function' || !ctx.viewer ||
          !global.Cesium || !global.document) return false;
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

  global.SDAWhatsHere = {
    init: init,
    open: function (p) { return instance ? instance.open(p) : false; },
    openAtCentre: function () { return instance ? instance.openAtCentre() : false; },
    close: function () { if (instance) instance.close(); },
    isOpen: function () { return !!(instance && instance.isOpen()); },
    point: function () { return instance ? instance.point() : null; },
    setEnabled: function (on) { if (instance) instance.setEnabled(on); },
    enabled: function () { return !!(instance && instance.enabled()); },
    destroy: function () { if (instance) instance.destroy(); instance = null; },
    STORE_KEY: STORE_KEY,
    LONG_PRESS_MS: LONG_PRESS_MS,
    LONG_PRESS_SLOP_PX: LONG_PRESS_SLOP_PX,
    MIN_ELEVATION_DEG: MIN_ELEVATION_DEG,
    EARTH_RADIUS_KM: EARTH_RADIUS_KM,
    wmoText: wmoText,
    compass: compass,
    formatOffset: formatOffset,
    localTime: localTime,
    weatherKey: weatherKey,
    weatherUrl: weatherUrl,
    elevationDeg: elevationDeg,
    overhead: overhead,
    describeWeather: describeWeather,
    attachLongPress: attachLongPress
  };
})(typeof window !== 'undefined' ? window : this);
