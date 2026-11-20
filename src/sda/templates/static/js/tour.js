// Cinematic tour of live hotspots on the Vantage globe: the ISS, the strongest
// tropical cyclone, the largest earthquake of the last day, the next (or just
// flown) orbital launch, and the busiest shipping chokepoint right now. Each
// stop flies in, circles slowly under a caption that names the source, then
// moves on; the tour loops with fresh data until any key, click, touch or
// wheel stops it.
//
// Data: the page's own endpoints (/api/weather/cyclones, /api/earthquakes,
// /api/launches, /api/maritime/chokepoints) and the satellite engine for the
// ISS. The static demo has no backend, so there the ISS comes from the engine,
// the earthquake from the USGS feed directly (keyless, CORS open), and the
// other stops are skipped. A stop with no live data is skipped, never filled
// with a stand-in.
//
// Camera: the ISS stop uses the page's own chase mode (setCameraMode); ground
// stops use flyToBoundingSphere and a slow lookAt orbit, released with
// lookAtTransform(IDENTITY) before the next flight and when the tour stops.
// While the tour runs, body carries 'sda-touring' (static/js/presentation.js
// hides the side panels on it).
// Text is only ever set with textContent.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Tour';
  var STYLE_ID = 'sda-tour-style';
  var CAPTION_ID = 'sda-tour-caption';
  var ISS_ID = 25544;
  var USGS_DAY = 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson';
  var DAY_MS = 86400000;
  var timing = { flyS: 4, dwellMs: 11000, graceMs: 400, fetchMs: 8000, emptyMs: 4000 };
  var ORBIT_DEG_S = 2.5;
  var STOPS = ['iss', 'storm', 'quake', 'launch', 'chokepoint'];
  var VIEW = {
    storm: { range: 1800000, pitch: -55 },
    quake: { range: 600000, pitch: -45 },
    launch: { range: 60000, pitch: -30 },
    chokepoint: { range: 260000, pitch: -40 }
  };

  var clockFn = function () { return Date.now(); };

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  // ---------------------------------------------------------------------
  // Pure: choose each stop from its feed
  // ---------------------------------------------------------------------

  function formatSpan(ms) {
    var min = Math.max(0, Math.round(Math.abs(ms) / 60000));
    if (min < 60) return min + ' min';
    var h = Math.floor(min / 60);
    if (h < 48) return h + ' h ' + (min % 60) + ' min';
    return Math.round(h / 24) + ' days';
  }

  // ISO strings, epoch ms, and the '2026-11-19 12:34 UTC' form /api/earthquakes
  // returns (Date.parse reads that one in Chromium only).
  function parseTime(v) {
    if (finite(v)) return v;
    if (typeof v !== 'string') return NaN;
    var m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?\s*UTC$/.exec(v.trim());
    if (m) return Date.parse(m[1] + 'T' + m[2] + (m[3] || ':00') + 'Z');
    return Date.parse(v);
  }

  function validLonLat(lon, lat) {
    return finite(lon) && finite(lat) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
  }

  function pickStorm(storms) {
    var best = null;
    (Array.isArray(storms) ? storms : []).forEach(function (s) {
      if (!s || !validLonLat(s.lon, s.lat) || !finite(s.wind_kt)) return;
      if (!best || s.wind_kt > best.wind_kt) best = s;
    });
    if (!best) return null;
    var parts = [];
    if (best.category) parts.push(String(best.category));
    parts.push(Math.round(best.wind_kt) + ' kt ' +
      (best.wind_basis === 'event peak' ? 'peak winds' : 'winds'));
    if (finite(best.pressure_hpa)) parts.push(Math.round(best.pressure_hpa) + ' hPa');
    if (finite(best.movement_speed_kt) && finite(best.movement_dir_deg)) {
      parts.push('moving ' + Math.round(best.movement_dir_deg) + ' deg at ' +
        Math.round(best.movement_speed_kt) + ' kt');
    }
    return { kind: 'storm', lon: best.lon, lat: best.lat,
      kicker: 'STRONGEST TROPICAL CYCLONE',
      title: String(best.name || 'Unnamed storm'),
      line: parts.join(', '),
      source: 'Source: ' + (best.agency || best.source || 'NOAA NHC / JTWC') };
  }

  function pickQuake(quakes, nowMs) {
    var best = null;
    (Array.isArray(quakes) ? quakes : []).forEach(function (q) {
      if (!q || !validLonLat(q.lon, q.lat) || !finite(q.magnitude)) return;
      var t = parseTime(q.time);
      if (!(t > nowMs - DAY_MS) || t > nowMs + 60000) return;
      if (!best || q.magnitude > best.magnitude) best = { q: q, t: t, magnitude: q.magnitude };
    });
    if (!best) return null;
    var q = best.q;
    var line = (q.place ? q.place + ', ' : '') + formatSpan(nowMs - best.t) + ' ago';
    if (finite(q.depth_km)) line += ', ' + Math.round(q.depth_km) + ' km deep';
    return { kind: 'quake', lon: q.lon, lat: q.lat, kicker: 'LARGEST EARTHQUAKE, LAST 24 H',
      title: 'M' + q.magnitude.toFixed(1) + ' earthquake', line: line, source: 'Source: USGS' };
  }

  // The USGS GeoJSON feed into the rows /api/earthquakes returns.
  function usgsRows(geo) {
    var out = [];
    ((geo && geo.features) || []).forEach(function (f) {
      var p = (f && f.properties) || {};
      var c = (f && f.geometry && f.geometry.coordinates) || [];
      if (!finite(p.mag) || !finite(p.time)) return;
      out.push({ magnitude: p.mag, place: p.place || '', time: new Date(p.time).toISOString(),
        lon: c[0], lat: c[1], depth_km: c[2] });
    });
    return out;
  }

  // The launch nearest to now: the next one, or one that left in the last six
  // hours if that is closer.
  function pickLaunch(launches, nowMs) {
    var best = null;
    (Array.isArray(launches) ? launches : []).forEach(function (l) {
      if (!l || !validLonLat(l.lon, l.lat)) return;
      var t = parseTime(l.net);
      if (!finite(t) || t < nowMs - 6 * 3600000) return;
      if (!best || Math.abs(t - nowMs) < Math.abs(best.t - nowMs)) best = { l: l, t: t };
    });
    if (!best) return null;
    var l = best.l;
    var when = best.t >= nowMs ? 'T-' + formatSpan(best.t - nowMs)
      : 'launched ' + formatSpan(nowMs - best.t) + ' ago';
    var where = [l.provider, l.pad || l.site].filter(Boolean).join(', ');
    return { kind: 'launch', lon: l.lon, lat: l.lat,
      kicker: best.t >= nowMs ? 'NEXT ORBITAL LAUNCH' : 'LATEST ORBITAL LAUNCH',
      title: String(l.name || 'Launch'), line: (where ? where + ', ' : '') + when +
        (l.status ? ' (' + l.status + ')' : ''),
      source: 'Source: Launch Library 2 (The Space Devs)' };
  }

  function pickChokepoint(list) {
    var best = null;
    (Array.isArray(list) ? list : []).forEach(function (c) {
      if (!c || !Array.isArray(c.center) || !finite(c.inside_now) || c.inside_now <= 0) return;
      if (!validLonLat(c.center[0], c.center[1])) return;
      if (!best || c.inside_now > best.inside_now) best = c;
    });
    if (!best) return null;
    var line = best.inside_now + ' vessels inside now';
    if (finite(best.transits_today) && best.transits_today > 0) {
      line += ', ' + best.transits_today + ' transits today';
    }
    return { kind: 'chokepoint', lon: best.center[0], lat: best.center[1],
      kicker: 'BUSIEST CHOKEPOINT NOW', title: String(best.name || best.id), line: line,
      source: 'Source: AIS position reports (aisstream.io)' };
  }

  function issStop(rec, altKm) {
    if (!rec) return null;
    var parts = [];
    if (finite(altKm) && altKm > 0) parts.push(Math.round(altKm) + ' km up');
    if (finite(rec.lastVel) && rec.lastVel > 0) parts.push(rec.lastVel.toFixed(2) + ' km/s');
    return { kind: 'iss', satId: ISS_ID, kicker: 'CREWED SPACECRAFT',
      title: 'International Space Station', line: parts.join(', ') || 'In orbit now',
      source: 'Position: SGP4 from CelesTrak elements' };
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var CSS = [
    '#' + CAPTION_ID + ' { position: fixed; left: 50%; bottom: 120px; transform: translateX(-50%);',
    '  z-index: 40; min-width: 280px; max-width: min(640px, calc(100vw - 24px)); padding: 12px 18px;',
    '  background: linear-gradient(90deg, rgba(0,0,0,0.82), rgba(0,0,0,0.6)); border-left: 3px solid var(--hud-color, #ffb000);',
    '  color: #f2e6cc; font: 12px "JetBrains Mono", monospace; letter-spacing: 1px; pointer-events: none;',
    '  opacity: 0; transition: opacity 0.6s ease; }',
    '#' + CAPTION_ID + '.on { opacity: 1; }',
    '#' + CAPTION_ID + ' .sdatour-kicker { font-size: 9px; letter-spacing: 3px; color: var(--hud-color, #ffb000); }',
    '#' + CAPTION_ID + ' .sdatour-title { font-size: 18px; font-weight: 600; margin: 4px 0; color: #fff; }',
    '#' + CAPTION_ID + ' .sdatour-line { font-size: 12px; }',
    '#' + CAPTION_ID + ' .sdatour-source { font-size: 9px; color: #a89a7c; margin-top: 6px; }',
    '#' + CAPTION_ID + ' .sdatour-hint { font-size: 9px; color: #a89a7c; margin-top: 2px; }',
    '.sdatour-row { display: flex; gap: 6px; align-items: center; margin: 4px 0; }',
    '.sdatour-row button { background: rgba(255,176,0,0.08); color: inherit; border: 1px solid rgba(255,176,0,0.35); font: inherit; font-size: 10px; padding: 3px 8px; cursor: pointer; }',
    '.sdatour-status { font-size: 9px; color: var(--hud-dim, #7a5500); margin-top: 6px; }'
  ].join('\n');

  function el(doc, tag, className, text) {
    var n = doc.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = { running: false, stops: [], index: 0, cycle: 0, timer: null, orbit: null,
      runId: 0, startedAt: 0, rode: false, lastStopped: '' };

    // ---- data -------------------------------------------------------------

    function getJson(url) {
      if (typeof global.fetch !== 'function') return Promise.resolve(null);
      var ctl = typeof global.AbortController === 'function' ? new global.AbortController() : null;
      var t = ctl ? global.setTimeout(function () { ctl.abort(); }, timing.fetchMs) : null;
      return global.fetch(url, ctl ? { signal: ctl.signal } : undefined).then(function (r) {
        return r && r.ok ? r.json() : null;
      }).then(function (j) { if (t) global.clearTimeout(t); return j; }, function () {
        if (t) global.clearTimeout(t);
        return null;
      });
    }

    function issFromEngine() {
      var eng = ctx.satEngine;
      var s = eng && eng.byNoradId ? eng.byNoradId[ISS_ID] : null;
      if (!s || !s.point || s.point.show === false || !s.point.position) return null;
      var alt = finite(s.lastAlt) && s.lastAlt > 0 ? s.lastAlt : null;
      if (alt === null && C.Cartographic) {
        try { alt = C.Cartographic.fromCartesian(s.point.position).height / 1000; } catch (e) { alt = null; }
      }
      return issStop(s, alt);
    }

    function gather() {
      var now = clockFn();
      var jobs = {};
      if (ctx.isStatic) {
        jobs.quake = getJson(USGS_DAY).then(function (g) { return pickQuake(usgsRows(g), now); });
      } else {
        jobs.storm = getJson(api + '/api/weather/cyclones').then(function (d) {
          return pickStorm(d && d.storms);
        });
        jobs.quake = getJson(api + '/api/earthquakes').then(function (d) {
          return pickQuake(d && d.earthquakes, now);
        });
        jobs.launch = getJson(api + '/api/launches').then(function (d) {
          return pickLaunch(d && d.launches, now);
        });
        jobs.chokepoint = getJson(api + '/api/maritime/chokepoints').then(function (d) {
          return pickChokepoint(d && d.chokepoints);
        });
      }
      var keys = Object.keys(jobs);
      return Promise.all(keys.map(function (k) {
        return jobs[k].then(null, function () { return null; });
      })).then(function (vals) {
        var got = { iss: issFromEngine() };
        keys.forEach(function (k, i) { got[k] = vals[i]; });
        return STOPS.map(function (k) { return got[k]; }).filter(Boolean);
      });
    }

    // ---- caption ------------------------------------------------------------

    function caption() {
      var cap = doc.getElementById(CAPTION_ID);
      if (cap) return cap;
      cap = el(doc, 'div', '');
      cap.id = CAPTION_ID;
      cap.setAttribute('role', 'status');
      cap.setAttribute('aria-live', 'polite');
      m.capKicker = el(doc, 'div', 'sdatour-kicker', '');
      m.capTitle = el(doc, 'div', 'sdatour-title', '');
      m.capLine = el(doc, 'div', 'sdatour-line', '');
      m.capSource = el(doc, 'div', 'sdatour-source', '');
      m.capHint = el(doc, 'div', 'sdatour-hint', 'Press any key or click to stop the tour');
      [m.capKicker, m.capTitle, m.capLine, m.capSource, m.capHint].forEach(function (n) {
        cap.appendChild(n);
      });
      doc.body.appendChild(cap);
      return cap;
    }

    function showCaption(kicker, title, line, source) {
      var cap = caption();
      m.capKicker.textContent = kicker || '';
      m.capTitle.textContent = title || '';
      m.capLine.textContent = line || '';
      m.capSource.textContent = source || '';
      cap.classList.add('on');
    }

    function hideCaption() {
      var cap = doc.getElementById(CAPTION_ID);
      if (cap) cap.classList.remove('on');
    }

    // ---- camera ---------------------------------------------------------------

    function releaseCamera() {
      m.orbit = null;
      var cam = viewer.camera;
      if (cam && typeof cam.lookAtTransform === 'function' && C.Matrix4 && C.Matrix4.IDENTITY) {
        try { cam.lookAtTransform(C.Matrix4.IDENTITY); } catch (e) { /* free already */ }
      }
      if (m.rode && typeof global.setCameraMode === 'function') {
        try { global.setCameraMode('free'); } catch (e) { /* page keeps its mode */ }
      }
      m.rode = false;
    }

    function orbitTick() {
      var o = m.orbit;
      if (!o || !m.running) return;
      var now = clockFn();
      var dt = Math.min(0.25, Math.max(0, (now - o.last) / 1000));
      o.last = now;
      o.heading += C.Math.toRadians(ORBIT_DEG_S) * dt;
      try {
        viewer.camera.lookAt(o.center, new C.HeadingPitchRange(o.heading, o.pitch, o.range));
      } catch (e) { m.orbit = null; }
    }

    function flyToStop(stop, runId, done) {
      var cam = viewer.camera;
      if (stop.kind === 'iss') {
        var ok = false;
        if (typeof ctx.selectSatellite === 'function' && typeof global.setCameraMode === 'function') {
          try {
            ctx.selectSatellite(stop.satId);
            global.setCameraMode('chase');
            ok = !global.camState || global.camState.mode === 'chase';
          } catch (e) { ok = false; }
        }
        m.rode = ok;
        if (!ok) return false;
        done();
        return true;
      }
      var v = VIEW[stop.kind];
      var center = C.Cartesian3.fromDegrees(stop.lon, stop.lat, 0);
      var finished = false;
      var heading = C.Math.toRadians((m.cycle * 47 + m.index * 73) % 360);
      var pitch = C.Math.toRadians(v.pitch);
      try {
        cam.flyToBoundingSphere(new C.BoundingSphere(center, 1), {
          offset: new C.HeadingPitchRange(heading, pitch, v.range),
          duration: timing.flyS,
          complete: function () {
            if (!m.running || runId !== m.runId || finished) return;
            finished = true;
            m.orbit = { center: center, heading: heading, pitch: pitch, range: v.range,
              last: clockFn() };
            done();
          },
          // stop() cancels with a new runId; any other cancel (the page moved the
          // camera) still moves the tour on so it never stalls.
          cancel: function () {
            if (!m.running || runId !== m.runId || finished) return;
            finished = true;
            done();
          }
        });
        return true;
      } catch (e) {
        return false;
      }
    }

    // ---- run ---------------------------------------------------------------

    function schedule(fn, ms) {
      if (m.timer) global.clearTimeout(m.timer);
      m.timerFn = fn;
      m.timer = global.setTimeout(function () { m.timer = null; m.timerFn = null; fn(); }, ms);
    }

    // Tests: run the pending dwell or skip timer now.
    function fire() {
      var fn = m.timerFn;
      if (!m.timer || !fn) return false;
      global.clearTimeout(m.timer);
      m.timer = null;
      m.timerFn = null;
      fn();
      return true;
    }

    function play() {
      if (!m.running) return;
      if (m.index >= m.stops.length) { nextCycle(); return; }
      var stop = m.stops[m.index];
      var runId = m.runId;
      releaseCamera();
      showCaption(stop.kicker + '  |  ' + (m.index + 1) + ' OF ' + m.stops.length, stop.title,
        stop.line, stop.source);
      setStatus('Stop ' + (m.index + 1) + ' of ' + m.stops.length + ': ' + stop.title);
      var flew = flyToStop(stop, runId, function () {
        schedule(function () {
          if (runId !== m.runId) return;
          m.index += 1;
          play();
        }, timing.dwellMs);
      });
      if (!flew) {
        m.index += 1;
        schedule(play, 0);
      }
    }

    function nextCycle() {
      var runId = m.runId;
      m.cycle += 1;
      gather().then(function (stops) {
        if (!m.running || runId !== m.runId) return;
        m.stops = stops;
        m.index = 0;
        if (!stops.length) { stop('nothing live'); return; }
        play();
      });
    }

    function onInput(evt) {
      if (!m.running) return;
      // The panel's own button toggles the tour; letting its pointerdown stop the
      // tour first would make the click that follows start it again.
      if (evt && evt.target && m.startBtn && m.startBtn.contains &&
          m.startBtn.contains(evt.target)) return;
      if (clockFn() - m.startedAt < timing.graceMs) return;
      // The key that ends the tour does nothing else: the page's hotkeys listen
      // on document in the bubble phase, after this capture listener.
      if (evt && evt.type === 'keydown') {
        if (evt.preventDefault) evt.preventDefault();
        if (evt.stopPropagation) evt.stopPropagation();
      }
      stop('input');
    }

    var INPUTS = ['keydown', 'pointerdown', 'mousedown', 'wheel', 'touchstart'];

    function start() {
      if (m.running) return false;
      m.running = true;
      m.runId += 1;
      m.cycle = 0;
      m.index = 0;
      m.startedAt = clockFn();
      var runId = m.runId;
      if (global.camState && global.camState.mode !== 'free' &&
          typeof global.setCameraMode === 'function') {
        try { global.setCameraMode('free'); } catch (e) { /* page keeps its mode */ }
      }
      doc.body.classList.add('sda-touring');
      INPUTS.forEach(function (t) { doc.addEventListener(t, onInput, true); });
      if (m.startBtn) m.startBtn.textContent = 'STOP TOUR';
      showCaption('LIVE TOUR', 'Gathering live hotspots', 'ISS, storms, earthquakes, launches, shipping', '');
      setStatus('Gathering live hotspots...');
      gather().then(function (stops) {
        if (!m.running || runId !== m.runId) return;
        m.stops = stops;
        if (!stops.length) {
          showCaption('LIVE TOUR', 'Nothing live to show right now', 'No hotspot feed answered', '');
          schedule(function () { if (runId === m.runId) stop('nothing live'); }, timing.emptyMs);
          return;
        }
        play();
      });
      return true;
    }

    function stop(reason) {
      if (!m.running) return false;
      m.running = false;
      m.runId += 1;
      m.lastStopped = reason || 'stopped';
      if (m.timer) { global.clearTimeout(m.timer); m.timer = null; m.timerFn = null; }
      INPUTS.forEach(function (t) { doc.removeEventListener(t, onInput, true); });
      try { if (viewer.camera.cancelFlight) viewer.camera.cancelFlight(); } catch (e) { /* none */ }
      releaseCamera();
      hideCaption();
      doc.body.classList.remove('sda-touring');
      if (m.startBtn) m.startBtn.textContent = 'START TOUR';
      setStatus(reason === 'nothing live' ? 'No live hotspot answered; try again later.'
        : 'Stopped. ' + intro());
      return true;
    }

    // ---- panel ---------------------------------------------------------------

    function intro() {
      return ctx.isStatic
        ? 'Demo build: flies to the ISS and the largest earthquake of the day.'
        : 'Flies to the ISS, the strongest storm, the largest quake today, the next launch ' +
          'and the busiest chokepoint. Any key or click stops it.';
    }

    function setStatus(text) { if (m.statusEl) m.statusEl.textContent = text; }

    function buildPanel() {
      var root = el(doc, 'div', 'panel sdatour');
      var h = el(doc, 'h3', '', PANEL_TITLE);
      root.appendChild(h);
      var body = el(doc, 'div', 'panel-body');
      root.appendChild(body);
      var row = el(doc, 'div', 'sdatour-row');
      var btn = el(doc, 'button', '', 'START TOUR');
      btn.type = 'button';
      btn.addEventListener('click', function (evt) {
        if (evt && evt.stopPropagation) evt.stopPropagation();
        if (m.running) stop('button'); else start();
      });
      row.appendChild(btn);
      body.appendChild(row);
      m.startBtn = btn;
      m.statusEl = el(doc, 'div', 'sdatour-status', intro());
      body.appendChild(m.statusEl);
      h.addEventListener('click', function () { root.classList.toggle('collapsed'); });
      return root;
    }

    if (!doc.getElementById(STYLE_ID)) {
      var s = doc.createElement('style');
      s.id = STYLE_ID;
      s.textContent = CSS;
      (doc.head || doc.body).appendChild(s);
    }
    m.root = buildPanel();
    if (viewer.scene && viewer.scene.preUpdate && viewer.scene.preUpdate.addEventListener) {
      viewer.scene.preUpdate.addEventListener(orbitTick);
    }

    m.start = start;
    m.stop = stop;
    m.gather = gather;
    m.orbitTick = orbitTick;
    m.fire = fire;
    m.state = function () {
      return { running: m.running, index: m.index, cycle: m.cycle,
        stops: m.stops.map(function (x) { return x.kind; }), orbiting: !!m.orbit,
        lastStopped: m.lastStopped };
    };
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !global.Cesium) return false;
      current = createModule(ctx);
      ctx.mount.appendChild(current.root);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDATour = {
    init: init,
    start: function () { return current ? current.start() : false; },
    stop: function () { return current ? current.stop('api') : false; },
    isRunning: function () { return !!(current && current.running); },
    // No layer of its own: the parent's layer toggle, if any, maps off to stop.
    setEnabled: function (on) { if (current && !on) current.stop('api'); },
    state: function () { return current ? current.state() : null; },
    gather: function () { return current ? current.gather() : Promise.resolve([]); },
    _orbitTick: function () { if (current) current.orbitTick(); },
    _fire: function () { return current ? current.fire() : false; },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    _setTiming: function (t) { Object.keys(t || {}).forEach(function (k) { if (k in timing) timing[k] = t[k]; }); },
    STOPS: STOPS,
    VIEW: VIEW,
    formatSpan: formatSpan,
    parseTime: parseTime,
    pickStorm: pickStorm,
    pickQuake: pickQuake,
    pickLaunch: pickLaunch,
    pickChokepoint: pickChokepoint,
    usgsRows: usgsRows,
    issStop: issStop
  };
})(typeof window !== 'undefined' ? window : this);
