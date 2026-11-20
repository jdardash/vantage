// Dated NASA imagery and natural events on the WorldView globe.
//
// Imagery: NASA GIBS daily true colour (MODIS Terra and Aqua, VIIRS NOAA-20 and
// SNPP) and the VIIRS Day/Night Band, drawn as one ImageryLayer above the
// basemap. Its date follows the simulation clock at day granularity, the way
// NASA Worldview's date slider does: the layer is rebuilt only when the UTC day
// on the clock changes, never per frame. GIBS fills a day in as the passes
// arrive, so while the clock is live the layer shows yesterday, and the panel
// says so. The browser fetches tiles straight from GIBS (keyless, CORS open), so
// this half works in the static demo build too.
//
// Compare: a second layer for another date. The clock's date is drawn on the
// right of a draggable divider and the compare date on the left, using Cesium's
// ImageryLayer.splitDirection and scene.splitPosition (Apache 2.0, already
// loaded). The divider is a DOM bar over the globe; drag it, or focus it and
// use the arrow keys.
//
// Events: GET /api/eonet (sda/routes/eonet.py), NASA EONET v3 open events of the
// last thirty days, one point each, coloured by category. A click opens a small
// card (title, category, date, magnitude, source links) and flies there. The
// static demo build has no server, so the events half says so and makes no
// request at all.
//
// Sources, all ideas rather than code: the GIBS layer ids, tile matrix sets and
// first days are from the GIBS WMTS GetCapabilities (epsg3857/best, read
// 2026-11-19); the A|B swipe with a date tag on each side, and "jump the
// imagery to an event's day", from NASA Worldview's compare mode and events tab
// (nasa-gibs/worldview, NOSA 1.3, no code read or copied).
//
// Frames: geodetic degrees only. Text is only ever set with textContent.
//
// Page wiring: list it in EXTENSIONS (it builds its panel in ctx.mount) and call
// SDANasaImagery.setEventsEnabled(on) from the events layer toggle. A span with
// id count-eonet, when the page has one, shows the number of event markers, and
// a checkbox with id layer-eonet is kept in step with the panel's own toggle.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'NASA imagery and events';
  var COUNT_ID = 'count-eonet';
  var PAGE_TOGGLE_ID = 'layer-eonet';
  var GIBS_ROOT = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/';
  var DAY_MS = 86400000;
  var LIVE_WINDOW_MS = 15 * 60 * 1000; // a clock this close to the wall clock is live
  var SYNC_MS = 1000;                  // the clock tick checks the day at most this often
  var COMPARE_OFFSET_DAYS = 7;         // the compare date starts a week before the clock's
  var SPLIT_MIN = 0.02;
  var SPLIT_MAX = 0.98;
  var SPLIT_STEP = 0.02;
  var EVENT_DAYS = 30;
  var EVENT_REFRESH_MS = 30 * 60 * 1000;
  var EVENT_PIXEL_SIZE = 10;
  var FLY_HEIGHT_M = 1500000;
  var MAX_EVENTS = 5000;

  // GIBS layers, epsg3857 "best". level is the GoogleMapsCompatible_LevelN tile
  // matrix set and start the first day GIBS holds; both from GetCapabilities.
  var LAYERS = [
    { id: 'modis_terra', label: 'MODIS Terra true colour',
      gibs: 'MODIS_Terra_CorrectedReflectance_TrueColor', ext: 'jpeg', level: 9,
      start: '2000-02-24', night: false },
    { id: 'modis_aqua', label: 'MODIS Aqua true colour',
      gibs: 'MODIS_Aqua_CorrectedReflectance_TrueColor', ext: 'jpeg', level: 9,
      start: '2002-07-03', night: false },
    { id: 'viirs_noaa20', label: 'VIIRS NOAA-20 true colour',
      gibs: 'VIIRS_NOAA20_CorrectedReflectance_TrueColor', ext: 'jpeg', level: 9,
      start: '2018-01-05', night: false },
    { id: 'viirs_snpp', label: 'VIIRS SNPP true colour',
      gibs: 'VIIRS_SNPP_CorrectedReflectance_TrueColor', ext: 'jpeg', level: 9,
      start: '2015-11-24', night: false },
    { id: 'viirs_night', label: 'VIIRS Day/Night Band (night)',
      gibs: 'VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance', ext: 'png', level: 8,
      start: '2024-03-25', night: true }
  ];
  var DEFAULT_LAYER = 'modis_terra';

  // EONET v3 category ids -> marker colour.
  var CATEGORY_COLORS = {
    wildfires: '#ff5722',
    severeStorms: '#40c4ff',
    volcanoes: '#ff1744',
    floods: '#2979ff',
    seaLakeIce: '#b2ebf2',
    dustHaze: '#d7a86e',
    drought: '#ffb300',
    earthquakes: '#ffd600',
    landslides: '#a1887f',
    snow: '#f5f5f5',
    tempExtremes: '#ff9100',
    waterColor: '#1de9b6',
    manmade: '#b0bec5'
  };
  var OTHER_COLOR = '#cfd8dc';
  var LEGEND = [
    ['wildfires', 'Fire'], ['severeStorms', 'Storm'], ['volcanoes', 'Volcano'],
    ['floods', 'Flood'], ['seaLakeIce', 'Ice'], ['dustHaze', 'Dust']
  ];

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the tests)
  // ---------------------------------------------------------------------

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // A Date or epoch milliseconds -> epoch milliseconds, NaN when unreadable.
  function toMs(t) {
    if (finite(t)) return t;
    if (t && typeof t.getTime === 'function') {
      var ms = t.getTime();
      return finite(ms) ? ms : NaN;
    }
    return NaN;
  }

  // The UTC day 'YYYY-MM-DD' of a Date or epoch milliseconds, null when unreadable.
  function isoDay(t) {
    var ms = toMs(t);
    if (!finite(ms)) return null;
    var d = new Date(ms);
    var y = d.getUTCFullYear();
    if (!(y >= 1000 && y <= 9999)) return null; // also refuses an Invalid Date's NaN
    return y + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
  }

  // 'YYYY-MM-DD' -> epoch milliseconds at 00:00 UTC, NaN unless a real calendar day.
  function dayMs(s) {
    if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return NaN;
    var ms = Date.UTC(Number(s.slice(0, 4)), Number(s.slice(5, 7)) - 1, Number(s.slice(8, 10)));
    return isoDay(ms) === s ? ms : NaN;
  }

  function validDay(s) { return finite(dayMs(s)); }

  function addDays(day, n) {
    var ms = dayMs(day);
    return finite(ms) && finite(n) ? isoDay(ms + Math.round(n) * DAY_MS) : null;
  }

  function findLayer(id) {
    for (var i = 0; i < LAYERS.length; i++) if (LAYERS[i].id === id) return LAYERS[i];
    return null;
  }

  function layerById(id) { return findLayer(id) || findLayer(DEFAULT_LAYER); }

  function isLive(sim, wall) {
    var s = toMs(sim);
    var w = toMs(wall);
    return finite(s) && finite(w) && Math.abs(s - w) <= LIVE_WINDOW_MS;
  }

  // Which GIBS day to show for the clock at `sim`, with the wall clock at `wall`.
  // reason: 'live' (yesterday, because today is still filling in), 'today' (the
  // clock was moved to today: shown, partly covered), 'clock' (the clock's own
  // day), 'future' (past the newest imagery: yesterday) or 'before-start' (before
  // the layer's first day: that first day).
  function pickDate(sim, wall, layer) {
    layer = layer || layerById(DEFAULT_LAYER);
    var wallMs = toMs(wall);
    if (!finite(wallMs)) wallMs = Date.now();
    var today = isoDay(wallMs);
    var yesterday = addDays(today, -1);
    var simDay = isoDay(sim);
    var day;
    var reason;
    if (simDay === null || isLive(sim, wallMs)) {
      day = yesterday;
      reason = 'live';
    } else if (simDay > today) {
      day = yesterday;
      reason = 'future';
    } else {
      day = simDay;
      reason = simDay === today ? 'today' : 'clock';
    }
    if (day < layer.start) {
      day = layer.start;
      reason = 'before-start';
    }
    return { day: day, reason: reason, simDay: simDay };
  }

  function reasonText(pick, layer) {
    var r = pick && pick.reason;
    if (r === 'live') return "live clock, so yesterday's: today's passes are still arriving";
    if (r === 'today') return 'today on the clock; only partly covered until the last passes';
    if (r === 'future') return "the clock is past the newest imagery, so yesterday's";
    if (r === 'before-start') {
      return 'the clock is before this layer begins, so its first day';
    }
    return 'the day on the clock';
  }

  // A compare date the layer can show: past the newest day -> yesterday, before
  // the first day -> the first day, anything unreadable -> null.
  function clampDay(day, layer, wall) {
    if (!validDay(day)) return null;
    layer = layer || layerById(DEFAULT_LAYER);
    var wallMs = toMs(wall);
    var latest = addDays(isoDay(finite(wallMs) ? wallMs : Date.now()), -1);
    if (day > latest) day = latest;
    if (day < layer.start) day = layer.start;
    return day;
  }

  function tileUrl(layer, day) {
    return GIBS_ROOT + layer.gibs + '/default/' + day + '/GoogleMapsCompatible_Level' +
      layer.level + '/{z}/{y}/{x}.' + layer.ext;
  }

  function clampSplit(x) {
    if (!finite(x)) return 0.5;
    return Math.max(SPLIT_MIN, Math.min(SPLIT_MAX, x));
  }

  // A pointer's clientX over the globe's box -> split fraction, null when unreadable.
  function splitFromPointer(clientX, rect) {
    if (!finite(clientX) || !rect || !finite(rect.width) || rect.width <= 0) return null;
    var left = finite(rect.left) ? rect.left : 0;
    return clampSplit((clientX - left) / rect.width);
  }

  // Percent (number or string) -> alpha 0..1; anything unreadable is opaque.
  function opacityFrom(v) {
    var n = (typeof v === 'string' && v.trim() !== '') ? Number(v) : v;
    if (!finite(n)) return 1;
    return Math.max(0, Math.min(100, n)) / 100;
  }

  function eventsUrl(api, days) {
    var d = finite(days) && days >= 1 ? Math.min(365, Math.round(days)) : EVENT_DAYS;
    return (api || '') + '/api/eonet?days=' + d + '&status=open';
  }

  // An absolute http(s) URL, else null. The server checks this too; the browser
  // checks again because the link becomes an href.
  function safeUrl(u) {
    if (typeof u !== 'string' || u.length > 2048) return null;
    if (/[\s\u0000-\u001f\u007f]/.test(u) || !/^https?:\/\/[^/?#]/i.test(u)) return null;
    if (typeof global.URL === 'function') {
      try {
        var p = new global.URL(u);
        if ((p.protocol !== 'http:' && p.protocol !== 'https:') || !p.hostname) return null;
      } catch (e) {
        return null;
      }
    }
    return u;
  }

  function str(v, max) { return typeof v === 'string' ? v.slice(0, max) : ''; }

  // The answer's events as drawable records; anything malformed is skipped.
  function cleanEvents(list) {
    var out = [];
    if (!Array.isArray(list)) return out;
    var seen = Object.create(null);
    for (var i = 0; i < list.length && out.length < MAX_EVENTS; i++) {
      var e = list[i];
      if (!e || typeof e !== 'object') continue;
      var id = str(e.id, 64);
      if (!id || seen[id]) continue;
      var pt = e.point;
      if (!Array.isArray(pt) || pt.length < 2) continue;
      var lon = pt[0];
      var lat = pt[1];
      if (!finite(lon) || !finite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90) {
        continue;
      }
      var cats = [];
      (Array.isArray(e.categories) ? e.categories : []).forEach(function (c) {
        if (cats.length >= 8 || !c || typeof c !== 'object') return;
        var cid = str(c.id, 64);
        if (cid) cats.push({ id: cid, title: str(c.title, 200) || cid });
      });
      var sources = [];
      (Array.isArray(e.sources) ? e.sources : []).forEach(function (s) {
        if (sources.length >= 8 || !s || typeof s !== 'object') return;
        var url = safeUrl(s.url);
        if (url) sources.push({ id: str(s.id, 64) || 'Source', url: url });
      });
      seen[id] = true;
      out.push({
        id: id, title: str(e.title, 200) || id, categories: cats, lon: lon, lat: lat,
        date: str(e.date, 40),
        magnitude: finite(e.magnitude_value) ? e.magnitude_value : null,
        unit: str(e.magnitude_unit, 32),
        observations: finite(e.observations) && e.observations >= 1
          ? Math.round(e.observations) : 1,
        sources: sources
      });
    }
    return out;
  }

  function categoryOf(ev) {
    return ev && ev.categories && ev.categories.length ? ev.categories[0]
      : { id: 'other', title: 'Other' };
  }

  function colorFor(catId) {
    return typeof catId === 'string' && own(CATEGORY_COLORS, catId) ? CATEGORY_COLORS[catId]
      : OTHER_COLOR;
  }

  // 2378.7 -> '2,378.7'; at most two decimals.
  function formatNumber(n) {
    if (!finite(n)) return '';
    var r = Math.round(Math.abs(n) * 100) / 100;
    var whole = Math.floor(r);
    var cents = Math.round((r - whole) * 100);
    var s = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    if (cents) s += '.' + (cents < 10 ? '0' + cents : String(cents).replace(/0$/, ''));
    return (n < 0 && r > 0 ? '-' : '') + s;
  }

  function magnitudeText(ev) {
    if (!ev || !finite(ev.magnitude)) return '';
    return formatNumber(ev.magnitude) + (ev.unit ? ' ' + ev.unit : '');
  }

  // '2026-11-19T12:00:00Z' -> '2026-11-19 12:00 UTC'
  function shortDateTime(s) {
    var m = /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?/.exec(typeof s === 'string' ? s : '');
    if (!m) return '';
    return m[1] + (m[2] ? ' ' + m[2] + ':' + m[3] + ' UTC' : '');
  }

  function eventsLine(events, data) {
    var counts = Object.create(null);
    var titles = Object.create(null);
    var order = [];
    (events || []).forEach(function (ev) {
      var c = categoryOf(ev);
      if (!counts[c.id]) { counts[c.id] = 0; titles[c.id] = c.title; order.push(c.id); }
      counts[c.id]++;
    });
    order.sort(function (a, b) { return counts[b] - counts[a]; });
    var days = data && finite(data.days) ? data.days : EVENT_DAYS;
    var n = (events || []).length;
    var line = 'NASA EONET: ' + n + ' open event' + (n === 1 ? '' : 's') + ' in the last ' +
      days + ' days';
    if (order.length) {
      line += ' (' + order.map(function (id) {
        return counts[id] + ' ' + String(titles[id]).toLowerCase();
      }).join(', ') + ')';
    }
    if (data && data.stale === true) line += ' (stale: last good copy, EONET did not answer)';
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

  function swatch(color) {
    var s = el('span', 'nasa-swatch');
    s.style.display = 'inline-block';
    s.style.width = '9px';
    s.style.height = '9px';
    s.style.borderRadius = '50%';
    s.style.marginRight = '3px';
    s.style.background = color;
    return s;
  }

  function byId(id) {
    try {
      var doc = global.document;
      return doc && typeof doc.getElementById === 'function' ? doc.getElementById(id) : null;
    } catch (e) {
      return null;
    }
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var api = ctx.api || '';
    var SD = C.SplitDirection || C.ImagerySplitDirection || null;
    var m = {
      imageryOn: false, layerId: DEFAULT_LAYER, opacity: 1, primary: null, primaryDay: null,
      primaryLayerId: null, pick: null, compareOn: false, compare: null, compareDay: null,
      split: 0.5, divider: null, leftLabel: null, rightLabel: null, dragging: false,
      lastSync: 0, untick: null, docListeners: [],
      eventsOn: false, events: [], points: null, timer: null, seq: 0, count: 0,
      lastLine: '', failed: false, staticNoted: false, card: null, cardEvent: null,
      root: null, dateEl: null, imageryBox: null, layerSelect: null, opacityInput: null,
      compareBox: null, compareInput: null, eventsBox: null, eventsStatusEl: null,
      cardHost: null
    };

    function note(msg) {
      try { if (typeof ctx.addIntelItem === 'function') ctx.addIntelItem(msg); } catch (e) {
        /* keep going */
      }
    }

    function simNow() {
      try {
        if (typeof ctx.simNow === 'function') {
          var t = ctx.simNow();
          if (finite(toMs(t))) return t;
        }
      } catch (e) { /* fall back to the wall clock */ }
      return new Date();
    }

    function container() {
      var c = viewer.container || (viewer.canvas && viewer.canvas.parentNode) || null;
      return c && typeof c.appendChild === 'function' ? c : null;
    }

    function layers() { return viewer.imageryLayers; }

    // -- imagery --------------------------------------------------------

    function makeLayer(layer, day) {
      var provider = new C.UrlTemplateImageryProvider({
        url: tileUrl(layer, day),
        maximumLevel: layer.level,
        credit: new C.Credit('NASA GIBS, ' + layer.label + ', ' + day)
      });
      var il = new C.ImageryLayer(provider);
      il.alpha = m.opacity;
      return il;
    }

    // The new layer takes the old one's place in the stack, so a day change
    // never jumps the imagery above or below anything another module added.
    function putLayer(old, fresh) {
      var coll = layers();
      if (old && coll.contains(old)) {
        coll.add(fresh, coll.indexOf(old) + 1);
        coll.remove(old, true);
      } else {
        coll.add(fresh);
      }
      return fresh;
    }

    function dropLayer(layer) {
      if (layer && layers().contains(layer)) layers().remove(layer, true);
    }

    function applySplit() {
      if (!SD) return;
      if (m.primary) m.primary.splitDirection = m.compareOn ? SD.RIGHT : SD.NONE;
      if (m.compare) m.compare.splitDirection = SD.LEFT;
      if (m.compareOn) scene.splitPosition = m.split;
    }

    function showDate() {
      var layer = layerById(m.layerId);
      if (m.dateEl) {
        if (!m.imageryOn) {
          m.dateEl.textContent = 'Off. Turn on to show NASA GIBS daily imagery for the day ' +
            'on the clock.';
        } else {
          var text = layer.label + ', ' + m.primaryDay + ' (' + reasonText(m.pick, layer) + ')';
          if (layer.night) text += '; the night side only';
          if (m.compareOn) text += '. Compare: ' + m.compareDay + ' on the left.';
          m.dateEl.textContent = text;
        }
      }
      if (m.leftLabel) m.leftLabel.textContent = m.compareDay || '';
      if (m.rightLabel) m.rightLabel.textContent = m.primaryDay || '';
    }

    // Bring the clock-driven layer onto the clock's day. Returns true when a new
    // layer (and so a new set of tile requests) was made.
    function sync(force) {
      if (!m.imageryOn) return false;
      var layer = layerById(m.layerId);
      var pick = pickDate(simNow(), Date.now(), layer);
      m.pick = pick;
      var changed = !!force || !m.primary || pick.day !== m.primaryDay ||
        layer.id !== m.primaryLayerId;
      if (changed) {
        m.primary = putLayer(m.primary, makeLayer(layer, pick.day));
        m.primaryDay = pick.day;
        m.primaryLayerId = layer.id;
        applySplit();
      }
      showDate();
      return changed;
    }

    function imageryLine() {
      var layer = layerById(m.layerId);
      return 'NASA GIBS imagery: ' + layer.label + ' for ' + m.primaryDay + ' (' +
        reasonText(m.pick, layer) + ')';
    }

    function setImagery(on) {
      on = !!on;
      if (m.imageryBox) m.imageryBox.checked = on;
      if (on === m.imageryOn) return;
      m.imageryOn = on;
      if (on) {
        sync(true);
        note(imageryLine());
      } else {
        setCompare(false);
        dropLayer(m.primary);
        m.primary = null;
        m.primaryDay = null;
        m.primaryLayerId = null;
        m.pick = null;
        showDate();
      }
    }

    function setLayer(id) {
      var layer = findLayer(id);
      if (!layer) {
        if (m.layerSelect) m.layerSelect.value = m.layerId;
        return false;
      }
      if (m.layerSelect) m.layerSelect.value = layer.id;
      if (layer.id === m.layerId) return false;
      m.layerId = layer.id;
      if (m.compareInput) m.compareInput.setAttribute('min', layer.start);
      if (m.imageryOn) {
        sync(true);
        if (m.compareOn) {
          m.compareDay = clampDay(m.compareDay, layer, Date.now()) || m.primaryDay;
          buildCompare();
        }
        note(imageryLine());
      }
      return true;
    }

    function setOpacity(v) {
      m.opacity = opacityFrom(v);
      if (m.primary) m.primary.alpha = m.opacity;
      if (m.compare) m.compare.alpha = m.opacity;
      if (m.opacityInput) m.opacityInput.value = String(Math.round(m.opacity * 100));
    }

    // -- compare --------------------------------------------------------

    function buildCompare() {
      var layer = layerById(m.layerId);
      if (!m.compareDay) {
        m.compareDay = clampDay(addDays(m.primaryDay, -COMPARE_OFFSET_DAYS), layer, Date.now()) ||
          m.primaryDay;
      }
      if (m.compareInput) m.compareInput.value = m.compareDay;
      m.compare = putLayer(m.compare, makeLayer(layer, m.compareDay));
      applySplit();
      showDate();
    }

    function setCompare(on) {
      on = !!on;
      if (on && !SD) {
        if (m.dateEl) m.dateEl.textContent = 'This Cesium build has no imagery split.';
        on = false;
      }
      if (m.compareBox) m.compareBox.checked = on;
      if (on === m.compareOn) return;
      if (on) {
        if (!m.imageryOn) setImagery(true);
        m.compareOn = true;
        buildCompare();
        showDivider();
        note('NASA GIBS compare: ' + m.compareDay + ' on the left, ' + m.primaryDay +
          ' on the right');
      } else {
        m.compareOn = false;
        dropLayer(m.compare);
        m.compare = null;
        hideDivider();
        applySplit();
        showDate();
      }
      if (m.compareBox) m.compareBox.checked = m.compareOn;
    }

    function setCompareDate(v) {
      var day = clampDay(typeof v === 'string' ? v.trim() : v, layerById(m.layerId), Date.now());
      if (!day) {
        if (m.compareInput) m.compareInput.value = m.compareDay || '';
        return false;
      }
      if (m.compareInput) m.compareInput.value = day;
      if (day === m.compareDay) return false;
      m.compareDay = day;
      if (m.compareOn) buildCompare();
      else showDate();
      return true;
    }

    // -- divider --------------------------------------------------------

    function setSplit(x) {
      m.split = clampSplit(x);
      if (m.divider) {
        m.divider.style.left = (m.split * 100).toFixed(2) + '%';
        m.divider.setAttribute('aria-valuenow', String(Math.round(m.split * 100)));
      }
      if (m.compareOn) scene.splitPosition = m.split;
    }

    function moveTo(clientX) {
      var c = container();
      var rect = c && typeof c.getBoundingClientRect === 'function' ? c.getBoundingClientRect()
        : null;
      var x = splitFromPointer(clientX, rect);
      if (x !== null) setSplit(x);
    }

    function listen(target, type, fn) {
      target.addEventListener(type, fn);
      m.docListeners.push([target, type, fn]);
    }

    function dateTag(cls, side) {
      var t = el('span', 'nasa-split-label ' + cls);
      t.style.position = 'absolute';
      t.style.top = '8px';
      t.style[side] = '10px';
      t.style.whiteSpace = 'nowrap';
      t.style.background = 'rgba(0,0,0,0.65)';
      t.style.color = '#fff';
      t.style.padding = '2px 5px';
      t.style.fontSize = '11px';
      t.style.pointerEvents = 'none';
      return t;
    }

    function buildDivider() {
      var d = el('div', 'nasa-split');
      d.style.position = 'absolute';
      d.style.top = '0';
      d.style.bottom = '0';
      d.style.width = '6px';
      d.style.marginLeft = '-3px';
      d.style.background = 'rgba(255,255,255,0.85)';
      d.style.boxShadow = '0 0 4px rgba(0,0,0,0.8)';
      d.style.cursor = 'ew-resize';
      d.style.zIndex = '20';
      d.style.touchAction = 'none';
      d.setAttribute('role', 'separator');
      d.setAttribute('aria-orientation', 'vertical');
      d.setAttribute('aria-label', 'Imagery compare divider: compare date left, clock date right');
      d.setAttribute('tabindex', '0');
      m.leftLabel = dateTag('nasa-split-left', 'right');
      m.rightLabel = dateTag('nasa-split-right', 'left');
      d.appendChild(m.leftLabel);
      d.appendChild(m.rightLabel);

      function stop() { m.dragging = false; }
      function move(e) { if (m.dragging && e) moveTo(e.clientX); }
      d.addEventListener('pointerdown', function (e) {
        m.dragging = true;
        if (e && e.pointerId !== undefined && typeof d.setPointerCapture === 'function') {
          try { d.setPointerCapture(e.pointerId); } catch (err) { /* keep going */ }
        }
        if (e && typeof e.preventDefault === 'function') e.preventDefault();
      });
      d.addEventListener('keydown', function (e) {
        var key = e && e.key;
        if (key !== 'ArrowLeft' && key !== 'ArrowRight') return;
        setSplit(m.split + (key === 'ArrowLeft' ? -SPLIT_STEP : SPLIT_STEP));
        if (typeof e.preventDefault === 'function') e.preventDefault();
      });
      // Moves and releases are heard on the document, so a fast drag that
      // leaves the thin bar still follows the pointer.
      var doc = global.document;
      var target = doc && typeof doc.addEventListener === 'function' ? doc : d;
      listen(target, 'pointermove', move);
      listen(target, 'pointerup', stop);
      listen(target, 'pointercancel', stop);
      m.divider = d;
    }

    function showDivider() {
      var c = container();
      if (!c) return;
      if (!m.divider) buildDivider();
      if (m.divider.parentNode !== c) c.appendChild(m.divider);
      setSplit(m.split);
      showDate();
    }

    function hideDivider() {
      m.dragging = false;
      if (m.divider && m.divider.parentNode) m.divider.parentNode.removeChild(m.divider);
    }

    // -- events ---------------------------------------------------------

    function setCount(n) {
      m.count = n;
      var box = byId(COUNT_ID);
      if (box) box.textContent = String(n);
    }

    function setEventsStatus(msg) { if (m.eventsStatusEl) m.eventsStatusEl.textContent = msg; }

    function syncEventBoxes() {
      if (m.eventsBox) m.eventsBox.checked = m.eventsOn;
      var page = byId(PAGE_TOGGLE_ID);
      if (page) page.checked = m.eventsOn;
    }

    function ensurePoints() {
      if (!m.points) m.points = scene.primitives.add(new C.PointPrimitiveCollection());
      return m.points;
    }

    function flyTo(ev) {
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(ev.lon, ev.lat, FLY_HEIGHT_M),
          duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function drawEvents(events) {
      var pts = ensurePoints();
      pts.removeAll();
      var outline = C.Color.BLACK.withAlpha(0.8);
      events.forEach(function (ev) {
        pts.add({
          position: C.Cartesian3.fromDegrees(ev.lon, ev.lat, 0),
          pixelSize: EVENT_PIXEL_SIZE,
          color: C.Color.fromCssColorString(colorFor(categoryOf(ev).id)),
          outlineColor: outline,
          outlineWidth: 1.5,
          id: { type: 'eonet', eventId: ev.id, sdaPick: function () { pickEvent(ev); } }
        });
      });
      pts.show = true;
      setCount(events.length);
    }

    function closeCard() {
      if (m.card && m.card.parentNode) m.card.parentNode.removeChild(m.card);
      m.card = null;
      m.cardEvent = null;
    }

    function cardRow(card, cls, label, value) {
      if (!value) return;
      var row = el('div', cls);
      row.appendChild(el('span', 'nasa-card-label', label + ' '));
      row.appendChild(el('span', 'nasa-card-value', value));
      card.appendChild(row);
    }

    function openCard(ev) {
      closeCard();
      var host = m.cardHost || container();
      if (!host) return null;
      var card = el('div', 'nasa-event-card');
      if (!m.cardHost) {
        card.style.position = 'absolute';
        card.style.top = '70px';
        card.style.right = '12px';
        card.style.zIndex = '21';
        card.style.background = 'rgba(0,0,0,0.8)';
        card.style.padding = '6px 8px';
        card.style.maxWidth = '280px';
      }
      var cat = categoryOf(ev);
      card.appendChild(el('div', 'nasa-card-title', ev.title));
      var catRow = el('div', 'nasa-card-category');
      catRow.appendChild(swatch(colorFor(cat.id)));
      catRow.appendChild(el('span', null, ev.categories.length
        ? ev.categories.map(function (c) { return c.title; }).join(', ') : 'Uncategorised'));
      card.appendChild(catRow);
      var when = shortDateTime(ev.date);
      if (when && ev.observations > 1) when += ' (' + ev.observations + ' observations)';
      cardRow(card, 'nasa-card-date', 'Latest', when);
      cardRow(card, 'nasa-card-magnitude', 'Magnitude', magnitudeText(ev));
      cardRow(card, 'nasa-card-position', 'At', ev.lat.toFixed(2) + ', ' + ev.lon.toFixed(2));
      if (ev.sources.length) {
        var links = el('div', 'nasa-card-sources');
        links.appendChild(el('span', 'nasa-card-label', 'Source '));
        ev.sources.forEach(function (s, i) {
          if (i) links.appendChild(el('span', null, ', '));
          var a = el('a', 'nasa-card-link', s.id);
          a.setAttribute('href', s.url);
          a.setAttribute('target', '_blank');
          a.setAttribute('rel', 'noopener noreferrer');
          links.appendChild(a);
        });
        card.appendChild(links);
      }
      var buttons = el('div', 'nasa-card-buttons');
      var fly = el('button', 'nasa-card-fly', 'Fly to');
      fly.addEventListener('click', function () { flyTo(ev); });
      buttons.appendChild(fly);
      var day = ev.date ? ev.date.slice(0, 10) : '';
      if (validDay(day)) {
        var cmp = el('button', 'nasa-card-compare', 'Compare on this day');
        cmp.addEventListener('click', function () {
          setCompareDate(day);
          setCompare(true);
        });
        buttons.appendChild(cmp);
      }
      var close = el('button', 'nasa-card-close', 'Close');
      close.addEventListener('click', closeCard);
      buttons.appendChild(close);
      card.appendChild(buttons);
      host.appendChild(card);
      m.card = card;
      m.cardEvent = ev;
      if (m.root && m.root.classList && typeof m.root.classList.remove === 'function') {
        m.root.classList.remove('collapsed');
      }
      return card;
    }

    function pickEvent(ev) {
      openCard(ev);
      flyTo(ev);
    }

    function loadEvents() {
      if (!m.eventsOn || ctx.isStatic) return Promise.resolve(null);
      var seq = ++m.seq;
      setEventsStatus('Loading NASA EONET events...');
      var req;
      try {
        req = global.fetch(eventsUrl(api, EVENT_DAYS));
      } catch (e) {
        req = Promise.reject(e);
      }
      return Promise.resolve(req).then(function (r) {
        if (!r || !r.ok) throw new Error('HTTP ' + (r && r.status));
        return r.json();
      }).then(function (data) {
        if (seq !== m.seq || !m.eventsOn) return null;
        var events = cleanEvents(data && data.events);
        m.events = events;
        drawEvents(events);
        m.failed = false;
        if (m.cardEvent && !events.some(function (ev) { return ev.id === m.cardEvent.id; })) {
          closeCard();
        }
        var line = eventsLine(events, data);
        setEventsStatus(line);
        if (line !== m.lastLine) {
          m.lastLine = line;
          note(line);
        }
        return events.length;
      }).catch(function () {
        if (seq !== m.seq || !m.eventsOn) return null;
        setEventsStatus('NASA EONET did not load; retrying in thirty minutes.');
        if (!m.failed) {
          m.failed = true;
          note('NASA EONET: events feed unavailable');
        }
        return null;
      });
    }

    function setEventsEnabled(on) {
      on = !!on;
      if (ctx.isStatic) {
        if (on && !m.staticNoted) {
          m.staticNoted = true;
          note('NASA EONET events need the live server.');
        }
        setEventsStatus('NASA EONET events need the live server; the static demo has no API.');
        syncEventBoxes();
        return;
      }
      if (on === m.eventsOn) {
        syncEventBoxes();
        return;
      }
      m.eventsOn = on;
      syncEventBoxes();
      if (on) {
        m.lastLine = '';
        m.failed = false;
        loadEvents();
        m.timer = global.setInterval(loadEvents, EVENT_REFRESH_MS);
      } else {
        if (m.timer) {
          global.clearInterval(m.timer);
          m.timer = null;
        }
        m.seq++;
        if (m.points) {
          m.points.removeAll();
          m.points.show = false;
        }
        m.events = [];
        setCount(0);
        closeCard();
        setEventsStatus('');
      }
    }

    // The panel's own events box goes through the page's layer checkbox when
    // there is one, so the page's layer state and share link stay in step.
    function requestEvents(on) {
      var page = byId(PAGE_TOGGLE_ID);
      if (page && typeof page.click === 'function' && !!page.checked !== on) {
        try { page.click(); } catch (e) { /* fall through */ }
      }
      if (m.eventsOn !== on || ctx.isStatic) setEventsEnabled(on);
    }

    function openEvent(id) {
      for (var i = 0; i < m.events.length; i++) {
        if (m.events[i].id === id) {
          pickEvent(m.events[i]);
          return true;
        }
      }
      return false;
    }

    // -- panel ----------------------------------------------------------

    function checkbox(cls, text, onChange) {
      var label = el('label', 'nasa-row');
      label.style.display = 'block';
      var box = el('input', cls);
      box.type = 'checkbox';
      box.addEventListener('change', function () { onChange(!!box.checked); });
      label.appendChild(box);
      label.appendChild(el('span', null, ' ' + text));
      return { label: label, box: box };
    }

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      m.root = el('div', 'panel');
      var body = el('div', 'panel-body');

      var img = checkbox('nasa-imagery-toggle', 'Dated imagery (NASA GIBS)', setImagery);
      m.imageryBox = img.box;
      body.appendChild(img.label);

      m.layerSelect = el('select', 'nasa-layer-select');
      LAYERS.forEach(function (layer) {
        var opt = el('option', null, layer.label);
        opt.value = layer.id;
        if (layer.id === m.layerId) opt.selected = true;
        m.layerSelect.appendChild(opt);
      });
      m.layerSelect.value = m.layerId;
      m.layerSelect.addEventListener('change', function () { setLayer(m.layerSelect.value); });
      body.appendChild(m.layerSelect);

      m.dateEl = el('div', 'nasa-date');
      m.dateEl.style.fontSize = '10px';
      m.dateEl.style.margin = '3px 0';
      body.appendChild(m.dateEl);

      var op = el('label', 'nasa-row', 'Opacity ');
      op.style.display = 'block';
      m.opacityInput = el('input', 'nasa-opacity');
      m.opacityInput.type = 'range';
      m.opacityInput.setAttribute('min', '0');
      m.opacityInput.setAttribute('max', '100');
      m.opacityInput.setAttribute('step', '5');
      m.opacityInput.value = '100';
      m.opacityInput.addEventListener('input', function () { setOpacity(m.opacityInput.value); });
      op.appendChild(m.opacityInput);
      body.appendChild(op);

      var cmp = checkbox('nasa-compare-toggle', 'Compare with', setCompare);
      m.compareBox = cmp.box;
      m.compareInput = el('input', 'nasa-compare-date');
      m.compareInput.type = 'date';
      m.compareInput.setAttribute('min', layerById(m.layerId).start);
      m.compareInput.addEventListener('change', function () {
        setCompareDate(m.compareInput.value);
      });
      cmp.label.appendChild(el('span', null, ' '));
      cmp.label.appendChild(m.compareInput);
      body.appendChild(cmp.label);

      var ev = checkbox('nasa-events-toggle', 'Natural events (NASA EONET)', requestEvents);
      m.eventsBox = ev.box;
      body.appendChild(ev.label);
      var legend = el('div', 'nasa-legend');
      legend.style.display = 'flex';
      legend.style.flexWrap = 'wrap';
      legend.style.gap = '6px';
      legend.style.fontSize = '10px';
      LEGEND.forEach(function (pair) {
        var item = el('span', 'nasa-key');
        item.appendChild(swatch(colorFor(pair[0])));
        item.appendChild(el('span', null, pair[1]));
        legend.appendChild(item);
      });
      body.appendChild(legend);
      m.eventsStatusEl = el('div', 'nasa-events-status', ctx.isStatic
        ? 'NASA EONET events need the live server; the static demo has no API.' : '');
      m.eventsStatusEl.style.fontSize = '10px';
      body.appendChild(m.eventsStatusEl);
      m.cardHost = el('div', 'nasa-card-host');
      body.appendChild(m.cardHost);

      m.root.appendChild(el('h3', null, PANEL_TITLE));
      m.root.appendChild(body);
      ctx.mount.appendChild(m.root);
      showDate();
    }

    // -- clock ----------------------------------------------------------

    function onTick() {
      if (!m.imageryOn) return;
      var now = Date.now();
      if (now - m.lastSync < SYNC_MS) return;
      m.lastSync = now;
      try { sync(false); } catch (e) { /* keep the clock running */ }
    }

    function watchClock() {
      var ev = viewer.clock && viewer.clock.onTick;
      if (!ev || typeof ev.addEventListener !== 'function') return;
      var remove = ev.addEventListener(onTick);
      m.untick = typeof remove === 'function' ? remove : function () {
        if (typeof ev.removeEventListener === 'function') ev.removeEventListener(onTick);
      };
    }

    function destroy() {
      setEventsEnabled(false);
      setImagery(false);
      hideDivider();
      if (m.untick) { m.untick(); m.untick = null; }
      m.docListeners.forEach(function (l) {
        if (typeof l[0].removeEventListener === 'function') l[0].removeEventListener(l[1], l[2]);
      });
      m.docListeners = [];
      if (m.points) {
        try { scene.primitives.remove(m.points); } catch (e) { /* keep going */ }
        m.points = null;
      }
      if (m.root && m.root.parentNode) m.root.parentNode.removeChild(m.root);
    }

    buildPanel();
    watchClock();
    m.api = {
      setImagery: setImagery, setLayer: setLayer, setOpacity: setOpacity, sync: sync,
      setCompare: setCompare, setCompareDate: setCompareDate, setSplit: setSplit,
      setEventsEnabled: setEventsEnabled, refreshEvents: loadEvents, openEvent: openEvent,
      closeCard: closeCard, destroy: destroy
    };
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.viewer || !ctx.viewer.scene || !ctx.viewer.imageryLayers ||
          !global.Cesium) {
        return false;
      }
      if (current) current.api.destroy();
      current = createModule(ctx);
      return true;
    } catch (err) {
      return false;
    }
  }

  function call(name) {
    return function (a) { return current ? current.api[name](a) : undefined; };
  }

  global.SDANasaImagery = {
    init: init,
    setImagery: call('setImagery'),
    setLayer: call('setLayer'),
    setOpacity: call('setOpacity'),
    sync: function () { return current ? current.api.sync(false) : false; },
    setCompare: call('setCompare'),
    setCompareDate: call('setCompareDate'),
    setSplit: call('setSplit'),
    setEventsEnabled: call('setEventsEnabled'),
    refreshEvents: function () {
      return current ? current.api.refreshEvents() : Promise.resolve(null);
    },
    openEvent: call('openEvent'),
    closeCard: call('closeCard'),
    isImageryOn: function () { return !!(current && current.imageryOn); },
    isCompareOn: function () { return !!(current && current.compareOn); },
    isEventsEnabled: function () { return !!(current && current.eventsOn); },
    currentDay: function () { return current ? current.primaryDay : null; },
    compareDay: function () { return current ? current.compareDay : null; },
    layerId: function () { return current ? current.layerId : DEFAULT_LAYER; },
    splitPosition: function () { return current ? current.split : 0.5; },
    opacity: function () { return current ? current.opacity : 1; },
    count: function () { return current ? current.count : 0; },
    LAYERS: LAYERS,
    CATEGORY_COLORS: CATEGORY_COLORS,
    EVENT_DAYS: EVENT_DAYS,
    EVENT_REFRESH_MS: EVENT_REFRESH_MS,
    LIVE_WINDOW_MS: LIVE_WINDOW_MS,
    GIBS_ROOT: GIBS_ROOT,
    isoDay: isoDay,
    validDay: validDay,
    addDays: addDays,
    isLive: isLive,
    pickDate: pickDate,
    reasonText: reasonText,
    clampDay: clampDay,
    tileUrl: tileUrl,
    clampSplit: clampSplit,
    splitFromPointer: splitFromPointer,
    opacityFrom: opacityFrom,
    eventsUrl: eventsUrl,
    safeUrl: safeUrl,
    cleanEvents: cleanEvents,
    colorFor: colorFor,
    formatNumber: formatNumber,
    magnitudeText: magnitudeText,
    shortDateTime: shortDateTime,
    eventsLine: eventsLine
  };
})(typeof window !== 'undefined' ? window : this);
