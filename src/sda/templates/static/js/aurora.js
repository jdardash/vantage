// Aurora oval on the WorldView globe: NOAA SWPC's OVATION Prime forecast of the
// chance of visible aurora overhead, on a 1-degree grid, drawn as a glowing
// translucent shell at aurora height and refreshed every ten minutes.
//
// Data: GET /api/aurora (sda/routes/space.py), which fetches the keyless SWPC
// product at most once per ten minutes and returns only the cells at or above a
// threshold. The static demo build has no server, so there the layer says so
// and makes no request at all.
//
// Adapted from Gods-view-sweden (https://github.com/Kajjan7610/Gods-view-sweden,
// MIT, Copyright (c) 2026 Bilawal Sidhu and Copyright (c) 2026 Kaj Obst,
// commit 981aef27bc0c): the green to
// yellow to red stops of auroraColor() in src/layers/aurora/records.js, and from
// src/layers/aurora/index.js the alpha that rises with the chance and the 110 km
// band height. As in that project the band is painted into a texture: one texel
// per cell, scaled up with a light blur (wrapped across the antimeridian so there
// is no seam), on one rectangle per hemisphere from EDGE_LAT to the pole at
// 110 km. Seen from orbit it reads as a continuous oval; from the ground in the
// sky view it is a tint across the part of the sky where aurora is likely.
//
// Frames: geodetic degrees only. Text is only ever set with textContent.
//
// Page wiring: list it in EXTENSIONS (it builds a small panel in ctx.mount when
// one is given) and call SDAAurora.setEnabled(on) from the layer toggle. A span
// with id count-aurora, when the page has one, shows the number of cells drawn.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Aurora oval';
  var COUNT_ID = 'count-aurora';
  var REFRESH_MS = 10 * 60 * 1000;
  var DEFAULT_MIN = 5;
  var MIN_CHOICES = [1, 5, 10, 20, 30, 50];
  var HEIGHT_M = 110000; // the green 557.7 nm emission peaks near 110 km
  var MATERIAL_ALPHA = 0.999;

  // Each hemisphere's texture runs from EDGE_LAT to the pole. OVATION's 1 % edge
  // reached about 40 degrees in the May 2024 storm, so 20 leaves room to spare.
  var EDGE_LAT = 20;
  var COLS = 360;                 // one texel per degree of longitude, 180 W first
  var ROWS = 90 - EDGE_LAT;       // one texel per degree of latitude, pole first
  var UPSCALE = 4;                // the drawn texture is 4x the cell grid
  var BLUR_PX = 3;                // canvas blur on the 4x texture, in texels

  // Chance in percent -> colour. Transparent green at the edge of the oval,
  // bright green, then yellow at 30 % and red from 50 %.
  var RAMP = [
    [0, 0.0, 0.8, 0.4],
    [10, 0.0, 0.902, 0.463],   // #00e676
    [30, 1.0, 0.839, 0.0],     // #ffd600
    [50, 1.0, 0.09, 0.267],    // #ff1744
    [100, 1.0, 0.09, 0.267]
  ];
  // Alpha rises with the chance. The donor used 0.04 + p / 55 up to 0.75; a
  // higher floor here keeps a quiet night's oval (10 to 30 %) clearly visible
  // from orbit, and the cap keeps the land under a strong storm readable.
  var ALPHA_BASE = 0.12;
  var ALPHA_SPAN = 60;
  var ALPHA_MAX = 0.8;

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the tests)
  // ---------------------------------------------------------------------

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function round3(x) { return Math.round(x * 1000) / 1000; }

  // A threshold in whole percent, 1..100; anything unreadable is the default.
  function clampMin(v) {
    var n = (typeof v === 'string' && v.trim() !== '') ? Number(v) : v;
    if (!finite(n)) return DEFAULT_MIN;
    n = Math.round(n);
    if (n < 1) return 1;
    if (n > 100) return 100;
    return n;
  }

  function alphaFor(prob) {
    if (!finite(prob) || prob <= 0) return 0;
    return round3(Math.min(ALPHA_MAX, ALPHA_BASE + Math.min(prob, 100) / ALPHA_SPAN));
  }

  // { r, g, b, a } in 0..1 for a chance in percent.
  function colorFor(prob) {
    var p = finite(prob) ? Math.max(0, Math.min(100, prob)) : 0;
    var i = 1;
    while (i < RAMP.length - 1 && p > RAMP[i][0]) i++;
    var lo = RAMP[i - 1];
    var hi = RAMP[i];
    var t = hi[0] === lo[0] ? 1 : (p - lo[0]) / (hi[0] - lo[0]);
    return {
      r: round3(lo[1] + (hi[1] - lo[1]) * t),
      g: round3(lo[2] + (hi[2] - lo[2]) * t),
      b: round3(lo[3] + (hi[3] - lo[3]) * t),
      a: alphaFor(prob)
    };
  }

  function auroraUrl(api, min) {
    return (api || '') + '/api/aurora?min=' + clampMin(min);
  }

  // The rows worth drawing: [lon -180..180, lat, prob] with every value a finite
  // number in range and the chance at or above the threshold.
  function cleanCells(cells, min) {
    var floor = clampMin(min);
    var out = [];
    if (!Array.isArray(cells)) return out;
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (!Array.isArray(c) || c.length < 3) continue;
      var lon = c[0];
      var lat = c[1];
      var p = c[2];
      if (!finite(lon) || !finite(lat) || !finite(p)) continue;
      if (lon > 180 && lon <= 360) lon -= 360;
      if (lon < -180 || lon > 180 || lat < -90 || lat > 90) continue;
      if (p < floor || p > 100) continue;
      out.push([lon, lat, p]);
    }
    return out;
  }

  // One hemisphere as RGBA bytes: COLS x ROWS texels, row 0 at the top of the
  // texture (the pole for the north band, EDGE_LAT south for the south band),
  // column 0 at 180 W. A cell outside the band is skipped; where two cells land
  // on one texel the stronger wins. rect is [west, south, east, north] degrees.
  function rasterBand(cells, north) {
    var px = new Uint8ClampedArray(COLS * ROWS * 4);
    var best = [];
    var count = 0;
    (cells || []).forEach(function (c) {
      var lon = c[0];
      var lat = c[1];
      if (north ? lat < EDGE_LAT : lat > -EDGE_LAT) return;
      var row = Math.floor(north ? 90 - lat : -EDGE_LAT - lat);
      row = Math.max(0, Math.min(ROWS - 1, row));
      var col = ((Math.round(lon + 180) % COLS) + COLS) % COLS;
      var at = row * COLS + col;
      var col4 = colorFor(c[2]);
      if (col4.a <= 0) return;
      if (best[at] === undefined) count++;
      else if (best[at] >= c[2]) return;
      best[at] = c[2];
      px[at * 4] = Math.round(col4.r * 255);
      px[at * 4 + 1] = Math.round(col4.g * 255);
      px[at * 4 + 2] = Math.round(col4.b * 255);
      px[at * 4 + 3] = Math.round(col4.a * 255);
    });
    return {
      north: !!north, width: COLS, height: ROWS, pixels: px, count: count,
      rect: north ? [-180, EDGE_LAT, 180, 90] : [-180, -90, 180, -EDGE_LAT]
    };
  }

  // Highest chance per hemisphere: the server's figures (taken over the whole
  // grid) when present, else the drawn cells'.
  function hemisphereMax(data, cells) {
    var h = data && data.hemisphere_max;
    if (h && finite(h.north) && finite(h.south)) return { north: h.north, south: h.south };
    var north = 0;
    var south = 0;
    (cells || []).forEach(function (c) {
      if (c[1] > 0) north = Math.max(north, c[2]);
      else if (c[1] < 0) south = Math.max(south, c[2]);
    });
    return { north: north, south: south };
  }

  // The chance at a point: the strongest drawn cell within a degree of it (the
  // OVATION grid is one degree), or 0 where nothing at or above the threshold is.
  function chanceAt(cells, lat, lon) {
    if (!finite(lat) || !finite(lon)) return 0;
    var best = 0;
    (cells || []).forEach(function (c) {
      var dlon = Math.abs(((c[0] - lon + 540) % 360) - 180);
      if (Math.abs(c[1] - lat) <= 1 && dlon <= 1 && c[2] > best) best = c[2];
    });
    return best;
  }

  function latLonText(lat, lon) {
    return Math.abs(lat).toFixed(1) + (lat >= 0 ? ' N' : ' S') + ', ' +
      Math.abs(lon).toFixed(1) + (lon >= 0 ? ' E' : ' W');
  }

  function withCommas(n) {
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  // '2026-11-19T12:30:00Z' -> '12:30 UTC'
  function shortTime(s) {
    var m = /T(\d{2}):(\d{2})/.exec(typeof s === 'string' ? s : '');
    return m ? m[1] + ':' + m[2] + ' UTC' : '';
  }

  function intelLine(data, cells, min) {
    var h = hemisphereMax(data, cells);
    var line = 'Aurora oval (NOAA SWPC OVATION): max ' + Math.round(h.north) + '% north, ' +
      Math.round(h.south) + '% south; ' + withCommas((cells || []).length) + ' cells at ' +
      clampMin(min) + '% or more';
    var t = shortTime(data && data.forecast_time);
    if (t) line += ', forecast ' + t;
    if (data && data.stale) line += ' (stale: last good copy, SWPC did not answer)';
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

  function cssRgba(c, alpha) {
    return 'rgba(' + Math.round(c.r * 255) + ',' + Math.round(c.g * 255) + ',' +
      Math.round(c.b * 255) + ',' + alpha + ')';
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      enabled: false, min: DEFAULT_MIN, layer: null, timer: null, seq: 0, count: 0,
      lastLine: '', failed: false, cells: [], data: null, staticNoted: false, root: null, statusEl: null,
      minSelect: null
    };

    function note(msg) {
      try { if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem(msg); } catch (e) {
        /* keep going */
      }
    }

    function setStatus(msg) { if (m.statusEl) m.statusEl.textContent = msg; }

    function setCount(n) {
      m.count = n;
      try {
        var doc = global.document;
        var box = doc && typeof doc.getElementById === 'function' ? doc.getElementById(COUNT_ID)
          : null;
        if (box) box.textContent = String(n);
      } catch (e) { /* keep going */ }
    }

    // -- panel ----------------------------------------------------------

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      m.root = el('div', 'panel');
      var body = el('div', 'panel-body');
      m.statusEl = el('div', 'aurora-status', ctx.isStatic
        ? 'The aurora forecast needs the live server.'
        : 'Turn on the aurora layer to load the NOAA SWPC OVATION forecast.');
      var legend = el('div', 'aurora-legend');
      legend.style.display = 'flex';
      legend.style.gap = '6px';
      legend.style.fontSize = '10px';
      legend.style.margin = '4px 0';
      [10, 30, 50].forEach(function (p) {
        var item = el('span', 'aurora-key');
        var swatch = el('span', 'aurora-swatch');
        swatch.style.display = 'inline-block';
        swatch.style.width = '10px';
        swatch.style.height = '10px';
        swatch.style.marginRight = '3px';
        swatch.style.background = cssRgba(colorFor(p), 0.9);
        item.appendChild(swatch);
        item.appendChild(el('span', null, p + '%'));
        legend.appendChild(item);
      });
      var label = el('label', 'aurora-min', 'Min chance ');
      m.minSelect = el('select', 'aurora-min-select');
      MIN_CHOICES.forEach(function (p) {
        var opt = el('option', null, p + '%');
        opt.value = String(p);
        if (p === m.min) opt.selected = true;
        m.minSelect.appendChild(opt);
      });
      m.minSelect.value = String(m.min);
      m.minSelect.addEventListener('change', function () { setMin(m.minSelect.value); });
      label.appendChild(m.minSelect);
      body.appendChild(m.statusEl);
      body.appendChild(legend);
      body.appendChild(label);
      m.root.appendChild(el('h3', null, PANEL_TITLE));
      m.root.appendChild(body);
      ctx.mount.appendChild(m.root);
    }

    // -- globe layer ----------------------------------------------------

    function ensureLayer() {
      if (!m.layer) m.layer = viewer.scene.primitives.add(new C.PrimitiveCollection());
      return m.layer;
    }

    function clearLayer() {
      if (m.layer) {
        m.layer.removeAll();
        m.layer.show = false;
      }
      setCount(0);
    }

    // The band's texels scaled up 4x with a blur. The grid is drawn three times
    // side by side so the blur wraps across 180 degrees instead of fading there.
    function bandCanvas(band) {
      var doc = global.document;
      var small = doc.createElement('canvas');
      small.width = band.width;
      small.height = band.height;
      var sctx = small.getContext('2d');
      var img = sctx.createImageData(band.width, band.height);
      img.data.set(band.pixels);
      sctx.putImageData(img, 0, 0);
      var big = doc.createElement('canvas');
      big.width = band.width * UPSCALE;
      big.height = band.height * UPSCALE;
      var bctx = big.getContext('2d');
      bctx.imageSmoothingEnabled = true;
      bctx.imageSmoothingQuality = 'high';
      bctx.filter = 'blur(' + BLUR_PX + 'px)';
      for (var k = -1; k <= 1; k++) {
        bctx.drawImage(small, k * big.width, 0, big.width, big.height);
      }
      return big;
    }

    function bandPrimitive(band) {
      var r = band.rect;
      return new C.Primitive({
        geometryInstances: new C.GeometryInstance({
          geometry: new C.RectangleGeometry({
            rectangle: C.Rectangle.fromDegrees(r[0], r[1], r[2], r[3]),
            height: HEIGHT_M,
            vertexFormat: C.EllipsoidSurfaceAppearance.VERTEX_FORMAT
          }),
          // sdaBackground: the page's click handler looks under the oval first, so
          // an aircraft or a quake beneath it still opens its own card.
          id: { type: 'aurora', hemisphere: band.north ? 'north' : 'south', sdaBackground: true,
                sdaPick: function (pos) { describeAt(band.north, pos); } }
        }),
        // flat: aurora is its own light. With sun shading the oval would go dark
        // in the polar night, which is exactly when and where it is seen.
        appearance: new C.EllipsoidSurfaceAppearance({
          aboveGround: true,
          translucent: true,
          flat: true,
          // An Image material counts as opaque while its colour alpha is 1, which
          // puts this shell in the opaque pass, and every translucent point and
          // label under it (camera clusters, counts) then vanished (2026-11-19).
          // Alpha just under 1 keeps it in the translucent pass.
          material: C.Material.fromType('Image', {
            image: bandCanvas(band),
            color: new C.Color(1.0, 1.0, 1.0, MATERIAL_ALPHA)
          })
        }),
        asynchronous: false
      });
    }

    // What a click on the oval says: the chance under the cursor and the forecast.
    function describeAt(north, pos) {
      var lat = null;
      var lon = null;
      try {
        var hit = pos ? viewer.camera.pickEllipsoid(pos, viewer.scene.globe.ellipsoid) : null;
        if (hit) {
          var g = C.Cartographic.fromCartesian(hit);
          lat = g.latitude * 180 / Math.PI;
          lon = g.longitude * 180 / Math.PI;
        }
      } catch (e) { lat = null; }
      var h = hemisphereMax(m.data, m.cells);
      var rows = [];
      if (lat !== null) {
        var p = chanceAt(m.cells, lat, lon);
        rows.push(['CHANCE HERE', p > 0 ? Math.round(p) + '%' : 'under ' + m.min + '%']);
        rows.push(['POSITION', latLonText(lat, lon)]);
      }
      rows.push(['MAX ' + (north ? 'NORTH' : 'SOUTH'), Math.round(north ? h.north : h.south) + '%']);
      var t = shortTime(m.data && m.data.forecast_time);
      if (t) rows.push(['FORECAST', t]);
      rows.push(['MEANING', 'Chance of aurora overhead in the next 30 to 90 minutes']);
      rows.push(['SOURCE', 'NOAA SWPC OVATION']);
      var card = { name: 'Aurora oval, ' + (north ? 'northern' : 'southern') + ' hemisphere',
                   type: 'aurora forecast', rows: rows };
      if (typeof ctx.showInfoCard === 'function') ctx.showInfoCard(card);
      else note(card.name + ': ' + rows.map(function (r) { return r[0] + ' ' + r[1]; }).join(', '));
      return card;
    }

    function draw(cells) {
      var layer = ensureLayer();
      layer.removeAll();
      var drawn = 0;
      [true, false].forEach(function (north) {
        var band = rasterBand(cells, north);
        if (!band.count) return;
        layer.add(bandPrimitive(band));
        drawn += band.count;
      });
      layer.show = true;
      setCount(drawn);
      return drawn;
    }

    function load() {
      if (!m.enabled || ctx.isStatic) return Promise.resolve(null);
      var seq = ++m.seq;
      var min = m.min;
      setStatus('Loading the OVATION forecast...');
      return global.fetch(auroraUrl(api, min)).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (data) {
        if (seq !== m.seq || !m.enabled) return null;
        var cells = cleanCells(data && data.cells, min);
        m.cells = cells;
        m.data = data;
        var drawn = draw(cells);
        m.failed = false;
        var line = intelLine(data, cells, min);
        setStatus(line);
        if (line !== m.lastLine) {
          m.lastLine = line;
          note(line);
        }
        return drawn;
      }).catch(function () {
        if (seq !== m.seq || !m.enabled) return null;
        setStatus('The OVATION forecast did not load; retrying in ten minutes.');
        if (!m.failed) {
          m.failed = true;
          note('Aurora oval: NOAA SWPC OVATION feed unavailable');
        }
        return null;
      });
    }

    function setEnabled(on) {
      on = !!on;
      if (ctx.isStatic) {
        if (on && !m.staticNoted) {
          m.staticNoted = true;
          note('Aurora oval needs the live server (NOAA SWPC OVATION).');
        }
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (on) {
        m.lastLine = '';
        m.failed = false;
        load();
        m.timer = global.setInterval(load, REFRESH_MS);
      } else {
        if (m.timer) {
          global.clearInterval(m.timer);
          m.timer = null;
        }
        m.seq++;
        clearLayer();
        setStatus('');
      }
    }

    function setMin(v) {
      var n = clampMin(v);
      if (m.minSelect) m.minSelect.value = String(n);
      if (n === m.min) return;
      m.min = n;
      if (m.enabled) load();
    }

    buildPanel();
    m.setEnabled = setEnabled;
    m.setMin = setMin;
    m.load = load;
    m.describeAt = describeAt;
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

  global.SDAAurora = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    setMin: function (v) { if (current) current.setMin(v); },
    refresh: function () { return current ? current.load() : Promise.resolve(null); },
    isEnabled: function () { return !!(current && current.enabled); },
    count: function () { return current ? current.count : 0; },
    minProb: function () { return current ? current.min : DEFAULT_MIN; },
    REFRESH_MS: REFRESH_MS,
    DEFAULT_MIN: DEFAULT_MIN,
    HEIGHT_M: HEIGHT_M,
    EDGE_LAT: EDGE_LAT,
    UPSCALE: UPSCALE,
    RAMP: RAMP,
    clampMin: clampMin,
    alphaFor: alphaFor,
    colorFor: colorFor,
    auroraUrl: auroraUrl,
    cleanCells: cleanCells,
    rasterBand: rasterBand,
    hemisphereMax: hemisphereMax,
    intelLine: intelLine,
    chanceAt: chanceAt,
    describeAt: function (north, pos) { return current ? current.describeAt(north, pos) : null; },
    shortTime: shortTime
  };
})(typeof window !== 'undefined' ? window : this);
