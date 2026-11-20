// Orbit visuals and object information for the Vantage globe.
//
// Eight tools that share one satellite layer (the page's point collection, one
// point per object, never an entity per satellite):
//
//   Colour by       object type, owner, orbit class, element-set age, radar size,
//                   launch decade, or sunlit / penumbra / umbra, with a legend that
//                   counts each class and isolates one on click
//   Watchlist       starred objects kept in localStorage, each with its next pass
//                   over the observer, its newest conjunction and the maneuver or
//                   decay flags from the element-set archive
//   Constellations  Starlink, OneWeb, Kuiper, GPS, Galileo, GLONASS, BeiDou and
//                   Iridium with counts; orbital planes found from the elements and
//                   drawn as rings, a ring click highlights its members
//   Debris clouds   large fragmentation events (Fengyun-1C, Cosmos 2251 and
//                   Iridium 33, Cosmos 1408, ...) and other launches with many
//                   pieces, with pieces on orbit, drawn and re-entered
//   Catalog history objects in orbit at the end of each year since 1957 (SATCAT
//                   launch and decay dates), a year slider that hides drawn objects
//                   launched later, and PLAY
//   Earth's shadow  umbra and penumbra cones for the sim time, the shadow ring at
//                   the geostationary distance
//   Footprint       horizon and elevation-mask circles under the selected object,
//                   and a 2D ground-track strip with day and night
//   Visible tonight brightest sunlit passes over the observer in the next 24 hours
//                   (server: /api/orbitviz/visible, built on sda.passes)
//
// Server routes used, all optional (the static demo has none and every panel says
// what it would need): /api/orbitviz/attributes, /api/orbitviz/history,
// /api/orbitviz/debris, /api/orbitviz/visible, /passes, /conjunctions/history,
// /tle/history/{id}/maneuvers.
//
// Ideas, not code: colour schemes, the time machine, the constellation and
// breakup plugins of keeptrack.space (AGPL, read for ideas only and written here
// from scratch); Heavens-Above for the visible-pass list. Palettes are the
// validated dark-mode steps of the dataviz reference palette.
//
// Frames: the elements are read straight from the TLE lines; rings are built in
// the inertial frame of the elements and turned into the Earth-fixed frame with
// the sidereal angle each frame (one matrix, no per-object work). Positions read
// from the page are the geodetic lat, lon and height it already computed. No text
// is ever set as HTML.
//
// Page wiring: listed in EXTENSIONS, init(ctx) builds the panels in ctx.mount.
// setEnabled(false) hides every primitive this module draws and gives the page its
// colours back. The history slider owns bit 4 of record.hideMask (categories.js
// owns 1, dossier.js 2); the selected object always stays visible.

