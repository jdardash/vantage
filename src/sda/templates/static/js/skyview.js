/* Sky view: stand anywhere on the globe and look up.
 *
 * Parks the Cesium camera two metres above a ground point and aims it at the
 * sky. Drawn on top of Cesium's own star backdrop: the 2,851 stars brighter
 * than magnitude 5.5 (coloured by B-V, faded by the Sun's altitude the way the
 * real sky fades), 156 named stars, constellation figures and names, the Sun,
 * the Moon with its phase, the seven other planets, an altitude-azimuth grid
 * with compass points, and the names of satellites above the horizon, which
 * the globe already draws where they are. A reticle names whatever sits under
 * the centre of the view.
 *
 * Positions: astronomy-engine (Don Cross, MIT) gives topocentric J2000
 * directions for the Sun, Moon and planets and the J2000-to-horizon rotation
 * for the observer and time (precession, nutation and sidereal time included).
 * Star and constellation data come from d3-celestial (Olaf Frohn, BSD-3) via
 * scripts/build_sky_data.py into static/data/sky.json. Every sky object sits
 * in one collection whose model matrix is that rotation, so the whole sky
 * turns with the simulation clock by updating one matrix, not 3,000 points.
 *
 * Camera handling follows satvis (Florian Mauracher, MIT,
 * src/modules/SkyView.ts and skyGeometry.ts at d7fe933): the basis is set
 * directly rather than through heading/pitch/roll, which goes wrong within a
 * few degrees of the zenith, and both enableInputs and
 * enableCollisionDetection come off while the module owns the camera.
 *
 * Contract: window.SDASkyView.init(ctx) with ctx.viewer, ctx.mount,
 * ctx.satEngine, ctx.addIntelItem; enter({lat, lon}) / exit() / isActive().
 * Every string from data reaches the page through textContent, never as markup.
 */
