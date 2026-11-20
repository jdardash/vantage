// Live global clouds on the Vantage globe, Zoom Earth style: geostationary
// satellite imagery laid over the basemap, with an opacity control, refreshed
// as new frames come out.
//
// Sources (all keyless, all send Access-Control-Allow-Origin: *):
//   mosaic    NOAA Global Mosaic of Geostationary Satellite Imagery (GMGSI),
//             long-wave infrared, hourly, through the nowCOAST WMS. The only
//             keyless global IR: it stitches GOES-East, GOES-West, Himawari
//             and Meteosat, so Europe, Africa and the Indian Ocean are covered.
//   geo       NASA GIBS ABI / AHI band 13 clean infrared from GOES-East,
//             GOES-West and Himawari, 10-minute frames. Sharper and fresher,
//             but nothing over Europe and Africa (no Meteosat in GIBS).
//   geocolor  NASA GIBS GOES-East and GOES-West GeoColor: true colour by day,
//             infrared with city lights at night. A full image, not clouds
//             only, so it covers the basemap where it reaches.
// GIBS tiles come from the Web Mercator endpoint (epsg3857), the grid Cesium's
// default tiling scheme expects; see the gibsImagery() note in worldview.html.
//
// Infrared sources are turned into clouds only, tile by tile on a 2D canvas:
// brightness (cold cloud tops are white in IR) becomes opacity, colour becomes
// near white, so warm ground and sea drop out. Enhanced colour on the coldest
// tops counts as fully cloudy. Cold ice sheets in polar winter read as cloud,
// as they do in any single-band IR picture.
//
// The imagery is live, not tied to the simulation clock; the panel says so and
// gives the frame time. Each refresh adds the new layer above the old one and
// removes the old one a few seconds later, so tiles never flash empty.
// Text is only ever set with textContent.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Live clouds';
  var STYLE_ID = 'sda-clouds-style';
  var GIBS = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/';
  var NOWCOAST = 'https://nowcoast.noaa.gov/geoserver/satellite/wms';
  var CHECK_MS = 60000;
  var timing = { swapMs: 8000 };
  var DEFAULT_OPACITY = 0.7;
  var CLOUD_LO = 0.42;
  var CLOUD_HI = 0.78;
  var COLOURED = 0.16;

  var SOURCES = {
    mosaic: {
      label: 'Global IR mosaic (NOAA GMGSI, hourly)',
      short: 'NOAA GMGSI global infrared',
      periodMs: 15 * 60000, lagMs: 0, cloudsOnly: true,
      layers: [{ kind: 'wms', layer: 'global_longwave_imagery_mosaic', maximumLevel: 5,
        credit: 'NOAA GMGSI (GOES, Himawari, Meteosat) via nowCOAST' }]
    },
    geo: {
      label: 'Geostationary IR (GOES-E, GOES-W, Himawari, 10 min)',
      short: 'GOES-East, GOES-West and Himawari infrared',
      periodMs: 10 * 60000, lagMs: 0, cloudsOnly: true,
      layers: [
        { kind: 'gibs', layer: 'GOES-East_ABI_Band13_Clean_Infrared',
          tms: 'GoogleMapsCompatible_Level6', maximumLevel: 6, credit: 'NOAA GOES-East via NASA GIBS' },
        { kind: 'gibs', layer: 'GOES-West_ABI_Band13_Clean_Infrared',
          tms: 'GoogleMapsCompatible_Level6', maximumLevel: 6, credit: 'NOAA GOES-West via NASA GIBS' },
        { kind: 'gibs', layer: 'Himawari_AHI_Band13_Clean_Infrared',
          tms: 'GoogleMapsCompatible_Level6', maximumLevel: 6, credit: 'JMA Himawari via NASA GIBS' }
      ]
    },
    geocolor: {
      label: 'GeoColor (GOES-E, GOES-W, 10 min, true colour by day)',
      short: 'GOES-East and GOES-West GeoColor',
      periodMs: 10 * 60000, lagMs: 0, cloudsOnly: false,
      layers: [
        { kind: 'gibs', layer: 'GOES-East_ABI_GeoColor', tms: 'GoogleMapsCompatible_Level7',
          maximumLevel: 7, credit: 'NOAA GOES-East GeoColor via NASA GIBS' },
        { kind: 'gibs', layer: 'GOES-West_ABI_GeoColor', tms: 'GoogleMapsCompatible_Level7',
          maximumLevel: 7, credit: 'NOAA GOES-West GeoColor via NASA GIBS' }
      ]
    }
  };
  var ORDER = ['mosaic', 'geo', 'geocolor'];

  var clockFn = function () { return Date.now(); };

  // ---------------------------------------------------------------------
  // Pure helpers
  // ---------------------------------------------------------------------

  function slotFor(nowMs, periodMs) { return Math.floor(nowMs / periodMs) * periodMs; }

  // GIBS answers "default" with its newest frame; the slot only changes the
  // cache key, so a refresh asks again instead of reusing the browser copy.
  function gibsUrl(spec, slot) {
    return GIBS + spec.layer + '/default/default/' + spec.tms + '/{z}/{y}/{x}.png?_=' + slot;
  }

  function wmsUrl(spec, slot) {
    return NOWCOAST + '?service=WMS&version=1.3.0&request=GetMap&layers=' + spec.layer +
      '&styles=&format=image/png&transparent=true&crs=EPSG:3857&width=256&height=256' +
      '&bbox={westProjected},{southProjected},{eastProjected},{northProjected}&_=' + slot;
  }

  function urlFor(spec, slot) { return spec.kind === 'wms' ? wmsUrl(spec, slot) : gibsUrl(spec, slot); }

  function smooth(x) { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); }

  // One RGBA pixel of an infrared tile to a cloud pixel, in place.
  function cloudPixels(data, lo, hi) {
    lo = lo === undefined ? CLOUD_LO : lo;
    hi = hi === undefined ? CLOUD_HI : hi;
    for (var i = 0; i < data.length; i += 4) {
      var r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
      if (a === 0) continue;
      var mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      var l = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
      if ((mx - mn) / 255 > COLOURED) l = 1;
      var k = smooth((l - lo) / (hi - lo));
      var shade = Math.round(205 + 50 * k);
      data[i] = shade;
      data[i + 1] = shade;
      data[i + 2] = Math.min(255, shade + 6);
      data[i + 3] = Math.round(a * k);
    }
    return data;
  }

  function utcClock(ms) {
    var d = new Date(ms);
    return ('0' + d.getUTCHours()).slice(-2) + ':' + ('0' + d.getUTCMinutes()).slice(-2) + ' UTC';
  }

  // ---------------------------------------------------------------------
  // Imagery: a provider that hands Cesium processed tiles
  // ---------------------------------------------------------------------

  function processTile(image, lo, hi) {
    var doc = global.document;
    try {
      var w = image.width, h = image.height;
      if (!w || !h) return image;
      var canvas = doc.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      var c2 = canvas.getContext('2d', { willReadFrequently: true });
      if (!c2) return image;
      c2.drawImage(image, 0, 0);
      var px = c2.getImageData(0, 0, w, h);
      cloudPixels(px.data, lo, hi);
      c2.putImageData(px, 0, 0);
      return canvas;
    } catch (e) {
      return image;  // a tainted or odd tile is drawn as it came
    }
  }

  function CloudProvider(inner, opts) {
    this._inner = inner;
    this._lo = opts.lo;
    this._hi = opts.hi;
    this.errorEvent = inner.errorEvent;
    this.credit = inner.credit;
  }
  ['tilingScheme', 'rectangle', 'tileWidth', 'tileHeight', 'maximumLevel', 'minimumLevel',
    'tileDiscardPolicy', 'proxy', 'url'].forEach(function (k) {
    Object.defineProperty(CloudProvider.prototype, k, {
      get: function () { return this._inner[k]; }
    });
  });
  Object.defineProperty(CloudProvider.prototype, 'hasAlphaChannel', { get: function () { return true; } });
  Object.defineProperty(CloudProvider.prototype, 'ready', { get: function () { return true; } });
  CloudProvider.prototype.getTileCredits = function (x, y, level) {
    return typeof this._inner.getTileCredits === 'function'
      ? this._inner.getTileCredits(x, y, level) : undefined;
  };
  CloudProvider.prototype.requestImage = function (x, y, level, request) {
    var self = this;
    var p = this._inner.requestImage(x, y, level, request);
    if (!p || typeof p.then !== 'function') return p;  // throttled: Cesium asks again
    return p.then(function (img) { return processTile(img, self._lo, self._hi); });
  };
  CloudProvider.prototype.pickFeatures = function () { return undefined; };

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var CSS = [
    '.sdaclouds-row { display: flex; align-items: center; gap: 6px; margin: 4px 0; font-size: 10px; }',
    '.sdaclouds-row label { display: flex; align-items: center; gap: 6px; cursor: pointer; }',
    '.sdaclouds-row select { background: rgba(0,0,0,0.6); color: inherit; border: 1px solid rgba(255,176,0,0.3); font: inherit; font-size: 10px; max-width: 100%; }',
    '.sdaclouds-row input[type=range] { flex: 1; }',
    '.sdaclouds-status { font-size: 9px; color: var(--hud-dim, #7a5500); margin-top: 6px; min-height: 2.4em; }'
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
    var m = {
      enabled: false, source: 'mosaic', opacity: DEFAULT_OPACITY, lo: CLOUD_LO, hi: CLOUD_HI,
      layers: [], slot: null, errors: 0, timer: null, retiring: []
    };

    function imagery() { return viewer.imageryLayers; }

    function buildLayers(slot) {
      var src = SOURCES[m.source];
      var out = [];
      src.layers.forEach(function (spec) {
        var inner = new C.UrlTemplateImageryProvider({
          url: urlFor(spec, slot), maximumLevel: spec.maximumLevel,
          credit: new C.Credit(spec.credit)
        });
        if (inner.errorEvent && inner.errorEvent.addEventListener) {
          inner.errorEvent.addEventListener(function () { m.errors += 1; });
        }
        var provider = src.cloudsOnly ? new CloudProvider(inner, { lo: m.lo, hi: m.hi }) : inner;
        var layer = new C.ImageryLayer(provider);
        layer.alpha = m.opacity;
        out.push(layer);
      });
      return out;
    }

    function removeLayers(list) {
      var coll = imagery();
      list.forEach(function (layer) {
        try { coll.remove(layer, true); } catch (e) { /* already gone */ }
      });
    }

    // New frame: add the new layers on top, retire the old ones shortly after.
    function load(force) {
      if (!m.enabled || !imagery()) return;
      var src = SOURCES[m.source];
      var slot = slotFor(clockFn() - src.lagMs, src.periodMs);
      if (!force && slot === m.slot && m.layers.length) return;
      var old = m.layers;
      m.slot = slot;
      m.errors = 0;
      m.layers = buildLayers(slot);
      var coll = imagery();
      m.layers.forEach(function (layer) { coll.add(layer); });
      if (old.length) {
        m.retiring.push(old);
        global.setTimeout(function () {
          var at = m.retiring.indexOf(old);
          if (at >= 0) m.retiring.splice(at, 1);
          removeLayers(old);
        }, timing.swapMs);
      }
      setStatus();
    }

    function clearAll() {
      removeLayers(m.layers);
      m.retiring.forEach(removeLayers);
      m.retiring = [];
      m.layers = [];
      m.slot = null;
    }

    function check() {
      if (!m.enabled) return;
      load(false);
      setStatus();
    }

    function setEnabled(on) {
      m.enabled = !!on;
      if (m.enableBox) m.enableBox.checked = m.enabled;
      if (m.enabled) {
        load(true);
        if (!m.timer) m.timer = global.setInterval(check, CHECK_MS);
      } else {
        clearAll();
        if (m.timer) { global.clearInterval(m.timer); m.timer = null; }
      }
      setStatus();
    }

    function setSource(name) {
      if (!SOURCES[name]) return;
      m.source = name;
      if (m.sourceSel) m.sourceSel.value = name;
      if (m.enabled) { clearAll(); load(true); }
      setStatus();
    }

    function setOpacity(v) {
      var a = Math.max(0, Math.min(1, Number(v)));
      if (!isFinite(a)) return;
      m.opacity = a;
      m.layers.forEach(function (layer) { layer.alpha = a; });
      if (m.opacityInput) m.opacityInput.value = String(Math.round(a * 100));
      if (m.opacityVal) m.opacityVal.textContent = Math.round(a * 100) + '%';
    }

    function setStatus() {
      if (!m.statusEl) return;
      var src = SOURCES[m.source];
      if (!m.enabled) {
        m.statusEl.textContent = 'Off. Live imagery, not tied to the sim clock.';
        return;
      }
      var s = src.short + ', live (not the sim clock). Frame slot ' + utcClock(m.slot) +
        '; refreshes every ' + Math.round(src.periodMs / 60000) + ' min.';
      if (m.errors > 0) s += ' ' + m.errors + ' tiles failed to load.';
      m.statusEl.textContent = s;
    }

    function buildPanel() {
      var root = el(doc, 'div', 'panel sdaclouds');
      var h = el(doc, 'h3', '', PANEL_TITLE);
      root.appendChild(h);
      var body = el(doc, 'div', 'panel-body');
      root.appendChild(body);

      var r1 = el(doc, 'div', 'sdaclouds-row');
      var lab = el(doc, 'label', '');
      var box = el(doc, 'input', '');
      box.type = 'checkbox';
      box.checked = m.enabled;
      box.addEventListener('change', function () { setEnabled(!!box.checked); });
      lab.appendChild(box);
      lab.appendChild(doc.createTextNode('Show live clouds'));
      r1.appendChild(lab);
      body.appendChild(r1);
      m.enableBox = box;

      var r2 = el(doc, 'div', 'sdaclouds-row');
      var sel = el(doc, 'select', '');
      ORDER.forEach(function (k) {
        var o = el(doc, 'option', '', SOURCES[k].label);
        o.value = k;
        sel.appendChild(o);
      });
      sel.value = m.source;
      sel.addEventListener('change', function () { setSource(sel.value); });
      r2.appendChild(sel);
      body.appendChild(r2);
      m.sourceSel = sel;

      var r3 = el(doc, 'div', 'sdaclouds-row');
      r3.appendChild(el(doc, 'span', '', 'Opacity'));
      var range = el(doc, 'input', '');
      range.type = 'range';
      range.min = '0';
      range.max = '100';
      range.value = String(Math.round(m.opacity * 100));
      range.addEventListener('input', function () { setOpacity(Number(range.value) / 100); });
      r3.appendChild(range);
      m.opacityVal = el(doc, 'span', '', Math.round(m.opacity * 100) + '%');
      r3.appendChild(m.opacityVal);
      body.appendChild(r3);
      m.opacityInput = range;

      m.statusEl = el(doc, 'div', 'sdaclouds-status', '');
      body.appendChild(m.statusEl);
      h.addEventListener('click', function (evt) {
        if (evt && evt.target && evt.target.tagName === 'INPUT') return;
        root.classList.toggle('collapsed');
      });
      return root;
    }

    if (doc && !doc.getElementById(STYLE_ID)) {
      var s = doc.createElement('style');
      s.id = STYLE_ID;
      s.textContent = CSS;
      (doc.head || doc.body).appendChild(s);
    }
    m.root = buildPanel();
    setStatus();

    m.setEnabled = setEnabled;
    m.setSource = setSource;
    m.setOpacity = setOpacity;
    m.check = check;
    m.setThreshold = function (lo, hi) {
      if (!(lo >= 0 && hi <= 1 && hi > lo)) return;
      m.lo = lo;
      m.hi = hi;
      if (m.enabled && SOURCES[m.source].cloudsOnly) load(true);
    };
    m.state = function () {
      return { enabled: m.enabled, source: m.source, opacity: m.opacity, slot: m.slot,
        layers: m.layers.length, retiring: m.retiring.length, errors: m.errors };
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

  global.SDAClouds = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    setSource: function (name) { if (current) current.setSource(name); },
    setOpacity: function (v) { if (current) current.setOpacity(v); },
    setThreshold: function (lo, hi) { if (current) current.setThreshold(lo, hi); },
    state: function () { return current ? current.state() : null; },
    _check: function () { if (current) current.check(); },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    _setTiming: function (t) { if (t && t.swapMs >= 0) timing.swapMs = t.swapMs; },
    SOURCES: SOURCES,
    ORDER: ORDER,
    CloudProvider: CloudProvider,
    slotFor: slotFor,
    urlFor: urlFor,
    cloudPixels: cloudPixels,
    utcClock: utcClock
  };
})(typeof window !== 'undefined' ? window : this);