(function (global) {
  'use strict';

  var STYLE_ID = 'sda-orbitviz-style';

  // WGS72, as sda.constants and the sgp4 package.
  var MU = 398600.8;
  var RE = 6378.135;
  var J2 = 0.001082616;
  var FLAT = 1 / 298.26;
  var DEG = Math.PI / 180;
  var TWO_PI = 2 * Math.PI;
  var DAY_MS = 86400000;
  var AU_KM = 149597870.7;
  var SUN_RADIUS_KM = 696000;
  var GEO_RADIUS_KM = 42164;

  var LS_MODE = 'sda.orbitviz.colorMode';
  var LS_WATCH = 'sda.orbitviz.watchlist';
  var LS_OBSERVER = 'sda.orbitviz.observer';
  var WATCH_MAX = 50;
  var HIDE_BIT = 4;
  var DIM_ALPHA = 0.12;
  var SHADOW_LENGTH_KM = 60000;
  var RING_POINTS = 90;
  var FOOT_POINTS = 72;
  var FOOT_HEIGHT_M = 5000;
  var FOOT_EVERY_MS = 250;
  var SLOW_EVERY_MS = 1000;
  var AGE_EVERY_MS = 60000;
  var PLANE_DRIFT_MS = 2 * 3600 * 1000;
  var CONCURRENCY = 3;
  var HISTORY_STEP_MS = 160;
  var PASS_HOURS = 24;
  var PASS_MIN_EL = 10;

  // ---------------------------------------------------------------------
  // Palettes (dataviz reference, dark surface, validated)
  // ---------------------------------------------------------------------

  var CAT = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c',
    '#9085e9', '#e66767'];
  var OTHER = '#898781';
  var NO_DATA = '#55544f';

  var OWNER_CODES = ['US', 'CIS', 'PRC', 'UK', 'FR', 'JPN', 'IND'];
  var OWNER_LABELS = {
    US: 'United States', CIS: 'Russia (CIS)', PRC: 'China', UK: 'United Kingdom',
    FR: 'France', JPN: 'Japan', IND: 'India'
  };

  function classes(pairs) {
    return pairs.map(function (p) { return { label: p[0], color: p[1] }; });
  }

  var MODES = [
    { key: 'default', label: 'Page default (server regime)', classes: null,
      note: 'The page colours each object by the regime the server gave it.' },
    { key: 'type', label: 'Object type', satcat: true,
      classes: classes([['Payload', CAT[0]], ['Rocket body', CAT[1]], ['Debris', CAT[2]],
        ['Unknown', OTHER]]),
      note: 'SATCAT object type; without SATCAT the name decides (DEB is debris, R/B a ' +
        'rocket body).' },
    { key: 'owner', label: 'Owner (SATCAT)', satcat: true,
      classes: classes(OWNER_CODES.map(function (c, i) { return [OWNER_LABELS[c], CAT[i]]; })
        .concat([['Other owners', OTHER], ['Not in SATCAT', NO_DATA]])),
      note: 'Owning country or organisation from SATCAT. Owners outside the seven ' +
        'largest are grouped.' },
    { key: 'regime', label: 'Orbit class (perigee, apogee, period)',
      classes: classes([['LEO', CAT[0]], ['MEO', CAT[1]], ['GEO', CAT[2]], ['HEO', CAT[3]],
        ['Beyond GEO', CAT[4]], ['No elements', NO_DATA]]),
      note: 'From the element set: GEO is 0.9 to 1.1 rev/day with e under 0.1, HEO ' +
        'has e of 0.25 or more or crosses 2,000 km, LEO stays under 2,000 km.' },
    { key: 'age', label: 'Element set age',
      classes: classes([['Under 1 day', '#cde2fb'], ['1 to 3 days', '#86b6ef'],
        ['3 to 7 days', '#3987e5'], ['7 to 30 days', '#256abf'], ['Over 30 days', '#184f95'],
        ['Unknown epoch', NO_DATA]]),
      note: 'Time between the element-set epoch and the sim clock. Old sets mean ' +
        'larger position errors.' },
    { key: 'rcs', label: 'Radar size (SATCAT)', satcat: true,
      classes: classes([['Large (over 1 m2)', '#cde2fb'], ['Medium (0.1 to 1 m2)', '#6da7ec'],
        ['Small (under 0.1 m2)', '#256abf'], ['Unknown', OTHER], ['Not in SATCAT', NO_DATA]]),
      note: 'Radar cross-section band published in SATCAT.' },
    { key: 'decade', label: 'Launch decade',
      classes: classes([['Before 1980', '#184f95'], ['1980s', '#256abf'], ['1990s', '#3987e5'],
        ['2000s', '#6da7ec'], ['2010s', '#9ec5f4'], ['2020s', '#cde2fb'], ['Unknown', NO_DATA]]),
      note: 'SATCAT launch year, else the year in the international designator.' },
    { key: 'sunlight', label: 'Sunlit or eclipsed',
      classes: classes([['Sunlit', '#c98500'], ['Penumbra', '#d55181'], ['Umbra', '#3987e5'],
        ['No position', NO_DATA]]),
      note: 'Conical Earth shadow at the sim time, repainted every second.' }
  ];
  var MODE_BY_KEY = {};
  MODES.forEach(function (md) { MODE_BY_KEY[md.key] = md; });

  var TYPE_LABELS = ['Payload', 'Rocket body', 'Debris', 'Unknown'];
  var TYPE_SERVER_KEYS = ['PAYLOAD', 'ROCKET BODY', 'DEBRIS', 'UNKNOWN'];

  var CONSTELLATIONS = [
    { key: 'STARLINK', name: 'Starlink', re: /^STARLINK/, incGap: 0.3, altGap: 15,
      raanGap: 1.5, min: 5 },
    { key: 'ONEWEB', name: 'OneWeb', re: /^ONEWEB/, incGap: 0.5, altGap: 30, raanGap: 4,
      min: 3 },
    { key: 'KUIPER', name: 'Kuiper', re: /^KUIPER/, incGap: 0.5, altGap: 20, raanGap: 2,
      min: 3 },
    { key: 'GPS', name: 'GPS', re: /^(?:GPS|BIIF|BIII)|NAVSTAR/, incGap: 3, altGap: 500,
      raanGap: 15, min: 2 },
    { key: 'GALILEO', name: 'Galileo', re: /^GSAT0\d|GALILEO/, incGap: 3, altGap: 500,
      raanGap: 15, min: 2 },
    { key: 'GLONASS', name: 'GLONASS', re: /GLONASS/, incGap: 3, altGap: 500, raanGap: 15,
      min: 2 },
    { key: 'BEIDOU', name: 'BeiDou', re: /BEIDOU/, incGap: 3, altGap: 2000, raanGap: 12,
      min: 2 },
    { key: 'IRIDIUM', name: 'Iridium', re: /^IRIDIUM/, incGap: 1, altGap: 40, raanGap: 8,
      min: 3 }
  ];
  CONSTELLATIONS.forEach(function (c, i) { c.color = CAT[i]; });
  var DEBRIS_NAME = /(?:^|\s)DEB(?:\s|$)|DEBRIS|R\/B|ROCKET BODY/;

  // Mirror of sda.routes.orbitviz.DEBRIS_EVENTS (a test keeps the two equal).
  var DEBRIS_EVENTS = [
    { key: 'fengyun-1c', name: 'Fengyun-1C', designators: ['1999-025'], event_year: 2007,
      kind: 'ASAT test' },
    { key: 'cosmos-2251-iridium-33', name: 'Cosmos 2251 and Iridium 33',
      designators: ['1993-036', '1997-051'], event_year: 2009, kind: 'Collision' },
    { key: 'cosmos-1408', name: 'Cosmos 1408', designators: ['1982-092'], event_year: 2021,
      kind: 'ASAT test' },
    { key: 'cz-6a-2022', name: 'CZ-6A upper stage (Yunhai 3 launch)',
      designators: ['2022-151'], event_year: 2022, kind: 'Upper stage breakup' },
    { key: 'cz-6a-2024', name: 'CZ-6A upper stage (Qianfan launch)',
      designators: ['2024-140'], event_year: 2024, kind: 'Upper stage breakup' },
    { key: 'cosmos-1275', name: 'Cosmos 1275', designators: ['1981-053'], event_year: 1981,
      kind: 'Breakup' },
    { key: 'noaa-16', name: 'NOAA 16', designators: ['2000-055'], event_year: 2015,
      kind: 'Breakup' },
    { key: 'noaa-17', name: 'NOAA 17', designators: ['2002-032'], event_year: 2021,
      kind: 'Breakup' },
    { key: 'dmsp-f13', name: 'DMSP F13', designators: ['1995-015'], event_year: 2015,
      kind: 'Breakup' },
    { key: 'pegasus-haps', name: 'Pegasus HAPS (STEP 2 launch)', designators: ['1994-029'],
      event_year: 1996, kind: 'Upper stage breakup' },
    { key: 'ariane-1-spot-1', name: 'Ariane 1 third stage (SPOT 1 launch)',
      designators: ['1986-019'], event_year: 1986, kind: 'Upper stage breakup' },
    { key: 'centaur-aehf-4', name: 'Atlas 5 Centaur (AEHF 4 launch)',
      designators: ['2018-079'], event_year: 2019, kind: 'Upper stage breakup' },
    { key: 'intelsat-33e', name: 'Intelsat 33e', designators: ['2016-053'], event_year: 2024,
      kind: 'Breakup' },
    { key: 'microsat-r', name: 'Microsat-R', designators: ['2019-006'], event_year: 2019,
      kind: 'ASAT test' }
  ];

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function fmtInt(n) {
    if (!isNum(n)) return '-';
    var s = String(Math.round(Math.abs(n)));
    var out = '';
    while (s.length > 3) { out = ',' + s.slice(-3) + out; s = s.slice(0, -3); }
    return (n < 0 && Math.round(n) !== 0 ? '-' : '') + s + out;
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function toMs(t) {
    if (isNum(t)) return t;
    if (t && typeof t.getTime === 'function') return t.getTime();
    if (typeof t === 'string') {
      var ms = Date.parse(t);
      return isNum(ms) ? ms : NaN;
    }
    return NaN;
  }

  // 'YYYY-MM-DD HH:MM UTC'
  function formatUtc(t) {
    var ms = toMs(t);
    if (!isNum(ms)) return '-';
    var d = new Date(ms);
    return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate()) +
      ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ' UTC';
  }

  function formatHm(t) {
    var ms = toMs(t);
    if (!isNum(ms)) return '--:--';
    var d = new Date(ms);
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
  }

  function twoDigitYear(yy) { return yy >= 57 ? 1900 + yy : 2000 + yy; }

  // Line 1: international designator (columns 10 to 17) and epoch (19 to 32).
  function parseLine1(line1) {
    var out = { designator: '', launchYear: 0, epochMs: NaN };
    if (typeof line1 !== 'string' || line1.length < 32) return out;
    var intl = line1.substring(9, 17);
    var yy = parseInt(intl.substring(0, 2), 10);
    var num = intl.substring(2, 5);
    if (isNum(yy) && /^\d{3}$/.test(num)) {
      out.launchYear = twoDigitYear(yy);
      out.designator = out.launchYear + '-' + num;
    }
    var ey = parseInt(line1.substring(18, 20), 10);
    var doy = parseFloat(line1.substring(20, 32));
    if (isNum(ey) && isNum(doy)) {
      out.epochMs = Date.UTC(twoDigitYear(ey), 0, 1) + (doy - 1) * DAY_MS;
    }
    return out;
  }

  // Line 2: inclination, node, eccentricity, mean motion (degrees, rev/day).
  function parseLine2(line2) {
    if (typeof line2 !== 'string' || line2.length < 63) return null;
    var inc = parseFloat(line2.substring(8, 16));
    var raan = parseFloat(line2.substring(17, 25));
    var ecc = parseFloat('0.' + line2.substring(26, 33).trim());
    var n = parseFloat(line2.substring(52, 63));
    if (!isNum(inc) || !isNum(raan) || !isNum(ecc) || !isNum(n) || n <= 0) return null;
    return { inc: inc, raan: raan, ecc: ecc, n: n };
  }

  // Semi-major axis, perigee and apogee heights (km) and period (min).
  function orbitShape(el) {
    if (!el) return null;
    var nRad = el.n * TWO_PI / 86400;
    var a = Math.pow(MU / (nRad * nRad), 1 / 3);
    return { a: a, perigee: a * (1 - el.ecc) - RE, apogee: a * (1 + el.ecc) - RE,
      period: 1440 / el.n };
  }

  // 0 LEO, 1 MEO, 2 GEO, 3 HEO, 4 Beyond GEO, 5 no elements.
  function regimeIndex(el) {
    if (!el) return 5;
    var sh = orbitShape(el);
    if (el.n >= 0.9 && el.n <= 1.1 && el.ecc < 0.1) return 2;
    if (el.ecc >= 0.25) return 3;
    if (sh.apogee < 2000) return 0;
    if (sh.perigee < 2000) return 3;
    if (sh.perigee > GEO_RADIUS_KM - RE + 1000) return 4;
    return 1;
  }

  // Element-set age in days -> bucket 0..4, 5 when unknown.
  function ageBucket(days) {
    if (!isNum(days)) return 5;
    var d = Math.abs(days);
    if (d < 1) return 0;
    if (d < 3) return 1;
    if (d < 7) return 2;
    if (d < 30) return 3;
    return 4;
  }

  // Launch year -> 0 before 1980, 1..5 the 1980s to the 2020s, 6 unknown.
  function decadeIndex(year) {
    if (!isNum(year) || year < 1957) return 6;
    if (year < 1980) return 0;
    return Math.min(5, Math.floor((year - 1980) / 10) + 1);
  }

  // 0 payload, 1 rocket body, 2 debris (name rules only).
  function typeFromName(name) {
    var n = typeof name === 'string' ? name.toUpperCase() : '';
    if (/R\/B|ROCKET BODY/.test(n)) return 1;
    if (/(?:^|\s)DEB(?:\s|$)|DEBRIS|^ISS OBJECT/.test(n)) return 2;
    return 0;
  }

  function typeFromCode(code) {
    return code === 'P' ? 0 : code === 'R' ? 1 : code === 'D' ? 2 : 3;
  }

  function rcsFromCode(code) {
    return code === 'L' ? 0 : code === 'M' ? 1 : code === 'S' ? 2 : 3;
  }

  // The packed /api/orbitviz/attributes body -> lookup tables.
  function decodeAttributes(body) {
    if (!body || !body.id_deltas || typeof body.types !== 'string') return null;
    var n = body.id_deltas.length;
    if (body.types.length !== n || String(body.rcs).length !== n ||
        String(body.years).length !== n || !body.owner_index ||
        body.owner_index.length !== n) return null;
    var alphabet = String(body.year_alphabet || '');
    var lut = {};
    for (var k = 0; k < alphabet.length; k++) lut[alphabet.charAt(k)] = k;
    var base = body.year_base | 0;
    var owners = (body.owners || []).map(function (o) { return o && o.code; });
    var index = Object.create(null);
    var ids = new Array(n);
    var years = new Array(n);
    var prev = 0;
    for (var i = 0; i < n; i++) {
      prev += body.id_deltas[i];
      ids[i] = prev;
      index[prev] = i;
      var pos = lut[body.years.charAt(i)];
      years[i] = pos ? base + pos : 0;
    }
    return { count: n, ids: ids, index: index, types: body.types, rcs: body.rcs,
      ownerIndex: body.owner_index, owners: owners, ownerList: body.owners || [],
      years: years };
  }

  function ownerClass(code) {
    var at = OWNER_CODES.indexOf(code);
    return at >= 0 ? at : OWNER_CODES.length;
  }

  // J2 secular node rate, degrees per day.
  function raanRateDegDay(el) {
    var sh = orbitShape(el);
    if (!sh) return 0;
    var nRad = el.n * TWO_PI / 86400;
    var p = sh.a * (1 - el.ecc * el.ecc);
    var rate = -1.5 * nRad * J2 * (RE / p) * (RE / p) * Math.cos(el.inc * DEG);
    return rate * 86400 / DEG;
  }

  function wrap360(x) { x %= 360; return x < 0 ? x + 360 : x; }

  function raanAt(el, epochMs, atMs) {
    if (!isNum(epochMs) || !isNum(atMs)) return el.raan;
    return wrap360(el.raan + raanRateDegDay(el) * (atMs - epochMs) / DAY_MS);
  }

  function splitByGap(list, key, gap) {
    var sorted = list.slice().sort(function (a, b) { return a[key] - b[key]; });
    var groups = [];
    var cur = [];
    for (var i = 0; i < sorted.length; i++) {
      if (cur.length && sorted[i][key] - cur[cur.length - 1][key] > gap) {
        groups.push(cur); cur = [];
      }
      cur.push(sorted[i]);
    }
    if (cur.length) groups.push(cur);
    return groups;
  }

  // Split node angles on a circle: start after the widest gap, then cut every gap
  // wider than `gap` degrees.
  function splitCircular(list, gap) {
    if (list.length < 2) return [list.slice()];
    var sorted = list.slice().sort(function (a, b) { return a.raan - b.raan; });
    var widest = -1;
    var start = 0;
    for (var i = 0; i < sorted.length; i++) {
      var next = i + 1 < sorted.length ? sorted[i + 1].raan : sorted[0].raan + 360;
      if (next - sorted[i].raan > widest) {
        widest = next - sorted[i].raan;
        start = (i + 1) % sorted.length;
      }
    }
    var groups = [];
    var cur = [];
    var prev = null;
    var turn = 0;
    for (var k = 0; k < sorted.length; k++) {
      var item = sorted[(start + k) % sorted.length];
      if (k > 0 && (start + k) % sorted.length === 0) turn = 360;
      var value = item.raan + turn;
      if (cur.length && value - prev > gap) { groups.push(cur); cur = []; }
      cur.push(item);
      prev = value;
    }
    if (cur.length) groups.push(cur);
    return groups;
  }

  function circularMeanDeg(values) {
    var sx = 0, sy = 0;
    for (var i = 0; i < values.length; i++) {
      sx += Math.cos(values[i] * DEG); sy += Math.sin(values[i] * DEG);
    }
    return wrap360(Math.atan2(sy, sx) / DEG);
  }

  function mean(values) {
    var s = 0;
    for (var i = 0; i < values.length; i++) s += values[i];
    return values.length ? s / values.length : NaN;
  }

  // Orbital planes from [{ id, inc, alt, raan }] (degrees, km). Groups by
  // inclination, then altitude, then node; near-equatorial groups (under 3 deg)
  // ignore the node, which is undefined there.
  function clusterPlanes(items, params) {
    var p = params || {};
    var minMembers = p.min || 2;
    var planes = [];
    splitByGap(items, 'inc', p.incGap || 1).forEach(function (byInc) {
      splitByGap(byInc, 'alt', p.altGap || 50).forEach(function (byAlt) {
        var meanInc = mean(byAlt.map(function (x) { return x.inc; }));
        var groups = meanInc < 3 ? [byAlt] : splitCircular(byAlt, p.raanGap || 5);
        groups.forEach(function (g) {
          if (g.length < minMembers) return;
          var inc = mean(g.map(function (x) { return x.inc; }));
          planes.push({
            inc: inc,
            alt: mean(g.map(function (x) { return x.alt; })),
            raan: inc < 3 ? 0 : circularMeanDeg(g.map(function (x) { return x.raan; })),
            ids: g.map(function (x) { return x.id; })
          });
        });
      });
    });
    planes.sort(function (a, b) { return a.inc - b.inc || a.alt - b.alt || a.raan - b.raan; });
    return planes;
  }

  // Points of a circular orbit ring in the inertial frame, km, [x, y, z] triples.
  function ringPoints(incDeg, raanDeg, radiusKm, count) {
    var n = count || RING_POINTS;
    var ci = Math.cos(incDeg * DEG), si = Math.sin(incDeg * DEG);
    var co = Math.cos(raanDeg * DEG), so = Math.sin(raanDeg * DEG);
    var out = [];
    for (var k = 0; k < n; k++) {
      var u = TWO_PI * k / n;
      var cu = Math.cos(u), su = Math.sin(u);
      out.push([radiusKm * (co * cu - so * su * ci), radiusKm * (so * cu + co * su * ci),
        radiusKm * su * si]);
    }
    return out;
  }

  function julianDateUtc(ms) { return ms / DAY_MS + 2440587.5; }

  // Greenwich mean sidereal angle (IAU-82, as sda.passes.gmst_rad and satellite.js).
  function gmstRad(ms) {
    var tut1 = (julianDateUtc(ms) - 2451545.0) / 36525;
    var sec = 67310.54841 + (876600 * 3600 + 8640184.812866) * tut1 +
      0.093104 * tut1 * tut1 - 6.2e-6 * tut1 * tut1 * tut1;
    var rad = (sec % 86400) / 240 * DEG;
    rad %= TWO_PI;
    return rad < 0 ? rad + TWO_PI : rad;
  }

  // Sun position in the inertial frame, km (Vallado algorithm 29, as
  // sda.passes.solar_position_eci_km). `out` is reused when given.
  function sunEci(ms, out) {
    var t = (julianDateUtc(ms) - 2451545.0) / 36525;
    var lm = (280.460 + 36000.771 * t) * DEG;
    var m = (357.5291092 + 35999.05034 * t) * DEG;
    var lam = lm + (1.914666471 * Math.sin(m) + 0.019994643 * Math.sin(2 * m)) * DEG;
    var r = (1.000140612 - 0.016708617 * Math.cos(m) - 0.000139589 * Math.cos(2 * m)) * AU_KM;
    var eps = (23.439291 - 0.0130042 * t) * DEG;
    var o = out || {};
    o.x = r * Math.cos(lam);
    o.y = r * Math.cos(eps) * Math.sin(lam);
    o.z = r * Math.sin(eps) * Math.sin(lam);
    return o;
  }

  // Inertial vector -> Earth-fixed by the sidereal angle. `out` reused when given.
  function toEarthFixed(v, gmst, out) {
    var c = Math.cos(gmst), s = Math.sin(gmst);
    var o = out || {};
    var x = c * v.x + s * v.y;
    var y = -s * v.x + c * v.y;
    o.x = x; o.y = y; o.z = v.z;
    return o;
  }

  // Unit vector towards the Sun in the Earth-fixed frame and the Sun distance.
  function sunEarthFixed(ms, out) {
    var o = toEarthFixed(sunEci(ms, out), gmstRad(ms), out);
    var d = Math.sqrt(o.x * o.x + o.y * o.y + o.z * o.z);
    o.x /= d; o.y /= d; o.z /= d; o.dist = d;
    return o;
  }

  var UMBRA_TAN = Math.tan(Math.asin((SUN_RADIUS_KM - RE) / AU_KM));
  var PENUMBRA_TAN = Math.tan(Math.asin((SUN_RADIUS_KM + RE) / AU_KM));

  function umbraRadiusKm(behindKm) { return Math.max(0, RE - behindKm * UMBRA_TAN); }
  function penumbraRadiusKm(behindKm) { return RE + behindKm * PENUMBRA_TAN; }

  // 0 sunlit, 1 penumbra, 2 umbra for a position (km) and a unit Sun vector in the
  // same frame. Conical shadow, Sun at its mean distance.
  function shadowClass(x, y, z, sun) {
    var along = -(x * sun.x + y * sun.y + z * sun.z);
    if (along <= 0) return 0;
    var px = x + along * sun.x, py = y + along * sun.y, pz = z + along * sun.z;
    var off = Math.sqrt(px * px + py * py + pz * pz);
    if (off < umbraRadiusKm(along)) return 2;
    if (off < penumbraRadiusKm(along)) return 1;
    return 0;
  }

  // Geodetic degrees and km -> Earth-fixed km (WGS72 ellipsoid).
  function geodeticToEarthFixed(latDeg, lonDeg, altKm, out) {
    var lat = latDeg * DEG, lon = lonDeg * DEG;
    var e2 = 2 * FLAT - FLAT * FLAT;
    var sl = Math.sin(lat);
    var nn = RE / Math.sqrt(1 - e2 * sl * sl);
    var h = (nn + altKm) * Math.cos(lat);
    var o = out || {};
    o.x = h * Math.cos(lon); o.y = h * Math.sin(lon); o.z = (nn * (1 - e2) + altKm) * sl;
    return o;
  }

  // Earth central angle (radians) from the sub-point to where the object sits at
  // `minElDeg` elevation; zero when it is too low to clear the mask anywhere.
  function footprintAngle(altKm, minElDeg) {
    if (!isNum(altKm) || altKm <= 0) return 0;
    var el = (isNum(minElDeg) ? minElDeg : 0) * DEG;
    var x = RE * Math.cos(el) / (RE + altKm);
    return Math.max(0, Math.acos(Math.min(1, x)) - el);
  }

  // Small circle of angular radius `angle` around (lat, lon), as [lat, lon] pairs,
  // closed (the last equals the first).
  function circlePoints(latDeg, lonDeg, angle, count) {
    var n = count || FOOT_POINTS;
    var lat0 = latDeg * DEG, lon0 = lonDeg * DEG;
    var out = [];
    for (var k = 0; k <= n; k++) {
      var b = TWO_PI * (k % n) / n;
      var lat = Math.asin(Math.sin(lat0) * Math.cos(angle) +
        Math.cos(lat0) * Math.sin(angle) * Math.cos(b));
      var lon = lon0 + Math.atan2(Math.sin(b) * Math.sin(angle) * Math.cos(lat0),
        Math.cos(angle) - Math.sin(lat0) * Math.sin(lat));
      var lonD = lon / DEG;
      lonD = ((lonD + 540) % 360) - 180;
      out.push([lat / DEG, lonD]);
    }
    return out;
  }

  // Subsolar latitude and longitude, degrees.
  function subsolarPoint(ms) {
    var s = sunEarthFixed(ms, {});
    return { lat: Math.asin(s.z) / DEG, lon: Math.atan2(s.y, s.x) / DEG };
  }

  // Night side along one meridian: { from, to } latitude span in darkness.
  function nightSpan(lonDeg, sunLat, sunLon) {
    var a = Math.sin(sunLat * DEG);
    var b = Math.cos(sunLat * DEG) * Math.cos((lonDeg - sunLon) * DEG);
    if (Math.abs(a) < 1e-9) return b < 0 ? { from: -90, to: 90 } : null;
    var t = Math.atan(-b / a) / DEG;
    return a > 0 ? { from: -90, to: t } : { from: t, to: 90 };
  }

  function launchDesignatorOf(line1) { return parseLine1(line1).designator; }

  function constellationOf(name) {
    var n = typeof name === 'string' ? name.toUpperCase() : '';
    if (!n || DEBRIS_NAME.test(n)) return null;
    for (var i = 0; i < CONSTELLATIONS.length; i++) {
      if (CONSTELLATIONS[i].re.test(n)) return CONSTELLATIONS[i].key;
    }
    return null;
  }

  // The pass that is next or still in progress at `nowMs`, or null.
  function nextPass(passes, nowMs) {
    var best = null;
    (passes || []).forEach(function (p) {
      var set = toMs(p && p.set_time);
      var rise = toMs(p && p.rise_time);
      if (!isNum(set) || set < nowMs) return;
      if (!best || rise < toMs(best.rise_time)) best = p;
    });
    return best;
  }

  // Newest conjunction (latest TCA) involving `id` from any number of event lists.
  function newestConjunction(id, lists) {
    var best = null;
    var bestMs = -Infinity;
    (lists || []).forEach(function (list) {
      (Array.isArray(list) ? list : []).forEach(function (ev) {
        if (!ev || (Number(ev.primary) !== id && Number(ev.secondary) !== id)) return;
        var ms = toMs(ev.tca);
        if (isNum(ms) && ms > bestMs) { best = ev; bestMs = ms; }
      });
    });
    return best;
  }

  function conjunctionText(id, ev) {
    if (!ev) return 'No conjunction on record.';
    var other = Number(ev.primary) === id ? (ev.secondary_name || ev.secondary)
      : (ev.primary_name || ev.primary);
    var miss = isNum(Number(ev.miss_distance_km)) ? Number(ev.miss_distance_km).toFixed(2) +
      ' km' : '?';
    return 'Conjunction ' + formatUtc(ev.tca) + ' with ' + other + ', ' + miss +
      (ev.risk ? ', ' + String(ev.risk).toUpperCase() : '');
  }

  function flagText(body) {
    if (!body) return 'No element history.';
    var man = body.maneuvers || [];
    var dec = body.decay_anomalies || [];
    var sets = isNum(body.snapshots) ? body.snapshots : 0;
    if (!man.length && !dec.length) {
      return 'No maneuver or decay flags in ' + fmtInt(sets) + ' element sets.';
    }
    var parts = [];
    if (man.length) {
      var last = man[man.length - 1];
      parts.push(man.length + (man.length === 1 ? ' maneuver' : ' maneuvers') +
        ', last ' + formatUtc(last.end_epoch).slice(0, 10));
    }
    if (dec.length) {
      parts.push(dec.length + (dec.length === 1 ? ' decay anomaly' : ' decay anomalies'));
    }
    return parts.join('; ') + ' (' + fmtInt(sets) + ' element sets).';
  }

  function passText(p) {
    if (!p) return 'No pass above ' + PASS_MIN_EL + ' deg in ' + PASS_HOURS + ' h.';
    var el = isNum(Number(p.max_elevation_deg)) ? Math.round(Number(p.max_elevation_deg)) : '?';
    return 'Next pass ' + formatUtc(p.rise_time) + ', max ' + el + ' deg' +
      (p.visible ? ', visible to the eye' : p.sunlit ? ', sunlit' : '');
  }

  function cleanWatch(raw) {
    var out = [];
    var seen = {};
    (Array.isArray(raw) ? raw : []).forEach(function (w) {
      var id = Number(w && typeof w === 'object' ? w.id : w);
      if (!isNum(id) || id <= 0 || id !== Math.floor(id) || seen[id]) return;
      seen[id] = true;
      var name = w && typeof w.name === 'string' ? w.name.slice(0, 80) : '';
      out.push({ id: id, name: name });
    });
    return out.slice(0, WATCH_MAX);
  }

  function cleanObserver(raw) {
    if (!raw || typeof raw !== 'object') return null;
    if (raw.lat === null || raw.lat === '' || raw.lon === null || raw.lon === '') return null;
    var lat = Number(raw.lat), lon = Number(raw.lon), alt = Number(raw.altM || 0);
    if (!isNum(lat) || !isNum(lon) || lat < -90 || lat > 90 || lon < -360 || lon > 360) {
      return null;
    }
    if (!isNum(alt) || alt < -500 || alt > 100000) alt = 0;
    if (lon > 180) lon -= 360;
    return { lat: lat, lon: lon, altM: alt };
  }

  function observerBody(o) {
    return { latitude_deg: o.lat, longitude_deg: o.lon, altitude_km: (o.altM || 0) / 1000 };
  }

  // Cumulative objects per launch year among drawn records (the static fallback
  // for the history chart: survivors only, decayed objects are not in the page).
  function historyFromYears(rows, lastYear) {
    var first = 1957;
    var span = Math.max(1, lastYear - first + 1);
    var add = TYPE_SERVER_KEYS.map(function () { return new Array(span).fill(0); });
    var noDate = 0;
    rows.forEach(function (r) {
      if (!r.year || r.year < first) { noDate++; return; }
      var at = Math.min(r.year, first + span - 1) - first;
      add[r.type][at]++;
    });
    var inOrbit = {};
    var total = new Array(span).fill(0);
    TYPE_SERVER_KEYS.forEach(function (k, t) {
      var run = 0;
      inOrbit[k] = add[t].map(function (v, i) { run += v; total[i] += run; return run; });
    });
    var years = [];
    for (var y = first; y < first + span; y++) years.push(y);
    return { first_year: first, last_year: first + span - 1, years: years, in_orbit: inOrbit,
      total: total, no_launch_date: noDate, fallback: true };
  }

  function historyText(hist, year) {
    if (!hist || !hist.years) return '';
    var i = year - hist.first_year;
    if (i < 0 || i >= hist.years.length) return '';
    var parts = TYPE_SERVER_KEYS.map(function (k, t) {
      var series = hist.in_orbit && hist.in_orbit[k];
      return TYPE_LABELS[t].toLowerCase() + ' ' + fmtInt(series ? series[i] : 0);
    });
    return year + ': ' + fmtInt(hist.total[i]) + (hist.fallback
      ? ' drawn objects launched by then' : ' in orbit at year end') + ' (' +
      parts.join(', ') + ')';
  }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.ov-note { font-size: 8px; color: var(--hud-dim, #9a6c00); line-height: 1.35;',
    '  margin: 3px 0 5px; }',
    '.ov-status { font-size: 9px; color: var(--hud-color, #ffb000); min-height: 11px;',
    '  margin: 2px 0; line-height: 1.35; flex: 1; }',
    '.ov-select, .ov-input { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.6);',
    '  border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000); font: inherit;',
    '  font-size: 9px; padding: 3px 4px; margin-bottom: 4px; }',
    '.ov-row { display: flex; gap: 3px; margin-bottom: 4px; flex-wrap: wrap;',
    '  align-items: center; }',
    '.ov-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.ov-row .ov-input, .ov-row .ov-select { flex: 1; margin-bottom: 0; }',
    '.ov-legend { display: flex; flex-direction: column; gap: 1px; margin: 3px 0; }',
    '.ov-item { display: flex; align-items: center; gap: 4px; width: 100%; text-align: left;',
    '  background: none; border: 0; border-bottom: 1px solid rgba(255,176,0,0.08);',
    '  color: var(--hud-color, #ffb000); font: inherit; font-size: 9px; padding: 2px;',
    '  cursor: pointer; }',
    '.ov-item:hover { background: rgba(255,176,0,0.08); }',
    '.ov-item.active { background: rgba(255,176,0,0.16); }',
    '.ov-item.muted { opacity: 0.45; }',
    '.ov-swatch { width: 8px; height: 8px; flex: none; border-radius: 50%;',
    '  display: inline-block; box-shadow: 0 0 0 1px rgba(0,0,0,0.8); }',
    '.ov-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
    '.ov-count { color: var(--hud-dim, #9a6c00); font-size: 8px; }',
    '.ov-list { max-height: 170px; overflow-y: auto; margin-top: 3px; }',
    '.ov-watch { border-bottom: 1px solid rgba(255,176,0,0.08); padding: 2px 0 3px; }',
    '.ov-watch-head { display: flex; gap: 3px; align-items: center; }',
    '.ov-watch-head .ov-item, .ov-watch .ov-item { border: 0; }',
    '.ov-watch-line { font-size: 8px; color: var(--hud-dim, #9a6c00); padding-left: 3px;',
    '  line-height: 1.35; }',
    '.ov-mini { background: none; border: 1px solid rgba(255,176,0,0.25);',
    '  color: var(--hud-dim, #9a6c00); font: inherit; font-size: 8px; padding: 1px 4px;',
    '  cursor: pointer; }',
    '.ov-sub { font-size: 8px; color: var(--hud-dim, #9a6c00); margin: 5px 0 2px;',
    '  letter-spacing: 0.5px; }',
    '.ov-canvas { display: block; width: 100%; background: rgba(0,0,0,0.45);',
    '  border: 1px solid rgba(255,176,0,0.15); margin: 3px 0; }',
    '.ov-slider { width: 100%; margin: 2px 0 4px; }'
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

  function swatch(color) {
    var s = el('span', 'ov-swatch');
    s.style.background = color;
    return s;
  }

  function itemRow(color, label, count, title) {
    var b = button('ov-item', null, title);
    if (color) b.appendChild(swatch(color));
    b.appendChild(el('span', 'ov-label', label));
    if (count != null) b.appendChild(el('span', 'ov-count', count));
    return b;
  }

  function setToggle(btn, on) {
    if (btn.classList.contains('active') !== !!on) btn.classList.toggle('active');
  }

  function loadJson(key, fallback) {
    try {
      var raw = global.localStorage && global.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }

  function saveJson(key, value) {
    try { if (global.localStorage) global.localStorage.setItem(key, JSON.stringify(value)); }
    catch (e) { /* private mode or full: keep going */ }
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: true, mode: 'default', focus: null, counts: [], total: 0,
      attrs: null, attrsState: 'idle', colors: {}, panels: {},
      watch: cleanWatch(loadJson(LS_WATCH, [])), watchInfo: {}, watchHighlight: false,
      observer: cleanObserver(loadJson(LS_OBSERVER, null)),
      planes: {}, planesAtMs: NaN, ringsOn: {}, ringPolys: [], focusPlane: null,
      planeKey: null, debris: null, debrisState: 'idle', debrisKey: null,
      history: null, historyState: 'idle', histYear: null, histTimer: null, histShown: 0,
      shadowOn: false, footOn: false, stripOn: false, footMinEl: 10,
      lastFoot: 0, lastSlow: 0, lastAge: 0, lastPaintMs: NaN, stripKey: '', seq: 0
    };

    function engine() { return ctx.satEngine || { sats: [], byNoradId: {} }; }
    function sats() { return engine().sats || []; }
    function simMs() {
      try {
        var ms = toMs(ctx.simNow ? ctx.simNow() : null);
        return isNum(ms) ? ms : Date.now();
      } catch (e) { return Date.now(); }
    }

    function request(path, opts) {
      if (ctx.isStatic) return Promise.reject(new Error('static build'));
      return global.fetch(api + path, opts).then(function (r) {
        if (!r.ok) { var err = new Error('HTTP ' + r.status); err.status = r.status; throw err; }
        return r.json();
      });
    }
    function getJson(path) { return request(path); }
    function postJson(path, body) {
      return request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    }

    MODES.forEach(function (md) {
      if (!md.classes) return;
      m.colors[md.key] = md.classes.map(function (c) {
        return C.Color.fromCssColorString(c.color);
      });
    });

    // -- per-record facts, cached on the record --------------------------

    function info(s) {
      var c = s._ov;
      if (c && c.l1 === s.line1 && c.l2 === s.line2) return c;
      var l1 = parseLine1(s.line1);
      var elements = parseLine2(s.line2);
      c = { l1: s.line1, l2: s.line2, designator: l1.designator, launchYear: l1.launchYear,
        epochMs: l1.epochMs, el: elements, regime: regimeIndex(elements),
        constellation: constellationOf(s.name), nameType: typeFromName(s.name) };
      s._ov = c;
      return c;
    }

    function satcatIndex(s) {
      if (!m.attrs) return -1;
      var i = m.attrs.index[s.noradId];
      return i === undefined ? -1 : i;
    }

    function launchYearOf(s) {
      var i = satcatIndex(s);
      if (i >= 0 && m.attrs.years[i]) return m.attrs.years[i];
      return info(s).launchYear;
    }

    function typeOf(s) {
      var i = satcatIndex(s);
      return i >= 0 ? typeFromCode(m.attrs.types.charAt(i)) : info(s).nameType;
    }

    // -- colour modes ----------------------------------------------------

    var scratchSun = {};
    var scratchPos = {};

    function classIndex(key, s, now, sun) {
      switch (key) {
        case 'type': return typeOf(s);
        case 'owner': {
          var i = satcatIndex(s);
          if (i < 0) return OWNER_CODES.length + 1;
          return ownerClass(m.attrs.owners[m.attrs.ownerIndex[i]]);
        }
        case 'regime': return info(s).regime;
        case 'age': return ageBucket((now - info(s).epochMs) / DAY_MS);
        case 'rcs': {
          var j = satcatIndex(s);
          return j < 0 ? 4 : rcsFromCode(m.attrs.rcs.charAt(j));
        }
        case 'decade': return decadeIndex(launchYearOf(s));
        case 'sunlight': {
          if (s.located !== true || !isNum(s.lastLat) || !isNum(s.lastLon)) return 3;
          var p = geodeticToEarthFixed(s.lastLat, s.lastLon, s.lastAlt || 0, scratchPos);
          return shadowClass(p.x, p.y, p.z, sun);
        }
        default: return -1;
      }
    }

    function outlineOf(col) {
      if (!col._ovOutline) col._ovOutline = col.withAlpha(0.6);
      return col._ovOutline;
    }
    function dimOf(col) {
      if (!col._ovDim) col._ovDim = col.withAlpha(DIM_ALPHA);
      return col._ovDim;
    }

    function inFocus(s, cls) {
      var f = m.focus;
      if (!f) return true;
      if (f.ids) return !!f.ids[s.noradId];
      return cls === f.cls;
    }

    // One pass over the catalog: class, colour, count. Shared colour objects, so
    // nothing is allocated per object once the dim and outline variants exist.
    function paint() {
      var key = m.enabled ? m.mode : 'default';
      var md = MODE_BY_KEY[key];
      var list = sats();
      var colors = m.colors[key];
      var counts = md.classes ? md.classes.map(function () { return 0; }) : [];
      var now = simMs();
      var sun = key === 'sunlight' ? sunEarthFixed(now, scratchSun) : null;
      var sel = engine().selectedId;
      var useFocus = m.enabled && m.focus;
      var total = 0;
      for (var i = 0; i < list.length; i++) {
        var s = list[i];
        if (!s || typeof s !== 'object' || !s.point) continue;
        if (s._ovBase === undefined) s._ovBase = s.color;
        total++;
        var cls = colors ? classIndex(key, s, now, sun) : -1;
        if (cls >= 0) counts[cls]++;
        var col = cls >= 0 ? colors[cls] : s._ovBase;
        if (useFocus && s.noradId !== sel && !inFocus(s, cls)) col = dimOf(col);
        if (s.color !== col) {
          s.color = col;
          s.point.color = col;
          if (s.noradId !== sel) s.point.outlineColor = outlineOf(col);
        }
      }
      m.counts = counts;
      m.total = total;
      m.lastPaintMs = now;
      engine().lastLabelRebuild = 0;
      renderLegend();
      return counts;
    }

    function setMode(key) {
      if (!MODE_BY_KEY[key]) key = 'default';
      m.mode = key;
      if (m.focus && !m.focus.ids) m.focus = null;
      saveJson(LS_MODE, key);
      if (m.modeSelect.value !== key) m.modeSelect.value = key;
      if (MODE_BY_KEY[key].satcat || key === 'decade') ensureAttributes();
      paint();
      renderFocus();
      return key;
    }

    function setFocus(focus) {
      m.focus = focus || null;
      if (!focus || !focus.plane) clearPlaneHighlight();
      if (!focus || focus.source !== 'watch') {
        m.watchHighlight = false;
        setToggle(m.watchHighlightBtn, false);
      }
      if (!focus || focus.source !== 'debris') m.debrisKey = null;
      paint();
      renderFocus();
      renderDebris();
    }

    // -- SATCAT attributes -----------------------------------------------

    function ensureAttributes() {
      if (m.attrs || m.attrsState === 'loading') return;
      if (ctx.isStatic) { m.attrsState = 'static'; renderLegend(); return; }
      m.attrsState = 'loading';
      renderLegend();
      getJson('/api/orbitviz/attributes').then(function (body) {
        m.attrs = decodeAttributes(body);
        m.attrsState = m.attrs ? 'ready' : 'bad';
        paint();
        if (m.histYear !== null) applyHistory();
        if (m.debris && m.debris.local) { m.debris = localDebris(); renderDebris(); }
      }).catch(function (err) {
        m.attrsState = err && err.status === 503 ? 'missing' : 'failed';
        renderLegend();
      });
    }

    // -- panels ----------------------------------------------------------

    function panel(key, title, collapsed, onOpen) {
      var root = el('div', 'panel' + (collapsed ? ' collapsed' : ''));
      var head = el('h3', null, title);
      var body = el('div', 'panel-body');
      root.appendChild(head);
      root.appendChild(body);
      head.addEventListener('click', function (e) {
        try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
        root.classList.toggle('collapsed');
        if (!root.classList.contains('collapsed') && onOpen) onOpen();
      });
      ctx.mount.appendChild(root);
      m.panels[key] = { root: root, head: head, body: body };
      return body;
    }

    function isOpen(key) {
      return !!(m.panels[key] && !m.panels[key].root.classList.contains('collapsed'));
    }

    // Colour by
    var colourBody = panel('colour', 'Colour by', false);
    m.modeSelect = el('select', 'ov-select');
    MODES.forEach(function (md) {
      var o = el('option', null, md.label);
      o.value = md.key;
      m.modeSelect.appendChild(o);
    });
    m.modeSelect.addEventListener('change', function () { setMode(m.modeSelect.value); });
    m.legend = el('div', 'ov-legend');
    var focusRow = el('div', 'ov-row');
    m.focusLine = el('div', 'ov-status');
    m.focusClear = button('ov-mini', 'CLEAR', 'Show every object at full colour');
    m.focusClear.addEventListener('click', function () { setFocus(null); });
    focusRow.appendChild(m.focusLine);
    focusRow.appendChild(m.focusClear);
    m.modeNote = el('div', 'ov-note');
    [m.modeSelect, m.legend, focusRow, m.modeNote].forEach(function (n) {
      colourBody.appendChild(n);
    });

    function renderFocus() {
      var f = m.enabled ? m.focus : null;
      m.focusLine.textContent = f ? 'Highlighting ' + f.label : '';
      m.focusClear.style.display = f ? '' : 'none';
    }

    function renderLegend() {
      var key = m.enabled ? m.mode : 'default';
      var md = MODE_BY_KEY[key];
      m.legend.replaceChildren();
      var noteText = md.note;
      if (md.satcat || key === 'decade') {
        if (m.attrsState === 'loading') noteText += ' Loading SATCAT...';
        else if (m.attrsState === 'static') noteText += ' SATCAT needs the live server.';
        else if (m.attrsState === 'missing') {
          noteText += ' SATCAT is not cached on the server yet (open the object dossier once).';
        } else if (m.attrsState === 'failed' || m.attrsState === 'bad') {
          noteText += ' SATCAT did not load.';
        } else if (m.attrs) {
          noteText += ' SATCAT: ' + fmtInt(m.attrs.count) + ' objects on orbit.';
        }
      }
      m.modeNote.textContent = noteText;
      if (!md.classes) {
        m.legend.appendChild(el('div', 'ov-note', fmtInt(m.total) +
          ' objects drawn in the page colours.'));
        return;
      }
      md.classes.forEach(function (c, i) {
        var label = c.label;
        if (key === 'owner' && i < OWNER_CODES.length && m.attrs) {
          var hit = m.attrs.ownerList.filter(function (o) {
            return o && o.code === OWNER_CODES[i];
          })[0];
          if (hit && hit.name) label = hit.name;
        }
        var row = itemRow(c.color, label, fmtInt(m.counts[i] || 0),
          'Show only this class at full colour');
        var classFocus = m.focus && !m.focus.ids;
        if (classFocus && m.focus.cls === i) row.classList.add('active');
        else if (classFocus) row.classList.add('muted');
        row.addEventListener('click', function () {
          if (m.focus && !m.focus.ids && m.focus.cls === i) setFocus(null);
          else setFocus({ cls: i, label: label + ' (' + md.label.toLowerCase() + ')' });
        });
        m.legend.appendChild(row);
      });
    }

    // Watchlist
    var watchBody = panel('watch', 'Watchlist', true, function () { refreshWatch(false); });
    var watchRow = el('div', 'ov-row');
    m.starBtn = button('mode-btn', 'STAR SELECTED', 'Add or remove the selected object');
    m.watchHighlightBtn = button('mode-btn', 'HIGHLIGHT', 'Dim everything not on the watchlist');
    var checkBtn = button('mode-btn', 'CHECK NOW', 'Ask for passes, conjunctions and flags');
    [m.starBtn, m.watchHighlightBtn, checkBtn].forEach(function (b) { watchRow.appendChild(b); });
    m.watchStatus = el('div', 'ov-status');
    m.watchList = el('div', 'ov-list');
    watchBody.appendChild(watchRow);
    watchBody.appendChild(m.watchStatus);
    watchBody.appendChild(m.watchList);
    watchBody.appendChild(el('div', 'ov-note', 'Up to ' + WATCH_MAX + ' objects, kept in ' +
      'this browser. Passes are for the observer set under Visible tonight, the next ' +
      PASS_HOURS + ' hours above ' + PASS_MIN_EL + ' deg.'));

    function isWatched(id) {
      id = Number(id);
      return m.watch.some(function (w) { return w.id === id; });
    }

    function toggleWatch(id, name) {
      id = Number(id);
      if (!isNum(id) || id <= 0) return false;
      if (isWatched(id)) {
        m.watch = m.watch.filter(function (w) { return w.id !== id; });
        delete m.watchInfo[id];
      } else {
        if (m.watch.length >= WATCH_MAX) {
          m.watchStatus.textContent = 'The watchlist holds ' + WATCH_MAX + ' objects.';
          return false;
        }
        var rec = engine().byNoradId[id];
        m.watch.push({ id: id, name: String(name || (rec && rec.name) || ('NORAD ' + id))
          .slice(0, 80) });
      }
      saveJson(LS_WATCH, m.watch);
      if (m.watchHighlight) highlightWatch(true);
      renderWatch();
      updateStarButton();
      var on = isWatched(id);
      if (on) checkOne(id);
      return on;
    }

    function updateStarButton() {
      var sel = engine().selectedId;
      m.starBtn.textContent = sel != null && isWatched(sel) ? 'UNSTAR SELECTED'
        : 'STAR SELECTED';
    }

    function highlightWatch(on) {
      if (!on) {
        m.watchHighlight = false;
        setToggle(m.watchHighlightBtn, false);
        if (m.focus && m.focus.source === 'watch') setFocus(null);
        return false;
      }
      var ids = Object.create(null);
      m.watch.forEach(function (w) { ids[w.id] = true; });
      setFocus({ ids: ids, source: 'watch', label: 'the watchlist (' + m.watch.length + ')' });
      m.watchHighlight = true;
      setToggle(m.watchHighlightBtn, true);
      return true;
    }

    function pageConjunctions() {
      try { return ctx.getConjunctions ? ctx.getConjunctions() || [] : []; }
      catch (e) { return []; }
    }

    function renderWatch() {
      m.watchList.replaceChildren();
      if (!m.watch.length) {
        m.watchStatus.textContent = 'Select an object, then STAR SELECTED.';
        return;
      }
      m.watchStatus.textContent = m.watch.length + ' watched.';
      var page = pageConjunctions();
      m.watch.forEach(function (w) {
        var box = el('div', 'ov-watch');
        var head = el('div', 'ov-watch-head');
        var drawn = !!engine().byNoradId[w.id];
        var name = itemRow(null, w.name || ('NORAD ' + w.id), String(w.id),
          drawn ? 'Select on the globe' : 'Not in the drawn catalog');
        if (!drawn) name.classList.add('muted');
        name.addEventListener('click', function () {
          if (engine().byNoradId[w.id] && ctx.selectSatellite) ctx.selectSatellite(w.id);
        });
        var remove = button('ov-mini', 'REMOVE', 'Take off the watchlist');
        remove.addEventListener('click', function () { toggleWatch(w.id); });
        head.appendChild(name);
        head.appendChild(remove);
        box.appendChild(head);
        var inf = m.watchInfo[w.id] || {};
        box.appendChild(el('div', 'ov-watch-line', inf.pass || ''));
        box.appendChild(el('div', 'ov-watch-line', inf.conj ||
          conjunctionText(w.id, newestConjunction(w.id, [page]))));
        box.appendChild(el('div', 'ov-watch-line', inf.flags || ''));
        m.watchList.appendChild(box);
      });
    }

    function checkOne(id) {
      var inf = m.watchInfo[id] = m.watchInfo[id] || {};
      var page = pageConjunctions();
      if (ctx.isStatic) {
        inf.pass = 'Passes and element history need the live server.';
        inf.conj = conjunctionText(id, newestConjunction(id, [page]));
        inf.flags = '';
        renderWatch();
        return Promise.resolve(inf);
      }
      var jobs = [];
      if (m.observer) {
        jobs.push(postJson('/passes', { observer: observerBody(m.observer), norad_id: id,
          start: new Date(simMs()).toISOString(), hours: PASS_HOURS,
          min_elevation_deg: PASS_MIN_EL }).then(function (body) {
          inf.pass = passText(nextPass(body && body.passes, simMs()));
        }, function (err) {
          inf.pass = err && err.status === 404 ? 'Not in the server catalog, no passes.'
            : 'Passes did not load.';
        }));
      } else {
        inf.pass = 'Set an observer under Visible tonight for passes.';
      }
      jobs.push(getJson('/conjunctions/history?norad_id=' + id + '&limit=50').then(function (list) {
        inf.conj = conjunctionText(id, newestConjunction(id, [list, page]));
      }, function () {
        inf.conj = conjunctionText(id, newestConjunction(id, [page]));
      }));
      jobs.push(getJson('/tle/history/' + id + '/maneuvers').then(function (body) {
        inf.flags = flagText(body);
      }, function (err) {
        inf.flags = err && err.status === 404 ? 'No element history archived.'
          : 'Element history did not load.';
      }));
      return Promise.all(jobs).then(function () { renderWatch(); return inf; });
    }

    // At most CONCURRENCY objects in flight.
    function refreshWatch(force) {
      var ids = m.watch.map(function (w) { return w.id; }).filter(function (id) {
        return force || !m.watchInfo[id];
      });
      renderWatch();
      var at = 0;
      function next() {
        if (at >= ids.length) return Promise.resolve();
        var id = ids[at++];
        return checkOne(id).then(next, next);
      }
      var workers = [];
      for (var k = 0; k < Math.min(CONCURRENCY, ids.length); k++) workers.push(next());
      return Promise.all(workers).then(function () { renderWatch(); return ids.length; });
    }

    m.starBtn.addEventListener('click', function () {
      var sel = engine().selectedId;
      if (sel == null) { m.watchStatus.textContent = 'Select an object on the globe first.'; return; }
      var rec = engine().byNoradId[sel];
      toggleWatch(Number(sel), rec && rec.name);
    });
    m.watchHighlightBtn.addEventListener('click', function () {
      highlightWatch(!m.watchHighlight);
    });
    checkBtn.addEventListener('click', function () { refreshWatch(true); });

    // Visible tonight (and the shared observer)
    var visBody = panel('visible', 'Visible tonight', true);
    var obsRow = el('div', 'ov-row');
    m.obsLat = el('input', 'ov-input');
    m.obsLon = el('input', 'ov-input');
    m.obsAlt = el('input', 'ov-input');
    [[m.obsLat, 'Lat deg'], [m.obsLon, 'Lon deg'], [m.obsAlt, 'Alt m']].forEach(function (pair) {
      pair[0].setAttribute('type', 'text');
      pair[0].setAttribute('placeholder', pair[1]);
      pair[0].setAttribute('title', pair[1]);
      pair[0].addEventListener('change', function () { readObserverFields(); });
      obsRow.appendChild(pair[0]);
    });
    var obsBtns = el('div', 'ov-row');
    var fromPassBtn = button('mode-btn', 'FROM PASS PANEL', 'Copy the pass panel observer');
    var locateBtn = button('mode-btn', 'MY LOCATION', 'Ask the browser for this location');
    var findBtn = button('mode-btn', 'FIND PASSES', 'Brightest visible passes, next 24 h');
    [fromPassBtn, locateBtn, findBtn].forEach(function (b) { obsBtns.appendChild(b); });
    m.obsStatus = el('div', 'ov-status');
    m.visStatus = el('div', 'ov-status');
    m.visList = el('div', 'ov-list');
    [obsRow, obsBtns, m.obsStatus, m.visStatus, m.visList].forEach(function (n) {
      visBody.appendChild(n);
    });
    visBody.appendChild(el('div', 'ov-note', 'Sunlit passes with the Sun at least 6 deg ' +
      'below the horizon, brightest first, from the ISS, the stations, the CelesTrak ' +
      'visual list and the watchlist. Magnitudes are estimates (standard magnitude, ' +
      'range and phase), not measurements.'));

    function showObserver() {
      var o = m.observer;
      m.obsLat.value = o ? String(o.lat) : '';
      m.obsLon.value = o ? String(o.lon) : '';
      m.obsAlt.value = o ? String(o.altM || 0) : '';
      m.obsStatus.textContent = o ? 'Observer ' + o.lat.toFixed(3) + ', ' + o.lon.toFixed(3) +
        ', ' + Math.round(o.altM || 0) + ' m.' : 'No observer yet.';
    }

    function setObserver(o, why) {
      var clean = cleanObserver(o);
      if (!clean) {
        m.obsStatus.textContent = why || 'That is not a valid observer.';
        return false;
      }
      m.observer = clean;
      saveJson(LS_OBSERVER, clean);
      showObserver();
      Object.keys(m.watchInfo).forEach(function (id) { delete m.watchInfo[id].pass; });
      m.stripKey = '';
      return true;
    }

    function validate(raw) {
      var pp = global.SDAPassPanel;
      if (pp && typeof pp.validateObserver === 'function') {
        var r = pp.validateObserver(raw);
        if (!r || !r.ok) return { ok: false, error: r && r.error };
      }
      if (String(raw.lat == null ? '' : raw.lat).trim() === '' ||
          String(raw.lon == null ? '' : raw.lon).trim() === '') {
        return { ok: false, error: 'Enter a latitude and a longitude.' };
      }
      var alt = raw.alt == null || String(raw.alt).trim() === '' ? 0 : Number(raw.alt);
      var o = cleanObserver({ lat: Number(raw.lat), lon: Number(raw.lon), altM: alt });
      return o ? { ok: true, observer: o } : { ok: false, error: 'That is not a valid observer.' };
    }

    function readObserverFields() {
      var r = validate({ lat: m.obsLat.value, lon: m.obsLon.value, alt: m.obsAlt.value });
      if (!r.ok) {
        m.obsStatus.textContent = r.error || 'That is not a valid observer.';
        return false;
      }
      return setObserver(r.observer);
    }

    fromPassBtn.addEventListener('click', function () {
      var doc = global.document;
      function val(id) { var n = doc.getElementById(id); return n ? n.value : ''; }
      var r = validate({ lat: val('passpanel-latitude'), lon: val('passpanel-longitude'),
        alt: val('passpanel-altitude') });
      if (!r.ok) {
        m.obsStatus.textContent = 'The pass panel has no observer: ' + (r.error || '');
        return;
      }
      setObserver(r.observer);
    });

    locateBtn.addEventListener('click', function () {
      var geo = global.navigator && global.navigator.geolocation;
      if (!geo) { m.obsStatus.textContent = 'Location is not available in this browser.'; return; }
      m.obsStatus.textContent = 'Asking the browser for the location...';
      geo.getCurrentPosition(function (pos) {
        var c = pos && pos.coords;
        setObserver({ lat: c && c.latitude, lon: c && c.longitude,
          altM: c && isNum(c.altitude) ? c.altitude : 0 });
      }, function () {
        m.obsStatus.textContent = 'The browser did not give a location.';
      }, { timeout: 10000, maximumAge: 600000 });
    });

    function findVisible() {
      if (ctx.isStatic) {
        m.visStatus.textContent = 'Visible passes need the live server.';
        return Promise.resolve(null);
      }
      if (!m.observer) {
        m.visStatus.textContent = 'Set an observer first.';
        return Promise.resolve(null);
      }
      var seq = ++m.seq;
      m.visStatus.textContent = 'Searching the next ' + PASS_HOURS + ' hours...';
      m.visList.replaceChildren();
      var body = { observer: observerBody(m.observer), start: new Date(simMs()).toISOString(),
        hours: PASS_HOURS, min_elevation_deg: PASS_MIN_EL, max_magnitude: 4.5, limit: 20,
        norad_ids: m.watch.map(function (w) { return w.id; }).slice(0, 50) };
      return postJson('/api/orbitviz/visible', body).then(function (res) {
        if (seq === m.seq) renderVisible(res);
        return res;
      }, function (err) {
        if (seq !== m.seq) return null;
        m.visStatus.textContent = err && err.status === 503
          ? 'The catalog is still loading on the server; try again shortly.'
          : 'Visible passes did not load.';
        return null;
      });
    }

    function renderVisible(res) {
      var passes = (res && res.passes) || [];
      var dark = res && isNum(res.dark_minutes) ? res.dark_minutes : 0;
      m.visList.replaceChildren();
      if (!passes.length) {
        m.visStatus.textContent = dark ? 'No bright pass in the next ' + PASS_HOURS +
          ' hours from ' + fmtInt(res.candidates) + ' candidates.'
          : 'The sky does not get dark here in the next ' + PASS_HOURS + ' hours.';
        return;
      }
      m.visStatus.textContent = passes.length + ' passes from ' + fmtInt(res.candidates) +
        ' candidates; ' + (dark / 60).toFixed(1) + ' h of dark sky.';
      passes.forEach(function (p) {
        var vs = p.visible_start || {}, ve = p.visible_end || {}, vp = p.visible_peak || {};
        var mag = isNum(p.magnitude) ? (p.magnitude >= 0 ? '+' : '') + p.magnitude.toFixed(1)
          : '?';
        var text = mag + '  ' + (p.name || p.norad_id) + '  ' + formatHm(vs.time) + ' to ' +
          formatHm(ve.time) + ' UTC';
        var meta = 'max ' + (isNum(vp.elevation_deg) ? Math.round(vp.elevation_deg) : '?') +
          ' deg, ' + (vs.compass || '?') + ' to ' + (ve.compass || '?');
        var row = itemRow(null, text, meta, formatUtc(vs.time).slice(0, 10) +
          ', select on the globe');
        if (!engine().byNoradId[p.norad_id]) row.classList.add('muted');
        row.addEventListener('click', function () {
          if (engine().byNoradId[p.norad_id] && ctx.selectSatellite) {
            ctx.selectSatellite(p.norad_id);
          }
        });
        m.visList.appendChild(row);
      });
    }

    findBtn.addEventListener('click', function () { findVisible(); });

    // Constellations
    var constBody = panel('const', 'Constellations', true, function () { buildPlanes(false); });
    var constRow = el('div', 'ov-row');
    var allRings = button('mode-btn', 'ALL RINGS', 'Draw every constellation plane');
    var noRings = button('mode-btn', 'NO RINGS', 'Remove the plane rings');
    constRow.appendChild(allRings);
    constRow.appendChild(noRings);
    m.constList = el('div', 'ov-legend');
    m.planeTitle = el('div', 'ov-sub');
    m.planeList = el('div', 'ov-list');
    [constRow, m.constList, m.planeTitle, m.planeList].forEach(function (n) {
      constBody.appendChild(n);
    });
    constBody.appendChild(el('div', 'ov-note', 'Planes are grouped from the element sets ' +
      'by inclination, altitude and node, with each node carried to the sim time by its J2 ' +
      'drift. Objects still raising or lowering their orbits form small extra planes. ' +
      'Click a ring or a plane to highlight its members.'));

    function anyRings() {
      return Object.keys(m.ringsOn).some(function (k) { return m.ringsOn[k]; });
    }

    function buildPlanes(force) {
      var now = simMs();
      if (!force && isNum(m.planesAtMs) && Math.abs(now - m.planesAtMs) < PLANE_DRIFT_MS) {
        renderConstellations();
        return m.planes;
      }
      var buckets = {};
      CONSTELLATIONS.forEach(function (c) { buckets[c.key] = []; });
      var list = sats();
      for (var i = 0; i < list.length; i++) {
        var s = list[i];
        if (!s || typeof s !== 'object') continue;
        var f = info(s);
        if (!f.constellation || !f.el) continue;
        var sh = orbitShape(f.el);
        buckets[f.constellation].push({ id: s.noradId, inc: f.el.inc, alt: sh.a - RE,
          raan: raanAt(f.el, f.epochMs, now) });
      }
      var planes = {};
      CONSTELLATIONS.forEach(function (c) {
        planes[c.key] = { count: buckets[c.key].length, planes: clusterPlanes(buckets[c.key], c) };
      });
      m.planes = planes;
      m.planesAtMs = now;
      if (m.focusPlane) {
        var fp = m.focusPlane;
        m.focusPlane = null;
        if (m.focus && m.focus.plane) { m.focus = null; paint(); renderFocus(); }
        if (planes[fp.key] && planes[fp.key].planes[fp.index]) focusPlane(fp.key, fp.index);
      }
      drawRings();
      renderConstellations();
      return planes;
    }

    function renderConstellations() {
      m.constList.replaceChildren();
      CONSTELLATIONS.forEach(function (c) {
        var p = m.planes[c.key] || { count: 0, planes: [] };
        var row = itemRow(c.color, c.name, fmtInt(p.count) + ' / ' + p.planes.length +
          (p.planes.length === 1 ? ' plane' : ' planes'),
          'Draw or remove this constellation\'s rings');
        if (m.ringsOn[c.key]) row.classList.add('active');
        if (!p.count) row.classList.add('muted');
        row.addEventListener('click', function () {
          m.ringsOn[c.key] = !m.ringsOn[c.key];
          m.planeKey = c.key;
          if (!m.ringsOn[c.key] && m.focusPlane && m.focusPlane.key === c.key) setFocus(null);
          drawRings();
          renderConstellations();
        });
        m.constList.appendChild(row);
      });
      renderPlanes();
    }

    function renderPlanes() {
      m.planeList.replaceChildren();
      var key = m.planeKey;
      var c = CONSTELLATIONS.filter(function (x) { return x.key === key; })[0];
      if (!c || !m.planes[key]) { m.planeTitle.textContent = ''; return; }
      var planes = m.planes[key].planes;
      m.planeTitle.textContent = c.name.toUpperCase() + ' PLANES (' + planes.length + ')';
      planes.forEach(function (p, i) {
        var row = itemRow(c.color, 'Plane ' + (i + 1) + ': ' + p.inc.toFixed(1) + ' deg, ' +
          fmtInt(p.alt) + ' km, node ' + p.raan.toFixed(1), String(p.ids.length),
          'Highlight this plane\'s members');
        if (m.focusPlane && m.focusPlane.key === key && m.focusPlane.index === i) {
          row.classList.add('active');
        }
        row.addEventListener('click', function () { focusPlane(key, i); });
        m.planeList.appendChild(row);
      });
    }

    function focusPlane(key, index) {
      var group = m.planes[key];
      var p = group && group.planes[index];
      if (!p) return false;
      if (m.focusPlane && m.focusPlane.key === key && m.focusPlane.index === index) {
        setFocus(null);
        return false;
      }
      var c = CONSTELLATIONS.filter(function (x) { return x.key === key; })[0];
      var ids = Object.create(null);
      p.ids.forEach(function (id) { ids[id] = true; });
      if (!m.ringsOn[key]) { m.ringsOn[key] = true; drawRings(); }
      m.planeKey = key;
      setFocus({ ids: ids, plane: true, label: c.name + ' plane ' + (index + 1) + ' (' +
        p.ids.length + ' objects, ' + p.inc.toFixed(1) + ' deg, ' + fmtInt(p.alt) + ' km)' });
      m.focusPlane = { key: key, index: index };
      highlightRing();
      renderConstellations();
      return true;
    }

    function clearPlaneHighlight() {
      if (!m.focusPlane) return;
      m.focusPlane = null;
      highlightRing();
      renderPlanes();
    }

    // Ring primitives: one collection in the inertial frame, rotated each frame.
    function drawRings() {
      if (!anyRings() && !m.rings) return;
      if (!m.rings) {
        m.rings = viewer.scene.primitives.add(new C.PolylineCollection());
        m.ringMatrix = new C.Matrix3();
      }
      m.rings.removeAll();
      m.ringPolys = [];
      CONSTELLATIONS.forEach(function (c) {
        if (!m.ringsOn[c.key] || !m.planes[c.key]) return;
        var color = C.Color.fromCssColorString(c.color).withAlpha(0.55);
        m.planes[c.key].planes.forEach(function (p, i) {
          var pts = ringPoints(p.inc, p.raan, RE + p.alt, RING_POINTS).map(function (v) {
            return new C.Cartesian3(v[0] * 1000, v[1] * 1000, v[2] * 1000);
          });
          var poly = m.rings.add({
            positions: pts, width: 1, loop: true,
            material: C.Material.fromType('Color', { color: color }),
            id: { type: 'orbitviz-plane', constellation: c.key, plane: i,
              sdaPick: function () { focusPlane(c.key, i); } }
          });
          m.ringPolys.push({ key: c.key, index: i, poly: poly, color: color });
        });
      });
      m.rings.show = m.enabled && anyRings() && is3d();
      highlightRing();
      rotateRings(simMs());
    }

    function highlightRing() {
      m.ringPolys.forEach(function (r) {
        var on = !!(m.focusPlane && m.focusPlane.key === r.key && m.focusPlane.index === r.index);
        try {
          r.poly.width = on ? 3 : 1;
          r.poly.material.uniforms.color = on ? C.Color.WHITE : r.color;
        } catch (e) { /* keep going */ }
      });
    }

    function rotateRings(ms) {
      if (!m.rings) return;
      C.Matrix3.fromRotationZ(-gmstRad(ms), m.ringMatrix);
      C.Matrix4.fromRotationTranslation(m.ringMatrix, C.Cartesian3.ZERO, m.rings.modelMatrix);
    }

    allRings.addEventListener('click', function () {
      buildPlanes(false);
      CONSTELLATIONS.forEach(function (c) { m.ringsOn[c.key] = true; });
      drawRings();
      renderConstellations();
    });
    noRings.addEventListener('click', function () {
      m.ringsOn = {};
      if (m.focus && m.focus.plane) setFocus(null);
      drawRings();
      renderConstellations();
    });

    // Debris clouds
    var debrisBody = panel('debris', 'Debris clouds', true, function () { loadDebris(); });
    m.debrisStatus = el('div', 'ov-status');
    m.debrisList = el('div', 'ov-list');
    debrisBody.appendChild(m.debrisStatus);
    debrisBody.appendChild(m.debrisList);
    debrisBody.appendChild(el('div', 'ov-note', 'Pieces are the debris objects whose ' +
      'international designator starts with the parent launch. On orbit counts every ' +
      'SATCAT piece without a decay date; drawn counts the pieces in the globe catalog. ' +
      'Click a cloud to highlight its pieces.'));

    // Debris records in the drawn catalog, grouped by launch designator.
    function drawnDebris() {
      var by = {};
      var list = sats();
      for (var i = 0; i < list.length; i++) {
        var s = list[i];
        if (!s || typeof s !== 'object' || typeOf(s) !== 2) continue;
        var d = info(s).designator;
        if (d) (by[d] = by[d] || []).push(s.noradId);
      }
      return by;
    }

    function localDebris() {
      var by = drawnDebris();
      var taken = {};
      var events = DEBRIS_EVENTS.map(function (ev) {
        var ids = [];
        ev.designators.forEach(function (d) { taken[d] = true; ids = ids.concat(by[d] || []); });
        return { key: ev.key, name: ev.name, designators: ev.designators,
          event_year: ev.event_year, kind: ev.kind, curated: true, catalogued: null,
          on_orbit: null, decayed: null, ids: ids };
      });
      var found = Object.keys(by).filter(function (d) { return !taken[d] && by[d].length >= 20; })
        .sort(function (a, b) { return by[b].length - by[a].length || (a < b ? -1 : 1); })
        .slice(0, 8)
        .map(function (d) {
          return { key: 'launch-' + d, name: d, designators: [d], event_year: null,
            kind: 'Fragmentation', curated: false, catalogued: null, on_orbit: null,
            decayed: null, ids: by[d] };
        });
      return { events: events, found: found, local: true };
    }

    function loadDebris() {
      if (m.debrisState === 'loading') return Promise.resolve(m.debris);
      if (m.debris && !m.debris.local) { renderDebris(); return Promise.resolve(m.debris); }
      if (ctx.isStatic) {
        m.debris = localDebris();
        m.debrisState = 'local';
        renderDebris();
        return Promise.resolve(m.debris);
      }
      m.debrisState = 'loading';
      m.debrisStatus.textContent = 'Loading SATCAT debris counts...';
      return getJson('/api/orbitviz/debris').then(function (body) {
        m.debris = body;
        m.debrisState = 'ready';
        renderDebris();
        return body;
      }, function () {
        m.debris = localDebris();
        m.debrisState = 'local';
        renderDebris();
        return m.debris;
      });
    }

    function drawnIds(ids) {
      var map = Object.create(null);
      var n = 0;
      var by = engine().byNoradId;
      (ids || []).forEach(function (id) { if (by[id]) { map[id] = true; n++; } });
      return { map: map, count: n };
    }

    function debrisRow(ev) {
      var drawn = drawnIds(ev.ids);
      var title = ev.name + (ev.event_year ? ' (' + ev.event_year + ' ' +
        String(ev.kind || '').toLowerCase() + ')' : '');
      var count = isNum(ev.on_orbit) ? fmtInt(ev.on_orbit) + ' on orbit'
        : fmtInt(drawn.count) + ' drawn';
      var row = itemRow(CAT[2], title, count, 'Highlight the pieces on the globe');
      if (m.debrisKey === ev.key) row.classList.add('active');
      if (!drawn.count) row.classList.add('muted');
      var detail = [(ev.designators || []).join(', ')];
      if (isNum(ev.on_orbit)) {
        detail.push(fmtInt(drawn.count) + ' drawn', fmtInt(ev.catalogued) + ' catalogued',
          fmtInt(ev.decayed) + ' re-entered');
      }
      var box = el('div', 'ov-watch');
      box.appendChild(row);
      box.appendChild(el('div', 'ov-watch-line', detail.join(', ')));
      row.addEventListener('click', function () {
        if (m.debrisKey === ev.key) { setFocus(null); return; }
        var d = drawnIds(ev.ids);
        setFocus({ ids: d.map, source: 'debris', label: ev.name + ' debris (' +
          fmtInt(d.count) + ' drawn)' });
        m.debrisKey = ev.key;
        renderDebris();
      });
      return box;
    }

    function renderDebris() {
      if (!m.debris) return;
      m.debrisList.replaceChildren();
      var d = m.debris;
      m.debrisStatus.textContent = d.local
        ? 'Counted from the drawn catalog only' + (ctx.isStatic ? ' (static build).'
          : ' (SATCAT not available).')
        : 'SATCAT counts' + (d.source ? ' from ' + d.source : '') + '.';
      (d.events || []).forEach(function (ev) { m.debrisList.appendChild(debrisRow(ev)); });
      if ((d.found || []).length) {
        m.debrisList.appendChild(el('div', 'ov-sub', 'OTHER LARGE CLOUDS BY LAUNCH'));
        d.found.forEach(function (ev) { m.debrisList.appendChild(debrisRow(ev)); });
      }
    }

    // Catalog history
    var histBody = panel('history', 'Catalog history', true, function () { loadHistory(); });
    m.histCanvas = el('canvas', 'ov-canvas');
    m.histCanvas.width = 260;
    m.histCanvas.height = 110;
    var histLegend = el('div', 'ov-row');
    TYPE_LABELS.forEach(function (label, t) {
      var item = el('span', 'ov-count');
      item.appendChild(swatch(MODE_BY_KEY.type.classes[t].color));
      item.appendChild(el('span', null, ' ' + label));
      histLegend.appendChild(item);
    });
    m.histSlider = el('input', 'ov-slider');
    m.histSlider.setAttribute('type', 'range');
    m.histReadout = el('div', 'ov-status');
    var histRow = el('div', 'ov-row');
    m.playBtn = button('mode-btn', 'PLAY', 'Step through the years');
    var allYearsBtn = button('mode-btn', 'ALL YEARS', 'Show every object again');
    histRow.appendChild(m.playBtn);
    histRow.appendChild(allYearsBtn);
    m.histNote = el('div', 'ov-note');
    [m.histCanvas, histLegend, m.histSlider, m.histReadout, histRow, m.histNote].forEach(
      function (n) { histBody.appendChild(n); });

    function localHistory() {
      var rows = [];
      var list = sats();
      for (var i = 0; i < list.length; i++) {
        var s = list[i];
        if (!s || typeof s !== 'object') continue;
        rows.push({ year: launchYearOf(s), type: typeOf(s) });
      }
      return historyFromYears(rows, new Date(simMs()).getUTCFullYear());
    }

    function loadHistory() {
      if (m.historyState === 'loading') return Promise.resolve(m.history);
      if (m.history && !m.history.fallback) { drawHistory(); return Promise.resolve(m.history); }
      if (ctx.isStatic) {
        m.historyState = 'local';
        setHistory(localHistory());
        return Promise.resolve(m.history);
      }
      m.historyState = 'loading';
      m.histReadout.textContent = 'Loading SATCAT history...';
      ensureAttributes();
      return getJson('/api/orbitviz/history').then(function (body) {
        m.historyState = 'ready';
        setHistory(body);
        return body;
      }, function () {
        m.historyState = 'local';
        setHistory(localHistory());
        return m.history;
      });
    }

    function setHistory(hist) {
      m.history = hist;
      m.histSlider.min = String(hist.first_year);
      m.histSlider.max = String(hist.last_year);
      m.histSlider.step = '1';
      if (m.histYear === null) m.histSlider.value = String(hist.last_year);
      m.histNote.textContent = hist.fallback
        ? 'Without SATCAT the chart counts the drawn objects by launch year; objects ' +
          'that re-entered are missing.'
        : (hist.definition || '') + ' ' + fmtInt(hist.no_launch_date || 0) +
          ' objects without a launch date are left out. The slider hides drawn objects ' +
          'launched after the year; objects that have re-entered are not on the globe.';
      drawHistory();
      showYear(m.histYear === null ? hist.last_year : m.histYear);
    }

    function showYear(year) {
      if (!m.history) return;
      var text = historyText(m.history, year);
      if (m.histYear !== null) {
        text += '. Globe: ' + fmtInt(m.histShown) + ' drawn objects launched by ' + year + '.';
      }
      m.histReadout.textContent = text;
    }

    function drawHistory(hoverIndex) {
      var hist = m.history;
      var cv = m.histCanvas;
      if (!hist || typeof cv.getContext !== 'function') return;
      var g = cv.getContext('2d');
      if (!g) return;
      var w = cv.width, h = cv.height;
      var left = 28, bottom = 12, top = 6;
      var n = hist.years.length;
      var maxTotal = Math.max.apply(null, hist.total.concat([1]));
      var plotH = h - bottom - top;
      var bw = (w - left - 2) / n;
      g.clearRect(0, 0, w, h);
      g.font = '8px monospace';
      g.lineWidth = 1;
      g.strokeStyle = 'rgba(255,176,0,0.12)';
      g.fillStyle = '#9a6c00';
      [0, 0.5, 1].forEach(function (f) {
        var y = top + plotH * (1 - f);
        g.beginPath(); g.moveTo(left, y + 0.5); g.lineTo(w - 2, y + 0.5); g.stroke();
        g.fillText(f === 0 ? '0' : fmtInt(maxTotal * f / 1000) + 'k', 1, y + 3);
      });
      for (var i = 0; i < n; i++) {
        var x = left + i * bw;
        var y0 = top + plotH;
        g.globalAlpha = hoverIndex === undefined || hoverIndex === i ? 1 : 0.7;
        for (var t = 0; t < TYPE_SERVER_KEYS.length; t++) {
          var series = hist.in_orbit[TYPE_SERVER_KEYS[t]];
          var v = series ? series[i] : 0;
          if (!v) continue;
          var hh = plotH * v / maxTotal;
          g.fillStyle = MODE_BY_KEY.type.classes[t].color;
          g.fillRect(x, y0 - hh, Math.max(1, bw - 1), hh);
          y0 -= hh;
        }
      }
      g.globalAlpha = 1;
      g.fillStyle = '#9a6c00';
      [hist.first_year, 1980, 2000, 2020].forEach(function (yr) {
        var at = yr - hist.first_year;
        if (at >= 0 && at < n) g.fillText(String(yr), Math.min(w - 22, left + at * bw), h - 2);
      });
      var sel = m.histYear === null ? hist.last_year : m.histYear;
      var sx = left + (sel - hist.first_year + 0.5) * bw;
      g.strokeStyle = '#ffffff';
      g.beginPath(); g.moveTo(sx, top); g.lineTo(sx, top + plotH); g.stroke();
    }

    m.histCanvas.addEventListener('mousemove', function (e) {
      var hist = m.history;
      if (!hist) return;
      var bw = (m.histCanvas.width - 30) / hist.years.length;
      var scale = m.histCanvas.clientWidth ? m.histCanvas.width / m.histCanvas.clientWidth : 1;
      var i = Math.floor(((e.offsetX || 0) * scale - 28) / bw);
      if (i < 0 || i >= hist.years.length) return;
      m.histReadout.textContent = historyText(hist, hist.years[i]);
      drawHistory(i);
    });
    m.histCanvas.addEventListener('mouseleave', function () {
      if (!m.history) return;
      drawHistory();
      showYear(m.histYear === null ? m.history.last_year : m.histYear);
    });

    // Hide drawn objects launched after the slider year (bit 4). Only records
    // whose bit changes are rewritten, so a record another filter forced visible
    // keeps that until its own bits change.
    function applyHistory() {
      var year = m.histYear;
      var active = m.enabled && year !== null;
      var list = sats();
      var sel = engine().selectedId;
      var shown = 0;
      for (var i = 0; i < list.length; i++) {
        var rec = list[i];
        if (!rec || typeof rec !== 'object') continue;
        var y = active ? launchYearOf(rec) : 0;
        var out = !!(y && y > year);
        var mask = rec.hideMask | 0;
        var next = out ? (mask | HIDE_BIT) : (mask & ~HIDE_BIT);
        if (next !== mask) { rec.hideMask = next; rec.hidden = next !== 0; }
        if (rec.noradId === sel && rec.hidden) rec.hidden = false;
        if (!out) shown++;
      }
      m.histShown = shown;
      try { if (ctx.refreshSatVisibility) ctx.refreshSatVisibility(); } catch (e) { /* */ }
      return shown;
    }

    function setYear(year) {
      var hist = m.history;
      if (!hist) return null;
      year = Math.round(Number(year));
      if (!isNum(year)) return null;
      year = Math.max(hist.first_year, Math.min(hist.last_year, year));
      m.histYear = year >= hist.last_year ? null : year;
      m.histSlider.value = String(year);
      applyHistory();
      drawHistory();
      showYear(year);
      return year;
    }

    function stopPlay() {
      if (m.histTimer) { global.clearInterval(m.histTimer); m.histTimer = null; }
      m.playBtn.textContent = 'PLAY';
    }

    m.histSlider.addEventListener('input', function () { stopPlay(); setYear(m.histSlider.value); });
    m.playBtn.addEventListener('click', function () {
      if (m.histTimer) { stopPlay(); return; }
      var hist = m.history;
      if (!hist) return;
      setYear(m.histYear === null ? hist.first_year : m.histYear);
      m.playBtn.textContent = 'PAUSE';
      m.histTimer = global.setInterval(function () {
        var next = (m.histYear === null ? hist.last_year : m.histYear) + 1;
        setYear(next);
        if (next >= hist.last_year) stopPlay();
      }, HISTORY_STEP_MS);
    });
    allYearsBtn.addEventListener('click', function () {
      stopPlay();
      if (m.history) setYear(m.history.last_year);
    });

    // Shadow and footprint
    var geoBody = panel('shadow', 'Shadow and footprint', true);
    var geoRow = el('div', 'ov-row');
    m.shadowBtn = button('mode-btn', 'EARTH SHADOW', 'Umbra and penumbra cones at the sim time');
    m.footBtn = button('mode-btn', 'FOOTPRINT', 'Visibility circle under the selected object');
    m.stripBtn = button('mode-btn', 'GROUND TRACK', '2D ground track of the selected object');
    [m.shadowBtn, m.footBtn, m.stripBtn].forEach(function (b) { geoRow.appendChild(b); });
    var maskRow = el('div', 'ov-row');
    maskRow.appendChild(el('span', 'ov-count', 'Elevation mask'));
    m.maskSelect = el('select', 'ov-select');
    [0, 5, 10, 20, 30].forEach(function (d) {
      var o = el('option', null, d + ' deg');
      o.value = String(d);
      m.maskSelect.appendChild(o);
    });
    m.maskSelect.value = String(m.footMinEl);
    maskRow.appendChild(m.maskSelect);
    var sunBtn = button('mode-btn', 'COLOUR BY SUNLIGHT',
      'Colour every object sunlit, penumbra or umbra');
    m.geoStatus = el('div', 'ov-status');
    m.strip = el('canvas', 'ov-canvas');
    m.strip.width = 260;
    m.strip.height = 130;
    m.strip.style.display = 'none';
    [geoRow, maskRow, sunBtn, m.geoStatus, m.strip].forEach(function (n) {
      geoBody.appendChild(n);
    });
    geoBody.appendChild(el('div', 'ov-note', 'The footprint is the ground the object can ' +
      'see down to the horizon (outer ring) and above the elevation mask (inner ring). ' +
      'The shadow is drawn to 60,000 km; rings mark it at the geostationary distance, ' +
      'where satellites pass through it near the equinoxes. The ground track runs half ' +
      'an orbit back and one and a half ahead; the yellow dot is the subsolar point.'));

    sunBtn.addEventListener('click', function () { setMode('sunlight'); });
    m.maskSelect.addEventListener('change', function () {
      var v = Number(m.maskSelect.value);
      m.footMinEl = isNum(v) ? v : 10;
      updateFootprint(true);
    });

    function is3d() {
      try {
        return !C.SceneMode || viewer.scene.mode === undefined ||
          viewer.scene.mode === C.SceneMode.SCENE3D;
      } catch (e) { return true; }
    }

    function ensureShadow() {
      if (m.shadow || m.shadowFailed) return m.shadow;
      try {
        var len = SHADOW_LENGTH_KM * 1000;
        var cone = function (topKm, color) {
          var geom = new C.CylinderGeometry({ length: len, topRadius: topKm * 1000,
            bottomRadius: RE * 1000, slices: 96,
            vertexFormat: C.PerInstanceColorAppearance.VERTEX_FORMAT });
          return viewer.scene.primitives.add(new C.Primitive({
            geometryInstances: new C.GeometryInstance({ geometry: geom,
              attributes: { color: C.ColorGeometryInstanceAttribute.fromColor(color) } }),
            appearance: new C.PerInstanceColorAppearance({ translucent: true, closed: false,
              flat: true }),
            modelMatrix: C.Matrix4.clone(C.Matrix4.IDENTITY),
            asynchronous: false
          }));
        };
        var umbra = cone(umbraRadiusKm(SHADOW_LENGTH_KM),
          C.Color.fromCssColorString('#3987e5').withAlpha(0.14));
        var pen = cone(penumbraRadiusKm(SHADOW_LENGTH_KM),
          C.Color.fromCssColorString('#d55181').withAlpha(0.06));
        var rings = viewer.scene.primitives.add(new C.PolylineCollection());
        var z = (GEO_RADIUS_KM - SHADOW_LENGTH_KM / 2) * 1000;
        [[umbraRadiusKm(GEO_RADIUS_KM), '#3987e5'], [penumbraRadiusKm(GEO_RADIUS_KM), '#d55181']]
          .forEach(function (pair) {
            var pts = [];
            for (var k = 0; k < RING_POINTS; k++) {
              var u = TWO_PI * k / RING_POINTS;
              pts.push(new C.Cartesian3(pair[0] * 1000 * Math.cos(u),
                pair[0] * 1000 * Math.sin(u), z));
            }
            rings.add({ positions: pts, width: 1, loop: true,
              material: C.Material.fromType('Color',
                { color: C.Color.fromCssColorString(pair[1]).withAlpha(0.8) }) });
          });
        m.shadow = { umbra: umbra, pen: pen, rings: rings, rot: new C.Matrix3(),
          shift: new C.Cartesian3(0, 0, 0), sun: {}, axis: null };
      } catch (e) {
        m.shadowFailed = true;
        m.geoStatus.textContent = 'This globe build cannot draw the shadow cone.';
      }
      return m.shadow;
    }

    function showShadow() {
      if (!m.shadow) return;
      var on = !!(m.enabled && m.shadowOn && is3d());
      m.shadow.umbra.show = on;
      m.shadow.pen.show = on;
      m.shadow.rings.show = on;
    }

    // Point the cone axis (local z) away from the Sun; columns of the rotation
    // are the local axes in the Earth-fixed frame.
    function updateShadow(ms) {
      var sh = m.shadow;
      if (!sh || !m.shadowOn) return null;
      var s = sunEarthFixed(ms, sh.sun);
      var zx = -s.x, zy = -s.y, zz = -s.z;
      // helper axis not parallel to z, crossed with z for the local x axis
      var xx, xy, xz;
      if (Math.abs(zz) < 0.9) { xx = -zy; xy = zx; xz = 0; } else { xx = 0; xy = -zz; xz = zy; }
      var xn = Math.sqrt(xx * xx + xy * xy + xz * xz) || 1;
      xx /= xn; xy /= xn; xz /= xn;
      var yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
      var r = sh.rot;
      r[0] = xx; r[1] = xy; r[2] = xz;
      r[3] = yx; r[4] = yy; r[5] = yz;
      r[6] = zx; r[7] = zy; r[8] = zz;
      var half = SHADOW_LENGTH_KM * 500;
      sh.shift.x = zx * half; sh.shift.y = zy * half; sh.shift.z = zz * half;
      C.Matrix4.fromRotationTranslation(r, sh.shift, sh.umbra.modelMatrix);
      C.Matrix4.fromRotationTranslation(r, sh.shift, sh.pen.modelMatrix);
      C.Matrix4.fromRotationTranslation(r, sh.shift, sh.rings.modelMatrix);
      sh.axis = { x: zx, y: zy, z: zz };
      return sh.axis;
    }

    function ensureFootprint() {
      if (m.foot) return m.foot;
      var coll = viewer.scene.primitives.add(new C.PolylineCollection());
      var ring = function (color) {
        var arr = [];
        for (var k = 0; k <= FOOT_POINTS; k++) arr.push(new C.Cartesian3(0, 0, 0));
        var poly = coll.add({ positions: arr, width: 1.5,
          material: C.Material.fromType('Color', { color: color }) });
        return { poly: poly, arr: arr };
      };
      m.foot = { coll: coll, horizon: ring(C.Color.fromCssColorString('#ffb000').withAlpha(0.5)),
        mask: ring(C.Color.fromCssColorString('#ffb000').withAlpha(0.95)) };
      return m.foot;
    }

    function selectedRecord() {
      var id = engine().selectedId;
      return id == null ? null : engine().byNoradId[id] || null;
    }

    function fillCircle(ring, lat, lon, angle) {
      var pts = circlePoints(lat, lon, angle, FOOT_POINTS);
      for (var k = 0; k < pts.length; k++) {
        C.Cartesian3.fromDegrees(pts[k][1], pts[k][0], FOOT_HEIGHT_M, undefined, ring.arr[k]);
      }
      ring.poly.positions = ring.arr;
    }

    function updateFootprint(force) {
      var s = selectedRecord();
      var want = m.enabled && m.footOn && s && s.located === true;
      if (!want) {
        if (m.foot) m.foot.coll.show = false;
        if (m.footOn && m.enabled) {
          m.geoStatus.textContent = s ? 'The selected object has no position yet.'
            : 'Select an object to draw its footprint.';
        }
        return null;
      }
      var f = ensureFootprint();
      var horizon = footprintAngle(s.lastAlt, 0);
      var mask = footprintAngle(s.lastAlt, m.footMinEl);
      fillCircle(f.horizon, s.lastLat, s.lastLon, horizon);
      fillCircle(f.mask, s.lastLat, s.lastLon, mask);
      f.coll.show = true;
      var text = s.name + ': ' + fmtInt(horizon * RE) + ' km to the horizon, ' +
        fmtInt(mask * RE) + ' km above ' + m.footMinEl + ' deg.';
      if (force || text !== m.geoStatus.textContent) m.geoStatus.textContent = text;
      return { horizonKm: horizon * RE, maskKm: mask * RE };
    }

    function stripSatrec(s) {
      var sat = global.satellite;
      if (!sat || typeof sat.twoline2satrec !== 'function') return null;
      if (m.stripRec && m.stripRec.id === s.noradId && m.stripRec.l2 === s.line2) {
        return m.stripRec.satrec;
      }
      var satrec = null;
      try { satrec = sat.twoline2satrec(s.line1, s.line2); } catch (e) { satrec = null; }
      m.stripRec = { id: s.noradId, l2: s.line2, satrec: satrec };
      return satrec;
    }

    // Ground track: half a period back, one and a half ahead (capped at a day).
    function trackPoints(s, ms) {
      var sat = global.satellite;
      var satrec = stripSatrec(s);
      if (!satrec) return null;
      var elements = info(s).el;
      var periodMs = elements ? Math.min(DAY_MS / 2, 1440 / elements.n * 60000) : 5400000;
      var n = 240;
      var t0 = ms - periodMs / 2, span = periodMs * 2;
      var out = [];
      for (var k = 0; k <= n; k++) {
        var t = t0 + span * k / n;
        try {
          var d = new Date(t);
          var pv = sat.propagate(satrec, d);
          if (!pv || !pv.position || typeof pv.position !== 'object') { out.push(null); continue; }
          var gd = sat.eciToGeodetic(pv.position, sat.gstime(d));
          out.push({ lat: gd.latitude / DEG, lon: gd.longitude / DEG, past: t < ms });
        } catch (e) { out.push(null); }
      }
      return out;
    }

    // Natural Earth II, the two level-0 tiles Cesium ships (public domain).
    function loadBasemap() {
      if (m.basemap || typeof global.Image !== 'function' || !C.buildModuleUrl) return;
      m.basemap = [];
      ['0/0/0.jpg', '0/1/0.jpg'].forEach(function (tile, i) {
        var img = new global.Image();
        img.onload = function () { m.stripKey = ''; };
        img.src = C.buildModuleUrl('Assets/Textures/NaturalEarthII/' + tile);
        m.basemap[i] = img;
      });
    }

    function drawStrip(ms, force) {
      var cv = m.strip;
      var s = selectedRecord();
      if (!m.stripOn || !m.enabled) { cv.style.display = 'none'; return false; }
      cv.style.display = '';
      if (!s) {
        if (!m.footOn) m.geoStatus.textContent = 'Select an object to draw its ground track.';
        return false;
      }
      var key = s.noradId + ':' + Math.floor(ms / 30000) + ':' +
        (m.observer ? m.observer.lat + ',' + m.observer.lon : '');
      if (!force && key === m.stripKey) return false;
      m.stripKey = key;
      var pts = trackPoints(s, ms);
      m.stripPoints = pts ? pts.length : 0;
      if (!pts && !m.footOn) m.geoStatus.textContent = 'The ground track needs satellite.js.';
      if (typeof cv.getContext !== 'function') return !!pts;
      var g = cv.getContext('2d');
      if (!g) return !!pts;
      loadBasemap();
      var w = cv.width, h = cv.height;
      var X = function (lon) { return (lon + 180) / 360 * w; };
      var Y = function (lat) { return (90 - lat) / 180 * h; };
      g.clearRect(0, 0, w, h);
      g.fillStyle = '#050a0e';
      g.fillRect(0, 0, w, h);
      (m.basemap || []).forEach(function (img, i) {
        if (img && img.complete && img.naturalWidth) {
          try { g.drawImage(img, i * w / 2, 0, w / 2, h); } catch (e) { /* not loaded */ }
        }
      });
      var sun = subsolarPoint(ms);
      g.fillStyle = 'rgba(0,0,0,0.55)';
      for (var px = 0; px < w; px += 2) {
        var night = nightSpan(px / w * 360 - 180, sun.lat, sun.lon);
        if (night) g.fillRect(px, Y(night.to), 2, Y(night.from) - Y(night.to));
      }
      g.strokeStyle = 'rgba(255,176,0,0.15)';
      g.lineWidth = 1;
      for (var lon = -150; lon <= 150; lon += 30) {
        g.beginPath(); g.moveTo(X(lon) + 0.5, 0); g.lineTo(X(lon) + 0.5, h); g.stroke();
      }
      for (var lat = -60; lat <= 60; lat += 30) {
        g.beginPath(); g.moveTo(0, Y(lat) + 0.5); g.lineTo(w, Y(lat) + 0.5); g.stroke();
      }
      if (pts) {
        g.lineWidth = 1.5;
        var prev = null;
        for (var k = 0; k < pts.length; k++) {
          var p = pts[k];
          if (!p) { prev = null; continue; }
          if (prev && Math.abs(p.lon - prev.lon) < 180) {
            g.strokeStyle = p.past ? 'rgba(255,176,0,0.45)' : '#ffb000';
            g.beginPath(); g.moveTo(X(prev.lon), Y(prev.lat)); g.lineTo(X(p.lon), Y(p.lat));
            g.stroke();
          }
          prev = p;
        }
      }
      g.fillStyle = '#ffd34d';
      g.beginPath(); g.arc(X(sun.lon), Y(sun.lat), 3, 0, TWO_PI); g.fill();
      if (m.observer) {
        g.strokeStyle = '#ffffff';
        var ox = X(m.observer.lon), oy = Y(m.observer.lat);
        g.beginPath(); g.moveTo(ox - 4, oy); g.lineTo(ox + 4, oy); g.moveTo(ox, oy - 4);
        g.lineTo(ox, oy + 4); g.stroke();
      }
      if (s.located === true) {
        g.fillStyle = '#ffffff';
        g.beginPath(); g.arc(X(s.lastLon), Y(s.lastLat), 3, 0, TWO_PI); g.fill();
      }
      return true;
    }

    m.shadowBtn.addEventListener('click', function () {
      m.shadowOn = !m.shadowOn;
      setToggle(m.shadowBtn, m.shadowOn);
      if (m.shadowOn) ensureShadow();
      showShadow();
      updateShadow(simMs());
    });
    m.footBtn.addEventListener('click', function () {
      m.footOn = !m.footOn;
      setToggle(m.footBtn, m.footOn);
      if (!m.footOn) m.geoStatus.textContent = '';
      updateFootprint(true);
    });
    m.stripBtn.addEventListener('click', function () {
      m.stripOn = !m.stripOn;
      setToggle(m.stripBtn, m.stripOn);
      drawStrip(simMs(), true);
    });

    // -- frame loop --------------------------------------------------------

    function julianToMs(t) {
      var leap = 37;
      try {
        if (C.JulianDate && C.JulianDate.computeTaiMinusUtc) {
          leap = C.JulianDate.computeTaiMinusUtc(t);
        }
      } catch (e) { leap = 37; }
      return (t.dayNumber - 2440587.5) * DAY_MS + (t.secondsOfDay - leap) * 1000;
    }

    // Per frame only the ring rotation (one matrix); everything else is throttled.
    function frame(ms, wallMs) {
      if (!m.enabled) return;
      if (m.rings && m.rings.show) rotateRings(ms);
      if (m.footOn && wallMs - m.lastFoot >= FOOT_EVERY_MS) {
        m.lastFoot = wallMs;
        updateFootprint(false);
      }
      if (wallMs - m.lastSlow < SLOW_EVERY_MS) return;
      m.lastSlow = wallMs;
      if (m.shadowOn) { showShadow(); updateShadow(ms); }
      if (m.rings) m.rings.show = anyRings() && is3d();
      if (m.mode === 'sunlight') paint();
      else if (m.mode === 'age' && (wallMs - m.lastAge >= AGE_EVERY_MS ||
          Math.abs(ms - m.lastPaintMs) > 600000)) {
        m.lastAge = wallMs;
        paint();
      }
      if (m.stripOn) drawStrip(ms, false);
      if (anyRings() && isNum(m.planesAtMs) && Math.abs(ms - m.planesAtMs) >= PLANE_DRIFT_MS) {
        buildPlanes(true);
      }
    }

    try {
      viewer.scene.preUpdate.addEventListener(function (scene, time) {
        var ms = time && isNum(time.dayNumber) ? julianToMs(time) : simMs();
        frame(ms, Date.now());
      });
    } catch (e) { /* no frame hook: the panels still work */ }

    // -- page events ---------------------------------------------------------

    function onCatalog() {
      if (m.mode !== 'default' || m.focus) paint();
      if (anyRings() || isOpen('const')) buildPlanes(true);
      if (m.histYear !== null) applyHistory();
      if (m.debris && m.debris.local) m.debris = localDebris();
      renderDebris();
      renderWatch();
      updateStarButton();
    }

    function onSelection() {
      updateStarButton();
      if (m.histYear !== null) applyHistory();
      if (m.focus) paint();
      updateFootprint(true);
      drawStrip(simMs(), true);
    }

    try {
      global.document.addEventListener('sda:catalog-loaded', onCatalog);
      global.document.addEventListener('sda:satellite-selected', onSelection);
      global.document.addEventListener('sda:satellite-deselected', onSelection);
    } catch (e) { /* no document events: keep going */ }

    function setEnabled(on) {
      on = !!on;
      if (on === m.enabled) return on;
      m.enabled = on;
      if (!on) stopPlay();
      paint();
      renderFocus();
      if (m.rings) m.rings.show = on && anyRings() && is3d();
      showShadow();
      if (on && m.shadowOn) updateShadow(simMs());
      updateFootprint(true);
      drawStrip(simMs(), true);
      applyHistory();
      return on;
    }

    // -- start -----------------------------------------------------------------

    showObserver();
    renderFocus();
    renderWatch();
    updateStarButton();
    var saved = loadJson(LS_MODE, 'default');
    m.mode = MODE_BY_KEY[saved] ? saved : 'default';
    m.modeSelect.value = m.mode;
    if (m.mode !== 'default') setMode(m.mode); else paint();

    m.api = {
      setMode: setMode, paint: paint, setFocus: setFocus, toggleWatch: toggleWatch,
      isWatched: isWatched, refreshWatch: refreshWatch, checkOne: checkOne,
      highlightWatch: highlightWatch, setObserver: setObserver, findVisible: findVisible,
      buildPlanes: buildPlanes, focusPlane: focusPlane, loadDebris: loadDebris,
      loadHistory: loadHistory, setYear: setYear, setEnabled: setEnabled, frame: frame,
      updateFootprint: updateFootprint, drawStrip: drawStrip, updateShadow: updateShadow,
      ensureShadow: ensureShadow
    };
    return m;
  }

  var current = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !global.Cesium) return false;
      injectStyle();
      current = createModule(ctx);
      return true;
    } catch (err) {
      try {
        if (ctx && ctx.addIntelItem) ctx.addIntelItem('Orbit visuals did not start: ' + err.message);
      } catch (e) { /* keep going */ }
      return false;
    }
  }

  function call(name) {
    return function () {
      if (!current) return null;
      return current.api[name].apply(null, arguments);
    };
  }

  global.SDAOrbitViz = {
    init: init,
    setEnabled: call('setEnabled'),
    isEnabled: function () { return !!(current && current.enabled); },
    setColorMode: call('setMode'),
    colorMode: function () { return current ? current.mode : null; },
    repaint: call('paint'),
    toggleWatch: call('toggleWatch'),
    isWatched: call('isWatched'),
    watchlist: function () { return current ? current.watch.slice() : []; },
    refreshWatch: call('refreshWatch'),
    highlightWatch: call('highlightWatch'),
    setObserver: call('setObserver'),
    findVisible: call('findVisible'),
    buildPlanes: call('buildPlanes'),
    focusPlane: call('focusPlane'),
    loadDebris: call('loadDebris'),
    loadHistory: call('loadHistory'),
    setYear: call('setYear'),
    clearFocus: function () { if (current) current.api.setFocus(null); },
    _state: function () { return current; },
    _frame: call('frame'),
    MODES: MODES,
    CONSTELLATIONS: CONSTELLATIONS,
    DEBRIS_EVENTS: DEBRIS_EVENTS,
    HIDE_BIT: HIDE_BIT,
    WATCH_MAX: WATCH_MAX,
    fmtInt: fmtInt,
    formatUtc: formatUtc,
    parseLine1: parseLine1,
    parseLine2: parseLine2,
    orbitShape: orbitShape,
    regimeIndex: regimeIndex,
    ageBucket: ageBucket,
    decadeIndex: decadeIndex,
    typeFromName: typeFromName,
    decodeAttributes: decodeAttributes,
    raanRateDegDay: raanRateDegDay,
    raanAt: raanAt,
    splitCircular: splitCircular,
    clusterPlanes: clusterPlanes,
    ringPoints: ringPoints,
    gmstRad: gmstRad,
    sunEci: sunEci,
    sunEarthFixed: sunEarthFixed,
    shadowClass: shadowClass,
    umbraRadiusKm: umbraRadiusKm,
    penumbraRadiusKm: penumbraRadiusKm,
    geodeticToEarthFixed: geodeticToEarthFixed,
    footprintAngle: footprintAngle,
    circlePoints: circlePoints,
    subsolarPoint: subsolarPoint,
    nightSpan: nightSpan,
    launchDesignatorOf: launchDesignatorOf,
    constellationOf: constellationOf,
    nextPass: nextPass,
    newestConjunction: newestConjunction,
    conjunctionText: conjunctionText,
    flagText: flagText,
    passText: passText,
    cleanWatch: cleanWatch,
    cleanObserver: cleanObserver,
    historyFromYears: historyFromYears,
    historyText: historyText
  };
})(typeof window !== 'undefined' ? window : this);