(function (global) {
  'use strict';

  var ASTRO_SRC = 'https://cdn.jsdelivr.net/npm/astronomy-engine@2.1.19/astronomy.browser.min.js';
  var ASTRO_SRI = 'sha384-LTVHvxfbG3XHla4Lu0C0s9U18B2sBOuzJIeOoVHm2t05SOOORvMbBMEVjXrGau9g';
  var SKY_DATA_PATH = 'static/data/sky.json';
  var STYLE_ID = 'sda-skyview-style';
  var PANEL_TITLE = 'Sky view';

  var D2R = Math.PI / 180;
  var R2D = 180 / Math.PI;
  /** Where stars and planets are drawn: beyond GEO, inside the default far plane. */
  var SKY_RADIUS_M = 2.0e8;
  var EYE_HEIGHT_M = 2;
  var DEFAULT_FOVY = 75;
  var MIN_FOVY = 4;
  var MAX_FOVY = 110;
  var DEFAULT_PITCH = 30;
  var MAX_SAT_LABELS = 40;
  var SAT_LABEL_MIN_EL = 10;
  var TARGET_RADIUS_DEG = 4;
  var DIVE_ALT_M = 15000;

  // Cesium draws the real Sun and Moon discs; those two get a ring, not a dot.
  var BODIES = [
    { key: 'Sun', label: 'Sun', color: '#ffd84a', size: 30, ring: true },
    { key: 'Moon', label: 'Moon', color: '#f2f2e6', size: 26, ring: true },
    { key: 'Mercury', label: 'Mercury', color: '#c9b8a6', size: 6 },
    { key: 'Venus', label: 'Venus', color: '#fff6d8', size: 9 },
    { key: 'Mars', label: 'Mars', color: '#ff7a4d', size: 7 },
    { key: 'Jupiter', label: 'Jupiter', color: '#ffe2b8', size: 9 },
    { key: 'Saturn', label: 'Saturn', color: '#f3d58f', size: 8 },
    { key: 'Uranus', label: 'Uranus', color: '#aef0ff', size: 5 },
    { key: 'Neptune', label: 'Neptune', color: '#7ea8ff', size: 5 }
  ];

  var COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

  // ---------------------------------------------------------------------
  // Pure helpers (exported for tests)
  // ---------------------------------------------------------------------

  /** Unit vector in east-north-up for an azimuth (from north, clockwise) and elevation. */
  function enuDirection(azDeg, elDeg) {
    var az = azDeg * D2R;
    var el = elDeg * D2R;
    var c = Math.cos(el);
    return [Math.sin(az) * c, Math.cos(az) * c, Math.sin(el)];
  }

  /** Camera up and right in ENU for an aim, defined at the zenith too (satvis levelBasis). */
  function levelBasis(azDeg, elDeg) {
    var az = azDeg * D2R;
    var el = elDeg * D2R;
    var sa = Math.sin(az);
    var ca = Math.cos(az);
    var se = Math.sin(el);
    return { up: [-sa * se, -ca * se, Math.cos(el)], right: [ca, -sa, 0] };
  }

  /** Azimuth (0..360, from north) and elevation of an ENU vector. */
  function azElOf(enu) {
    var e = enu[0];
    var n = enu[1];
    var u = enu[2];
    var len = Math.sqrt(e * e + n * n + u * u) || 1;
    var az = Math.atan2(e, n) * R2D;
    if (az < 0) az += 360;
    return { az: az, el: Math.asin(Math.max(-1, Math.min(1, u / len))) * R2D };
  }

  /** J2000 equatorial unit vector for right ascension and declination in degrees. */
  function eqjVector(raDeg, decDeg) {
    var ra = raDeg * D2R;
    var dec = decDeg * D2R;
    var c = Math.cos(dec);
    return [c * Math.cos(ra), c * Math.sin(ra), Math.sin(dec)];
  }

  /**
   * astronomy-engine's RotateVector: out[j] = sum_i rot[i][j] * v[i]. Its
   * horizontal frame has x north, y west, z zenith.
   */
  function rotate(rot, v) {
    return [0, 1, 2].map(function (j) {
      return rot[0][j] * v[0] + rot[1][j] * v[1] + rot[2][j] * v[2];
    });
  }

  /** astronomy-engine horizontal (x north, y west, z up) to east-north-up. */
  function horToEnu(h) {
    return [-h[1], h[0], h[2]];
  }

  /**
   * Row-major 3x3 taking a J2000 vector straight to ENU: horToEnu after rotate.
   * ENU row e = -(HOR row y), n = HOR row x, u = HOR row z.
   */
  function eqjToEnuRows(rot) {
    function horRow(j) { return [rot[0][j], rot[1][j], rot[2][j]]; }
    var x = horRow(0);
    var y = horRow(1);
    var z = horRow(2);
    return [[-y[0], -y[1], -y[2]], x, z];
  }

  /** Cesium's fov is horizontal on a landscape viewport, vertical otherwise (satvis). */
  function fovFromFovy(fovyRad, aspect) {
    if (!isFinite(aspect) || aspect <= 1) return fovyRad;
    return 2 * Math.atan(Math.tan(fovyRad / 2) * aspect);
  }

  function compassPoint(azDeg) {
    var i = Math.round((((azDeg % 360) + 360) % 360) / 22.5) % 16;
    return COMPASS[i];
  }

  /** Faintest magnitude the eye picks out with the Sun at this altitude. */
  function limitingMag(sunAltDeg, noDaylight) {
    if (noDaylight || sunAltDeg <= -18) return 5.5;
    if (sunAltDeg <= -12) return 4.5;
    if (sunAltDeg <= -6) return 3.0;
    if (sunAltDeg <= -0.8) return 1.5;
    return -1.5;
  }

  function skyState(sunAltDeg) {
    if (sunAltDeg > -0.8) return 'Day';
    if (sunAltDeg > -6) return 'Civil twilight';
    if (sunAltDeg > -12) return 'Nautical twilight';
    if (sunAltDeg > -18) return 'Astronomical twilight';
    return 'Night';
  }

  function moonPhaseName(deg) {
    var d = ((deg % 360) + 360) % 360;
    if (d < 11.25 || d >= 348.75) return 'New moon';
    if (d < 78.75) return 'Waxing crescent';
    if (d < 101.25) return 'First quarter';
    if (d < 168.75) return 'Waxing gibbous';
    if (d < 191.25) return 'Full moon';
    if (d < 258.75) return 'Waning gibbous';
    if (d < 281.25) return 'Last quarter';
    return 'Waning crescent';
  }

  /** Pixel size for a star of this magnitude. */
  function starSize(mag) {
    return Math.max(1.4, Math.min(7.5, 5.2 - 0.85 * mag));
  }

  /** Approximate star colour from the B-V colour index. */
  function starColor(bv) {
    if (!isFinite(bv)) return '#f8f7ff';
    if (bv < 0.0) return '#9bb0ff';
    if (bv < 0.3) return '#cad7ff';
    if (bv < 0.6) return '#f8f7ff';
    if (bv < 1.0) return '#fff4ea';
    if (bv < 1.4) return '#ffd2a1';
    return '#ffb56c';
  }

  /** Angle in degrees between two unit-ish vectors. */
  function angleDeg(a, b) {
    var la = Math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) || 1;
    var lb = Math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2]) || 1;
    var c = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (la * lb);
    return Math.acos(Math.max(-1, Math.min(1, c))) * R2D;
  }

  /**
   * The labelled object nearest the view direction, within maxDeg, or null.
   * targets: [{name, enu, kind, priority}], a lower priority wins a near tie.
   */
  function nearestTarget(targets, dirEnu, maxDeg) {
    var best = null;
    var bestScore = Infinity;
    (targets || []).forEach(function (t) {
      if (!t || !t.enu) return;
      var a = angleDeg(t.enu, dirEnu);
      if (a > maxDeg) return;
      var score = a + (t.priority || 0) * 0.5;
      if (score < bestScore) { bestScore = score; best = { name: t.name, kind: t.kind, angle: a }; }
    });
    return best;
  }

  function fmt(n, digits) {
    return isFinite(n) ? n.toFixed(digits) : '--';
  }

  // ---------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------

  var astroLoading = null;

  function loadAstronomy() {
    if (global.Astronomy) return Promise.resolve(global.Astronomy);
    if (astroLoading) return astroLoading;
    astroLoading = new Promise(function (resolve, reject) {
      var s = global.document.createElement('script');
      s.src = ASTRO_SRC;
      s.integrity = ASTRO_SRI;
      s.crossOrigin = 'anonymous';
      s.onload = function () {
        if (global.Astronomy) resolve(global.Astronomy); else reject(new Error('no Astronomy'));
      };
      s.onerror = function () { astroLoading = null; reject(new Error('astronomy-engine did not load')); };
      global.document.head.appendChild(s);
    });
    return astroLoading;
  }

  var skyLoading = null;

  function loadSky(base) {
    if (skyLoading) return skyLoading;
    skyLoading = global.fetch((base || '') + SKY_DATA_PATH).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).catch(function (e) { skyLoading = null; throw e; });
    return skyLoading;
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.sky-note { font-size: 9px; color: var(--hud-dim, #7a5500); line-height: 1.4;',
    '  margin-bottom: 6px; }',
    '.sky-row { display: flex; gap: 3px; margin-bottom: 5px; }',
    '.sky-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.sky-status { font-size: 9px; min-height: 11px; color: var(--hud-color, #ffb000); }',
    '.sky-reticle { position: fixed; left: 50%; top: 50%; width: 34px; height: 34px;',
    '  margin: -17px 0 0 -17px; border: 1px solid rgba(255,176,0,0.55); border-radius: 50%;',
    '  pointer-events: none; z-index: 44; display: none; }',
    '.sky-reticle::before, .sky-reticle::after { content: ""; position: absolute;',
    '  background: rgba(255,176,0,0.55); }',
    '.sky-reticle::before { left: 16px; top: -8px; width: 1px; height: 50px; }',
    '.sky-reticle::after { top: 16px; left: -8px; height: 1px; width: 50px; }',
    '.sky-hud { position: fixed; left: 50%; bottom: 74px; transform: translateX(-50%);',
    '  z-index: 45; display: none; align-items: center; gap: 10px; padding: 6px 10px;',
    '  background: rgba(0,0,0,0.78); border: 1px solid rgba(255,176,0,0.45); border-radius: 4px;',
    '  font-size: 10px; letter-spacing: 1px; color: var(--hud-color, #ffb000);',
    '  max-width: max(320px, calc(100vw - 620px)); }',
    '.sky-hud .sky-read { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }',
    '.sky-hud .sky-target { color: #fff; font-weight: 700; }',
    '.sky-hud .sky-dim { color: var(--hud-dim, #7a5500); }',
    '.sky-hud button { background: none; border: 1px solid rgba(255,176,0,0.35);',
    '  color: var(--hud-color, #ffb000); font: inherit; font-size: 9px; padding: 2px 7px;',
    '  border-radius: 2px; cursor: pointer; }',
    '.sky-hud button.on { background: rgba(255,176,0,0.18); }',
    'body.sky-active #cesiumContainer canvas { cursor: grab; }'
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

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var m = {
      active: false, entering: false, observer: null, aim: { az: 180, el: DEFAULT_PITCH },
      fovy: DEFAULT_FOVY, saved: null, sky: null, astro: null, layers: null,
      noDaylight: false, showLines: true, showLabels: true, limit: null,
      targets: [], lastSkyUpdate: 0, picking: false, pickHandler: null, drag: null,
      listeners: [], removePreRender: null, enuRot: null
    };

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    // -- panel ------------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    body.appendChild(el('div', 'sky-note',
      'Stand anywhere on Earth and look up: stars, constellations, the Sun, Moon and ' +
      'planets where they are now, and the satellites passing overhead. Drag to look ' +
      'around, scroll to zoom, Esc to come back.'));
    var row1 = el('div', 'sky-row');
    m.hereBtn = button('mode-btn', 'LOOK UP HERE', 'Sky above the centre of the view');
    m.pickBtn = button('mode-btn', 'PICK A SPOT', 'Click the globe to choose where to stand');
    row1.appendChild(m.hereBtn);
    row1.appendChild(m.pickBtn);
    var row2 = el('div', 'sky-row');
    m.meBtn = button('mode-btn', 'MY LOCATION', 'Sky above you (asks the browser for your location)');
    m.satBtn = button('mode-btn', 'UNDER SAT', 'Stand under the selected satellite');
    row2.appendChild(m.meBtn);
    row2.appendChild(m.satBtn);
    m.status = el('div', 'sky-status');
    m.status.setAttribute('role', 'status');
    body.appendChild(row1);
    body.appendChild(row2);
    body.appendChild(m.status);
    m.root.appendChild(head);
    m.root.appendChild(body);

    function setStatus(msg) { m.status.textContent = msg; }

    // -- HUD (reticle and readout) -----------------------------------------

    m.reticle = el('div', 'sky-reticle');
    m.hud = el('div', 'sky-hud');
    m.read = el('span', 'sky-read');
    m.target = el('span', 'sky-target');
    m.sunRead = el('span', 'sky-read sky-dim');
    m.dayBtn = button(null, 'DAYLIGHT', 'Show or remove the daytime sky');
    m.dayBtn.classList.add('on');
    m.linesBtn = button(null, 'LINES', 'Constellation figures');
    m.linesBtn.classList.add('on');
    m.labelsBtn = button(null, 'LABELS', 'Names of stars, constellations and satellites');
    m.labelsBtn.classList.add('on');
    m.exitBtn = button(null, 'EXIT [ESC]', 'Back to the globe');
    [m.read, m.target, m.sunRead, m.dayBtn, m.linesBtn, m.labelsBtn, m.exitBtn].forEach(
      function (n) { m.hud.appendChild(n); });
    global.document.body.appendChild(m.reticle);
    global.document.body.appendChild(m.hud);

    // -- geometry --------------------------------------------------------

    function enuToFixedRotation(position) {
      var m4 = C.Transforms.eastNorthUpToFixedFrame(position);
      return C.Matrix4.getMatrix3(m4, new C.Matrix3());
    }

    function toFixed(enu) {
      return C.Matrix3.multiplyByVector(m.enuRot, new C.Cartesian3(enu[0], enu[1], enu[2]),
        new C.Cartesian3());
    }

    function applyCamera() {
      var cam = viewer.camera;
      var dir = enuDirection(m.aim.az, m.aim.el);
      var basis = levelBasis(m.aim.az, m.aim.el);
      cam.lookAtTransform(C.Matrix4.IDENTITY);
      cam.position = C.Cartesian3.clone(m.eye);
      cam.direction = toFixed(dir);
      cam.up = toFixed(basis.up);
      cam.right = toFixed(basis.right);
      try {
        var aspect = viewer.canvas.clientWidth / Math.max(1, viewer.canvas.clientHeight);
        cam.frustum.fov = fovFromFovy(m.fovy * D2R, aspect);
      } catch (e) { /* keep going */ }
    }

    // -- sky layers --------------------------------------------------------

    function buildLayers(sky) {
      var L = {
        stars: scene.primitives.add(new C.PointPrimitiveCollection()),
        bodies: scene.primitives.add(new C.PointPrimitiveCollection()),
        lines: scene.primitives.add(new C.PolylineCollection()),
        labels: scene.primitives.add(new C.LabelCollection()),
        grid: scene.primitives.add(new C.PolylineCollection()),
        gridLabels: scene.primitives.add(new C.LabelCollection()),
        satLabels: scene.primitives.add(new C.LabelCollection()),
        starPoints: [], bodyPoints: {}, bodyLabels: {}, starLabels: [], conLabels: [],
        satPool: []
      };
      for (var k = 0; k < MAX_SAT_LABELS; k++) {
        L.satPool.push(L.satLabels.add({
          position: C.Cartesian3.ZERO, text: '', show: false,
          font: '10px "JetBrains Mono", Consolas, monospace',
          fillColor: C.Color.fromCssColorString('#5ee0ff'),
          outlineColor: C.Color.BLACK, outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new C.Cartesian2(9, 0),
          horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER
        }));
      }
      sky.stars.forEach(function (s) {
        var v = eqjVector(s[0], s[1]);
        var p = L.stars.add({
          position: new C.Cartesian3(v[0] * SKY_RADIUS_M, v[1] * SKY_RADIUS_M, v[2] * SKY_RADIUS_M),
          pixelSize: starSize(s[2]),
          color: C.Color.fromCssColorString(starColor(s[3]))
        });
        p.sdaMag = s[2];
        L.starPoints.push(p);
      });
      var labelFont = '11px "JetBrains Mono", Consolas, monospace';
      sky.named.forEach(function (s) {
        var v = eqjVector(s[0], s[1]);
        var lab = L.labels.add({
          position: new C.Cartesian3(v[0] * SKY_RADIUS_M, v[1] * SKY_RADIUS_M, v[2] * SKY_RADIUS_M),
          text: String(s[3]), font: labelFont,
          fillColor: C.Color.fromCssColorString('#cfe3ff').withAlpha(0.85),
          pixelOffset: new C.Cartesian2(8, -8),
          horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER
        });
        lab.sdaMag = s[2];
        L.starLabels.push({ label: lab, name: String(s[3]), v: v, mag: s[2] });
      });
      var lineColor = C.Color.fromCssColorString('#5f8fd6').withAlpha(0.45);
      sky.lines.forEach(function (flat) {
        var pts = [];
        for (var i = 0; i + 1 < flat.length; i += 2) {
          var v = eqjVector(flat[i], flat[i + 1]);
          pts.push(new C.Cartesian3(v[0] * SKY_RADIUS_M, v[1] * SKY_RADIUS_M, v[2] * SKY_RADIUS_M));
        }
        L.lines.add({ positions: pts, width: 1.0,
          material: C.Material.fromType('Color', { color: lineColor }) });
      });
      sky.constellations.forEach(function (c) {
        if (c[3] > 2) return;
        var v = eqjVector(c[0], c[1]);
        var lab = L.labels.add({
          position: new C.Cartesian3(v[0] * SKY_RADIUS_M, v[1] * SKY_RADIUS_M, v[2] * SKY_RADIUS_M),
          text: String(c[2]).toUpperCase(), font: '10px "JetBrains Mono", Consolas, monospace',
          fillColor: C.Color.fromCssColorString('#6f9be0').withAlpha(0.7),
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.CENTER
        });
        L.conLabels.push({ label: lab, name: String(c[2]), v: v });
      });
      BODIES.forEach(function (b) {
        var color = C.Color.fromCssColorString(b.color);
        L.bodyPoints[b.key] = L.bodies.add({
          position: C.Cartesian3.ZERO, pixelSize: b.size,
          color: b.ring ? color.withAlpha(0.0) : color,
          outlineColor: color.withAlpha(b.ring ? 0.8 : 0.25), outlineWidth: b.ring ? 1.5 : 3
        });
        L.bodyLabels[b.key] = L.labels.add({
          position: C.Cartesian3.ZERO, text: b.label, font: 'bold 12px "JetBrains Mono", Consolas, monospace',
          fillColor: C.Color.fromCssColorString(b.color),
          outlineColor: C.Color.BLACK, outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new C.Cartesian2(12, -12),
          horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.CENTER
        });
      });
      return L;
    }

    function buildGrid() {
      var L = m.layers;
      L.grid.removeAll();
      L.gridLabels.removeAll();
      var r = SKY_RADIUS_M * 0.999;
      function ring(el, color, width) {
        var pts = [];
        for (var az = 0; az <= 360; az += 3) {
          var d = enuDirection(az, el);
          pts.push(new C.Cartesian3(d[0] * r, d[1] * r, d[2] * r));
        }
        L.grid.add({ positions: pts, width: width,
          material: C.Material.fromType('Color', { color: color }) });
      }
      ring(0, C.Color.fromCssColorString('#ffb000').withAlpha(0.7), 1.5);
      ring(30, C.Color.fromCssColorString('#ffb000').withAlpha(0.18), 1.0);
      ring(60, C.Color.fromCssColorString('#ffb000').withAlpha(0.18), 1.0);
      for (var az = 0; az < 360; az += 45) {
        var d = enuDirection(az, 1.2);
        L.gridLabels.add({
          position: new C.Cartesian3(d[0] * r, d[1] * r, d[2] * r),
          text: compassPoint(az), font: 'bold 13px "JetBrains Mono", Consolas, monospace',
          fillColor: C.Color.fromCssColorString(az === 0 ? '#ff5a4d' : '#ffb000'),
          outlineColor: C.Color.BLACK, outlineWidth: 2, style: C.LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: C.HorizontalOrigin.CENTER, verticalOrigin: C.VerticalOrigin.BOTTOM
        });
      }
      [30, 60].forEach(function (alt) {
        var d = enuDirection(180, alt);
        L.gridLabels.add({
          position: new C.Cartesian3(d[0] * r, d[1] * r, d[2] * r),
          text: alt + ' deg', font: '9px "JetBrains Mono", Consolas, monospace',
          fillColor: C.Color.fromCssColorString('#ffb000').withAlpha(0.5),
          horizontalOrigin: C.HorizontalOrigin.LEFT, verticalOrigin: C.VerticalOrigin.BOTTOM
        });
      });
      var enuMatrix = C.Matrix4.fromRotationTranslation(m.enuRot, m.eye);
      L.grid.modelMatrix = enuMatrix;
      L.gridLabels.modelMatrix = enuMatrix;
    }

    function setLayersShown(on) {
      var L = m.layers;
      if (!L) return;
      L.stars.show = on;
      L.bodies.show = on;
      L.lines.show = on && m.showLines;
      L.labels.show = on && m.showLabels;
      L.grid.show = on;
      L.gridLabels.show = on;
      L.satLabels.show = on && m.showLabels;
    }

    /** Recompute the sky for the clock: one model matrix plus nine bodies. */
    function updateSky(force) {
      var A = m.astro;
      if (!A || !m.active) return;
      var date = C.JulianDate.toDate(viewer.clock.currentTime);
      var t = date.getTime();
      if (!force && Math.abs(t - m.lastSkyUpdate) < 500) return;
      m.lastSkyUpdate = t;
      var obs = new A.Observer(m.observer.lat, m.observer.lon, 0);
      var rot = A.Rotation_EQJ_HOR(date, obs).rot;
      var rows = eqjToEnuRows(rot);
      var eqjToEnu = new C.Matrix3(
        rows[0][0], rows[0][1], rows[0][2],
        rows[1][0], rows[1][1], rows[1][2],
        rows[2][0], rows[2][1], rows[2][2]);
      var total = C.Matrix3.multiply(m.enuRot, eqjToEnu, new C.Matrix3());
      var model = C.Matrix4.fromRotationTranslation(total, m.eye);
      var L = m.layers;
      L.stars.modelMatrix = model;
      L.bodies.modelMatrix = model;
      L.lines.modelMatrix = model;
      L.labels.modelMatrix = model;

      function toEnu(v) {
        return [rows[0][0] * v[0] + rows[0][1] * v[1] + rows[0][2] * v[2],
                rows[1][0] * v[0] + rows[1][1] * v[1] + rows[1][2] * v[2],
                rows[2][0] * v[0] + rows[2][1] * v[1] + rows[2][2] * v[2]];
      }

      var targets = [];
      var sunAlt = -90;
      BODIES.forEach(function (b) {
        try {
          var eq = A.Equator(A.Body[b.key], date, obs, false, true);
          var v = eqjVector(eq.ra * 15, eq.dec);
          var pos = new C.Cartesian3(v[0] * SKY_RADIUS_M * 0.98, v[1] * SKY_RADIUS_M * 0.98,
            v[2] * SKY_RADIUS_M * 0.98);
          L.bodyPoints[b.key].position = pos;
          L.bodyLabels[b.key].position = pos;
          var enu = toEnu(v);
          var ae = azElOf(enu);
          var text = b.label;
          if (b.key === 'Sun') {
            sunAlt = ae.el;
          } else if (b.key === 'Moon') {
            var ill = A.Illumination(A.Body.Moon, date);
            text = 'Moon ' + Math.round(ill.phase_fraction * 100) + '% ' +
              moonPhaseName(A.MoonPhase(date)).toLowerCase();
          } else {
            var mag = A.Illumination(A.Body[b.key], date).mag;
            text = b.label + ' ' + (mag >= 0 ? '+' : '') + fmt(mag, 1);
          }
          L.bodyLabels[b.key].text = text;
          targets.push({ name: text, enu: enu, kind: 'body', priority: 0 });
        } catch (e) { /* one body failing must not stop the rest */ }
      });

      var limit = limitingMag(sunAlt, m.noDaylight);
      if (limit !== m.limit) {
        m.limit = limit;
        L.starPoints.forEach(function (p) { p.show = p.sdaMag <= limit; });
        L.starLabels.forEach(function (s) { s.label.show = s.mag <= Math.min(limit, 2.5); });
      }
      L.starLabels.forEach(function (s) {
        if (s.mag <= limit) targets.push({ name: s.name, enu: toEnu(s.v), kind: 'star', priority: 1 });
      });
      L.conLabels.forEach(function (c) {
        targets.push({ name: c.name, enu: toEnu(c.v), kind: 'constellation', priority: 3 });
      });
      m.targets = targets;
      m.sunAlt = sunAlt;
      // Past astronomical twilight the sky has no glow left to draw.
      try { scene.skyAtmosphere.show = !m.noDaylight && sunAlt > -18; } catch (e) { /* keep going */ }
      m.sunRead.textContent = 'Sun ' + fmt(sunAlt, 1) + ' deg, ' + skyState(sunAlt) +
        ' | stars to mag ' + fmt(limit, 1);
    }

    /** Satellite names above the horizon, nearest first, every frame. */
    function updateSatLabels() {
      var L = m.layers;
      if (!L || !m.showLabels) return;
      var eng = ctx.satEngine;
      var list = [];
      try {
        var ids = Object.keys((eng && eng.byNoradId) || {});
        var inv = C.Matrix3.transpose(m.enuRot, new C.Matrix3());
        var rel = new C.Cartesian3();
        var enu = new C.Cartesian3();
        for (var i = 0; i < ids.length; i++) {
          var s = eng.byNoradId[ids[i]];
          if (!s || !s.point || !s.point.position || s.point.show === false) continue;
          C.Cartesian3.subtract(s.point.position, m.eye, rel);
          C.Matrix3.multiplyByVector(inv, rel, enu);
          var range = C.Cartesian3.magnitude(enu);
          if (!(range > 0)) continue;
          var el = Math.asin(enu.z / range) * R2D;
          if (el < SAT_LABEL_MIN_EL) continue;
          list.push({ s: s, range: range, el: el, az: azElOf([enu.x, enu.y, enu.z]).az });
        }
      } catch (e) { list = []; }
      list.sort(function (a, b) { return a.range - b.range; });
      list = list.slice(0, MAX_SAT_LABELS);
      L.satPool.forEach(function (lab, i) {
        var it = list[i];
        if (!it) { lab.show = false; return; }
        var text = String(it.s.name || 'SAT') + '  ' + Math.round(it.range / 1000) + ' km';
        lab.position = it.s.point.position;
        if (lab.text !== text) lab.text = text;
        lab.show = true;
      });
      m.satTargets = list.map(function (it) {
        return { name: String(it.s.name || 'SAT') + ' (' + Math.round(it.range / 1000) + ' km)',
          enu: enuDirection(it.az, it.el), kind: 'satellite', priority: -1 };
      });
    }

    function hideSatLabels() {
      if (m.layers) m.layers.satPool.forEach(function (lab) { lab.show = false; });
      m.satTargets = [];
    }

    function updateHud() {
      var dir = enuDirection(m.aim.az, m.aim.el);
      m.read.textContent = 'AZ ' + fmt(m.aim.az, 1) + ' ' + compassPoint(m.aim.az) +
        '  EL ' + fmt(m.aim.el, 1) + '  FOV ' + Math.round(m.fovy);
      var hit = nearestTarget((m.satTargets || []).concat(m.targets), dir,
        Math.max(1.5, Math.min(TARGET_RADIUS_DEG, m.fovy / 15)));
      m.target.textContent = hit ? hit.name : '';
    }

    function onPreRender() {
      if (!m.active) return;
      applyCamera();
      updateSky(false);
      updateSatLabels();
      updateHud();
    }

    // -- input -------------------------------------------------------------

    function listen(target, type, fn, opts) {
      target.addEventListener(type, fn, opts);
      m.listeners.push(function () { target.removeEventListener(type, fn, opts); });
    }

    function degPerPixel() {
      return m.fovy / Math.max(1, viewer.canvas.clientHeight);
    }

    function look(daz, del) {
      m.aim.az = (((m.aim.az + daz) % 360) + 360) % 360;
      m.aim.el = Math.max(-20, Math.min(90, m.aim.el + del));
    }

    function zoom(factor) {
      m.fovy = Math.max(MIN_FOVY, Math.min(MAX_FOVY, m.fovy * factor));
    }

    function bindInput() {
      var canvas = viewer.canvas;
      listen(canvas, 'pointerdown', function (e) {
        m.drag = { x: e.clientX, y: e.clientY };
      });
      listen(global, 'pointermove', function (e) {
        if (!m.drag) return;
        var k = degPerPixel();
        look(-(e.clientX - m.drag.x) * k, (e.clientY - m.drag.y) * k);
        m.drag = { x: e.clientX, y: e.clientY };
      });
      listen(global, 'pointerup', function () { m.drag = null; });
      listen(canvas, 'wheel', function (e) {
        zoom(e.deltaY > 0 ? 1.1 : 1 / 1.1);
        if (e.preventDefault) e.preventDefault();
      }, { passive: false });
      listen(global.document, 'keydown', function (e) {
        var tag = (e.target && e.target.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;
        var step = Math.max(0.5, m.fovy / 20);
        var used = true;
        if (e.key === 'Escape') exit();
        else if (e.key === 'ArrowLeft') look(-step, 0);
        else if (e.key === 'ArrowRight') look(step, 0);
        else if (e.key === 'ArrowUp') look(0, step);
        else if (e.key === 'ArrowDown') look(0, -step);
        else if (e.key === '+' || e.key === '=') zoom(1 / 1.15);
        else if (e.key === '-' || e.key === '_') zoom(1.15);
        else used = false;
        if (used && e.stopImmediatePropagation) e.stopImmediatePropagation();
      }, true);
    }

    // -- enter / exit ------------------------------------------------------

    function saveState() {
      var cam = viewer.camera;
      var ssc = scene.screenSpaceCameraController;
      return {
        position: C.Cartesian3.clone(cam.positionWC),
        direction: C.Cartesian3.clone(cam.directionWC),
        up: C.Cartesian3.clone(cam.upWC),
        fov: cam.frustum.fov,
        enableInputs: ssc.enableInputs,
        collision: ssc.enableCollisionDetection,
        skyAtmosphere: scene.skyAtmosphere.show,
        sun: scene.sun.show,
        moon: scene.moon.show,
        tracked: viewer.trackedEntity,
        lighting: globeFlags(),
        pageLabels: pageLabels() ? pageLabels().show : null
      };
    }

    // The globe's lighting flags. Without sun-driven lighting Cesium paints the
    // sky as if the Sun stood behind the camera, so a midnight sky came out grey.
    function globeFlags() {
      var g = scene.globe;
      if (!g) return null;
      return { enable: g.enableLighting, dyn: g.dynamicAtmosphereLighting,
               fromSun: g.dynamicAtmosphereLightingFromSun };
    }

    function setGlobeFlags(f) {
      var g = scene.globe;
      if (!g || !f) return;
      try {
        g.enableLighting = f.enable;
        g.dynamicAtmosphereLighting = f.dyn;
        g.dynamicAtmosphereLightingFromSun = f.fromSun;
      } catch (e) { /* keep going */ }
    }

    // The page's own satellite name labels. The sky view draws its own (with the
    // range), so the page's are hidden while it is up instead of doubling every name.
    function pageLabels() {
      var eng = ctx.satEngine;
      return eng && eng.labels ? eng.labels : null;
    }

    function begin(observer) {
      m.observer = observer;
      var base = C.Cartesian3.fromDegrees(observer.lon, observer.lat, 0);
      m.enuRot = enuToFixedRotation(base);
      var ground = 0;
      try {
        var gh = scene.globe.getHeight(C.Cartographic.fromDegrees(observer.lon, observer.lat));
        // An unloaded tile can return garbage (satvis saw -36990 m): keep only plausible heights.
        if (isFinite(gh) && gh > -500 && gh < 9000) ground = gh;
      } catch (e) { ground = 0; }
      m.eye = C.Cartesian3.fromDegrees(observer.lon, observer.lat, ground + EYE_HEIGHT_M);
      m.aim = { az: observer.lat >= 0 ? 180 : 0, el: DEFAULT_PITCH };
      m.fovy = DEFAULT_FOVY;
      m.limit = null;
      m.lastSkyUpdate = 0;
      var ssc = scene.screenSpaceCameraController;
      ssc.enableInputs = false;
      ssc.enableCollisionDetection = false;
      try { viewer.trackedEntity = undefined; } catch (e) { /* keep going */ }
      try { scene.moon.show = true; scene.sun.show = true; } catch (e) { /* keep going */ }
      setGlobeFlags({ enable: true, dyn: true, fromSun: true });
      if (pageLabels()) pageLabels().show = false;
      m.active = true;
      buildGrid();
      setLayersShown(true);
      m.reticle.style.display = 'block';
      m.hud.style.display = 'flex';
      try { global.document.body.classList.add('sky-active'); } catch (e) { /* keep going */ }
      bindInput();
      m.removePreRender = scene.preRender.addEventListener(onPreRender);
      applyCamera();
      updateSky(true);
      updateHud();
      var where = fmt(Math.abs(observer.lat), 3) + (observer.lat >= 0 ? ' N ' : ' S ') +
        fmt(Math.abs(observer.lon), 3) + (observer.lon >= 0 ? ' E' : ' W');
      setStatus('Standing at ' + where + (observer.name ? ' (' + observer.name + ')' : '') + '.');
      note('Sky view: ' + where + ', ' + skyState(m.sunAlt) + (observer.name ? ', ' + observer.name : ''));
    }

    function enter(observer) {
      if (!observer || !isFinite(observer.lat) || !isFinite(observer.lon)) {
        setStatus('No position to stand on.');
        return Promise.resolve(false);
      }
      if (m.entering) return Promise.resolve(false);
      if (m.active) exit(true);
      m.entering = true;
      setStatus('Loading the sky...');
      return Promise.all([loadAstronomy(), loadSky(ctx.api ? ctx.api + '/' : '')]).then(function (res) {
        m.astro = res[0];
        if (!m.layers) m.layers = buildLayers(res[1]);
        m.saved = saveState();
        return new Promise(function (resolve) {
          var done = false;
          function go() {
            if (done) return;
            done = true;
            m.entering = false;
            begin(observer);
            resolve(true);
          }
          try {
            viewer.camera.flyTo({
              destination: C.Cartesian3.fromDegrees(observer.lon, observer.lat, DIVE_ALT_M),
              duration: 1.6, complete: go, cancel: go
            });
          } catch (e) { go(); }
        });
      }).catch(function (e) {
        m.entering = false;
        setStatus('The sky did not load: ' + (e && e.message ? e.message : 'error') + '.');
        return false;
      });
    }

    function exit(quiet) {
      if (!m.active) return;
      m.active = false;
      m.drag = null;
      if (m.removePreRender) { m.removePreRender(); m.removePreRender = null; }
      m.listeners.forEach(function (off) { try { off(); } catch (e) { /* keep going */ } });
      m.listeners = [];
      setLayersShown(false);
      hideSatLabels();
      m.reticle.style.display = 'none';
      m.hud.style.display = 'none';
      try { global.document.body.classList.remove('sky-active'); } catch (e) { /* keep going */ }
      var s = m.saved;
      if (s) {
        var ssc = scene.screenSpaceCameraController;
        ssc.enableInputs = s.enableInputs;
        ssc.enableCollisionDetection = s.collision;
        scene.skyAtmosphere.show = s.skyAtmosphere;
        scene.moon.show = s.moon;
        scene.sun.show = s.sun;
        setGlobeFlags(s.lighting);
        if (pageLabels() && s.pageLabels !== null) pageLabels().show = s.pageLabels;
        try { viewer.camera.frustum.fov = s.fov; } catch (e) { /* keep going */ }
        if (!quiet) {
          try {
            viewer.camera.flyTo({ destination: s.position,
              orientation: { direction: s.direction, up: s.up }, duration: 1.2 });
          } catch (e) { /* keep going */ }
        }
      }
      if (!quiet) setStatus('');
    }

    // -- panel actions -----------------------------------------------------

    function viewCentre() {
      try {
        var pt = viewer.camera.pickEllipsoid(
          new C.Cartesian2(viewer.canvas.clientWidth / 2, viewer.canvas.clientHeight / 2),
          scene.globe.ellipsoid);
        var carto = pt ? C.Cartographic.fromCartesian(pt) : viewer.camera.positionCartographic;
        return { lat: carto.latitude * R2D, lon: carto.longitude * R2D };
      } catch (e) {
        return null;
      }
    }

    m.hereBtn.addEventListener('click', function () { enter(viewCentre()); });

    m.meBtn.addEventListener('click', function () {
      var geo = global.navigator && global.navigator.geolocation;
      if (!geo) { setStatus('This browser does not share a location.'); return; }
      setStatus('Asking the browser for your location...');
      geo.getCurrentPosition(function (p) {
        enter({ lat: p.coords.latitude, lon: p.coords.longitude, name: 'your location' });
      }, function () {
        setStatus('The location was not shared. Use LOOK UP HERE or PICK A SPOT.');
      }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
    });

    m.satBtn.addEventListener('click', function () {
      try {
        var eng = ctx.satEngine;
        var id = eng && eng.selectedId;
        var s = (id !== null && id !== undefined && eng.byNoradId) ? eng.byNoradId[id] : null;
        if (!s || !s.point || !s.point.position) { setStatus('Select a satellite on the globe first.'); return; }
        var carto = C.Cartographic.fromCartesian(s.point.position);
        enter({ lat: carto.latitude * R2D, lon: carto.longitude * R2D,
          name: 'under ' + String(s.name || 'the satellite') });
      } catch (e) {
        setStatus('The satellite has no position yet.');
      }
    });

    m.pickBtn.addEventListener('click', function () {
      if (m.picking) return;
      m.picking = true;
      setStatus('Click the globe where you want to stand.');
      var handler = new C.ScreenSpaceEventHandler(viewer.canvas);
      m.pickHandler = handler;
      handler.setInputAction(function (click) {
        var pt = viewer.camera.pickEllipsoid(click.position, scene.globe.ellipsoid);
        handler.destroy();
        m.pickHandler = null;
        m.picking = false;
        if (!pt) { setStatus('That click missed the globe.'); return; }
        var carto = C.Cartographic.fromCartesian(pt);
        enter({ lat: carto.latitude * R2D, lon: carto.longitude * R2D });
      }, C.ScreenSpaceEventType.LEFT_CLICK);
    });

    m.dayBtn.addEventListener('click', function () {
      m.noDaylight = !m.noDaylight;
      m.dayBtn.classList.toggle('on', !m.noDaylight);
      updateSky(true);
    });
    m.linesBtn.addEventListener('click', function () {
      m.showLines = !m.showLines;
      m.linesBtn.classList.toggle('on', m.showLines);
      setLayersShown(m.active);
    });
    m.labelsBtn.addEventListener('click', function () {
      m.showLabels = !m.showLabels;
      m.labelsBtn.classList.toggle('on', m.showLabels);
      setLayersShown(m.active);
      if (!m.showLabels) hideSatLabels();
    });
    m.exitBtn.addEventListener('click', function () { exit(); });

    m.enter = enter;
    m.exit = exit;
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

  global.SDASkyView = {
    init: init,
    enter: function (observer) { return current ? current.enter(observer) : Promise.resolve(false); },
    exit: function () { if (current) current.exit(); },
    isActive: function () { return !!(current && current.active); },
    ASTRO_SRC: ASTRO_SRC,
    ASTRO_SRI: ASTRO_SRI,
    SKY_DATA_PATH: SKY_DATA_PATH,
    enuDirection: enuDirection,
    levelBasis: levelBasis,
    azElOf: azElOf,
    eqjVector: eqjVector,
    rotate: rotate,
    horToEnu: horToEnu,
    eqjToEnuRows: eqjToEnuRows,
    fovFromFovy: fovFromFovy,
    compassPoint: compassPoint,
    limitingMag: limitingMag,
    skyState: skyState,
    moonPhaseName: moonPhaseName,
    starSize: starSize,
    starColor: starColor,
    nearestTarget: nearestTarget
  };
})(typeof window !== 'undefined' ? window : this);
