// Weather on the WorldView globe: precipitation radar, animated 10 m wind and
// tropical cyclones, each with its own switch in one panel.
//
// Radar: RainViewer's keyless Weather Maps API. The tile index
// (api.rainviewer.com/public/weather-maps.json) lists the past two hours in
// ten-minute frames; each frame is a Cesium UrlTemplateImageryProvider on
// tilecache.rainviewer.com. Since 2026-01-01 the free API serves zoom 0..7
// only (a deeper tile is a grey "Zoom Level Not Supported" picture sent with
// HTTP 200, so maximumLevel must be 7 or the globe fills with it), one colour
// scheme (2, Universal Blue), no nowcast, and 100 requests per IP per minute.
// Tiles are 512 px so a view needs a quarter of the requests, and every tile
// and index request draws from one 90-a-minute budget; when it is spent
// requestImage returns undefined, which is Cesium's "try again later". The
// browser reads RainViewer directly (it sends CORS headers), so radar also
// works in the static demo build.
//
// Wind: GET /api/weather/wind (sda/routes/weather.py), a 5-degree global grid
// of 10 m u/v from Open-Meteo, interpolated to the current hour. While the
// server is still building the grid in its paced passes the answer says
// complete: false and this module asks again every 70 s; after that every 30
// minutes. Drawing follows earth.nullschool: a few thousand particles advected
// through the bilinear-interpolated field, drawn as short segments bucketed by
// speed colour onto a 2D canvas laid over the globe, with the previous frame
// faded by a 'destination-in' fill so each particle leaves a trail.
//
// Why a screen-space canvas and not a texture on the globe or a
// PolylineCollection: the particles are projected with
// scene.cartesianToCanvasCoordinates after each globe frame (scene.postRender),
// so a frame costs two projections and one segment per particle and four
// thousand of them stay well inside a 60 fps budget on a laptop. A canvas
// texture on a RectangleGeometry has to be re-uploaded every frame (8 MB for a
// 2048 x 1024 RGBA texture) and is blurry when zoomed in; a PolylineCollection
// rebuilds its vertex buffers when most of its lines move, which is every frame
// here. Points on the far side of the globe are culled with a horizon test, the
// canvas is cleared rather than faded while the camera moves so trails never
// smear, the time step scales with camera height so particles cross the screen
// at the same pace at any zoom, and the particle count backs off when a frame
// takes too long.
//
// Cyclones: GET /api/weather/cyclones, NOAA NHC storms with forecast track,
// points and cone, plus GDACS events for the other basins. Markers carry
// id.sdaPick, so the page's click handler opens the storm in the panel.
//
// The static demo build has no /api routes: there the wind and cyclone switches
// say they need the live server and make no request.
//
// Donor code (MIT unless noted):
// - cambecc/earth (https://github.com/cambecc/earth, MIT, Copyright (c) 2014
//   Cameron Beccario, commit 5f091f0c3b60): the particle animation in
//   public/libs/earth/1.0.0/earth.js animate(): particles with a random initial
//   age that are re-seeded at MAX_PARTICLE_AGE, colour buckets drawn as one
//   path each, and the 'destination-in' rgba(0,0,0,0.97) fade per 40 ms frame
//   (made time-based here).
// - gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view, MIT,
//   Copyright (c) 2026 Bilawal Sidhu, commit 9423a26487de): sampleWind() and
//   normalizeLongitude() from src/layers/wind/model.js (bilinear sampling of a
//   north-to-south grid that wraps at the antimeridian), and the calm-blue to
//   red speed ramp there, lightened at the calm end to read over dark imagery.
// Frames: geodetic degrees only. Text is only ever set with textContent.
//
// Page wiring: list it in EXTENSIONS ({ global: 'SDAWeather', mount:
// 'ext-weather' }) and call SDAWeather.setEnabled(on) from one weather layer
// toggle, or SDAWeather.setLayer('radar' | 'wind' | 'cyclones', on) from three.
// Spans with ids count-weather (storms drawn), count-radar (frames),
// count-wind (particles) and count-cyclones (storms) are filled when present.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Weather';
  var LAYERS = ['radar', 'wind', 'cyclones'];
  var COUNT_IDS = {
    weather: 'count-weather', radar: 'count-radar', wind: 'count-wind',
    cyclones: 'count-cyclones'
  };
  var DEG = Math.PI / 180;
  var M_PER_DEG = 111320;

  // ---- radar (RainViewer) ----------------------------------------------
  var RAINVIEWER_INDEX_URL = 'https://api.rainviewer.com/public/weather-maps.json';
  var RADAR_HOST_RE = /^https:\/\/([a-z0-9-]+\.)*rainviewer\.com$/;
  var RADAR_PATH_RE = /^\/v2\/radar\/[A-Za-z0-9_-]{1,64}$/;
  var RADAR_TILE_PX = 512;
  var RADAR_MAX_LEVEL = 7;
  var RADAR_COLOR = 2;           // Universal Blue, the only scheme the free API serves
  var RADAR_OPTIONS = '1_1';     // smoothed, snow in its own colours
  var RADAR_MAX_FRAMES = 48;
  var RADAR_INDEX_REFRESH_MS = 5 * 60 * 1000;
  var RADAR_BUDGET = 90;         // of RainViewer's 100 requests per IP per minute
  var RADAR_BUDGET_WINDOW_MS = 60 * 1000;
  var RADAR_FRAME_MS = 700;
  var RADAR_HOLD_MS = 1600;      // the newest frame stays up a little longer
  var RADAR_RECHECK_MS = 250;
  var RADAR_MAX_WAIT_MS = 3000;  // advance anyway when tiles are this slow
  var DEFAULT_RADAR_OPACITY = 70;

  // ---- wind --------------------------------------------------------------
  var WIND_REFRESH_MS = 30 * 60 * 1000;
  var WIND_BUILD_POLL_MS = 70 * 1000;
  var WIND_MAX_SPEED = 30;           // m/s at the top of the colour scale
  var WIND_BUCKETS = 16;
  var WIND_MAX_AGE_MS = 3500;
  var WIND_FADE_PER_40MS = 0.97;     // earth.nullschool's per-frame trail keep
  var WIND_SECONDS_PER_M = 1.05e-4;  // simulated s per frame per metre of camera height
  var WIND_MIN_PARTICLES = 400;
  var WIND_MAX_PARTICLES = 4000;
  var WIND_PX_PER_PARTICLE = 500;
  var WIND_LINE_WIDTH = 1.25;
  var WIND_MAX_SEGMENT_PX = 120;
  var WIND_SLOW_FRAME_MS = 8;
  var WIND_FAST_FRAME_MS = 3;
  var WIND_ALPHA = 0.9;
  // [m/s, r, g, b]
  var WIND_RAMP = [
    [0, 110, 130, 200],
    [3, 70, 160, 230],
    [6, 60, 210, 210],
    [9, 110, 225, 120],
    [12, 235, 225, 90],
    [16, 250, 165, 60],
    [20, 240, 90, 70],
    [25, 220, 70, 170],
    [30, 245, 210, 255]
  ];

  // ---- cyclones ------------------------------------------------------------
  var CYCLONE_REFRESH_MS = 15 * 60 * 1000;
  var CYCLONE_FLY_HEIGHT_M = 3500000;
  var CONE_HEIGHT_M = 2000;
  var MAX_STORMS = 64;
  var MAX_PARTS = 16;
  var MAX_PART_POINTS = 2000;
  // Saffir-Simpson track colours
  var INTENSITY_COLORS = {
    TD: '#5ebaff', TS: '#00faf4', C1: '#ffffcc', C2: '#ffe775', C3: '#ffc140',
    C4: '#ff8f20', C5: '#ff6060', UNK: '#c8c8c8'
  };
  var INTENSITY_SHORT = {
    TD: 'TD', TS: 'TS', C1: 'Cat 1', C2: 'Cat 2', C3: 'Cat 3', C4: 'Cat 4', C5: 'Cat 5',
    UNK: 'TC'
  };

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the tests)
  // ---------------------------------------------------------------------

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function round(x, places) {
    var k = Math.pow(10, places || 0);
    return Math.round(x * k) / k;
  }

  function clamp(x, lo, hi) { return x < lo ? lo : (x > hi ? hi : x); }

  // A percentage 0..100; anything unreadable is the default.
  function clampPct(v, dflt) {
    var n = (typeof v === 'string' && v.trim() !== '') ? Number(v) : v;
    if (!finite(n)) return dflt;
    return Math.round(clamp(n, 0, 100));
  }

  function normLon(lon) { return ((((lon + 180) % 360) + 360) % 360) - 180; }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // RainViewer's index as { host, generated, frames: [{ time, path, kind }] },
  // oldest first, or null when it is not usable. Only https hosts under
  // rainviewer.com and /v2/radar/<id> paths are accepted.
  function parseRainviewer(data) {
    if (!data || typeof data !== 'object') return null;
    var host = typeof data.host === 'string' ? data.host.replace(/\/+$/, '') : '';
    if (!RADAR_HOST_RE.test(host)) return null;
    var radar = data.radar;
    if (!radar || typeof radar !== 'object') return null;
    var seen = {};
    var frames = [];
    [['past', radar.past], ['nowcast', radar.nowcast]].forEach(function (group) {
      if (!Array.isArray(group[1])) return;
      group[1].forEach(function (f) {
        if (!f || typeof f !== 'object') return;
        if (!finite(f.time) || f.time <= 0 || typeof f.path !== 'string') return;
        if (!RADAR_PATH_RE.test(f.path) || seen[f.path]) return;
        seen[f.path] = true;
        frames.push({ time: f.time, path: f.path, kind: group[0] });
      });
    });
    if (!frames.length) return null;
    frames.sort(function (a, b) { return a.time - b.time; });
    if (frames.length > RADAR_MAX_FRAMES) frames = frames.slice(-RADAR_MAX_FRAMES);
    return { host: host, generated: finite(data.generated) ? data.generated : null,
             frames: frames };
  }

  function radarTileUrl(host, path) {
    return host + path + '/' + RADAR_TILE_PX + '/{z}/{x}/{y}/' + RADAR_COLOR + '/' +
      RADAR_OPTIONS + '.png';
  }

  // 1795130400, now -> '14:00 UTC (-20 min)'; a nowcast frame reads '+10 min'.
  function frameLabel(unixS, nowMs) {
    if (!finite(unixS)) return '';
    var d = new Date(unixS * 1000);
    var label = pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ' UTC';
    if (finite(nowMs)) {
      var mins = Math.round((unixS * 1000 - nowMs) / 60000);
      label += mins === 0 ? ' (now)' : ' (' + (mins > 0 ? '+' : '') + mins + ' min)';
    }
    return label;
  }

  // A sliding-window request budget: take() is false once `limit` requests
  // were taken in the last `windowMs`; refund() hands the newest one back.
  function makeBudget(limit, windowMs) {
    var stamps = [];
    function prune(now) {
      while (stamps.length && now - stamps[0] >= windowMs) stamps.shift();
    }
    return {
      limit: limit,
      take: function (now) {
        prune(now);
        if (stamps.length >= limit) return false;
        stamps.push(now);
        return true;
      },
      refund: function () { stamps.pop(); },
      used: function (now) { prune(now); return stamps.length; }
    };
  }

  // The server's grid checked and packed: Float32 u/v, a global wrap in
  // longitude, rows from la1 running south. Junk values become calm.
  function cleanWindGrid(data) {
    if (!data || typeof data !== 'object') return null;
    var nx = data.nx, ny = data.ny, dx = data.dx, dy = data.dy;
    var lo1 = data.lo1, la1 = data.la1;
    if (!finite(nx) || !finite(ny) || nx % 1 || ny % 1 || nx < 2 || ny < 2) return null;
    if (nx * ny > 400000) return null;
    if (!finite(dx) || !finite(dy) || dx <= 0 || dy <= 0) return null;
    if (Math.abs(nx * dx - 360) > 1e-6) return null;
    if (!finite(lo1) || !finite(la1) || la1 > 90 || la1 - (ny - 1) * dy < -90 - 1e-9) {
      return null;
    }
    var u = data.u, v = data.v;
    var n = nx * ny;
    if (!Array.isArray(u) || !Array.isArray(v) || u.length !== n || v.length !== n) return null;
    var U = new Float32Array(n);
    var V = new Float32Array(n);
    var top = 0;
    for (var i = 0; i < n; i++) {
      var a = u[i], b = v[i];
      if (!finite(a) || !finite(b) || Math.abs(a) > 150 || Math.abs(b) > 150) { a = 0; b = 0; }
      U[i] = a;
      V[i] = b;
      var s = Math.sqrt(a * a + b * b);
      if (s > top) top = s;
    }
    var south = la1 - (ny - 1) * dy;
    return { nx: nx, ny: ny, lo1: lo1, la1: la1, dx: dx, dy: dy, u: U, v: V,
             maxSpeed: round(top, 2), latLimit: Math.min(la1, -south) };
  }

  // Bilinear u, v at (lon, lat), wrapped at the antimeridian, clamped at the
  // first and last rows. Adapted from gods-eye-view src/layers/wind/model.js.
  function sampleWind(field, lon, lat, out) {
    out = out || {};
    if (!field || !finite(lon) || !finite(lat)) {
      out.u = NaN;
      out.v = NaN;
      return out;
    }
    var nx = field.nx, ny = field.ny;
    var x = ((((lon - field.lo1) % 360) + 360) % 360) / field.dx;
    var y = clamp((field.la1 - lat) / field.dy, 0, ny - 1);
    var x0 = Math.floor(x);
    var tx = x - x0;
    var y0 = Math.floor(y);
    var y1 = Math.min(ny - 1, y0 + 1);
    var ty = y - y0;
    var left = ((x0 % nx) + nx) % nx;
    var right = (left + 1) % nx;
    var a = y0 * nx + left, b = y0 * nx + right, c = y1 * nx + left, d = y1 * nx + right;
    var U = field.u, V = field.v;
    out.u = (U[a] * (1 - tx) + U[b] * tx) * (1 - ty) + (U[c] * (1 - tx) + U[d] * tx) * ty;
    out.v = (V[a] * (1 - tx) + V[b] * tx) * (1 - ty) + (V[c] * (1 - tx) + V[d] * tx) * ty;
    return out;
  }

  // Move particle p ({ lon, lat }) on through the field for `seconds` of wind
  // time, keeping where it was in plon, plat. Returns the wind speed it moved
  // at, or NaN when it left the grid (past latLimit) or found no wind value.
  function stepParticle(p, field, seconds, scratch, latLimit) {
    var s = sampleWind(field, p.lon, p.lat, scratch || {});
    if (!finite(s.u) || !finite(s.v)) return NaN;
    var coslat = Math.cos(p.lat * DEG);
    if (coslat < 0.01) return NaN;
    p.plon = p.lon;
    p.plat = p.lat;
    p.lon = normLon(p.lon + s.u * seconds / (M_PER_DEG * coslat));
    p.lat = p.lat + s.v * seconds / M_PER_DEG;
    var limit = finite(latLimit) ? latLimit : 90;
    if (Math.abs(p.lat) > limit) return NaN;
    return Math.sqrt(s.u * s.u + s.v * s.v);
  }

  // [r, g, b] 0..255 for a speed in m/s.
  function windRgb(speed) {
    var s = finite(speed) ? clamp(speed, 0, WIND_RAMP[WIND_RAMP.length - 1][0]) : 0;
    var i = 1;
    while (i < WIND_RAMP.length - 1 && s > WIND_RAMP[i][0]) i++;
    var lo = WIND_RAMP[i - 1];
    var hi = WIND_RAMP[i];
    var t = hi[0] === lo[0] ? 1 : (s - lo[0]) / (hi[0] - lo[0]);
    return [1, 2, 3].map(function (k) { return Math.round(lo[k] + (hi[k] - lo[k]) * t); });
  }

  function windColor(speed, alpha) {
    var c = windRgb(speed);
    return alpha === undefined ? 'rgb(' + c.join(',') + ')'
      : 'rgba(' + c.join(',') + ',' + alpha + ')';
  }

  function bucketFor(speed) {
    if (!finite(speed) || speed <= 0) return 0;
    return Math.round(Math.min(speed, WIND_MAX_SPEED) / WIND_MAX_SPEED * (WIND_BUCKETS - 1));
  }

  function bucketColors() {
    var out = [];
    for (var i = 0; i < WIND_BUCKETS; i++) {
      out.push(windColor(i * WIND_MAX_SPEED / (WIND_BUCKETS - 1), WIND_ALPHA));
    }
    return out;
  }

  // CSS gradient for the legend, the same stops as the particles.
  function legendGradient() {
    var top = WIND_RAMP[WIND_RAMP.length - 1][0];
    return 'linear-gradient(90deg, ' + WIND_RAMP.map(function (s) {
      return 'rgb(' + s[1] + ',' + s[2] + ',' + s[3] + ') ' + round(s[0] / top * 100, 1) + '%';
    }).join(', ') + ')';
  }

  // How much of the trail canvas survives a frame of `frameMs`: 0.97 per 40 ms.
  function fadeKeep(frameMs) {
    var ms = finite(frameMs) ? clamp(frameMs, 1, 250) : 16.7;
    return round(clamp(Math.pow(WIND_FADE_PER_40MS, ms / 40), 0.5, 0.999), 4);
  }

  // Wind time per frame, scaled with camera height so a 10 m/s wind crosses
  // about one pixel per 60 fps frame at any zoom.
  function simSecondsPerFrame(heightM, frameMs) {
    var h = finite(heightM) ? clamp(heightM, 5e4, 4e7) : 2e7;
    var ms = finite(frameMs) ? clamp(frameMs, 1, 100) : 16.7;
    return h * WIND_SECONDS_PER_M * (ms / 16.67);
  }

  function particleCount(width, height) {
    if (!finite(width) || !finite(height) || width <= 0 || height <= 0) return WIND_MIN_PARTICLES;
    return Math.round(clamp(width * height / WIND_PX_PER_PARTICLE, WIND_MIN_PARTICLES,
                            WIND_MAX_PARTICLES));
  }

  // A random place, area-uniform, between -limit and limit degrees of latitude.
  function seedGlobal(rand, limit) {
    var lim = (finite(limit) ? clamp(limit, 1, 90) : 90) * DEG;
    var lat = Math.asin(Math.sin(-lim) + rand() * (Math.sin(lim) - Math.sin(-lim))) / DEG;
    return { lon: normLon(rand() * 360 - 180), lat: lat };
  }

  function intensityFor(kt) {
    if (!finite(kt)) return 'UNK';
    if (kt < 34) return 'TD';
    if (kt < 64) return 'TS';
    if (kt < 83) return 'C1';
    if (kt < 96) return 'C2';
    if (kt < 113) return 'C3';
    if (kt < 137) return 'C4';
    return 'C5';
  }

  function cleanText(v, limit) {
    if (typeof v !== 'string') return '';
    return v.replace(/[\u0000-\u001f\u007f<>]/g, '').trim().slice(0, limit || 80);
  }

  function httpsUrl(v) {
    if (typeof v !== 'string' || v.length > 512) return null;
    return /^https:\/\/[A-Za-z0-9.-]+(\/[^\s"'<>]*)?$/.test(v) ? v : null;
  }

  function lonLat(pair) {
    if (!Array.isArray(pair) || pair.length < 2) return null;
    var lon = pair[0], lat = pair[1];
    if (!finite(lon) || !finite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90) return null;
    return [lon, lat];
  }

  function cleanParts(parts, minPoints) {
    var out = [];
    if (!Array.isArray(parts)) return out;
    parts.slice(0, MAX_PARTS).forEach(function (part) {
      if (!Array.isArray(part) || part.length < minPoints || part.length > MAX_PART_POINTS) return;
      var pts = [];
      for (var i = 0; i < part.length; i++) {
        var p = lonLat(part[i]);
        if (!p) return;
        pts.push(p);
      }
      out.push(pts);
    });
    return out;
  }

  function num(v, lo, hi) { return finite(v) && v >= lo && v <= hi ? v : null; }

  // The server's storms, every field re-checked; a storm without a name or a
  // position is dropped.
  function cleanStorms(data) {
    var list = data && Array.isArray(data.storms) ? data.storms : [];
    var out = [];
    var seen = {};
    list.slice(0, MAX_STORMS).forEach(function (s) {
      if (!s || typeof s !== 'object') return;
      var id = cleanText(s.id, 40);
      var name = cleanText(s.name, 40);
      var where = lonLat([s.lon, s.lat]);
      if (!id || !name || !where || seen[id]) return;
      seen[id] = true;
      var wind = num(s.wind_kt, 0, 250);
      var level = INTENSITY_COLORS[s.intensity] ? s.intensity : intensityFor(wind);
      var fps = [];
      (Array.isArray(s.forecast_points) ? s.forecast_points : []).slice(0, 32).forEach(
        function (fp) {
          var at = fp && lonLat([fp.lon, fp.lat]);
          if (at) fps.push({ lon: at[0], lat: at[1], tau_h: num(fp.tau_h, 0, 240),
                             wind_kt: num(fp.wind_kt, 0, 250) });
        });
      out.push({
        id: id, name: name,
        source: cleanText(s.source, 16) || 'unknown',
        agency: cleanText(s.agency, 16),
        category: cleanText(s.category, 60) || 'Tropical cyclone',
        intensity: level,
        wind_kt: wind,
        wind_basis: cleanText(s.wind_basis, 30),
        pressure_hpa: num(s.pressure_hpa, 850, 1100),
        lon: where[0], lat: where[1],
        movement_dir_deg: num(s.movement_dir_deg, 0, 360),
        movement_speed_kt: num(s.movement_speed_kt, 0, 200),
        advisory: cleanText(s.advisory, 8),
        issued_at: cleanText(s.issued_at, 40),
        updated_at: cleanText(s.updated_at, 40),
        url: httpsUrl(s.url),
        alert_level: cleanText(s.alert_level, 8),
        country: cleanText(s.country, 80),
        track: cleanParts(s.track, 2),
        cone: cleanParts(s.cone, 4),
        forecast_points: fps,
        geometry_status: cleanText(s.geometry_status, 20)
      });
    });
    return out;
  }

  // 'MILTON: Hurricane (category 4), 135 kt, 920 hPa'
  function stormLine(s) {
    var parts = [s.category];
    if (finite(s.wind_kt)) {
      parts.push(Math.round(s.wind_kt) + ' kt' + (s.wind_basis === 'event peak' ? ' peak' : ''));
    }
    if (finite(s.pressure_hpa)) parts.push(Math.round(s.pressure_hpa) + ' hPa');
    return s.name + ': ' + parts.join(', ');
  }

  function stormLabel(s) {
    var line = s.name + ' ' + (INTENSITY_SHORT[s.intensity] || 'TC');
    if (finite(s.wind_kt)) line += ' ' + Math.round(s.wind_kt) + ' kt';
    return line;
  }

  function windUrl(api) { return (api || '') + '/api/weather/wind'; }
  function cyclonesUrl(api) { return (api || '') + '/api/weather/cyclones'; }

  // '2026-11-19T14:30:00Z' -> '14:30 UTC'
  function shortTime(s) {
    var m = /T(\d{2}):(\d{2})/.exec(typeof s === 'string' ? s : '');
    return m ? m[1] + ':' + m[2] + ' UTC' : '';
  }

  function windLine(meta) {
    var line = 'Wind (Open-Meteo 10 m): ' + meta.resolution + '-degree grid';
    if (!meta.complete) {
      line += ', still building (' + meta.filled + ' of ' + meta.total + ' points fetched)';
    }
    line += ', max ' + meta.maxSpeed.toFixed(1) + ' m/s';
    var t = shortTime(meta.validTime);
    if (t) line += ', valid ' + t;
    if (meta.stale) line += ' (stale: last good copy)';
    return line;
  }

  function cycloneLine(storms, data) {
    var line;
    if (!storms.length) {
      line = 'Cyclones: no active tropical cyclones (NHC, GDACS)';
    } else {
      line = 'Cyclones: ' + storms.length + ' active: ' + storms.slice(0, 4).map(function (s) {
        return s.name + ' (' + (INTENSITY_SHORT[s.intensity] || 'TC') +
          (finite(s.wind_kt) ? ', ' + Math.round(s.wind_kt) + ' kt' : '') + ')';
      }).join(', ') + (storms.length > 4 ? ', ...' : '');
    }
    if (data && data.stale) line += ' (a source is stale or unavailable)';
    return line;
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

  function byId(id) {
    var doc = global.document;
    return doc && typeof doc.getElementById === 'function' ? doc.getElementById(id) : null;
  }

  function nowMs() {
    var p = global.performance;
    return p && typeof p.now === 'function' ? p.now() : Date.now();
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var api = ctx.api || '';
    var rand = typeof ctx.random === 'function' ? ctx.random : Math.random;
    var m = {
      enabled: false,
      want: { radar: true, wind: true, cyclones: true },
      on: { radar: false, wind: false, cyclones: false },
      staticNoted: {},
      lastLine: {},
      failed: {},
      ui: {},
      radar: {
        host: '', frames: [], layers: {}, index: 0, playing: false, timer: null,
        refreshTimer: null, opacity: DEFAULT_RADAR_OPACITY, seq: 0, shownAt: 0,
        budget: makeBudget(RADAR_BUDGET, RADAR_BUDGET_WINDOW_MS)
      },
      wind: {
        field: null, meta: null, timer: null, seq: 0, canvas: null, g: null, cssW: 0,
        cssH: 0, dpr: 1, particles: [], count: 0, target: 0, listening: false,
        lastFrame: 0, cam: null, ema: 0, cooldown: 0, colors: bucketColors(),
        buckets: [], scratch: { u: 0, v: 0 }, a: { x: 0, y: 0 }, b: { x: 0, y: 0 },
        cart: null, win: null, pick: null, drawn: 0
      },
      cyc: { storms: [], data: null, root: null, timer: null, seq: 0, selected: null }
    };
    for (var bi = 0; bi < WIND_BUCKETS; bi++) m.wind.buckets.push([]);

    function note(msg) {
      try { if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem(msg); } catch (e) {
        /* keep going */
      }
    }

    function noteOnce(key, line) {
      if (line === m.lastLine[key]) return;
      m.lastLine[key] = line;
      note(line);
    }

    function later(fn, ms) { return global.setTimeout(fn, ms); }
    function cancel(id) { if (id) global.clearTimeout(id); }

    function setCount(key, n) {
      try {
        var box = byId(COUNT_IDS[key]);
        if (box) box.textContent = String(n);
      } catch (e) { /* keep going */ }
    }

    function setStatus(key, msg) {
      var box = m.ui[key + 'Status'];
      if (box) box.textContent = msg;
    }

    function container() {
      var c = viewer.container || (viewer.canvas && viewer.canvas.parentNode) || null;
      return c && typeof c.appendChild === 'function' ? c : null;
    }

    // -- panel ----------------------------------------------------------

    function sliderRow(label, min, max, value, onInput) {
      var row = el('div', 'slider-row');
      row.appendChild(el('label', null, label));
      var input = el('input');
      input.type = 'range';
      input.min = String(min);
      input.max = String(max);
      input.step = '1';
      input.value = String(value);
      var val = el('span', 'val', '');
      input.addEventListener('input', function () { onInput(input.value); });
      row.appendChild(input);
      row.appendChild(val);
      return { row: row, input: input, val: val };
    }

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      var root = el('div', 'panel');
      var body = el('div', 'panel-body');
      var ui = m.ui;
      ui.root = root;
      ui.status = el('div', 'weather-status', 'Turn on the weather layer for radar, wind and ' +
        'tropical cyclones.');
      body.appendChild(ui.status);

      ui.boxes = {};
      var labels = { radar: 'Radar (RainViewer)', wind: 'Wind 10 m (Open-Meteo)',
                     cyclones: 'Cyclones (NHC, GDACS)' };
      LAYERS.forEach(function (name) {
        var row = el('label', 'layer-toggle weather-sub');
        var box = el('input', 'weather-sub-' + name);
        box.type = 'checkbox';
        box.checked = m.want[name];
        box.addEventListener('change', function () { setLayer(name, box.checked); });
        row.appendChild(box);
        row.appendChild(el('span', null, labels[name]));
        body.appendChild(row);
        ui.boxes[name] = box;
      });

      // radar
      var radar = el('div', 'weather-radar');
      var playRow = el('div', 'weather-play-row');
      playRow.style.display = 'flex';
      playRow.style.alignItems = 'center';
      playRow.style.gap = '6px';
      playRow.style.margin = '4px 0';
      ui.play = el('button', 'preset-btn weather-play', 'PLAY');
      ui.play.type = 'button';
      ui.play.addEventListener('click', function () { playRadar(!m.radar.playing); });
      ui.frame = el('span', 'weather-frame', '--:-- UTC');
      ui.frame.style.fontSize = '10px';
      playRow.appendChild(ui.play);
      playRow.appendChild(ui.frame);
      radar.appendChild(playRow);
      var frameRow = sliderRow('FRAME', 0, 0, 0, function (v) {
        playRadar(false);
        showFrame(Number(v));
      });
      ui.frameSlider = frameRow.input;
      ui.frameVal = frameRow.val;
      radar.appendChild(frameRow.row);
      var opRow = sliderRow('OPACITY', 0, 100, m.radar.opacity, function (v) {
        setRadarOpacity(v);
      });
      ui.opacity = opRow.input;
      ui.opacityVal = opRow.val;
      ui.opacityVal.textContent = m.radar.opacity + '%';
      radar.appendChild(opRow.row);
      ui.radarStatus = el('div', 'weather-radar-status', '');
      radar.appendChild(ui.radarStatus);
      body.appendChild(radar);

      // wind legend
      var wind = el('div', 'weather-wind');
      var bar = el('div', 'weather-wind-legend');
      bar.style.height = '6px';
      bar.style.margin = '6px 0 2px';
      bar.style.borderRadius = '2px';
      bar.style.background = legendGradient();
      wind.appendChild(bar);
      var ticks = el('div', 'weather-wind-ticks');
      ticks.style.display = 'flex';
      ticks.style.justifyContent = 'space-between';
      ticks.style.fontSize = '9px';
      ['0', '10', '20', '30+ m/s'].forEach(function (t) { ticks.appendChild(el('span', null, t)); });
      wind.appendChild(ticks);
      ui.windStatus = el('div', 'weather-wind-status', ctx.isStatic
        ? 'Wind needs the live server.' : '');
      wind.appendChild(ui.windStatus);
      body.appendChild(wind);

      // cyclones
      var cyc = el('div', 'weather-cyclones');
      ui.cyclonesStatus = el('div', 'weather-cyclones-status', ctx.isStatic
        ? 'Cyclones need the live server.' : '');
      cyc.appendChild(ui.cyclonesStatus);
      ui.stormList = el('div', 'weather-storm-list');
      cyc.appendChild(ui.stormList);
      ui.stormCard = el('div', 'weather-storm-card');
      cyc.appendChild(ui.stormCard);
      body.appendChild(cyc);

      var credit = el('div', 'weather-credit',
        'Radar: RainViewer. Wind: Open-Meteo (CC BY 4.0). Cyclones: NOAA NHC, GDACS.');
      credit.style.fontSize = '8px';
      credit.style.marginTop = '6px';
      credit.style.opacity = '0.7';
      body.appendChild(credit);

      [ui.status, ui.radarStatus, ui.windStatus, ui.cyclonesStatus].forEach(function (s) {
        s.style.fontSize = '9px';
        s.style.letterSpacing = '0.5px';
        s.style.margin = '3px 0';
      });

      root.appendChild(el('h3', null, PANEL_TITLE));
      root.appendChild(body);
      ctx.mount.appendChild(root);
    }

    function syncBoxes() {
      if (!m.ui.boxes) return;
      LAYERS.forEach(function (name) { m.ui.boxes[name].checked = m.want[name]; });
    }

    function syncStatus() {
      if (!m.ui.status) return;
      var on = LAYERS.filter(function (n) { return m.on[n]; });
      m.ui.status.textContent = !m.enabled
        ? 'Turn on the weather layer for radar, wind and tropical cyclones.'
        : (on.length ? 'Showing: ' + on.join(', ') + '.' : 'Pick a weather layer below.');
    }

    // -- radar ------------------------------------------------------------

    function radarProvider(frame) {
      var r = m.radar;
      var provider = new C.UrlTemplateImageryProvider({
        url: radarTileUrl(r.host, frame.path),
        tilingScheme: new C.WebMercatorTilingScheme(),
        tileWidth: RADAR_TILE_PX,
        tileHeight: RADAR_TILE_PX,
        minimumLevel: 0,
        maximumLevel: RADAR_MAX_LEVEL,
        credit: 'RainViewer'
      });
      var inner = provider.requestImage;
      if (typeof inner === 'function') {
        provider.requestImage = function (x, y, level, request) {
          if (!r.budget.take(nowMs())) return undefined;
          var out = inner.call(provider, x, y, level, request);
          if (out === undefined) r.budget.refund();
          return out;
        };
      }
      return provider;
    }

    function radarLayer(frame) {
      var r = m.radar;
      var layer = r.layers[frame.path];
      if (layer) return layer;
      layer = new C.ImageryLayer(radarProvider(frame), { alpha: 0, show: false });
      viewer.imageryLayers.add(layer);
      r.layers[frame.path] = layer;
      return layer;
    }

    function dropRadarLayer(path) {
      var layer = m.radar.layers[path];
      delete m.radar.layers[path];
      try {
        if (layer && viewer.imageryLayers.contains(layer)) viewer.imageryLayers.remove(layer, true);
      } catch (e) { /* keep going */ }
    }

    function showFrame(i) {
      var r = m.radar;
      var n = r.frames.length;
      if (!n || !m.on.radar) return;
      i = finite(i) ? ((Math.round(i) % n) + n) % n : n - 1;
      r.index = i;
      var cur = r.frames[i];
      var next = r.playing && n > 1 ? r.frames[(i + 1) % n] : null;
      r.frames.forEach(function (f) {
        var layer = r.layers[f.path];
        if (f === cur) {
          layer = radarLayer(f);
          layer.show = true;
          layer.alpha = r.opacity / 100;
        } else if (f === next) {
          layer = radarLayer(f);
          layer.show = true;
          layer.alpha = 0;
        } else if (layer) {
          layer.show = false;
          layer.alpha = 0;
        }
      });
      if (m.ui.frame) m.ui.frame.textContent = frameLabel(cur.time, Date.now());
      if (m.ui.frameSlider) {
        m.ui.frameSlider.max = String(n - 1);
        m.ui.frameSlider.value = String(i);
      }
      if (m.ui.frameVal) m.ui.frameVal.textContent = (i + 1) + '/' + n;
    }

    function radarTick() {
      var r = m.radar;
      r.timer = null;
      if (!r.playing || !m.on.radar || !r.frames.length) return;
      var globe = scene.globe;
      var loaded = !globe || globe.tilesLoaded !== false;
      if (!loaded && nowMs() - r.shownAt < RADAR_MAX_WAIT_MS) {
        r.timer = later(radarTick, RADAR_RECHECK_MS);
        return;
      }
      showFrame(r.index + 1);
      r.shownAt = nowMs();
      r.timer = later(radarTick, r.index === r.frames.length - 1 ? RADAR_HOLD_MS : RADAR_FRAME_MS);
    }

    function playRadar(on) {
      var r = m.radar;
      on = !!on && m.on.radar && r.frames.length > 1;
      cancel(r.timer);
      r.timer = null;
      r.playing = on;
      if (m.ui.play) m.ui.play.textContent = on ? 'PAUSE' : 'PLAY';
      showFrame(r.index);
      if (on) {
        r.shownAt = nowMs();
        r.timer = later(radarTick, RADAR_FRAME_MS);
      }
    }

    function setRadarOpacity(v) {
      var r = m.radar;
      r.opacity = clampPct(v, DEFAULT_RADAR_OPACITY);
      if (m.ui.opacity) m.ui.opacity.value = String(r.opacity);
      if (m.ui.opacityVal) m.ui.opacityVal.textContent = r.opacity + '%';
      var cur = r.frames[r.index];
      var layer = cur && r.layers[cur.path];
      if (layer && layer.show) layer.alpha = r.opacity / 100;
    }

    function applyIndex(parsed) {
      var r = m.radar;
      var hostChanged = parsed.host !== r.host;
      r.host = parsed.host;
      var keep = {};
      parsed.frames.forEach(function (f) { keep[f.path] = true; });
      Object.keys(r.layers).forEach(function (path) {
        if (hostChanged || !keep[path]) dropRadarLayer(path);
      });
      var wasLatest = !r.frames.length || r.index === r.frames.length - 1;
      r.frames = parsed.frames;
      if (!r.playing || wasLatest) r.index = r.frames.length - 1;
      if (r.index >= r.frames.length) r.index = r.frames.length - 1;
      showFrame(r.index);
      setCount('radar', r.frames.length);
      var first = r.frames[0];
      var last = r.frames[r.frames.length - 1];
      var line = 'Radar (RainViewer): ' + r.frames.length + ' frames, ' +
        frameLabel(first.time) + ' to ' + frameLabel(last.time);
      setStatus('radar', line);
      noteOnce('radar', line);
    }

    function loadRadarIndex() {
      var r = m.radar;
      if (!m.on.radar) return Promise.resolve(null);
      if (!r.budget.take(nowMs())) return Promise.resolve(null);
      var seq = ++r.seq;
      if (!r.frames.length) setStatus('radar', 'Loading the RainViewer radar frames...');
      return global.fetch(RAINVIEWER_INDEX_URL).then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      }).then(function (data) {
        if (seq !== r.seq || !m.on.radar) return null;
        var parsed = parseRainviewer(data);
        if (!parsed) throw new Error('no frames');
        m.failed.radar = false;
        applyIndex(parsed);
        return parsed.frames.length;
      }).catch(function () {
        if (seq !== r.seq || !m.on.radar) return null;
        setStatus('radar', r.frames.length
          ? 'RainViewer did not answer; showing the frames already loaded.'
          : 'RainViewer radar did not load; retrying in five minutes.');
        if (!m.failed.radar) {
          m.failed.radar = true;
          note('Radar: RainViewer weather maps unavailable');
        }
        return null;
      });
    }

    function radarOn() {
      m.on.radar = true;
      m.failed.radar = false;
      m.lastLine.radar = '';
      loadRadarIndex();
      m.radar.refreshTimer = global.setInterval(loadRadarIndex, RADAR_INDEX_REFRESH_MS);
    }

    function radarOff() {
      var r = m.radar;
      m.on.radar = false;
      r.seq++;
      r.playing = false;
      cancel(r.timer);
      r.timer = null;
      if (r.refreshTimer) global.clearInterval(r.refreshTimer);
      r.refreshTimer = null;
      Object.keys(r.layers).forEach(dropRadarLayer);
      r.frames = [];
      r.index = 0;
      if (m.ui.play) m.ui.play.textContent = 'PLAY';
      if (m.ui.frame) m.ui.frame.textContent = '--:-- UTC';
      setStatus('radar', '');
      setCount('radar', 0);
    }

    // -- wind -------------------------------------------------------------

    function windCanvas() {
      var w = m.wind;
      if (w.canvas) return w.canvas;
      var host = container();
      if (!host) return null;
      var c = global.document.createElement('canvas');
      c.className = 'weather-wind-canvas';
      c.style.position = 'absolute';
      c.style.left = '0';
      c.style.top = '0';
      c.style.width = '100%';
      c.style.height = '100%';
      c.style.pointerEvents = 'none';
      c.style.zIndex = '1';
      var g = c.getContext && c.getContext('2d');
      if (!g) return null;
      host.appendChild(c);
      w.canvas = c;
      w.g = g;
      w.cssW = 0;
      w.cssH = 0;
      return c;
    }

    // Match the canvas to its container; true when the size changed.
    function syncCanvasSize() {
      var w = m.wind;
      var host = w.canvas.parentNode || container();
      var cw = (host && host.clientWidth) || (scene.canvas && scene.canvas.clientWidth) || 0;
      var ch = (host && host.clientHeight) || (scene.canvas && scene.canvas.clientHeight) || 0;
      var dpr = Math.min(2, global.devicePixelRatio || 1);
      if (cw === w.cssW && ch === w.cssH && dpr === w.dpr) return false;
      w.cssW = cw;
      w.cssH = ch;
      w.dpr = dpr;
      w.canvas.width = Math.round(cw * dpr);
      w.canvas.height = Math.round(ch * dpr);
      if (typeof w.g.setTransform === 'function') w.g.setTransform(dpr, 0, 0, dpr, 0, 0);
      w.g.lineWidth = WIND_LINE_WIDTH;
      w.target = particleCount(cw, ch);
      if (!w.count || w.count > w.target) w.count = w.target;
      return true;
    }

    function cameraMoved() {
      var w = m.wind;
      var cam = scene.camera;
      var p = cam.positionWC || cam.position;
      var d = cam.directionWC || cam.direction;
      if (!p || !d) return true;
      var now = [p.x, p.y, p.z, d.x, d.y, d.z, scene.mode];
      var last = w.cam;
      w.cam = now;
      if (!last) return true;
      for (var i = 0; i < 3; i++) if (Math.abs(now[i] - last[i]) > 0.5) return true;
      for (i = 3; i < 6; i++) if (Math.abs(now[i] - last[i]) > 1e-9) return true;
      return now[6] !== last[6];
    }

    function cameraHeight() {
      var cam = scene.camera;
      var carto = cam.positionCartographic;
      if (carto && finite(carto.height)) return carto.height;
      var p = cam.positionWC || cam.position;
      return p ? Math.sqrt(p.x * p.x + p.y * p.y + p.z * p.z) - 6371000 : 2e7;
    }

    // Screen position of (lon, lat) at the surface, or false when it is behind
    // the globe or off the drawing.
    function project(lon, lat, out) {
      var w = m.wind;
      var cart = C.Cartesian3.fromDegrees(lon, lat, 0, undefined, w.cart || undefined);
      w.cart = cart;
      if (!C.SceneMode || scene.mode === C.SceneMode.SCENE3D) {
        var cp = scene.camera.positionWC;
        if (cp && cart.x * cp.x + cart.y * cp.y + cart.z * cp.z <=
            cart.x * cart.x + cart.y * cart.y + cart.z * cart.z) {
          return false;
        }
      }
      var win = scene.cartesianToCanvasCoordinates(cart, w.win || undefined);
      if (!win || !finite(win.x) || !finite(win.y)) return false;
      w.win = win;
      out.x = win.x;
      out.y = win.y;
      return true;
    }

    // A new particle where the screen shows ground: a random pixel picked onto
    // the globe, which keeps the density even on screen at any zoom (as
    // earth.nullschool does); anywhere on the grid when no pick lands.
    function seed(p, ageMs) {
      var w = m.wind;
      var limit = w.field ? w.field.latLimit : 75;
      var placed = false;
      for (var tries = 0; tries < 3 && !placed && w.cssW && w.cssH; tries++) {
        try {
          if (!w.pick) w.pick = new C.Cartesian2(0, 0);
          w.pick.x = rand() * w.cssW;
          w.pick.y = rand() * w.cssH;
          var hit = scene.camera.pickEllipsoid(w.pick, scene.globe && scene.globe.ellipsoid);
          if (!hit) continue;
          var carto = C.Cartographic.fromCartesian(hit);
          if (!carto) continue;
          var lat = carto.latitude / DEG;
          if (!finite(lat) || Math.abs(lat) >= limit) continue;
          p.lon = normLon(carto.longitude / DEG);
          p.lat = lat;
          placed = true;
        } catch (e) { break; }
      }
      if (!placed) {
        var g = seedGlobal(rand, limit);
        p.lon = g.lon;
        p.lat = g.lat;
      }
      p.plon = p.lon;
      p.plat = p.lat;
      p.age = finite(ageMs) ? ageMs : 0;
      p.vis = false;
      return p;
    }

    function fitParticles() {
      var w = m.wind;
      while (w.particles.length > w.count) w.particles.pop();
      while (w.particles.length < w.count) {
        w.particles.push(seed({}, rand() * WIND_MAX_AGE_MS));
      }
    }

    // Back off when the frame work is slow, creep back up when it is quick.
    function govern(workMs) {
      var w = m.wind;
      w.ema = w.ema ? w.ema * 0.9 + workMs * 0.1 : workMs;
      if (w.cooldown > 0) { w.cooldown--; return; }
      if (w.ema > WIND_SLOW_FRAME_MS && w.count > WIND_MIN_PARTICLES) {
        w.count = Math.max(WIND_MIN_PARTICLES, Math.floor(w.count * 0.85));
        w.cooldown = 30;
      } else if (w.ema < WIND_FAST_FRAME_MS && w.count < w.target) {
        w.count = Math.min(w.target, Math.ceil(w.count * 1.1));
        w.cooldown = 30;
      }
    }

    function windFrame() {
      var w = m.wind;
      var field = w.field;
      if (!m.on.wind || !field || !w.canvas || !w.g) return 0;
      var t0 = nowMs();
      var frameMs = w.lastFrame ? clamp(t0 - w.lastFrame, 1, 100) : 16.7;
      w.lastFrame = t0;
      var resized = syncCanvasSize();
      if (!w.cssW || !w.cssH) return 0;
      var moved = cameraMoved() || resized;
      var g = w.g;
      if (moved) {
        g.clearRect(0, 0, w.cssW, w.cssH);
      } else {
        g.globalCompositeOperation = 'destination-in';
        g.fillStyle = 'rgba(0,0,0,' + fadeKeep(frameMs) + ')';
        g.fillRect(0, 0, w.cssW, w.cssH);
        g.globalCompositeOperation = 'source-over';
      }
      fitParticles();
      var sim = simSecondsPerFrame(cameraHeight(), frameMs);
      var buckets = w.buckets;
      for (var k = 0; k < buckets.length; k++) buckets[k].length = 0;
      var a = w.a, b = w.b;
      var drawn = 0;
      for (var i = 0; i < w.particles.length; i++) {
        var p = w.particles[i];
        if (p.age >= WIND_MAX_AGE_MS) { seed(p, 0); continue; }
        var speed = stepParticle(p, field, sim, w.scratch, field.latLimit);
        p.age += frameMs;
        if (!(speed >= 0)) { p.age = WIND_MAX_AGE_MS; p.vis = false; continue; }
        var prevOk;
        if (!moved && p.vis) {
          a.x = p.sx;
          a.y = p.sy;
          prevOk = true;
        } else {
          prevOk = project(p.plon, p.plat, a);
        }
        var curOk = project(p.lon, p.lat, b);
        p.vis = curOk;
        if (curOk) {
          p.sx = b.x;
          p.sy = b.y;
        }
        if (!prevOk || !curOk) continue;
        if (Math.abs(b.x - a.x) + Math.abs(b.y - a.y) > WIND_MAX_SEGMENT_PX) continue;
        buckets[bucketFor(speed)].push(a.x, a.y, b.x, b.y);
        drawn++;
      }
      for (k = 0; k < buckets.length; k++) {
        var seg = buckets[k];
        if (!seg.length) continue;
        g.beginPath();
        g.strokeStyle = w.colors[k];
        for (var j = 0; j < seg.length; j += 4) {
          g.moveTo(seg[j], seg[j + 1]);
          g.lineTo(seg[j + 2], seg[j + 3]);
        }
        g.stroke();
      }
      w.drawn = drawn;
      var before = w.count;
      govern(nowMs() - t0);
      if (w.count !== before || w.shownCount !== w.count) {
        w.shownCount = w.count;
        setCount('wind', w.count);
      }
      return drawn;
    }

    function windListen(on) {
      var w = m.wind;
      var ev = scene.postRender;
      if (!ev || typeof ev.addEventListener !== 'function') return;
      if (on && !w.listening) {
        ev.addEventListener(windFrame);
        w.listening = true;
      } else if (!on && w.listening) {
        ev.removeEventListener(windFrame);
        w.listening = false;
      }
    }

    function scheduleWind(ms) {
      var w = m.wind;
      cancel(w.timer);
      w.timer = m.on.wind ? later(loadWind, ms) : null;
    }

    function loadWind() {
      var w = m.wind;
      cancel(w.timer);
      w.timer = null;
      if (!m.on.wind || ctx.isStatic) return Promise.resolve(null);
      var seq = ++w.seq;
      if (!w.field) setStatus('wind', 'Loading the Open-Meteo wind grid...');
      return global.fetch(windUrl(api)).then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      }).then(function (data) {
        if (seq !== w.seq || !m.on.wind) return null;
        var field = cleanWindGrid(data);
        if (!field) throw new Error('bad grid');
        w.field = field;
        w.meta = {
          resolution: finite(data.resolution_deg) ? data.resolution_deg : field.dx,
          complete: data.complete !== false,
          filled: finite(data.filled) ? data.filled : field.nx * field.ny,
          total: finite(data.total) ? data.total : field.nx * field.ny,
          maxSpeed: field.maxSpeed,
          validTime: data.valid_time,
          stale: !!data.stale
        };
        m.failed.wind = false;
        if (windCanvas()) windListen(true);
        var line = windLine(w.meta);
        setStatus('wind', line);
        noteOnce('wind', line);
        scheduleWind(w.meta.complete ? WIND_REFRESH_MS : WIND_BUILD_POLL_MS);
        return field;
      }).catch(function () {
        if (seq !== w.seq || !m.on.wind) return null;
        setStatus('wind', w.field
          ? 'The wind grid did not refresh; showing the last one.'
          : 'The server is still building the wind grid; asking again in a minute.');
        if (!m.failed.wind) {
          m.failed.wind = true;
          note('Wind: Open-Meteo grid not available yet');
        }
        scheduleWind(WIND_BUILD_POLL_MS);
        return null;
      });
    }

    function windOn() {
      m.on.wind = true;
      m.failed.wind = false;
      m.lastLine.wind = '';
      loadWind();
    }

    function windOff() {
      var w = m.wind;
      m.on.wind = false;
      w.seq++;
      cancel(w.timer);
      w.timer = null;
      windListen(false);
      if (w.canvas) {
        try { w.g.clearRect(0, 0, w.cssW, w.cssH); } catch (e) { /* keep going */ }
        if (w.canvas.parentNode) w.canvas.parentNode.removeChild(w.canvas);
      }
      w.canvas = null;
      w.g = null;
      w.field = null;
      w.meta = null;
      w.particles = [];
      w.count = 0;
      w.shownCount = 0;
      w.cam = null;
      w.lastFrame = 0;
      setStatus('wind', '');
      setCount('wind', 0);
    }

    // -- cyclones ---------------------------------------------------------

    function cycRoot() {
      var c = m.cyc;
      if (!c.root) c.root = scene.primitives.add(new C.PrimitiveCollection());
      return c.root;
    }

    function cssColor(level) { return INTENSITY_COLORS[level] || INTENSITY_COLORS.UNK; }

    function flat(points) {
      var out = [];
      points.forEach(function (p) { out.push(p[0], p[1]); });
      return out;
    }

    function drawStorms(storms) {
      var root = cycRoot();
      root.removeAll();
      var pts = root.add(new C.PointPrimitiveCollection());
      var labels = root.add(new C.LabelCollection());
      var lines = root.add(new C.PolylineCollection());
      var black = C.Color.BLACK;
      storms.forEach(function (s) {
        var color = C.Color.fromCssColorString(cssColor(s.intensity));
        var pickId = { type: 'cyclone', stormId: s.id,
                       sdaPick: function () { selectStorm(s.id, true); } };
        s.cone.forEach(function (ring) {
          var positions = C.Cartesian3.fromDegreesArray(flat(ring));
          try {
            root.add(new C.Primitive({
              geometryInstances: new C.GeometryInstance({
                geometry: new C.PolygonGeometry({
                  polygonHierarchy: new C.PolygonHierarchy(positions),
                  height: CONE_HEIGHT_M
                }),
                attributes: {
                  color: C.ColorGeometryInstanceAttribute.fromColor(color.withAlpha(0.16))
                },
                id: pickId
              }),
              appearance: new C.PerInstanceColorAppearance({ translucent: true, flat: true }),
              asynchronous: false
            }));
          } catch (e) { /* the outline below still marks the cone */ }
          lines.add({
            positions: positions, width: 1.5, id: pickId,
            material: C.Material.fromType('Color', { color: color.withAlpha(0.75) })
          });
        });
        s.track.forEach(function (line) {
          lines.add({
            positions: C.Cartesian3.fromDegreesArray(flat(line)), width: 2, id: pickId,
            material: C.Material.fromType('Color', { color: C.Color.WHITE.withAlpha(0.85) })
          });
        });
        s.forecast_points.forEach(function (fp) {
          pts.add({
            position: C.Cartesian3.fromDegrees(fp.lon, fp.lat, 0),
            pixelSize: 6,
            color: C.Color.fromCssColorString(cssColor(intensityFor(fp.wind_kt))),
            outlineColor: black, outlineWidth: 1, id: pickId
          });
        });
        var at = C.Cartesian3.fromDegrees(s.lon, s.lat, 0);
        pts.add({ position: at, pixelSize: 14, color: color, outlineColor: black,
                  outlineWidth: 2, id: pickId });
        labels.add({
          position: at, text: stormLabel(s), font: '11px monospace', fillColor: color,
          outlineColor: black, outlineWidth: 3,
          style: C.LabelStyle ? C.LabelStyle.FILL_AND_OUTLINE : undefined,
          verticalOrigin: C.VerticalOrigin ? C.VerticalOrigin.BOTTOM : undefined,
          pixelOffset: new C.Cartesian2(0, -12), id: pickId
        });
      });
      root.show = true;
    }

    function clearStorms() {
      if (m.cyc.root) {
        m.cyc.root.removeAll();
        m.cyc.root.show = false;
      }
    }

    function stormById(id) {
      for (var i = 0; i < m.cyc.storms.length; i++) {
        if (m.cyc.storms[i].id === id) return m.cyc.storms[i];
      }
      return null;
    }

    function cardRow(card, label, value) {
      if (value === null || value === undefined || value === '') return;
      var row = el('div', 'weather-card-row');
      row.appendChild(el('span', 'weather-card-label', label + ' '));
      row.appendChild(el('span', 'weather-card-value', value));
      card.appendChild(row);
    }

    function showCard(s) {
      var card = m.ui.stormCard;
      if (!card) return;
      card.textContent = '';
      if (!s) return;
      card.style.marginTop = '4px';
      card.style.fontSize = '9px';
      card.appendChild(el('div', 'weather-card-title', s.name));
      cardRow(card, 'Class', s.category);
      if (finite(s.wind_kt)) {
        cardRow(card, 'Wind', Math.round(s.wind_kt) + ' kt (' +
          Math.round(s.wind_kt * 1.852) + ' km/h)' + (s.wind_basis ? ', ' + s.wind_basis : ''));
      }
      if (finite(s.pressure_hpa)) cardRow(card, 'Pressure', Math.round(s.pressure_hpa) + ' hPa');
      if (finite(s.movement_speed_kt)) {
        cardRow(card, 'Moving', (finite(s.movement_dir_deg) ? Math.round(s.movement_dir_deg) +
          ' deg at ' : '') + Math.round(s.movement_speed_kt) + ' kt');
      }
      cardRow(card, 'Position', round(s.lat, 1) + ', ' + round(s.lon, 1));
      cardRow(card, 'Advisory', s.advisory);
      cardRow(card, 'Updated', s.updated_at || s.issued_at);
      cardRow(card, 'Source', s.source + (s.agency && s.agency !== s.source ? ' (' + s.agency +
        ')' : '') + (s.alert_level ? ', ' + s.alert_level + ' alert' : ''));
      if (s.cone.length || s.track.length) {
        cardRow(card, 'Forecast', 'track and cone' +
          (s.geometry_status === 'older advisory' ? ' from an earlier advisory' : ''));
      }
      if (s.url) {
        var a = el('a', 'weather-card-link', 'Official advisory');
        a.setAttribute('href', s.url);
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        card.appendChild(a);
      }
    }

    function selectStorm(id, fly) {
      var s = stormById(id);
      m.cyc.selected = s ? s.id : null;
      showCard(s);
      if (!s) return null;
      note(stormLine(s));
      if (fly) {
        try {
          viewer.camera.flyTo({
            destination: C.Cartesian3.fromDegrees(s.lon, s.lat, CYCLONE_FLY_HEIGHT_M),
            duration: 1.6
          });
        } catch (e) { /* keep going */ }
      }
      return s;
    }

    function listStorms(storms) {
      var list = m.ui.stormList;
      if (!list) return;
      list.textContent = '';
      storms.forEach(function (s) {
        var row = el('div', 'weather-storm');
        row.style.cursor = 'pointer';
        row.style.fontSize = '10px';
        row.style.padding = '2px 0';
        var sw = el('span', 'weather-storm-swatch');
        sw.style.display = 'inline-block';
        sw.style.width = '8px';
        sw.style.height = '8px';
        sw.style.marginRight = '4px';
        sw.style.borderRadius = '50%';
        sw.style.background = cssColor(s.intensity);
        row.appendChild(sw);
        row.appendChild(el('span', 'weather-storm-name', stormLine(s)));
        row.addEventListener('click', function () { selectStorm(s.id, true); });
        list.appendChild(row);
      });
    }

    function loadCyclones() {
      var c = m.cyc;
      if (!m.on.cyclones || ctx.isStatic) return Promise.resolve(null);
      var seq = ++c.seq;
      if (!c.data) setStatus('cyclones', 'Loading tropical cyclones...');
      return global.fetch(cyclonesUrl(api)).then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
      }).then(function (data) {
        if (seq !== c.seq || !m.on.cyclones) return null;
        var storms = cleanStorms(data);
        c.storms = storms;
        c.data = data || {};
        drawStorms(storms);
        listStorms(storms);
        if (c.selected) showCard(stormById(c.selected));
        setCount('cyclones', storms.length);
        setCount('weather', storms.length);
        m.failed.cyclones = false;
        var line = cycloneLine(storms, data);
        setStatus('cyclones', line);
        noteOnce('cyclones', line);
        return storms.length;
      }).catch(function () {
        if (seq !== c.seq || !m.on.cyclones) return null;
        setStatus('cyclones', 'The cyclone feeds did not load; retrying in fifteen minutes.');
        if (!m.failed.cyclones) {
          m.failed.cyclones = true;
          note('Cyclones: NHC and GDACS feeds unavailable');
        }
        return null;
      });
    }

    function cyclonesOn() {
      m.on.cyclones = true;
      m.failed.cyclones = false;
      m.lastLine.cyclones = '';
      loadCyclones();
      m.cyc.timer = global.setInterval(loadCyclones, CYCLONE_REFRESH_MS);
    }

    function cyclonesOff() {
      var c = m.cyc;
      m.on.cyclones = false;
      c.seq++;
      if (c.timer) global.clearInterval(c.timer);
      c.timer = null;
      clearStorms();
      c.storms = [];
      c.data = null;
      c.selected = null;
      if (m.ui.stormList) m.ui.stormList.textContent = '';
      showCard(null);
      setStatus('cyclones', '');
      setCount('cyclones', 0);
      setCount('weather', 0);
    }

    // -- switches ---------------------------------------------------------

    var STARTERS = { radar: radarOn, wind: windOn, cyclones: cyclonesOn };
    var STOPPERS = { radar: radarOff, wind: windOff, cyclones: cyclonesOff };
    var NEEDS_SERVER = {
      wind: 'Wind needs the live server (Open-Meteo grid).',
      cyclones: 'Tropical cyclones need the live server (NHC, GDACS).'
    };

    function apply(name, on) {
      if (on === m.on[name]) return;
      if (on && ctx.isStatic && NEEDS_SERVER[name]) {
        setStatus(name, NEEDS_SERVER[name]);
        if (!m.staticNoted[name]) {
          m.staticNoted[name] = true;
          note(NEEDS_SERVER[name]);
        }
        return;
      }
      (on ? STARTERS : STOPPERS)[name]();
    }

    function setEnabled(on) {
      m.enabled = !!on;
      LAYERS.forEach(function (name) { apply(name, m.enabled && m.want[name]); });
      syncBoxes();
      syncStatus();
    }

    function setLayer(name, on) {
      if (LAYERS.indexOf(name) < 0) return;
      m.want[name] = !!on;
      if (on) m.enabled = true;
      apply(name, m.enabled && m.want[name]);
      syncBoxes();
      syncStatus();
    }

    function refresh() {
      return Promise.all([
        m.on.radar ? loadRadarIndex() : null,
        m.on.wind ? loadWind() : null,
        m.on.cyclones ? loadCyclones() : null
      ]);
    }

    function state() {
      var r = m.radar;
      var shown = Object.keys(r.layers).filter(function (k) { return r.layers[k].show; });
      return {
        enabled: m.enabled,
        want: { radar: m.want.radar, wind: m.want.wind, cyclones: m.want.cyclones },
        on: { radar: m.on.radar, wind: m.on.wind, cyclones: m.on.cyclones },
        radar: { frames: r.frames.length, index: r.index, playing: r.playing,
                 layers: Object.keys(r.layers).length, shown: shown.length,
                 opacity: r.opacity },
        wind: { field: !!m.wind.field, particles: m.wind.particles.length,
                count: m.wind.count, drawn: m.wind.drawn, listening: m.wind.listening,
                canvas: !!m.wind.canvas, meta: m.wind.meta },
        cyclones: { storms: m.cyc.storms.length, selected: m.cyc.selected }
      };
    }

    buildPanel();
    m.setEnabled = setEnabled;
    m.setLayer = setLayer;
    m.playRadar = playRadar;
    m.showFrame = showFrame;
    m.setRadarOpacity = setRadarOpacity;
    m.selectStorm = selectStorm;
    m.refresh = refresh;
    m.state = state;
    m.windFrame = windFrame;
    m.radarTick = radarTick;
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.viewer || !ctx.viewer.scene || !global.Cesium) return false;
      if (current) current.setEnabled(false);
      current = createModule(ctx);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAWeather = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    setLayer: function (name, on) { if (current) current.setLayer(name, on); },
    isEnabled: function () { return !!(current && current.enabled); },
    isLayerOn: function (name) { return !!(current && current.on[name]); },
    playRadar: function (on) { if (current) current.playRadar(on); },
    setRadarFrame: function (i) { if (current) current.showFrame(i); },
    setRadarOpacity: function (v) { if (current) current.setRadarOpacity(v); },
    selectStorm: function (id) { return current ? current.selectStorm(id, true) : null; },
    refresh: function () { return current ? current.refresh() : Promise.resolve(null); },
    state: function () { return current ? current.state() : null; },
    // test and debugging hooks: one wind frame, one radar tick
    _windFrame: function () { return current ? current.windFrame() : 0; },
    _radarTick: function () { if (current) current.radarTick(); },
    LAYERS: LAYERS.slice(),
    RAINVIEWER_INDEX_URL: RAINVIEWER_INDEX_URL,
    RADAR_TILE_PX: RADAR_TILE_PX,
    RADAR_MAX_LEVEL: RADAR_MAX_LEVEL,
    RADAR_COLOR: RADAR_COLOR,
    RADAR_BUDGET: RADAR_BUDGET,
    RADAR_INDEX_REFRESH_MS: RADAR_INDEX_REFRESH_MS,
    RADAR_FRAME_MS: RADAR_FRAME_MS,
    RADAR_HOLD_MS: RADAR_HOLD_MS,
    WIND_REFRESH_MS: WIND_REFRESH_MS,
    WIND_BUILD_POLL_MS: WIND_BUILD_POLL_MS,
    WIND_MAX_SPEED: WIND_MAX_SPEED,
    WIND_BUCKETS: WIND_BUCKETS,
    WIND_MAX_AGE_MS: WIND_MAX_AGE_MS,
    WIND_MIN_PARTICLES: WIND_MIN_PARTICLES,
    WIND_MAX_PARTICLES: WIND_MAX_PARTICLES,
    CYCLONE_REFRESH_MS: CYCLONE_REFRESH_MS,
    INTENSITY_COLORS: INTENSITY_COLORS,
    clampPct: clampPct,
    normLon: normLon,
    parseRainviewer: parseRainviewer,
    radarTileUrl: radarTileUrl,
    frameLabel: frameLabel,
    makeBudget: makeBudget,
    cleanWindGrid: cleanWindGrid,
    sampleWind: sampleWind,
    stepParticle: stepParticle,
    windRgb: windRgb,
    windColor: windColor,
    bucketFor: bucketFor,
    bucketColors: bucketColors,
    legendGradient: legendGradient,
    fadeKeep: fadeKeep,
    simSecondsPerFrame: simSecondsPerFrame,
    particleCount: particleCount,
    seedGlobal: seedGlobal,
    intensityFor: intensityFor,
    cleanStorms: cleanStorms,
    stormLine: stormLine,
    stormLabel: stormLabel,
    windLine: windLine,
    cycloneLine: cycloneLine,
    windUrl: windUrl,
    cyclonesUrl: cyclonesUrl,
    shortTime: shortTime
  };
})(typeof window !== 'undefined' ? window : this);
