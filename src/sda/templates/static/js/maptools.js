// General-purpose map tools for the WorldView globe: the things any map is
// expected to have that a satellite globe usually lacks.
//
//   Cursor readout (decimal, DMS, MGRS, terrain height) with metric and
//   imperial scale bars; compass with reset-north, zoom, top-down and home
//   buttons; graticule with labels; day/night terminator with civil, nautical
//   and astronomical twilight lines; a go-to box that reads decimal, DMS, DMM,
//   MGRS, UTM, plus codes, geo: URIs and map URLs, or falls back to a place
//   search; right-click popup with every coordinate format, a reverse-geocoded
//   place name and quick actions; geodesic distance, area and bearing
//   measurement; pins, lines, polygons and range rings in a choice of colours,
//   kept in localStorage and exchanged as GeoJSON, KML and GPX; saved views;
//   2D / 3D / Columbus switch; keyless terrain with vertical exaggeration; PNG
//   screenshot; fullscreen; per-layer imagery show and opacity; elevation
//   profile and line of sight.
//
// Measurement runs on the ellipsoid Cesium draws (a = 6378137 m,
// f = 1/298.257223563): Vincenty's inverse and direct solutions, with polygon
// area on the authalic sphere along densified geodesic edges. This is display
// geometry only. Orbit propagation stays WGS72 in the Python engine and nothing
// here feeds it.
//
// Adapted from gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view,
// MIT, Copyright (c) 2026 Bilawal Sidhu, commit 9423a26): the strict coordinate
// grammar of src/search/coordinateParser.js (one component, one optional
// hemisphere letter, never a sign and a letter together, an unambiguous split),
// the inverse transverse Mercator series, MGRS square decoding, band minimum
// northings, month rejection and Norway / Svalbard zone checks of
// src/search/gridReference.js, the plus code decoder of src/search/plusCode.js,
// the GPX waypoint / route / track mapping of gpxToGeoJson in
// src/data/geoFileImport.js, and the idea in src/annotations/drawTool.js of
// lifting the viewer's double-click tracking while a drawing is open. The rest
// (forward projection, MGRS encoding, geodesics, XML reader, KML and GeoJSON
// exchange, sun position, line of sight, the whole UI) is written for this
// project.
//
// Text is only ever set with textContent. No key is needed for anything: the
// terrain is Esri's keyless World Elevation 3D unless the page holds a Cesium
// ion token, and reverse geocoding asks /api/geocode/reverse when the server
// has it and OpenStreetMap Nominatim directly (at most one request a second)
// when it does not.
//
// Page wiring: list it in EXTENSIONS with a mount div in the sidebar. The
// readout, the navigation widget and the right-click popup float over the globe
// on their own.

(function (global) {
  'use strict';

  // ---------------------------------------------------------------- constants

  var STYLE_ID = 'sda-maptools-style';
  var STORE_KEYS = {
    drawings: 'sda.maptools.v1.drawings',
    bookmarks: 'sda.maptools.v1.bookmarks',
    prefs: 'sda.maptools.v1.prefs'
  };
  var ARCGIS_ELEVATION_URL =
    'https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer';
  var NOMINATIM_URL = 'https://nominatim.openstreetmap.org';
  var NOMINATIM_GAP_MS = 1100;
  var DEG = '\u00b0';
  var D2R = Math.PI / 180;
  var R2D = 180 / Math.PI;
  var ELL = (function () {
    var a = 6378137;
    var f = 1 / 298.257223563;
    var e2 = f * (2 - f);
    return { a: a, f: f, b: a * (1 - f), e2: e2, e: Math.sqrt(e2) };
  })();
  var MEAN_RADIUS_M = 6371008.8;
  var REFRACTION_K = 0.13;
  var COLORS = ['#ffb000', '#ff4040', '#00e676', '#00d4ff', '#ff40ff', '#ffffff'];
  var KINDS = ['pin', 'line', 'polygon', 'circle'];
  var TOOL_TAGS = ['', 'measure', 'profile', 'los'];
  var UNITS = ['metric', 'imperial', 'nautical'];
  var MAX_DRAWINGS = 2000;
  var MAX_POINTS = 5000;
  var MAX_TOTAL_POINTS = 200000;
  var MAX_RADII = 12;
  var MAX_RADIUS_M = 2e7;
  var MAX_BOOKMARKS = 200;
  var MAX_IMPORT_CHARS = 20 * 1024 * 1024;
  var MAX_XML_NODES = 500000;
  var GRID_STEPS = [30, 20, 15, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.025, 0.01, 0.005];
  var TWILIGHT = [
    { depression: 0.833, name: 'Sunset', alpha: 0.9 },
    { depression: 6, name: 'Civil', alpha: 0.55 },
    { depression: 12, name: 'Nautical', alpha: 0.4 },
    { depression: 18, name: 'Astronomical', alpha: 0.28 }
  ];
  var FT = 0.3048;
  var MI = 1609.344;
  var NM = 1852;

  // ---------------------------------------------------------------- small helpers

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  function wrapLon(lon) {
    var x = (((lon + 180) % 360) + 360) % 360 - 180;
    return x === -180 && lon > 0 ? 180 : x;
  }

  function validLatLon(lat, lon) {
    return isNum(lat) && isNum(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
  }

  function round7(v) { return Math.round(v * 1e7) / 1e7; }

  function withCommas(v) {
    var s = String(v);
    var neg = s.charAt(0) === '-';
    if (neg) s = s.slice(1);
    var dot = s.indexOf('.');
    var whole = dot >= 0 ? s.slice(0, dot) : s;
    var frac = dot >= 0 ? s.slice(dot) : '';
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (neg ? '-' : '') + whole + frac;
  }

  function fixed(v, dp) { return withCommas(v.toFixed(dp)); }

  // Accepts [lon, lat] (GeoJSON order) or { lat, lon }.
  function pt(p) {
    if (Array.isArray(p)) return { lon: Number(p[0]), lat: Number(p[1]) };
    if (p && typeof p === 'object') return { lon: Number(p.lon), lat: Number(p.lat) };
    return { lon: NaN, lat: NaN };
  }

  // ---------------------------------------------------------------- geodesics

  // Great circle distance on a sphere (default: IUGG mean radius 6371008.8 m).
  function haversineDistance(lat1, lon1, lat2, lon2, radius) {
    var R = isNum(radius) ? radius : MEAN_RADIUS_M;
    var p1 = lat1 * D2R;
    var p2 = lat2 * D2R;
    var dp = p2 - p1;
    var dl = (lon2 - lon1) * D2R;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) +
      Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function angularDistance(lat1, lon1, lat2, lon2) {
    return haversineDistance(lat1, lon1, lat2, lon2, 1) * R2D;
  }

  function sphericalBearing(lat1, lon1, lat2, lon2) {
    var p1 = lat1 * D2R;
    var p2 = lat2 * D2R;
    var dl = (lon2 - lon1) * D2R;
    var y = Math.sin(dl) * Math.cos(p2);
    var x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return (Math.atan2(y, x) * R2D + 360) % 360;
  }

  // Vincenty (1975) inverse on the display ellipsoid. Nearly antipodal pairs,
  // where the iteration does not converge, fall back to the sphere and say so.
  function vincentyInverse(lat1, lon1, lat2, lon2) {
    if (lat1 === lat2 && wrapLon(lon1) === wrapLon(lon2)) {
      return { distance: 0, azimuth1: 0, azimuth2: 0, converged: true };
    }
    var a = ELL.a;
    var b = ELL.b;
    var f = ELL.f;
    var L = (lon2 - lon1) * D2R;
    L = Math.atan2(Math.sin(L), Math.cos(L));
    var tanU1 = (1 - f) * Math.tan(lat1 * D2R);
    var cosU1 = 1 / Math.sqrt(1 + tanU1 * tanU1);
    var sinU1 = tanU1 * cosU1;
    var tanU2 = (1 - f) * Math.tan(lat2 * D2R);
    var cosU2 = 1 / Math.sqrt(1 + tanU2 * tanU2);
    var sinU2 = tanU2 * cosU2;
    var lambda = L;
    var lambdaP;
    var sinL = 0;
    var cosL = 1;
    var sinS = 0;
    var cosS = 1;
    var sigma = 0;
    var cos2A = 1;
    var cos2Sm = 0;
    var sinA = 0;
    var ok = true;
    var iter = 0;
    do {
      sinL = Math.sin(lambda);
      cosL = Math.cos(lambda);
      var t1 = cosU2 * sinL;
      var t2 = cosU1 * sinU2 - sinU1 * cosU2 * cosL;
      var sinSq = t1 * t1 + t2 * t2;
      if (sinSq < 1e-24) { ok = false; break; }
      sinS = Math.sqrt(sinSq);
      cosS = sinU1 * sinU2 + cosU1 * cosU2 * cosL;
      sigma = Math.atan2(sinS, cosS);
      sinA = cosU1 * cosU2 * sinL / sinS;
      cos2A = 1 - sinA * sinA;
      cos2Sm = cos2A !== 0 ? cosS - 2 * sinU1 * sinU2 / cos2A : 0;
      var C = f / 16 * cos2A * (4 + f * (4 - 3 * cos2A));
      lambdaP = lambda;
      lambda = L + (1 - C) * f * sinA *
        (sigma + C * sinS * (cos2Sm + C * cosS * (-1 + 2 * cos2Sm * cos2Sm)));
      if (Math.abs(lambda) > Math.PI) { ok = false; break; }
    } while (Math.abs(lambda - lambdaP) > 1e-12 && ++iter < 200);
    if (!ok || iter >= 200) {
      return {
        distance: haversineDistance(lat1, lon1, lat2, lon2),
        azimuth1: sphericalBearing(lat1, lon1, lat2, lon2),
        azimuth2: (sphericalBearing(lat2, lon2, lat1, lon1) + 180) % 360,
        converged: false
      };
    }
    var uSq = cos2A * (a * a - b * b) / (b * b);
    var A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)));
    var B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)));
    var dS = B * sinS * (cos2Sm + B / 4 * (cosS * (-1 + 2 * cos2Sm * cos2Sm) -
      B / 6 * cos2Sm * (-3 + 4 * sinS * sinS) * (-3 + 4 * cos2Sm * cos2Sm)));
    var s = b * A * (sigma - dS);
    var az1 = Math.atan2(cosU2 * sinL, cosU1 * sinU2 - sinU1 * cosU2 * cosL);
    var az2 = Math.atan2(cosU1 * sinL, -sinU1 * cosU2 + cosU1 * sinU2 * cosL);
    return {
      distance: s,
      azimuth1: (az1 * R2D + 360) % 360,
      azimuth2: (az2 * R2D + 360) % 360,
      converged: true
    };
  }

  // Vincenty direct: the point `distance` metres from (lat, lon) on `azimuth`.
  function vincentyDirect(lat1, lon1, azimuthDeg, distance) {
    var a = ELL.a;
    var b = ELL.b;
    var f = ELL.f;
    var a1 = azimuthDeg * D2R;
    var sinA1 = Math.sin(a1);
    var cosA1 = Math.cos(a1);
    var tanU1 = (1 - f) * Math.tan(lat1 * D2R);
    var cosU1 = 1 / Math.sqrt(1 + tanU1 * tanU1);
    var sinU1 = tanU1 * cosU1;
    var sigma1 = Math.atan2(tanU1, cosA1);
    var sinA = cosU1 * sinA1;
    var cos2A = 1 - sinA * sinA;
    var uSq = cos2A * (a * a - b * b) / (b * b);
    var A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)));
    var B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)));
    var sigma = distance / (b * A);
    var sigmaP;
    var cos2Sm;
    var sinS;
    var cosS;
    var iter = 0;
    do {
      cos2Sm = Math.cos(2 * sigma1 + sigma);
      sinS = Math.sin(sigma);
      cosS = Math.cos(sigma);
      var dS = B * sinS * (cos2Sm + B / 4 * (cosS * (-1 + 2 * cos2Sm * cos2Sm) -
        B / 6 * cos2Sm * (-3 + 4 * sinS * sinS) * (-3 + 4 * cos2Sm * cos2Sm)));
      sigmaP = sigma;
      sigma = distance / (b * A) + dS;
    } while (Math.abs(sigma - sigmaP) > 1e-12 && ++iter < 200);
    cos2Sm = Math.cos(2 * sigma1 + sigma);
    sinS = Math.sin(sigma);
    cosS = Math.cos(sigma);
    var x = sinU1 * sinS - cosU1 * cosS * cosA1;
    var lat2 = Math.atan2(sinU1 * cosS + cosU1 * sinS * cosA1, (1 - f) * Math.sqrt(sinA * sinA + x * x));
    var lam = Math.atan2(sinS * sinA1, cosU1 * cosS - sinU1 * sinS * cosA1);
    var C = f / 16 * cos2A * (4 + f * (4 - 3 * cos2A));
    var L = lam - (1 - C) * f * sinA * (sigma + C * sinS * (cos2Sm + C * cosS * (-1 + 2 * cos2Sm * cos2Sm)));
    var az2 = Math.atan2(sinA, -x);
    return { lat: lat2 * R2D, lon: wrapLon(lon1 + L * R2D), azimuth: (az2 * R2D + 360) % 360 };
  }

  function geodesicDistance(lat1, lon1, lat2, lon2) {
    return vincentyInverse(lat1, lon1, lat2, lon2).distance;
  }

  function initialBearing(lat1, lon1, lat2, lon2) {
    return vincentyInverse(lat1, lon1, lat2, lon2).azimuth1;
  }

  // Sum of geodesic segment lengths along [lon, lat] or { lat, lon } points.
  function pathLength(points) {
    var total = 0;
    if (!Array.isArray(points)) return 0;
    for (var i = 1; i < points.length; i++) {
      var p = pt(points[i - 1]);
      var q = pt(points[i]);
      total += geodesicDistance(p.lat, p.lon, q.lat, q.lon);
    }
    return total;
  }

  // Authalic latitude support: q(phi), and the radius of the sphere with the
  // ellipsoid's surface area.
  function authalicQ(sinPhi) {
    var e = ELL.e;
    var es = e * sinPhi;
    return (1 - ELL.e2) * (sinPhi / (1 - es * es) - (1 / (2 * e)) * Math.log((1 - es) / (1 + es)));
  }
  var QP = authalicQ(1);
  var AUTHALIC_R2 = ELL.a * ELL.a * QP / 2;

  // Area of a polygon whose edges are geodesics, in square metres. Edges are
  // densified along the true geodesic (at most 10 km apart, 500 pieces per
  // edge) and summed in the equal-area cylindrical plane of the authalic
  // sphere. A ring that circles a pole gets the smaller of its two sides.
  function polygonArea(points) {
    if (!Array.isArray(points) || points.length < 3) return 0;
    var ring = [];
    for (var i = 0; i < points.length; i++) {
      var p = pt(points[i]);
      if (!validLatLon(p.lat, p.lon)) return 0;
      ring.push(p);
    }
    var first = ring[0];
    var last = ring[ring.length - 1];
    if (first.lat === last.lat && first.lon === last.lon) ring.pop();
    if (ring.length < 3) return 0;
    var dense = [];
    for (var j = 0; j < ring.length; j++) {
      var s = ring[j];
      var t = ring[(j + 1) % ring.length];
      dense.push(s);
      var inv = vincentyInverse(s.lat, s.lon, t.lat, t.lon);
      var pieces = Math.min(500, Math.ceil(inv.distance / 10000));
      for (var k = 1; k < pieces; k++) {
        var mid = vincentyDirect(s.lat, s.lon, inv.azimuth1, inv.distance * k / pieces);
        dense.push({ lat: mid.lat, lon: mid.lon });
      }
    }
    var sum = 0;
    var turn = 0;
    for (var m = 0; m < dense.length; m++) {
      var u = dense[m];
      var v = dense[(m + 1) % dense.length];
      var dl = v.lon - u.lon;
      if (dl > 180) dl -= 360;
      if (dl < -180) dl += 360;
      var y1 = authalicQ(Math.sin(u.lat * D2R)) / QP;
      var y2 = authalicQ(Math.sin(v.lat * D2R)) / QP;
      sum += dl * D2R * (y1 + y2) / 2;
      turn += dl;
    }
    var area = Math.abs(sum);
    if (Math.abs(turn) > 180) area = 2 * Math.PI - area;
    return area * AUTHALIC_R2;
  }

  // A closed ring of points `radius` metres from a centre, as [lon, lat].
  function ringPoints(lat, lon, radius, steps) {
    var n = clamp(Math.round(steps || 120), 8, 720);
    var out = [];
    for (var i = 0; i <= n; i++) {
      var q = vincentyDirect(lat, lon, (i % n) * 360 / n, radius);
      out.push([round7(q.lon), round7(q.lat)]);
    }
    return out;
  }

  // ---------------------------------------------------------------- formatting

  // 51.5074 -> 51\u00b030'26.6"N (degree sign). Rounds the total seconds once, so 59.96" never
  // prints as 60.0".
  function toDMS(value, axis, decimals) {
    if (!isNum(value)) return '';
    var dp = isNum(decimals) ? clamp(Math.round(decimals), 0, 4) : 1;
    var hemi = axis === 'lon' ? (value < 0 ? 'W' : 'E') : (value < 0 ? 'S' : 'N');
    var scale = Math.pow(10, dp);
    var total = Math.round(Math.abs(value) * 3600 * scale);
    var perDeg = 3600 * scale;
    var perMin = 60 * scale;
    var d = Math.floor(total / perDeg);
    var rem = total - d * perDeg;
    var m = Math.floor(rem / perMin);
    var s = (rem - m * perMin) / scale;
    var sText = s.toFixed(dp);
    if (s < 10) sText = '0' + sText;
    return d + DEG + (m < 10 ? '0' : '') + m + "'" + sText + '"' + hemi;
  }

  function formatDecimal(lat, lon, dp) {
    var n = isNum(dp) ? dp : 5;
    return lat.toFixed(n) + ', ' + lon.toFixed(n);
  }

  function formatHemi(lat, lon, dp) {
    var n = isNum(dp) ? dp : 4;
    return Math.abs(lat).toFixed(n) + DEG + (lat < 0 ? 'S' : 'N') + ' ' +
      Math.abs(lon).toFixed(n) + DEG + (lon < 0 ? 'W' : 'E');
  }

  function formatDistance(m, units) {
    if (!isNum(m)) return '';
    var u = UNITS.indexOf(units) >= 0 ? units : 'metric';
    if (u === 'imperial') {
      if (m < 0.1 * MI) return fixed(m / FT, 0) + ' ft';
      var mi = m / MI;
      return fixed(mi, mi < 10 ? 2 : mi < 1000 ? 1 : 0) + ' mi';
    }
    if (u === 'nautical') {
      if (m < 0.1 * NM) return fixed(m, 0) + ' m';
      var nm = m / NM;
      return fixed(nm, nm < 10 ? 2 : nm < 1000 ? 1 : 0) + ' NM';
    }
    if (m < 1000) return fixed(m, m < 10 ? 1 : 0) + ' m';
    var km = m / 1000;
    return fixed(km, km < 10 ? 2 : km < 1000 ? 1 : 0) + ' km';
  }

  function formatArea(m2, units) {
    if (!isNum(m2)) return '';
    var u = UNITS.indexOf(units) >= 0 ? units : 'metric';
    if (u === 'imperial') {
      var acres = m2 / 4046.8564224;
      if (acres < 1) return fixed(m2 / (FT * FT), 0) + ' sq ft';
      if (acres < 640) return fixed(acres, acres < 10 ? 2 : 1) + ' ac';
      var sqmi = m2 / (MI * MI);
      return fixed(sqmi, sqmi < 100 ? 2 : sqmi < 10000 ? 1 : 0) + ' sq mi';
    }
    if (u === 'nautical') {
      var sqnm = m2 / (NM * NM);
      return fixed(sqnm, sqnm < 100 ? 2 : sqnm < 10000 ? 1 : 0) + ' sq NM';
    }
    if (m2 < 1e4) return fixed(m2, 0) + ' sq m';
    if (m2 < 1e6) return fixed(m2 / 1e4, 2) + ' ha';
    var sqkm = m2 / 1e6;
    return fixed(sqkm, sqkm < 100 ? 2 : sqkm < 10000 ? 1 : 0) + ' sq km';
  }

  var COMPASS_16 = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];

  function compassPoint(deg) {
    if (!isNum(deg)) return '';
    return COMPASS_16[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
  }

  function formatBearing(deg) {
    if (!isNum(deg)) return '';
    var d = ((deg % 360) + 360) % 360;
    if (d.toFixed(1) === '360.0') d = 0;
    return d.toFixed(1) + DEG + ' ' + compassPoint(d);
  }

  function niceLength(maxValue) {
    if (!(maxValue > 0) || !isFinite(maxValue)) return 0;
    var p = Math.pow(10, Math.floor(Math.log(maxValue) / Math.LN10));
    var d = maxValue / p;
    if (d < 1) { p /= 10; d *= 10; }
    var n = d >= 5 ? 5 : d >= 2 ? 2 : 1;
    return n * p;
  }

  function scaleLabel(v, unit) {
    var s = v >= 1 ? withCommas(String(Math.round(v))) : String(Number(v.toPrecision(3)));
    return s + ' ' + unit;
  }

  // The longest 1 / 2 / 5 x 10^k length that fits in maxPx at metersPerPixel,
  // in metric and in imperial, with the bar width each needs.
  function niceScale(metersPerPixel, maxPx) {
    if (!(metersPerPixel > 0) || !isFinite(metersPerPixel)) return null;
    var px = maxPx > 0 ? maxPx : 100;
    var maxM = metersPerPixel * px;
    var mu = maxM >= 1000 ? { size: 1000, name: 'km' } : { size: 1, name: 'm' };
    var mv = niceLength(maxM / mu.size);
    var iu = maxM >= MI ? { size: MI, name: 'mi' } : { size: FT, name: 'ft' };
    var iv = niceLength(maxM / iu.size);
    return {
      metric: { value: mv, unit: mu.name, label: scaleLabel(mv, mu.name), meters: mv * mu.size, px: mv * mu.size / metersPerPixel },
      imperial: { value: iv, unit: iu.name, label: scaleLabel(iv, iu.name), meters: iv * iu.size, px: iv * iu.size / metersPerPixel }
    };
  }

  var UNIT_METERS = { m: 1, km: 1000, mi: MI, ft: FT, yd: 0.9144, nm: NM };
  var DEFAULT_RING_UNIT = { metric: 'km', imperial: 'mi', nautical: 'nm' };

  // "1, 5, 10 km" or "500 m 2 km 3 mi" to metres, sorted, unique. A bare
  // number takes the unit of the next number that has one, else the default
  // for the unit system. Anything that is not a number or a unit: null.
  function parseRadii(text, units) {
    var src = String(text == null ? '' : text).trim();
    if (!src) return [];
    if (src.length > 200) return null;
    var re = /(\d+(?:\.\d+)?|\.\d+)\s*(km|nm|mi|ft|yd|m)?\b/gi;
    var leftover = src.replace(re, '').replace(/[\s,;]+/g, '');
    if (leftover) return null;
    var tokens = [];
    var m;
    re.lastIndex = 0;
    while ((m = re.exec(src))) tokens.push({ v: Number(m[1]), u: m[2] ? m[2].toLowerCase() : '' });
    var fallback = DEFAULT_RING_UNIT[units] || 'km';
    var pending = fallback;
    for (var i = tokens.length - 1; i >= 0; i--) {
      if (tokens[i].u) pending = tokens[i].u;
      else tokens[i].u = pending;
    }
    var out = [];
    tokens.forEach(function (t) {
      var metres = t.v * UNIT_METERS[t.u];
      if (metres > 0 && metres <= MAX_RADIUS_M && out.indexOf(metres) < 0) out.push(metres);
    });
    out.sort(function (a, b) { return a - b; });
    return out.slice(0, MAX_RADII);
  }

  // ---------------------------------------------------------------- UTM and MGRS

  var K0 = 0.9996;
  var BANDS = 'CDEFGHJKLMNPQRSTUVWX';
  var COLUMN_SETS = ['ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ'];
  var ROW_LETTERS = 'ABCDEFGHJKLMNPQRSTUV';
  var BAND_MIN_NORTHING = {
    C: 1100000, D: 2000000, E: 2800000, F: 3700000, G: 4600000, H: 5500000,
    J: 6400000, K: 7300000, L: 8200000, M: 9100000, N: 0, P: 800000,
    Q: 1700000, R: 2600000, S: 3500000, T: 4400000, U: 5300000, V: 6200000,
    W: 7000000, X: 7900000
  };
  var MONTHS = ['JAN', 'FEB', 'MAR', 'MAY', 'JUN', 'JUL', 'SEP', 'NOV', 'DEC'];
  var SVALBARD_ZONES = { 31: [0, 9], 33: [9, 21], 35: [21, 33], 37: [33, 42] };

  // Krueger series to n^6 (Karney 2011): alpha forward, beta inverse.
  var TM = (function () {
    var n = ELL.f / (2 - ELL.f);
    var n2 = n * n;
    var n3 = n2 * n;
    var n4 = n3 * n;
    var n5 = n4 * n;
    var n6 = n5 * n;
    return {
      A: ELL.a / (1 + n) * (1 + n2 / 4 + n4 / 64 + n6 / 256),
      alpha: [
        n / 2 - 2 / 3 * n2 + 5 / 16 * n3 + 41 / 180 * n4 - 127 / 288 * n5 + 7891 / 37800 * n6,
        13 / 48 * n2 - 3 / 5 * n3 + 557 / 1440 * n4 + 281 / 630 * n5 - 1983433 / 1935360 * n6,
        61 / 240 * n3 - 103 / 140 * n4 + 15061 / 26880 * n5 + 167603 / 181440 * n6,
        49561 / 161280 * n4 - 179 / 168 * n5 + 6601661 / 7257600 * n6,
        34729 / 80640 * n5 - 3418889 / 1995840 * n6,
        212378941 / 319334400 * n6
      ],
      beta: [
        n / 2 - 2 / 3 * n2 + 37 / 96 * n3 - n4 / 360 - 81 / 512 * n5 + 96199 / 604800 * n6,
        n2 / 48 + n3 / 15 - 437 / 1440 * n4 + 46 / 105 * n5 - 1118711 / 3870720 * n6,
        17 / 480 * n3 - 37 / 840 * n4 - 209 / 4480 * n5 + 5569 / 90720 * n6,
        4397 / 161280 * n4 - 11 / 504 * n5 - 830251 / 7257600 * n6,
        4583 / 161280 * n5 - 108847 / 3991680 * n6,
        20648693 / 638668800 * n6
      ]
    };
  })();

  function bandRange(letter) {
    var i = BANDS.indexOf(letter);
    if (i < 0) return null;
    var south = -80 + i * 8;
    return { south: south, north: letter === 'X' ? 84 : south + 8 };
  }

  function bandLetter(lat) {
    if (!isNum(lat) || lat < -80 || lat > 84) return null;
    return BANDS.charAt(Math.min(19, Math.floor((lat + 80) / 8)));
  }

  // UTM zone with the southwest Norway and Svalbard exceptions.
  function utmZone(lat, lon) {
    var lo = wrapLon(lon);
    var zone = Math.min(60, Math.floor((lo + 180) / 6) + 1);
    if (lat >= 56 && lat < 64 && lo >= 3 && lo < 12) zone = 32;
    if (lat >= 72 && lat <= 84 && lo >= 0 && lo < 42) {
      zone = lo < 9 ? 31 : lo < 21 ? 33 : lo < 33 ? 35 : 37;
    }
    return zone;
  }

  function zoneExists(zone, band) {
    return !(band === 'X' && (zone === 32 || zone === 34 || zone === 36));
  }

  function zoneLongitudes(zone, band) {
    if (band === 'V' && zone === 31) return [0, 3];
    if (band === 'V' && zone === 32) return [3, 12];
    if (band === 'X' && SVALBARD_ZONES[zone]) return SVALBARD_ZONES[zone];
    var west = (zone - 1) * 6 - 180;
    return [west, west + 6];
  }

  // Latitude and longitude to UTM, or null outside 80S to 84N (the polar caps
  // are UPS, which this does not do).
  function latLonToUtm(lat, lon, forceZone) {
    if (!validLatLon(lat, lon) || lat < -80 || lat > 84) return null;
    var zone = forceZone >= 1 && forceZone <= 60 ? Math.floor(forceZone) : utmZone(lat, lon);
    var lon0 = (zone - 1) * 6 - 180 + 3;
    var phi = lat * D2R;
    var lam = (lon - lon0) * D2R;
    lam = Math.atan2(Math.sin(lam), Math.cos(lam));
    var e = ELL.e;
    var tau = Math.tan(phi);
    var sigma = Math.sinh(e * Math.atanh(e * tau / Math.sqrt(1 + tau * tau)));
    var tauP = tau * Math.sqrt(1 + sigma * sigma) - sigma * Math.sqrt(1 + tau * tau);
    var cl = Math.cos(lam);
    var xiP = Math.atan2(tauP, cl);
    var etaP = Math.asinh(Math.sin(lam) / Math.sqrt(tauP * tauP + cl * cl));
    var xi = xiP;
    var eta = etaP;
    for (var j = 1; j <= 6; j++) {
      xi += TM.alpha[j - 1] * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
      eta += TM.alpha[j - 1] * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
    }
    var easting = 500000 + K0 * TM.A * eta;
    var northing = K0 * TM.A * xi + (lat < 0 ? 10000000 : 0);
    return { zone: zone, band: bandLetter(lat), hemisphere: lat < 0 ? 'S' : 'N', easting: easting, northing: northing };
  }

  // Inverse transverse Mercator (the donor's series and Newton step on tan phi).
  function utmToLatLon(zone, easting, northing, southern) {
    var xi = (southern ? northing - 10000000 : northing) / (K0 * TM.A);
    var eta = (easting - 500000) / (K0 * TM.A);
    var xiP = xi;
    var etaP = eta;
    for (var j = 1; j <= 6; j++) {
      xiP -= TM.beta[j - 1] * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
      etaP -= TM.beta[j - 1] * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
    }
    var conformal = Math.asin(Math.sin(xiP) / Math.cosh(etaP));
    var dLon = Math.atan2(Math.sinh(etaP), Math.cos(xiP));
    var e = ELL.e;
    var tauP = Math.tan(conformal);
    var tau = tauP;
    for (var i = 0; i < 8; i++) {
      var sg = Math.sinh(e * Math.atanh(e * tau / Math.sqrt(1 + tau * tau)));
      var tauI = tau * Math.sqrt(1 + sg * sg) - sg * Math.sqrt(1 + tau * tau);
      var delta = (tauP - tauI) / Math.sqrt(1 + tauI * tauI) *
        (1 + (1 - ELL.e2) * tau * tau) / ((1 - ELL.e2) * Math.sqrt(1 + tau * tau));
      tau += delta;
      if (Math.abs(delta) < 1e-12) break;
    }
    return { lat: Math.atan(tau) * R2D, lon: wrapLon((zone - 1) * 6 - 180 + 3 + dLon * R2D) };
  }

  function formatUTM(lat, lon) {
    var u = latLonToUtm(lat, lon);
    if (!u) return '';
    return u.zone + u.band + ' ' + Math.floor(u.easting) + ' ' + Math.floor(u.northing);
  }

  function pad(n, width) {
    var s = String(n);
    while (s.length < width) s = '0' + s;
    return s;
  }

  // MGRS with 1 to 5 digits per axis: 40.7488, -73.9854 -> 18T WL 85650 11369.
  function toMGRS(lat, lon, digits) {
    var u = latLonToUtm(lat, lon);
    if (!u) return '';
    var dg = isNum(digits) ? clamp(Math.round(digits), 1, 5) : 5;
    var col = COLUMN_SETS[(u.zone - 1) % 3].charAt(Math.floor(u.easting / 1e5) - 1);
    var row = ROW_LETTERS.charAt((Math.floor(u.northing / 1e5) % 20 + (u.zone % 2 === 0 ? 5 : 0)) % 20);
    if (!col || !row) return '';
    var div = Math.pow(10, 5 - dg);
    var e = Math.floor((u.easting % 1e5) / div);
    var n = Math.floor((u.northing % 1e5) / div);
    return u.zone + u.band + ' ' + col + row + ' ' + pad(e, dg) + ' ' + pad(n, dg);
  }

  function inBand(lat, range, slack) {
    var s = slack || 0;
    return lat >= range.south - s && lat <= range.north + s;
  }

  // UTM `18T 585628 4511322`; a bare N or S that does not fit as a band reads
  // as the hemisphere.
  function parseUTM(text) {
    var m = /^(\d{1,2})\s*([C-HJ-NP-X])\s+(\d{6}(?:\.\d+)?)\s*m?\s+(\d{1,7}(?:\.\d+)?)\s*m?$/i.exec(String(text).trim());
    if (!m) return null;
    var zone = Number(m[1]);
    var letter = m[2].toUpperCase();
    var easting = Number(m[3]);
    var northing = Number(m[4]);
    if (zone < 1 || zone > 60 || easting < 100000 || easting > 900000 || northing > 10000000 || !zoneExists(zone, letter)) return null;
    var range = bandRange(letter);
    var asBand = utmToLatLon(zone, easting, northing, range.south < 0);
    if (inBand(asBand.lat, range)) return asBand;
    if (letter === 'N') {
      var north = utmToLatLon(zone, easting, northing, false);
      if (north.lat >= 0 && north.lat <= 84) return north;
    }
    if (letter === 'S') {
      var south = utmToLatLon(zone, easting, northing, true);
      if (south.lat < 0 && south.lat >= -80) return south;
    }
    return null;
  }

  // MGRS spaced or not, 3 to 5 digits per axis (coarser squares read too much
  // ordinary text as grid references). Returns the centre of a coarse square.
  function parseMGRS(text) {
    var src = String(text).trim();
    var m = /^(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])([A-HJ-NP-V])(?:\s*(\d{6,10})|\s*(\d{3,5})\s+(\d{3,5}))$/i.exec(src);
    if (!m) return null;
    var zone = Number(m[1]);
    var band = m[2].toUpperCase();
    var column = m[3].toUpperCase();
    var row = m[4].toUpperCase();
    var east;
    var north;
    if (m[5]) {
      if (m[5].length % 2) return null;
      east = m[5].slice(0, m[5].length / 2);
      north = m[5].slice(m[5].length / 2);
    } else {
      if (m[6].length !== m[7].length) return null;
      east = m[6];
      north = m[7];
    }
    if (zone < 1 || zone > 60 || !zoneExists(zone, band)) return null;
    var letters = src.replace(/[\d\s]/g, '');
    if (MONTHS.indexOf(letters.toUpperCase()) >= 0) return null;
    if (letters !== letters.toUpperCase() && /\s/.test(src)) return null;
    var colIndex = COLUMN_SETS[(zone - 1) % 3].indexOf(column);
    if (colIndex < 0) return null;
    var rowIndex = (ROW_LETTERS.indexOf(row) - (zone % 2 === 0 ? 5 : 0) + 20) % 20;
    var precision = east.length;
    var scale = Math.pow(10, 5 - precision);
    var half = precision < 5 ? scale / 2 : 0;
    var easting = (colIndex + 1) * 100000 + Number(east) * scale + half;
    var northing = rowIndex * 100000 + Number(north) * scale + half;
    while (northing < BAND_MIN_NORTHING[band]) northing += 2000000;
    var range = bandRange(band);
    var p = utmToLatLon(zone, easting, northing, range.south < 0);
    var slack = scale / 111000;
    if (!inBand(p.lat, range, slack)) return null;
    var lons = zoneLongitudes(zone, band);
    var lonSlack = slack / Math.max(Math.cos(p.lat * D2R), 0.01);
    var lon = p.lon;
    if (lon - lons[0] > 180) lon -= 360;
    if (lons[1] - lon > 180) lon += 360;
    return lon >= lons[0] - lonSlack && lon <= lons[1] + lonSlack ? p : null;
  }

  // Full Open Location Code (plus code) to its centre.
  var OLC = '23456789CFGHJMPQRVWX';
  function parsePlusCode(text) {
    var code = String(text).trim().toUpperCase();
    if (!code || code.length > 32) return null;
    var sep = code.indexOf('+');
    if (sep !== 8 || sep !== code.lastIndexOf('+') || code.length === 10) return null;
    var padAt = code.indexOf('0');
    if (padAt >= 0) {
      if (padAt === 0 || padAt % 2) return null;
      var run = /0+/.exec(code)[0];
      if (run.length % 2 || run.length !== code.split('0').length - 1) return null;
      if (padAt + run.length !== sep || code.length > sep + 1) return null;
    }
    for (var c = 0; c < code.length; c++) {
      var ch = code.charAt(c);
      if (ch !== '+' && ch !== '0' && OLC.indexOf(ch) < 0) return null;
    }
    if (OLC.indexOf(code.charAt(0)) * 20 >= 180 || OLC.indexOf(code.charAt(1)) * 20 >= 360) return null;
    var digits = code.replace('+', '').replace(/0+$/, '').slice(0, 15);
    var south = -90;
    var west = -180;
    var latStep = 400;
    var lonStep = 400;
    for (var i = 0; i < Math.min(digits.length, 10); i += 2) {
      latStep /= 20;
      lonStep /= 20;
      south += OLC.indexOf(digits.charAt(i)) * latStep;
      if (i + 1 < digits.length) west += OLC.indexOf(digits.charAt(i + 1)) * lonStep;
    }
    for (var k = 10; k < digits.length; k++) {
      latStep /= 5;
      lonStep /= 4;
      var idx = OLC.indexOf(digits.charAt(k));
      south += Math.floor(idx / 4) * latStep;
      west += (idx % 4) * lonStep;
    }
    var lon = west + lonStep / 2;
    if (lon >= 180) lon -= 360;
    return { lat: Math.min(90, south + latStep / 2), lon: lon };
  }

  // ---------------------------------------------------------------- coordinate parsing

  var COMPONENT = /^([NSEW])?\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*\u00b0?\s*([NSEW])?$/i;
  var DMS_COMPONENT = new RegExp(
    '^([NSEW])?\\s*([+-])?(\\d{1,3})\\s*[\\u00b0\\u00ba]\\s*(\\d{1,2}(?:\\.\\d+)?)\\s*[\'\\u2032\\u2019]' +
    '\\s*(?:(\\d{1,2}(?:\\.\\d+)?)\\s*(?:"|\\u2033|\\u201d|\'\'|\\u2032\\u2032))?\\s*([NSEW])?$', 'i');
  // Unmarked "40 26 46 N" or "40 26.767 N": only with a hemisphere letter.
  var UNMARKED_COMPONENT = /^([NSEW])?\s*(\d{1,3})\s+(\d{1,2}(?:\.\d+)?)(?:\s+(\d{1,2}(?:\.\d+)?))?\s*([NSEW])?$/i;

  function signed(magnitude, letter, sign) {
    if (!letter) return { value: sign === '-' ? -magnitude : magnitude, axis: null };
    return {
      value: letter === 'S' || letter === 'W' ? -magnitude : magnitude,
      axis: letter === 'N' || letter === 'S' ? 'lat' : 'lon'
    };
  }

  function readComponent(text) {
    var src = String(text).trim();
    var m = COMPONENT.exec(src);
    if (m) {
      if (m[1] && m[3]) return null;
      var letter = (m[1] || m[3] || '').toUpperCase();
      var value = Number(m[2]);
      if (!isFinite(value)) return null;
      if (letter && /^[+-]/.test(m[2])) return null;
      if (!letter) return { value: value, axis: null, dms: false };
      var r = signed(Math.abs(value), letter);
      r.dms = false;
      return r;
    }
    var d = DMS_COMPONENT.exec(src);
    if (d) {
      if (d[1] && d[6]) return null;
      var dl = (d[1] || d[6] || '').toUpperCase();
      if (dl && d[2]) return null;
      if (d[5] !== undefined && d[4].indexOf('.') >= 0) return null;
      if (Number(d[4]) >= 60 || (d[5] !== undefined && Number(d[5]) >= 60)) return null;
      var mag = Number(d[3]) + Number(d[4]) / 60 + Number(d[5] || 0) / 3600;
      var rd = signed(mag, dl, d[2]);
      rd.dms = true;
      return rd;
    }
    var u = UNMARKED_COMPONENT.exec(src);
    if (u) {
      if ((u[1] && u[5]) || (!u[1] && !u[5])) return null;
      if (u[4] !== undefined && u[3].indexOf('.') >= 0) return null;
      if (Number(u[3]) >= 60 || (u[4] !== undefined && Number(u[4]) >= 60)) return null;
      var ru = signed(Number(u[2]) + Number(u[3]) / 60 + Number(u[4] || 0) / 3600, (u[1] || u[5]).toUpperCase());
      ru.dms = true;
      return ru;
    }
    return null;
  }

  // Split on a comma or semicolon, else at the one whitespace boundary that
  // leaves two valid components. Two possible splits is ambiguous: decline.
  function splitComponents(text) {
    if (/[,;]/.test(text)) {
      var parts = text.split(/[,;]/).map(function (s) { return s.trim(); });
      return parts.length === 2 && parts[0] && parts[1] ? parts : null;
    }
    var words = text.split(/\s+/);
    if (words.length > 14) return null;
    var result = null;
    for (var b = words.length - 1; b > 0; b--) {
      var pair = [words.slice(0, b).join(' '), words.slice(b).join(' ')];
      if (readComponent(pair[0]) && readComponent(pair[1])) {
        if (result) return null;
        result = pair;
      }
    }
    return result;
  }

  function pairFrom(a, b) {
    var first = readComponent(a);
    var second = readComponent(b);
    if (!first || !second) return null;
    if (first.axis && first.axis === second.axis) return null;
    var lat;
    var lon;
    if (first.axis === 'lon' || second.axis === 'lat') {
      lat = second.value;
      lon = first.value;
    } else {
      lat = first.value;
      lon = second.value;
    }
    if (!validLatLon(lat, lon)) return null;
    return { lat: lat, lon: lon, kind: first.dms || second.dms ? 'dms' : 'decimal' };
  }

  function fromUrl(text) {
    if (!/^https?:\/\//i.test(text)) return null;
    var num = '(-?\\d{1,3}(?:\\.\\d+)?)';
    var patterns = [
      new RegExp('@' + num + ',' + num),
      new RegExp('[?&](?:q|ll|query|center|destination)=' + num + '(?:,|%2C)\\s*' + num, 'i'),
      new RegExp('[?&]mlat=' + num + '&mlon=' + num, 'i'),
      new RegExp('#map=\\d+(?:\\.\\d+)?/' + num + '/' + num)
    ];
    for (var i = 0; i < patterns.length; i++) {
      var m = patterns[i].exec(text);
      if (m) {
        var lat = Number(m[1]);
        var lon = Number(m[2]);
        if (validLatLon(lat, lon)) return { lat: lat, lon: lon, kind: 'url' };
      }
    }
    return null;
  }

  // Any coordinate a person is likely to paste, or null (a place name, junk).
  // Returns { lat, lon, kind, label }.
  function parseCoordinate(text) {
    if (typeof text !== 'string') return null;
    var src = text.trim();
    if (!src || src.length > 300) return null;
    var hit = fromUrl(src);
    if (!hit && /^geo:/i.test(src)) {
      var g = /^geo:(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)(?:,-?\d+(?:\.\d+)?)?(?:;[a-z0-9=._-]+)*$/i.exec(src);
      if (!g) return null;
      var glat = Number(g[1]);
      var glon = Number(g[2]);
      if (!validLatLon(glat, glon)) return null;
      hit = { lat: glat, lon: glon, kind: 'geo' };
    }
    if (!hit && src.length <= 40) {
      var grid = parseUTM(src);
      if (grid) hit = { lat: grid.lat, lon: grid.lon, kind: 'utm' };
      if (!hit) {
        grid = parseMGRS(src);
        if (grid) hit = { lat: grid.lat, lon: grid.lon, kind: 'mgrs' };
      }
      if (!hit) {
        grid = parsePlusCode(src);
        if (grid) hit = { lat: grid.lat, lon: grid.lon, kind: 'pluscode' };
      }
    }
    if (!hit) {
      var parts = splitComponents(src);
      if (parts) hit = pairFrom(parts[0], parts[1]);
    }
    if (!hit) return null;
    hit.label = formatHemi(hit.lat, hit.lon, 5);
    return hit;
  }

  // ---------------------------------------------------------------- XML reader

  // Enough XML for KML and GPX, without a DOM (so it runs in a worker or Node):
  // elements with local, lower-case names and attributes, text with entities
  // decoded, CDATA, comments, processing instructions and DOCTYPE skipped.
  // Unbalanced closing tags are tolerated rather than fatal.
  function decodeEntities(s) {
    if (s.indexOf('&') < 0) return s;
    return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, function (all, e) {
      var k = e.toLowerCase();
      if (k === 'amp') return '&';
      if (k === 'lt') return '<';
      if (k === 'gt') return '>';
      if (k === 'quot') return '"';
      if (k === 'apos') return "'";
      var code = k.charAt(1) === 'x' ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
      if (!(code > 0 && code <= 0x10ffff)) return '';
      try { return String.fromCodePoint(code); } catch (err) { return ''; }
    });
  }

  function localName(name) {
    var i = name.indexOf(':');
    return (i >= 0 ? name.slice(i + 1) : name).toLowerCase();
  }

  function parseXml(text) {
    var src = String(text);
    var n = src.length;
    var i = 0;
    var count = 0;
    var root = { name: '#document', attrs: {}, children: [], text: '' };
    var stack = [root];
    function top() { return stack[stack.length - 1]; }
    while (i < n) {
      var lt = src.indexOf('<', i);
      if (lt < 0) { top().text += decodeEntities(src.slice(i)); break; }
      if (lt > i) top().text += decodeEntities(src.slice(i, lt));
      var end;
      if (src.substr(lt, 4) === '<!--') {
        end = src.indexOf('-->', lt + 4);
        i = end < 0 ? n : end + 3;
        continue;
      }
      if (src.substr(lt, 9) === '<![CDATA[') {
        end = src.indexOf(']]>', lt + 9);
        top().text += src.slice(lt + 9, end < 0 ? n : end);
        i = end < 0 ? n : end + 3;
        continue;
      }
      var next = src.charAt(lt + 1);
      if (next === '?') {
        end = src.indexOf('?>', lt + 2);
        i = end < 0 ? n : end + 2;
        continue;
      }
      if (next === '!') {
        var j = lt + 2;
        var depth = 0;
        while (j < n) {
          var c = src.charAt(j);
          if (c === '[') depth++;
          else if (c === ']') depth--;
          else if (c === '>' && depth <= 0) break;
          j++;
        }
        i = j + 1;
        continue;
      }
      if (next === '/') {
        end = src.indexOf('>', lt);
        var closing = localName(src.slice(lt + 2, end < 0 ? n : end).trim());
        for (var k = stack.length - 1; k > 0; k--) {
          if (stack[k].name === closing) { stack.length = k; break; }
        }
        i = end < 0 ? n : end + 1;
        continue;
      }
      var q = null;
      var p = lt + 1;
      while (p < n) {
        var ch = src.charAt(p);
        if (q) { if (ch === q) q = null; } else if (ch === '"' || ch === "'") q = ch; else if (ch === '>') break;
        p++;
      }
      var body = src.slice(lt + 1, p);
      i = p + 1;
      var selfClose = /\/\s*$/.test(body);
      if (selfClose) body = body.replace(/\/\s*$/, '');
      var nm = /^([^\s\/>]+)/.exec(body);
      if (!nm) continue;
      if (++count > MAX_XML_NODES) throw new Error('That file has too many XML elements to read.');
      var node = { name: localName(nm[1]), attrs: {}, children: [], text: '' };
      var re = /([^\s=]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"']+))/g;
      var rest = body.slice(nm[1].length);
      var a;
      while ((a = re.exec(rest))) {
        node.attrs[localName(a[1])] = decodeEntities(a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : a[5]);
      }
      top().children.push(node);
      if (!selfClose) stack.push(node);
    }
    return root;
  }

  function kids(node, name) {
    var out = [];
    if (!node) return out;
    for (var i = 0; i < node.children.length; i++) if (node.children[i].name === name) out.push(node.children[i]);
    return out;
  }
  function kid(node, name) { return kids(node, name)[0] || null; }
  function textOf(node) {
    if (!node) return '';
    var s = node.text;
    for (var i = 0; i < node.children.length; i++) s += textOf(node.children[i]);
    return s;
  }
  function descendants(node, name, out) {
    var list = out || [];
    if (!node) return list;
    for (var i = 0; i < node.children.length; i++) {
      var c = node.children[i];
      if (c.name === name) list.push(c);
      descendants(c, name, list);
    }
    return list;
  }

  function escapeXml(s) {
    return String(s).replace(/[<>&"']/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' }[c];
    });
  }

  // ---------------------------------------------------------------- drawings model

  // A drawing: { id, kind: pin|line|polygon|circle, name, color '#rrggbb',
  // points [[lon, lat], ...], radii [metres] (circle only), tool
  // ''|measure|profile|los, created (ms) }.

  var idCounter = 0;
  function newId(prefix) {
    idCounter = (idCounter + 1) % 1679616;
    return (prefix || 'd') + Date.now().toString(36) + idCounter.toString(36) +
      Math.floor(Math.random() * 1296).toString(36);
  }

  function cleanText(v, max) {
    if (v == null) return '';
    var s = String(v).replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
    return s.length > (max || 120) ? s.slice(0, max || 120) : s;
  }

  function cleanColor(v) {
    if (typeof v !== 'string') return '';
    var s = v.trim().toLowerCase();
    if (/^#[0-9a-f]{6}$/.test(s)) return s;
    if (/^#[0-9a-f]{3}$/.test(s)) return '#' + s.charAt(1) + s.charAt(1) + s.charAt(2) + s.charAt(2) + s.charAt(3) + s.charAt(3);
    return '';
  }

  function cleanPoints(list, max) {
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length && out.length < (max || MAX_POINTS); i++) {
      var p = pt(list[i]);
      if (validLatLon(p.lat, p.lon)) out.push([round7(p.lon), round7(p.lat)]);
    }
    return out;
  }

  function cleanRadii(list) {
    var out = [];
    if (!Array.isArray(list)) return out;
    list.forEach(function (r) {
      var v = Number(r);
      if (isNum(v) && v > 0 && v <= MAX_RADIUS_M && out.indexOf(v) < 0) out.push(Math.round(v * 1000) / 1000);
    });
    out.sort(function (a, b) { return a - b; });
    return out.slice(0, MAX_RADII);
  }

  function sanitizeDrawing(d) {
    if (!d || typeof d !== 'object' || KINDS.indexOf(d.kind) < 0) return null;
    var points = cleanPoints(d.points, MAX_POINTS);
    if (d.kind === 'polygon' && points.length > 3) {
      var f = points[0];
      var l = points[points.length - 1];
      if (f[0] === l[0] && f[1] === l[1]) points.pop();
    }
    var need = d.kind === 'line' ? 2 : d.kind === 'polygon' ? 3 : 1;
    if (points.length < need) return null;
    if (d.kind === 'pin' || d.kind === 'circle') points = points.slice(0, 1);
    var radii = d.kind === 'circle' ? cleanRadii(d.radii) : [];
    if (d.kind === 'circle' && !radii.length) return null;
    var id = typeof d.id === 'string' && /^[A-Za-z0-9_-]{1,40}$/.test(d.id) ? d.id : newId('d');
    return {
      id: id,
      kind: d.kind,
      name: cleanText(d.name, 120),
      color: cleanColor(d.color) || COLORS[0],
      points: points,
      radii: radii,
      tool: TOOL_TAGS.indexOf(d.tool) >= 0 ? d.tool : '',
      created: isNum(d.created) ? d.created : Date.now()
    };
  }

  function sanitizeView(v) {
    if (!v || typeof v !== 'object') return null;
    var lat = Number(v.lat);
    var lon = Number(v.lon);
    var height = Number(v.height);
    if (!validLatLon(lat, lon) || !isNum(height) || height < 1 || height > 1e9) return null;
    var ang = function (x, dflt) { var n = Number(x); return isNum(n) ? n : dflt; };
    return {
      lat: round7(lat),
      lon: round7(lon),
      height: Math.round(height * 10) / 10,
      heading: ang(v.heading, 0),
      pitch: clamp(ang(v.pitch, -Math.PI / 2), -Math.PI / 2, Math.PI / 2),
      roll: ang(v.roll, 0)
    };
  }

  // A one-line summary of a drawing's size: length, area or ring radii.
  function drawingMetric(d, units) {
    if (!d) return '';
    if (d.kind === 'line') return formatDistance(pathLength(d.points), units);
    if (d.kind === 'polygon') return formatArea(polygonArea(d.points), units);
    if (d.kind === 'circle') return d.radii.map(function (r) { return formatDistance(r, units); }).join(' / ');
    var p = d.points[0];
    return p ? toMGRS(p[1], p[0], 4) : '';
  }

  // ---------------------------------------------------------------- GeoJSON

  function closeRing(points) {
    var r = points.slice();
    if (r.length) r.push(r[0]);
    return r;
  }

  function drawingsToGeoJSON(list) {
    var features = [];
    (list || []).forEach(function (raw) {
      var d = sanitizeDrawing(raw);
      if (!d) return;
      var props = { name: d.name, kind: d.kind, created: new Date(d.created).toISOString() };
      if (d.tool) props.tool = d.tool;
      var geometry;
      if (d.kind === 'pin') {
        props['marker-color'] = d.color;
        geometry = { type: 'Point', coordinates: d.points[0] };
      } else if (d.kind === 'line') {
        props.stroke = d.color;
        props['stroke-width'] = 2;
        geometry = { type: 'LineString', coordinates: d.points };
      } else if (d.kind === 'polygon') {
        props.stroke = d.color;
        props.fill = d.color;
        props['fill-opacity'] = 0.2;
        geometry = { type: 'Polygon', coordinates: [closeRing(d.points)] };
      } else {
        // A range ring travels as its centre plus the rings themselves, so a
        // tool that knows nothing of radii_m still draws it.
        props.stroke = d.color;
        props['marker-color'] = d.color;
        props.radii_m = d.radii.slice();
        var geoms = [{ type: 'Point', coordinates: d.points[0] }];
        d.radii.forEach(function (r) {
          geoms.push({ type: 'LineString', coordinates: ringPoints(d.points[0][1], d.points[0][0], r, 120) });
        });
        geometry = { type: 'GeometryCollection', geometries: geoms };
      }
      features.push({ type: 'Feature', properties: props, geometry: geometry });
    });
    return { type: 'FeatureCollection', features: features };
  }

  function propColor(props) {
    if (!props) return '';
    return cleanColor(props.stroke) || cleanColor(props['marker-color']) || cleanColor(props.fill) || cleanColor(props.color);
  }

  function propName(props) {
    if (!props) return '';
    return cleanText(props.name != null ? props.name : props.title != null ? props.title : props.label, 120);
  }

  function geoJSONToDrawings(input) {
    var obj = input;
    if (typeof input === 'string') {
      try { obj = JSON.parse(input); } catch (err) { throw new Error('That GeoJSON file is not valid JSON.'); }
    }
    if (!obj || typeof obj !== 'object') throw new Error('That GeoJSON file has no features.');
    var out = [];
    var skipped = 0;
    var budget = { points: 0 };
    function add(d) {
      if (out.length >= MAX_DRAWINGS) { skipped++; return; }
      var s = sanitizeDrawing(d);
      if (!s) { skipped++; return; }
      budget.points += s.points.length;
      if (budget.points > MAX_TOTAL_POINTS) throw new Error('That file has more points than the globe can hold.');
      out.push(s);
    }
    function geometry(g, props) {
      if (!g || typeof g !== 'object') { skipped++; return; }
      var name = propName(props);
      var color = propColor(props);
      var tool = props && TOOL_TAGS.indexOf(props.tool) >= 0 ? props.tool : '';
      var c = g.coordinates;
      switch (g.type) {
        case 'Point':
          add({ kind: 'pin', name: name, color: color, points: [c] });
          break;
        case 'MultiPoint':
          (c || []).forEach(function (p) { add({ kind: 'pin', name: name, color: color, points: [p] }); });
          break;
        case 'LineString':
          add({ kind: 'line', name: name, color: color, points: c, tool: tool });
          break;
        case 'MultiLineString':
          (c || []).forEach(function (l) { add({ kind: 'line', name: name, color: color, points: l }); });
          break;
        case 'Polygon':
          add({ kind: 'polygon', name: name, color: color, points: c && c[0], tool: tool });
          break;
        case 'MultiPolygon':
          (c || []).forEach(function (poly) { add({ kind: 'polygon', name: name, color: color, points: poly && poly[0] }); });
          break;
        case 'GeometryCollection':
          var parts = Array.isArray(g.geometries) ? g.geometries : [];
          if (props && props.kind === 'circle' && Array.isArray(props.radii_m)) {
            var centre = null;
            parts.forEach(function (p) { if (!centre && p && p.type === 'Point') centre = p.coordinates; });
            add({ kind: 'circle', name: name, color: color, points: [centre], radii: props.radii_m });
          } else {
            parts.forEach(function (p) { geometry(p, props); });
          }
          break;
        default:
          skipped++;
      }
    }
    var list = obj.type === 'FeatureCollection' ? obj.features :
      obj.type === 'Feature' ? [obj] : obj.type ? [{ type: 'Feature', properties: {}, geometry: obj }] : null;
    if (!Array.isArray(list)) throw new Error('That GeoJSON file has no features.');
    list.forEach(function (f) {
      if (!f || f.type !== 'Feature') { skipped++; return; }
      var props = f.properties && typeof f.properties === 'object' ? f.properties : {};
      if (props.kind === 'circle' && Array.isArray(props.radii_m) && f.geometry && f.geometry.type === 'Point') {
        add({ kind: 'circle', name: propName(props), color: propColor(props), points: [f.geometry.coordinates], radii: props.radii_m });
        return;
      }
      geometry(f.geometry, props);
    });
    return { drawings: out, skipped: skipped };
  }

  // ---------------------------------------------------------------- KML

  // KML colours are aabbggrr.
  function cssToKml(css, alpha) {
    var c = cleanColor(css) || COLORS[0];
    var a = pad(Math.round(clamp(isNum(alpha) ? alpha : 1, 0, 1) * 255).toString(16), 2);
    return a + c.slice(5, 7) + c.slice(3, 5) + c.slice(1, 3);
  }

  function kmlToCss(kml) {
    var s = String(kml || '').trim().toLowerCase().replace(/^#/, '');
    if (!/^[0-9a-f]{8}$/.test(s)) return '';
    return '#' + s.slice(6, 8) + s.slice(4, 6) + s.slice(2, 4);
  }

  function kmlCoords(points) {
    return points.map(function (p) { return p[0] + ',' + p[1] + ',0'; }).join(' ');
  }

  function drawingsToKML(list, title) {
    var styles = {};
    var marks = [];
    (list || []).forEach(function (raw) {
      var d = sanitizeDrawing(raw);
      if (!d) return;
      var sid = 'c' + d.color.slice(1);
      styles[sid] = d.color;
      var data = '<ExtendedData><Data name="kind"><value>' + d.kind + '</value></Data>' +
        (d.tool ? '<Data name="tool"><value>' + d.tool + '</value></Data>' : '') +
        (d.kind === 'circle' ? '<Data name="radii_m"><value>' + d.radii.join(',') + '</value></Data>' : '') +
        '</ExtendedData>';
      var geom;
      if (d.kind === 'pin') {
        geom = '<Point><coordinates>' + kmlCoords(d.points) + '</coordinates></Point>';
      } else if (d.kind === 'line') {
        geom = '<LineString><tessellate>1</tessellate><coordinates>' + kmlCoords(d.points) + '</coordinates></LineString>';
      } else if (d.kind === 'polygon') {
        geom = '<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>' +
          kmlCoords(closeRing(d.points)) + '</coordinates></LinearRing></outerBoundaryIs></Polygon>';
      } else {
        geom = '<MultiGeometry><Point><coordinates>' + kmlCoords(d.points) + '</coordinates></Point>' +
          d.radii.map(function (r) {
            return '<LineString><tessellate>1</tessellate><coordinates>' +
              kmlCoords(ringPoints(d.points[0][1], d.points[0][0], r, 120)) + '</coordinates></LineString>';
          }).join('') + '</MultiGeometry>';
      }
      marks.push('<Placemark><name>' + escapeXml(d.name) + '</name><styleUrl>#' + sid + '</styleUrl>' + data + geom + '</Placemark>');
    });
    var styleXml = Object.keys(styles).map(function (sid) {
      var c = styles[sid];
      return '<Style id="' + sid + '"><IconStyle><color>' + cssToKml(c, 1) + '</color></IconStyle>' +
        '<LineStyle><color>' + cssToKml(c, 1) + '</color><width>2</width></LineStyle>' +
        '<PolyStyle><color>' + cssToKml(c, 0.25) + '</color></PolyStyle></Style>';
    }).join('\n');
    return '<?xml version="1.0" encoding="UTF-8"?>\n' +
      '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>' + escapeXml(title || 'SDA map annotations') + '</name>\n' +
      styleXml + '\n' + marks.join('\n') + '\n</Document></kml>\n';
  }

  function parseKmlCoords(text) {
    var out = [];
    String(text || '').trim().split(/\s+/).forEach(function (tuple) {
      if (!tuple) return;
      var parts = tuple.split(',');
      if (parts.length < 2) return;
      out.push([Number(parts[0]), Number(parts[1])]);
    });
    return out;
  }

  function styleColor(style) {
    if (!style) return '';
    var order = ['linestyle', 'iconstyle', 'polystyle', 'labelstyle'];
    for (var i = 0; i < order.length; i++) {
      var s = kid(style, order[i]);
      var c = s && kmlToCss(textOf(kid(s, 'color')));
      if (c) return c;
    }
    return '';
  }

  function kmlToDrawings(text) {
    var root = parseXml(text);
    var kml = descendants(root, 'kml')[0] || root;
    var styles = {};
    descendants(kml, 'style').forEach(function (s) { if (s.attrs.id) styles[s.attrs.id] = styleColor(s); });
    var maps = {};
    descendants(kml, 'stylemap').forEach(function (sm) {
      if (!sm.attrs.id) return;
      kids(sm, 'pair').forEach(function (p) {
        if (textOf(kid(p, 'key')).trim() === 'normal') maps[sm.attrs.id] = textOf(kid(p, 'styleurl')).trim();
      });
    });
    function resolve(url, depth) {
      var id = String(url || '').trim().replace(/^.*#/, '');
      if (!id || depth > 3) return '';
      if (styles[id]) return styles[id];
      if (maps[id]) return resolve(maps[id], depth + 1);
      return '';
    }
    var placemarks = descendants(kml, 'placemark');
    if (!placemarks.length && !descendants(kml, 'document').length && kml === root) {
      throw new Error('That KML file has no <kml> document.');
    }
    var out = [];
    var skipped = 0;
    var total = 0;
    function add(d) {
      if (out.length >= MAX_DRAWINGS) { skipped++; return; }
      var s = sanitizeDrawing(d);
      if (!s) { skipped++; return; }
      total += s.points.length;
      if (total > MAX_TOTAL_POINTS) throw new Error('That file has more points than the globe can hold.');
      out.push(s);
    }
    placemarks.forEach(function (pm) {
      var name = cleanText(textOf(kid(pm, 'name')), 120);
      var color = styleColor(kid(pm, 'style')) || resolve(textOf(kid(pm, 'styleurl')), 0);
      var data = {};
      var ext = kid(pm, 'extendeddata');
      if (ext) {
        kids(ext, 'data').forEach(function (dn) { data[dn.attrs.name] = textOf(kid(dn, 'value')).trim(); });
        descendants(ext, 'simpledata').forEach(function (sd) { data[sd.attrs.name] = textOf(sd).trim(); });
      }
      var tool = TOOL_TAGS.indexOf(data.tool) >= 0 ? data.tool : '';
      function geom(g) {
        switch (g.name) {
          case 'point':
            add({ kind: 'pin', name: name, color: color, points: parseKmlCoords(textOf(kid(g, 'coordinates'))).slice(0, 1) });
            break;
          case 'linestring':
            add({ kind: 'line', name: name, color: color, points: parseKmlCoords(textOf(kid(g, 'coordinates'))), tool: tool });
            break;
          case 'linearring':
            add({ kind: 'polygon', name: name, color: color, points: parseKmlCoords(textOf(kid(g, 'coordinates'))), tool: tool });
            break;
          case 'polygon':
            var outer = kid(g, 'outerboundaryis');
            add({ kind: 'polygon', name: name, color: color, tool: tool,
              points: parseKmlCoords(textOf(kid(kid(outer, 'linearring'), 'coordinates'))) });
            break;
          case 'track':
            add({ kind: 'line', name: name, color: color, points: kids(g, 'coord').map(function (c) {
              var parts = textOf(c).trim().split(/\s+/);
              return [Number(parts[0]), Number(parts[1])];
            }) });
            break;
          case 'multitrack':
          case 'multigeometry':
            var radii = data.kind === 'circle' && data.radii_m ? data.radii_m.split(',').map(Number) : null;
            var centre = radii && kid(g, 'point');
            if (centre) {
              add({ kind: 'circle', name: name, color: color, radii: radii,
                points: parseKmlCoords(textOf(kid(centre, 'coordinates'))).slice(0, 1) });
            } else {
              g.children.forEach(geom);
            }
            break;
          default:
            break;
        }
      }
      var before = out.length + skipped;
      pm.children.forEach(geom);
      if (out.length + skipped === before) skipped++;
    });
    return { drawings: out, skipped: skipped };
  }

  // ---------------------------------------------------------------- GPX

  function gpxToDrawings(text) {
    var root = parseXml(text);
    var gpx = kid(root, 'gpx');
    if (!gpx) throw new Error('That GPX file has no <gpx> document.');
    var out = [];
    var skipped = 0;
    var total = 0;
    function at(el) {
      var raw1 = el.attrs.lat;
      var raw2 = el.attrs.lon;
      if (raw1 == null || raw2 == null || !String(raw1).trim() || !String(raw2).trim()) return null;
      var lat = Number(raw1);
      var lon = Number(raw2);
      return validLatLon(lat, lon) ? [lon, lat] : null;
    }
    function add(d) {
      if (out.length >= MAX_DRAWINGS) { skipped++; return; }
      var s = sanitizeDrawing(d);
      if (!s) { skipped++; return; }
      total += s.points.length;
      if (total > MAX_TOTAL_POINTS) throw new Error('That file has more points than the globe can hold.');
      out.push(s);
    }
    kids(gpx, 'wpt').forEach(function (w) {
      var p = at(w);
      if (p) add({ kind: 'pin', name: textOf(kid(w, 'name')), points: [p] }); else skipped++;
    });
    kids(gpx, 'rte').forEach(function (r) {
      var line = [];
      kids(r, 'rtept').forEach(function (q) { var p = at(q); if (p) line.push(p); });
      add({ kind: 'line', name: textOf(kid(r, 'name')), points: line });
    });
    kids(gpx, 'trk').forEach(function (t) {
      var name = cleanText(textOf(kid(t, 'name')), 100);
      var segs = kids(t, 'trkseg');
      segs.forEach(function (seg, i) {
        var line = [];
        kids(seg, 'trkpt').forEach(function (q) { var p = at(q); if (p) line.push(p); });
        add({ kind: 'line', name: segs.length > 1 ? name + ' (' + (i + 1) + ')' : name, points: line });
      });
    });
    return { drawings: out, skipped: skipped };
  }

  // ---------------------------------------------------------------- import dispatch

  function detectFormat(text, filename) {
    var ext = /\.([a-z0-9]+)$/i.exec(String(filename || ''));
    var e = ext ? ext[1].toLowerCase() : '';
    if (e === 'gpx') return 'gpx';
    if (e === 'kml') return 'kml';
    if (e === 'kmz') return 'kmz';
    if (e === 'geojson' || e === 'json') return 'geojson';
    var head = String(text || '').slice(0, 2000).replace(/^\ufeff/, '').trim();
    if (head.charAt(0) === '{' || head.charAt(0) === '[') return 'geojson';
    if (/<gpx[\s>]/i.test(head)) return 'gpx';
    if (/<kml[\s>]/i.test(head)) return 'kml';
    return '';
  }

  function importDrawings(text, filename) {
    var src = String(text == null ? '' : text);
    if (src.length > MAX_IMPORT_CHARS) throw new Error('That file is larger than 20 MB.');
    src = src.replace(/^\ufeff/, '');
    var format = detectFormat(src, filename);
    var res;
    if (format === 'geojson') res = geoJSONToDrawings(src);
    else if (format === 'kml') res = kmlToDrawings(src);
    else if (format === 'gpx') res = gpxToDrawings(src);
    else if (format === 'kmz') throw new Error('KMZ is zipped KML: unzip it and import the .kml inside.');
    else throw new Error('Not a GeoJSON, KML or GPX file.');
    res.format = format;
    return res;
  }

  // ---------------------------------------------------------------- stores

  function memoryStorage() {
    var data = {};
    return {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; }
    };
  }

  function readJson(storage, key) {
    try {
      var raw = storage && storage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function writeJson(storage, key, value) {
    try {
      storage.setItem(key, JSON.stringify(value));
      return true;
    } catch (err) {
      return false;
    }
  }

  function createBookmarkStore(storage, key) {
    var k = key || STORE_KEYS.bookmarks;
    var items = [];
    function load() {
      var raw = readJson(storage, k);
      items = [];
      if (Array.isArray(raw)) {
        raw.forEach(function (b) {
          var view = b && sanitizeView(b.view);
          if (!view) return;
          items.push({
            id: typeof b.id === 'string' && /^[A-Za-z0-9_-]{1,40}$/.test(b.id) ? b.id : newId('b'),
            name: cleanText(b.name, 80) || formatHemi(view.lat, view.lon, 3),
            view: view,
            created: isNum(b.created) ? b.created : Date.now()
          });
        });
      }
      items = items.slice(-MAX_BOOKMARKS);
    }
    function save() { return writeJson(storage, k, items); }
    function find(id) {
      for (var i = 0; i < items.length; i++) if (items[i].id === id) return i;
      return -1;
    }
    load();
    return {
      list: function () { return items.slice(); },
      get: function (id) { var i = find(id); return i >= 0 ? items[i] : null; },
      add: function (name, view) {
        var v = sanitizeView(view);
        if (!v) return null;
        var b = { id: newId('b'), name: cleanText(name, 80) || formatHemi(v.lat, v.lon, 3), view: v, created: Date.now() };
        items.push(b);
        if (items.length > MAX_BOOKMARKS) items.shift();
        save();
        return b;
      },
      rename: function (id, name) {
        var i = find(id);
        var n = cleanText(name, 80);
        if (i < 0 || !n) return false;
        items[i].name = n;
        return save();
      },
      remove: function (id) {
        var i = find(id);
        if (i < 0) return false;
        items.splice(i, 1);
        return save();
      },
      clear: function () { items = []; return save(); },
      reload: load
    };
  }

  function createDrawingStore(storage, key) {
    var k = key || STORE_KEYS.drawings;
    var items = [];
    function load() {
      var raw = readJson(storage, k);
      items = [];
      if (Array.isArray(raw)) raw.forEach(function (d) { var s = sanitizeDrawing(d); if (s) items.push(s); });
      items = items.slice(-MAX_DRAWINGS);
    }
    function save() { return writeJson(storage, k, items); }
    function find(id) {
      for (var i = 0; i < items.length; i++) if (items[i].id === id) return i;
      return -1;
    }
    function put(d) {
      var s = sanitizeDrawing(d);
      if (!s) return null;
      while (find(s.id) >= 0) s.id = newId('d');
      items.push(s);
      if (items.length > MAX_DRAWINGS) items.shift();
      return s;
    }
    load();
    return {
      list: function () { return items.slice(); },
      get: function (id) { var i = find(id); return i >= 0 ? items[i] : null; },
      add: function (d) { var s = put(d); if (s) save(); return s; },
      addMany: function (list) {
        var added = [];
        (list || []).forEach(function (d) { var s = put(d); if (s) added.push(s); });
        if (added.length) save();
        return added;
      },
      update: function (id, patch) {
        var i = find(id);
        if (i < 0) return null;
        var merged = {};
        Object.keys(items[i]).forEach(function (key) { merged[key] = items[i][key]; });
        Object.keys(patch || {}).forEach(function (key) { merged[key] = patch[key]; });
        merged.id = id;
        var s = sanitizeDrawing(merged);
        if (!s) return null;
        items[i] = s;
        save();
        return s;
      },
      remove: function (id) {
        var i = find(id);
        if (i < 0) return null;
        var gone = items.splice(i, 1)[0];
        save();
        return gone;
      },
      removeLast: function () {
        var gone = items.pop() || null;
        if (gone) save();
        return gone;
      },
      clear: function () { items = []; return save(); },
      reload: load
    };
  }

  // ---------------------------------------------------------------- graticule

  function graticuleStep(span, maxLines) {
    var m = maxLines > 0 ? maxLines : 8;
    var best = GRID_STEPS[0];
    for (var i = 0; i < GRID_STEPS.length; i++) {
      if (span / GRID_STEPS[i] <= m) best = GRID_STEPS[i];
      else break;
    }
    return best;
  }

  function stepDecimals(step) {
    var s = String(step);
    var dot = s.indexOf('.');
    return dot < 0 ? 0 : Math.min(3, s.length - dot - 1);
  }

  function formatGraticuleLabel(value, axis, step) {
    var dp = stepDecimals(step || 1);
    var v = axis === 'lon' ? wrapLon(value) : value;
    var mag = Math.abs(v).toFixed(dp);
    if (Number(mag) === 0) return (0).toFixed(dp) + DEG;
    if (axis === 'lon' && Number(mag) === 180) return mag + DEG;
    var hemi = axis === 'lon' ? (v < 0 ? 'W' : 'E') : (v < 0 ? 'S' : 'N');
    return mag + DEG + hemi;
  }

  // Meridians and parallels for a view rectangle in degrees ({ west, south,
  // east, north }, east may be less than west across the antimeridian).
  // Longitudes come back unwrapped (west <= lon <= east + 360) so the caller can
  // draw them in order; labels are wrapped.
  function graticuleLines(rect, step) {
    var r = rect || { west: -180, south: -90, east: 180, north: 90 };
    var west = r.west;
    var east = r.east;
    if (east <= west) east += 360;
    var south = clamp(r.south, -90, 90);
    var north = clamp(r.north, -90, 90);
    var st = step > 0 ? step : graticuleStep(Math.max(north - south, (east - west) * Math.cos(clamp((north + south) / 2, -80, 80) * D2R)));
    var meridians = [];
    var parallels = [];
    var lon0 = Math.ceil(west / st - 1e-9) * st;
    for (var lon = lon0; lon <= east + 1e-9 && meridians.length < 120; lon += st) {
      var lv = Math.round(lon / st) * st;
      if (east - west >= 360 - 1e-9 && lv >= west + 360 - 1e-9) break;
      meridians.push({ lon: lv, label: formatGraticuleLabel(lv, 'lon', st), south: Math.max(south, -89.5), north: Math.min(north, 89.5) });
    }
    var lat0 = Math.ceil(south / st - 1e-9) * st;
    for (var lat = lat0; lat <= north + 1e-9 && parallels.length < 120; lat += st) {
      var pv = Math.round(lat / st) * st;
      if (Math.abs(pv) >= 90) continue;
      parallels.push({ lat: pv, label: formatGraticuleLabel(pv, 'lat', st), west: west, east: east });
    }
    return { step: st, meridians: meridians, parallels: parallels };
  }

  // True when a ground point is on the near side of the globe from a camera at
  // camLon, camLat (degrees) and camHeight (metres): its angular distance from
  // the nadir is inside the horizon cap, less `marginDeg` so a label does not
  // sit on the limb. Labels draw with the depth test off, so without this a
  // label on the far side shows through the globe.
  function labelFacesCamera(lon, lat, camLon, camLat, camHeight, marginDeg) {
    if (!isNum(lon) || !isNum(lat) || !isNum(camLon) || !isNum(camLat) || !isNum(camHeight)) return true;
    var cap = Math.acos(MEAN_RADIUS_M / (MEAN_RADIUS_M + Math.max(camHeight, 0))) * R2D;
    var p1 = lat * D2R;
    var p2 = camLat * D2R;
    var c = Math.sin(p1) * Math.sin(p2) + Math.cos(p1) * Math.cos(p2) * Math.cos((lon - camLon) * D2R);
    var dist = Math.acos(clamp(c, -1, 1)) * R2D;
    return dist < cap - (isNum(marginDeg) ? marginDeg : 3);
  }

  // ---------------------------------------------------------------- sun

  // Subsolar point from the Astronomical Almanac's low-precision solar
  // coordinates (about 0.01 degree from 1950 to 2050) and GMST.
  function subsolarPoint(date) {
    var t = date instanceof Date ? date.getTime() : Number(date);
    if (!isNum(t)) return null;
    var n = t / 86400000 + 2440587.5 - 2451545.0;
    var L = (280.460 + 0.9856474 * n) % 360;
    var g = ((357.528 + 0.9856003 * n) % 360) * D2R;
    var lambda = (L + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * D2R;
    var eps = (23.439 - 0.0000004 * n) * D2R;
    var ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda));
    var dec = Math.asin(Math.sin(eps) * Math.sin(lambda));
    var gmst = (280.46061837 + 360.98564736629 * n) % 360;
    return { lat: dec * R2D, lon: wrapLon(ra * R2D - gmst) };
  }

  // Point at an angular distance and bearing on the sphere.
  function sphericalDestination(lat, lon, bearingDeg, angleDeg) {
    var p1 = lat * D2R;
    var th = bearingDeg * D2R;
    var dl = angleDeg * D2R;
    var p2 = Math.asin(Math.sin(p1) * Math.cos(dl) + Math.cos(p1) * Math.sin(dl) * Math.cos(th));
    var l2 = lon * D2R + Math.atan2(Math.sin(th) * Math.sin(dl) * Math.cos(p1), Math.cos(dl) - Math.sin(p1) * Math.sin(p2));
    return { lat: p2 * R2D, lon: wrapLon(l2 * R2D) };
  }

  // Where the sun's centre sits `depression` degrees below the horizon
  // (0.833 for sunset with refraction and the solar radius, 6 civil, 12
  // nautical, 18 astronomical): a closed ring of [lon, lat].
  function terminatorPoints(date, depression, steps) {
    var sun = subsolarPoint(date);
    if (!sun) return [];
    var dep = isNum(depression) ? depression : 0.833;
    var n = clamp(Math.round(steps || 180), 12, 1440);
    var out = [];
    for (var i = 0; i <= n; i++) {
      var p = sphericalDestination(sun.lat, sun.lon, (i % n) * 360 / n, 90 + dep);
      out.push([p.lon, p.lat]);
    }
    return out;
  }

  // ---------------------------------------------------------------- line of sight and profile

  // samples: [{ d: metres along the path, h: terrain metres }], observer first,
  // target last. Earth curvature with refraction coefficient k (0.13 default)
  // through an effective radius R / (1 - k).
  function lineOfSight(samples, h0, h1, opts) {
    var o = opts || {};
    if (!Array.isArray(samples) || samples.length < 2) return null;
    var k = isNum(o.k) ? o.k : REFRACTION_K;
    var R = (isNum(o.radius) ? o.radius : MEAN_RADIUS_M) / (1 - k);
    var first = samples[0];
    var last = samples[samples.length - 1];
    var D = last.d - first.d;
    if (!(D > 0)) return null;
    var a = (Number(first.h) || 0) + (Number(h0) || 0);
    var b = (Number(last.h) || 0) + (Number(h1) || 0);
    var blockedAt = null;
    var minClear = Infinity;
    var minAt = null;
    for (var i = 1; i < samples.length - 1; i++) {
      var s = samples[i];
      var x = s.d - first.d;
      var sight = a + (b - a) * x / D;
      var ground = (Number(s.h) || 0) + x * (D - x) / (2 * R);
      var clearance = sight - ground;
      if (clearance < minClear) { minClear = clearance; minAt = x; }
      if (clearance < 0 && blockedAt === null) blockedAt = x;
    }
    return {
      visible: blockedAt === null,
      blockedAt: blockedAt,
      minClearance: minClear === Infinity ? null : minClear,
      minClearanceAt: minAt,
      distance: D,
      k: k
    };
  }

  // n points evenly spaced by distance along a path of [lon, lat], each with
  // its distance from the start: [{ lon, lat, d }].
  function profilePoints(points, n) {
    var pts = cleanPoints(points, MAX_POINTS);
    if (pts.length < 2) return [];
    var count = clamp(Math.round(n || 200), 2, 2000);
    var segs = [];
    var total = 0;
    for (var i = 1; i < pts.length; i++) {
      var inv = vincentyInverse(pts[i - 1][1], pts[i - 1][0], pts[i][1], pts[i][0]);
      segs.push({ from: pts[i - 1], az: inv.azimuth1, len: inv.distance, start: total });
      total += inv.distance;
    }
    var out = [];
    var si = 0;
    for (var j = 0; j < count; j++) {
      var d = total * j / (count - 1);
      while (si < segs.length - 1 && d > segs[si].start + segs[si].len) si++;
      var seg = segs[si];
      var along = clamp(d - seg.start, 0, seg.len);
      var p = along > 0 ? vincentyDirect(seg.from[1], seg.from[0], seg.az, along) : { lat: seg.from[1], lon: seg.from[0] };
      out.push({ lon: p.lon, lat: p.lat, d: d });
    }
    out[out.length - 1].lon = pts[pts.length - 1][0];
    out[out.length - 1].lat = pts[pts.length - 1][1];
    return out;
  }

  function profileStats(samples) {
    if (!Array.isArray(samples) || !samples.length) return null;
    var min = Infinity;
    var max = -Infinity;
    var gain = 0;
    var loss = 0;
    for (var i = 0; i < samples.length; i++) {
      var h = Number(samples[i].h) || 0;
      if (h < min) min = h;
      if (h > max) max = h;
      if (i) {
        var dh = h - (Number(samples[i - 1].h) || 0);
        if (dh > 0) gain += dh; else loss -= dh;
      }
    }
    return { min: min, max: max, gain: gain, loss: loss, distance: samples[samples.length - 1].d - samples[0].d };
  }

  // ---------------------------------------------------------------- geocoding URLs

  function nominatimReverseUrl(lat, lon, zoom) {
    return NOMINATIM_URL + '/reverse?format=jsonv2&lat=' + lat.toFixed(6) + '&lon=' + lon.toFixed(6) +
      '&zoom=' + clamp(Math.round(zoom || 14), 3, 18);
  }

  function nominatimSearchUrl(query, limit) {
    return NOMINATIM_URL + '/search?format=jsonv2&limit=' + clamp(Math.round(limit || 5), 1, 10) +
      '&q=' + encodeURIComponent(String(query).slice(0, 200));
  }

  function reverseGeocodeUrl(api, lat, lon) {
    return (api || '') + '/api/geocode/reverse?lat=' + lat.toFixed(6) + '&lon=' + lon.toFixed(6);
  }

  // A place name from a Nominatim reverse answer or this server's geocode shape.
  function placeName(json) {
    if (!json || typeof json !== 'object') return '';
    var name = json.display_name || json.name ||
      (Array.isArray(json.results) && json.results[0] && (json.results[0].display_name || json.results[0].name)) || '';
    return cleanText(name, 200);
  }

  function imageryLabelFromUrl(url) {
    var m = /^[a-z]+:\/\/([^\/?#:]+)/i.exec(String(url || ''));
    return m ? m[1].replace(/^www\./, '') : '';
  }

  // ---------------------------------------------------------------- UI support

  var STYLE_CSS = [
    '.mt-row{display:flex;gap:4px;align-items:center;margin:3px 0;flex-wrap:wrap}',
    '.mt-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:3px;margin:4px 0}',
    '.mt-btn{font:inherit;font-size:10px;letter-spacing:.5px;background:rgba(0,0,0,.5);color:var(--hud-color,#ffb000);',
    '  border:1px solid var(--hud-dim,#9a6c00);padding:3px 4px;cursor:pointer;line-height:1.2}',
    '.mt-btn:hover{border-color:var(--hud-color,#ffb000)}',
    '.mt-btn.active{background:var(--hud-color,#ffb000);color:#000}',
    '.mt-btn[disabled]{opacity:.4;cursor:default}',
    '.mt-input{font:inherit;font-size:11px;background:rgba(0,0,0,.6);color:var(--hud-color,#ffb000);',
    '  border:1px solid var(--hud-dim,#9a6c00);padding:3px 4px;min-width:0;flex:1}',
    '.mt-label{font-size:9px;opacity:.75;min-width:34px}',
    '.mt-status{font-size:10px;opacity:.85;min-height:12px;word-break:break-word;margin:3px 0}',
    '.mt-hint{font-size:9px;opacity:.7;margin:2px 0}',
    '.mt-list{max-height:150px;overflow-y:auto;margin:3px 0}',
    '.mt-item{display:flex;align-items:center;gap:4px;font-size:10px;padding:1px 0}',
    '.mt-dot{width:8px;height:8px;border-radius:50%;flex:none;border:1px solid #000}',
    '.mt-name{flex:1;text-align:left;background:none;border:none;color:inherit;font:inherit;cursor:pointer;',
    '  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0}',
    '.mt-name:hover{text-decoration:underline}',
    '.mt-metric{opacity:.7;white-space:nowrap}',
    '.mt-x{background:none;border:none;color:inherit;font:inherit;cursor:pointer;opacity:.6;padding:0 2px}',
    '.mt-x:hover{opacity:1}',
    '.mt-swatch{width:16px;height:14px;border:1px solid #000;cursor:pointer;padding:0}',
    '.mt-swatch.active{outline:2px solid var(--hud-color,#ffb000);outline-offset:1px}',
    '.mt-file{display:none}',
    '.mt-chart{width:100%;height:84px;display:block;background:rgba(0,0,0,.45);border:1px solid var(--hud-dim,#9a6c00)}',
    '.mt-layer{display:flex;align-items:center;gap:4px;font-size:9px}',
    '.mt-layer span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.mt-layer input[type=range]{width:70px}',
    '.mt-readout{position:fixed;left:260px;bottom:102px;z-index:11;font-family:"JetBrains Mono",monospace;font-size:10px;',
    '  color:var(--hud-color,#ffb000);background:rgba(0,0,0,.55);border:1px solid var(--hud-dim,#9a6c00);',
    '  padding:4px 6px;pointer-events:none;line-height:1.4;min-width:200px}',
    '.mt-readout div{white-space:nowrap}',
    '.mt-scale{display:flex;align-items:flex-end;gap:6px;margin-top:2px}',
    '.mt-bar{height:5px;border:1px solid var(--hud-color,#ffb000);border-top:none}',
    '.mt-nav{position:fixed;right:12px;top:44%;z-index:11;display:flex;flex-direction:column;gap:3px;align-items:center;',
    '  font-family:"JetBrains Mono",monospace}',
    '#info-panel:not(.hidden) ~ .mt-nav{right:302px}',
    '.mt-nav .mt-btn{width:36px;padding:3px 0}',
    '.mt-compass{width:36px;height:36px;border-radius:50%;border:1px solid var(--hud-color,#ffb000);',
    '  background:rgba(0,0,0,.6);position:relative;cursor:pointer;padding:0}',
    '.mt-needle{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}',
    '.mt-needle b{position:absolute;left:50%;top:2px;transform:translateX(-50%);font-size:10px;color:#ff4040}',
    '.mt-needle i{position:absolute;left:50%;top:14px;width:2px;height:16px;margin-left:-1px;',
    '  background:linear-gradient(#ff4040 50%,var(--hud-color,#ffb000) 50%)}',
    '.mt-pop{position:fixed;z-index:40;font-family:"JetBrains Mono",monospace;font-size:10px;line-height:1.45;',
    '  color:var(--hud-color,#ffb000);background:rgba(0,0,0,.88);border:1px solid var(--hud-color,#ffb000);',
    '  padding:6px 8px;max-width:280px;box-shadow:0 0 8px rgba(255,176,0,.25)}',
    '.mt-pop div{word-break:break-word}',
    '.mt-pop .mt-row{margin-top:5px}',
    'body.mt-drawing .cesium-widget canvas{cursor:crosshair !important}',
    '@media (max-width:760px){.mt-readout{left:10px;bottom:110px}.mt-nav{top:auto;bottom:150px}}'
  ].join('\n');

  function injectStyle(doc) {
    if (!doc || !doc.head || doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement('style');
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    doc.head.appendChild(s);
  }

  var TOOLS = [
    { id: 'dist', label: 'DIST', kind: 'line', tag: 'measure', min: 2,
      title: 'Measure distance and bearing: click points, right-click, double-click or Enter to finish' },
    { id: 'area', label: 'AREA', kind: 'polygon', tag: 'measure', min: 3,
      title: 'Measure area and perimeter: click corners, right-click, double-click or Enter to finish' },
    { id: 'pin', label: 'PIN', kind: 'pin', tag: '', min: 1,
      title: 'Drop a pin at each click, named from the Label box (Esc to stop)' },
    { id: 'line', label: 'LINE', kind: 'line', tag: '', min: 2,
      title: 'Draw a line: click points, right-click, double-click or Enter to finish' },
    { id: 'poly', label: 'POLY', kind: 'polygon', tag: '', min: 3,
      title: 'Draw a polygon: click corners, right-click, double-click or Enter to finish' },
    { id: 'ring', label: 'RING', kind: 'circle', tag: '', min: 1,
      title: 'Range rings at each click, radii from the Rings box (Esc to stop)' },
    { id: 'profile', label: 'PROF', kind: 'line', tag: 'profile', min: 2,
      title: 'Elevation profile along a path (needs terrain): click points, then finish' },
    { id: 'los', label: 'LOS', kind: 'line', tag: 'los', min: 2, max: 2,
      title: 'Line of sight between two clicks: terrain, earth curvature and refraction' }
  ];

  function toolById(id) {
    for (var i = 0; i < TOOLS.length; i++) if (TOOLS[i].id === id) return TOOLS[i];
    return null;
  }

  var DEFAULT_PREFS = {
    units: 'metric', readout: true, nav: true, grid: false, sun: false, terrain: false,
    exaggeration: 1, color: COLORS[0], rings: '1, 5, 10', eyes: '2, 2'
  };

  function cleanPrefs(raw) {
    var p = {};
    Object.keys(DEFAULT_PREFS).forEach(function (k) { p[k] = DEFAULT_PREFS[k]; });
    if (!raw || typeof raw !== 'object') return p;
    if (UNITS.indexOf(raw.units) >= 0) p.units = raw.units;
    ['readout', 'nav', 'grid', 'sun', 'terrain'].forEach(function (k) { if (typeof raw[k] === 'boolean') p[k] = raw[k]; });
    if (isNum(raw.exaggeration)) p.exaggeration = clamp(raw.exaggeration, 1, 10);
    if (cleanColor(raw.color)) p.color = cleanColor(raw.color);
    if (typeof raw.rings === 'string' && raw.rings.length <= 200) p.rings = raw.rings;
    if (typeof raw.eyes === 'string' && raw.eyes.length <= 40) p.eyes = raw.eyes;
    return p;
  }

  // "2, 2" or "10" (both) to [observer, target] metres above ground.
  function parseEyeHeights(text) {
    var nums = String(text == null ? '' : text).split(/[\s,;]+/).filter(Boolean).map(Number);
    if (!nums.length || nums.length > 2 || nums.some(function (v) { return !isNum(v) || v < 0 || v > 100000; })) return null;
    return [nums[0], nums.length > 1 ? nums[1] : nums[0]];
  }

  function pickStorage() {
    try {
      var s = global.localStorage;
      if (!s) return memoryStorage();
      s.setItem('__sda_mt_probe', '1');
      s.removeItem('__sda_mt_probe');
      return s;
    } catch (err) {
      return memoryStorage();
    }
  }

  function errMsg(err) { return err && err.message ? err.message : String(err); }

  // ---------------------------------------------------------------- the module

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var camera = scene.camera;
    var api = typeof ctx.api === 'string' ? ctx.api : '';
    var isStatic = !!ctx.isStatic;
    var storage = pickStorage();
    var prefs = cleanPrefs(readJson(storage, STORE_KEYS.prefs));
    var store = createDrawingStore(storage, STORE_KEYS.drawings);
    var marks = createBookmarkStore(storage, STORE_KEYS.bookmarks);
    var cleanup = [];
    var m = {
      destroyed: false, session: null, rendered: {}, terrainOn: false, terrainWant: false, terrainSource: '',
      popup: null, cursor: null, lastMove: 0, lastFrame: 0, sunTime: 0, gotoEntity: null, clearArmed: 0,
      savedDbl: null, layerTimer: null, geo: { serverMissing: isStatic, last: 0, cache: {}, queue: Promise.resolve() }
    };
    injectStyle(doc);

    // -------- DOM helpers

    function el(tag, cls, text) {
      var e = doc.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function button(label, title, onClick, cls) {
      var b = el('button', cls || 'mt-btn', label);
      b.type = 'button';
      b.title = title || '';
      b.addEventListener('click', function (e) { if (e && e.preventDefault) e.preventDefault(); onClick(e); });
      return b;
    }
    function input(placeholder, title, value) {
      var i = el('input', 'mt-input');
      i.type = 'text';
      i.placeholder = placeholder;
      i.title = title;
      i.value = value || '';
      i.setAttribute('spellcheck', 'false');
      i.setAttribute('autocomplete', 'off');
      return i;
    }
    function row(parent, children, cls) {
      var r = el('div', cls || 'mt-row');
      children.forEach(function (c) { if (c) r.appendChild(c); });
      parent.appendChild(r);
      return r;
    }
    function panel(title, collapsed) {
      var p = el('div', collapsed ? 'panel collapsed' : 'panel');
      var h = el('h3', null, title);
      var body = el('div', 'panel-body');
      p.appendChild(h);
      p.appendChild(body);
      return { root: p, body: body };
    }
    function clearChildren(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function detach(node) { if (node && node.parentNode) node.parentNode.removeChild(node); }
    function setActive(btn, on, base) { btn.className = (base || 'mt-btn') + (on ? ' active' : ''); }
    function status(text) { ui.status.textContent = text || ''; }
    function intel(text) {
      if (typeof ctx.addIntelItem === 'function') { try { ctx.addIntelItem(text); } catch (err) { /* page feed only */ } }
    }
    function savePrefs() { writeJson(storage, STORE_KEYS.prefs, prefs); }
    function on(target, type, fn, opts) {
      if (!target || typeof target.addEventListener !== 'function') return;
      target.addEventListener(type, fn, opts);
      cleanup.push(function () { try { target.removeEventListener(type, fn, opts); } catch (err) { /* gone */ } });
    }
    function onCesium(evt, fn) {
      if (!evt || typeof evt.addEventListener !== 'function') return;
      var rm = evt.addEventListener(fn);
      cleanup.push(function () {
        if (typeof rm === 'function') rm();
        else if (typeof evt.removeEventListener === 'function') evt.removeEventListener(fn);
      });
    }

    // -------- panels

    var ui = { tools: {}, swatches: [], modes: {} };

    var pTools = panel('Map tools', false);
    ui.goto = input('lat, lon / MGRS / place', 'Go to a decimal or DMS coordinate, MGRS, UTM, plus code, geo: URI, map link, or a place name');
    on(ui.goto, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); goTo(ui.goto.value); } });
    row(pTools.body, [ui.goto, button('GO', 'Fly to the coordinate or place', function () { goTo(ui.goto.value); })]);
    var toolGrid = el('div', 'mt-grid');
    TOOLS.forEach(function (t) {
      var b = button(t.label, t.title, function () { toggleTool(t.id); });
      ui.tools[t.id] = b;
      toolGrid.appendChild(b);
    });
    pTools.body.appendChild(toolGrid);
    ui.finish = button('FINISH', 'Finish the drawing (Enter, right-click or double-click)', function () { finish(); });
    ui.undo = button('UNDO PT', 'Remove the last point (Backspace)', function () { undoPoint(); });
    ui.cancel = button('CANCEL', 'Stop drawing (Esc)', function () { cancel(); });
    ui.sessionRow = row(pTools.body, [ui.finish, ui.undo, ui.cancel]);
    ui.sessionRow.style.display = 'none';
    ui.live = el('div', 'mt-hint', '');
    pTools.body.appendChild(ui.live);
    ui.label = input('label (optional)', 'Name for the next pin or drawing', '');
    row(pTools.body, [el('span', 'mt-label', 'Label'), ui.label]);
    ui.rings = input('1, 5, 10', 'Range ring radii: numbers with km, m, mi, ft or nm; a bare number takes the next unit written, else the unit system', prefs.rings);
    on(ui.rings, 'change', function () { prefs.rings = ui.rings.value; savePrefs(); });
    row(pTools.body, [el('span', 'mt-label', 'Rings'), ui.rings]);
    ui.eyes = input('2, 2', 'Line of sight: observer and target height above ground, metres', prefs.eyes);
    on(ui.eyes, 'change', function () { prefs.eyes = ui.eyes.value; savePrefs(); });
    row(pTools.body, [el('span', 'mt-label', 'Eye m'), ui.eyes]);
    var swatchRow = row(pTools.body, [el('span', 'mt-label', 'Colour')]);
    COLORS.forEach(function (c) {
      var b = button('', 'Draw in ' + c, function () { setColor(c); }, 'mt-swatch');
      b.style.background = c;
      ui.swatches.push({ color: c, el: b });
      swatchRow.appendChild(b);
    });
    ui.chartWrap = el('div');
    ui.chartWrap.style.display = 'none';
    ui.chart = el('canvas', 'mt-chart');
    ui.chart.width = 232;
    ui.chart.height = 84;
    ui.chartInfo = el('div', 'mt-hint', '');
    ui.chartWrap.appendChild(ui.chart);
    ui.chartWrap.appendChild(ui.chartInfo);
    pTools.body.appendChild(ui.chartWrap);
    ui.listHead = el('div', 'mt-hint', 'Drawings: 0');
    pTools.body.appendChild(ui.listHead);
    ui.list = el('div', 'mt-list');
    pTools.body.appendChild(ui.list);
    ui.file = el('input', 'mt-file');
    ui.file.type = 'file';
    ui.file.setAttribute('accept', '.geojson,.json,.kml,.gpx');
    on(ui.file, 'change', function () {
      var f = ui.file.files && ui.file.files[0];
      if (f) importFile(f);
      ui.file.value = '';
    });
    pTools.body.appendChild(ui.file);
    ui.clearBtn = button('CLEAR', 'Remove every drawing (click twice)', function () { clearAll(); });
    row(pTools.body, [button('UNDO LAST', 'Remove the newest drawing', function () { removeLast(); }), ui.clearBtn]);
    row(pTools.body, [
      button('GEOJSON', 'Download every drawing as GeoJSON', function () { exportFile('geojson'); }),
      button('KML', 'Download every drawing as KML for Google Earth', function () { exportFile('kml'); }),
      button('IMPORT', 'Load drawings from GeoJSON, KML or GPX', function () { ui.file.click(); })
    ]);
    ui.status = el('div', 'mt-status', '');
    pTools.body.appendChild(ui.status);

    var pView = panel('Map view', true);
    ui.modes['3d'] = button('3D', 'Globe', function () { setSceneMode('3d'); });
    ui.modes['2d'] = button('2D', 'Flat map', function () { setSceneMode('2d'); });
    ui.modes.cv = button('CV', 'Columbus view: flat map that keeps height', function () { setSceneMode('cv'); });
    row(pView.body, [ui.modes['3d'], ui.modes['2d'], ui.modes.cv]);
    ui.terrain = button('TERRAIN', 'Terrain on or off: Esri World Elevation 3D (keyless), or Cesium World Terrain when the server has an ion token', function () { setTerrain(!m.terrainWant); });
    ui.exag = el('input');
    ui.exag.type = 'range';
    ui.exag.min = '1';
    ui.exag.max = '5';
    ui.exag.step = '0.5';
    ui.exag.value = String(prefs.exaggeration);
    ui.exag.title = 'Vertical exaggeration of terrain';
    on(ui.exag, 'input', function () { setExaggeration(Number(ui.exag.value)); });
    ui.exagVal = el('span', 'mt-label', 'x' + prefs.exaggeration);
    row(pView.body, [ui.terrain, ui.exag, ui.exagVal]);
    ui.gridBtn = button('GRID', 'Latitude and longitude graticule', function () { setGraticule(!prefs.grid); });
    ui.sunBtn = button('SUN', 'Day and night terminator, with civil, nautical and astronomical twilight, at the simulation time', function () { setTerminator(!prefs.sun); });
    ui.hudBtn = button('HUD', 'Cursor readout and scale bars', function () { setReadout(!prefs.readout); });
    ui.navBtn = button('NAV', 'Compass and zoom buttons', function () { setNav(!prefs.nav); });
    row(pView.body, [ui.gridBtn, ui.sunBtn, ui.hudBtn, ui.navBtn]);
    ui.unitsBtn = button('UNITS', 'Measurement units: metric, imperial, nautical', function () {
      setUnits(UNITS[(UNITS.indexOf(prefs.units) + 1) % UNITS.length]);
    });
    row(pView.body, [
      button('PNG', 'Save a screenshot of the globe', function () { screenshot().catch(function () { /* status set */ }); }),
      button('FULL', 'Fullscreen on or off', function () { toggleFullscreen(); }),
      ui.unitsBtn
    ]);
    pView.body.appendChild(el('div', 'mt-hint', 'Imagery layers (show, opacity)'));
    ui.layers = el('div', 'mt-list');
    pView.body.appendChild(ui.layers);

    var pViews = panel('Saved views', true);
    ui.viewName = input('name this view', 'Name for the current camera view', '');
    on(ui.viewName, 'keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); saveView(ui.viewName.value); } });
    row(pViews.body, [ui.viewName, button('SAVE', 'Save the current camera view', function () { saveView(ui.viewName.value); })]);
    ui.views = el('div', 'mt-list');
    pViews.body.appendChild(ui.views);

    var rootEl = el('div', 'mt-root');
    rootEl.appendChild(pTools.root);
    rootEl.appendChild(pView.root);
    rootEl.appendChild(pViews.root);
    if (ctx.mount && typeof ctx.mount.appendChild === 'function') ctx.mount.appendChild(rootEl);

    // -------- floating readout and navigation

    function scaleRow() {
      var r = el('div', 'mt-scale');
      var bar = el('div', 'mt-bar');
      var lab = el('span', null, '');
      r.appendChild(bar);
      r.appendChild(lab);
      return { row: r, bar: bar, label: lab };
    }
    ui.readout = el('div', 'mt-readout');
    ui.rLat = el('div', null, 'Cursor: move over the globe');
    ui.rDms = el('div', null, '');
    ui.rMgrs = el('div', null, '');
    ui.rElev = el('div', null, '');
    ui.scaleM = scaleRow();
    ui.scaleI = scaleRow();
    [ui.rLat, ui.rDms, ui.rMgrs, ui.rElev, ui.scaleM.row, ui.scaleI.row].forEach(function (n) { ui.readout.appendChild(n); });
    ui.nav = el('div', 'mt-nav');
    ui.compass = button('', 'Compass: click to face north', function () { resetNorth(); }, 'mt-compass');
    ui.needle = el('div', 'mt-needle');
    ui.needle.appendChild(el('b', null, 'N'));
    ui.needle.appendChild(el('i', null, ''));
    ui.compass.appendChild(ui.needle);
    ui.nav.appendChild(ui.compass);
    ui.nav.appendChild(button('+', 'Zoom in', function () { zoomIn(); }));
    ui.nav.appendChild(button('-', 'Zoom out', function () { zoomOut(); }));
    ui.nav.appendChild(button('TOP', 'Look straight down', function () { topDown(); }));
    ui.nav.appendChild(button('HOME', 'Home view', function () { home(); }));
    if (doc.body) {
      doc.body.appendChild(ui.readout);
      doc.body.appendChild(ui.nav);
    }

    // -------- Cesium helpers

    var ds = new C.CustomDataSource('maptools');
    var gridDs = new C.CustomDataSource('maptools-grid');
    var sunDs = new C.CustomDataSource('maptools-sun');
    [ds, gridDs, sunDs].forEach(function (d) { try { viewer.dataSources.add(d); } catch (err) { /* headless */ } });

    function color(css, alpha) {
      var c = C.Color.fromCssColorString(css);
      return isNum(alpha) ? c.withAlpha(alpha) : c;
    }
    function deg(lon, lat, h) { return C.Cartesian3.fromDegrees(lon, lat, h || 0); }
    function degArray(points) {
      var flat = [];
      points.forEach(function (p) { flat.push(p[0], p[1]); });
      return C.Cartesian3.fromDegreesArray(flat);
    }
    function labelGraphics(text, css, small) {
      return {
        text: text,
        font: (small ? '10px' : '12px') + ' "JetBrains Mono", monospace',
        fillColor: color(css),
        outlineColor: C.Color.BLACK,
        outlineWidth: 3,
        style: C.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new C.Cartesian2(0, small ? -4 : -14),
        verticalOrigin: C.VerticalOrigin.BOTTOM,
        heightReference: C.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      };
    }
    function pointGraphics(css, size) {
      return {
        pixelSize: size || 9,
        color: color(css),
        outlineColor: C.Color.BLACK,
        outlineWidth: 1,
        heightReference: C.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      };
    }
    function lineGraphics(points, css, width) {
      return { positions: degArray(points), width: width || 2.5, material: color(css), clampToGround: true, arcType: C.ArcType.GEODESIC };
    }
    function cameraHeight() {
      var c = camera.positionCartographic;
      return c && isNum(c.height) ? c.height : 1e7;
    }
    function canvasSize() {
      var cv = scene.canvas || {};
      return { w: cv.clientWidth || cv.width || 0, h: cv.clientHeight || cv.height || 0 };
    }
    function pickCartesian(pos) {
      if (!pos) return null;
      var hit = null;
      var globe = scene.globe;
      try {
        if (globe && globe.show === false && scene.pickPositionSupported && typeof scene.pickPosition === 'function') {
          hit = scene.pickPosition(pos);
        }
        if (!hit && globe && globe.show !== false && typeof camera.getPickRay === 'function' && typeof globe.pick === 'function') {
          var ray = camera.getPickRay(pos);
          if (ray) hit = globe.pick(ray, scene);
        }
      } catch (err) { hit = null; }
      if (!hit && typeof camera.pickEllipsoid === 'function') {
        try { hit = camera.pickEllipsoid(pos, globe ? globe.ellipsoid : undefined); } catch (err2) { hit = null; }
      }
      return hit || null;
    }
    function pickLatLon(pos) {
      var c = pickCartesian(pos);
      if (!c) return null;
      var g = C.Cartographic.fromCartesian(c);
      if (!g) return null;
      var p = { lat: g.latitude * R2D, lon: wrapLon(g.longitude * R2D), height: g.height };
      return validLatLon(p.lat, p.lon) ? p : null;
    }
    function exaggeration() {
      if (isNum(scene.verticalExaggeration)) return scene.verticalExaggeration || 1;
      if (scene.globe && isNum(scene.globe.terrainExaggeration)) return scene.globe.terrainExaggeration || 1;
      return 1;
    }
    function simTime() {
      try {
        if (typeof ctx.simNow === 'function') {
          var d = ctx.simNow();
          if (d && typeof d.getTime === 'function' && isNum(d.getTime())) return new Date(d.getTime());
        }
      } catch (err) { /* fall through */ }
      try {
        if (viewer.clock && C.JulianDate && typeof C.JulianDate.toDate === 'function') return C.JulianDate.toDate(viewer.clock.currentTime);
      } catch (err2) { /* fall through */ }
      return new Date();
    }
    function elevText(h) {
      if (!isNum(h)) return '--';
      return prefs.units === 'imperial' ? fixed(h / FT, 0) + ' ft' : fixed(h, 0) + ' m';
    }

    // -------- readout, scale and compass

    function updateReadout(p) {
      if (!prefs.readout) return;
      if (!p) {
        ui.rLat.textContent = 'Cursor: off the globe';
        ui.rDms.textContent = '';
        ui.rMgrs.textContent = '';
        ui.rElev.textContent = '';
        return;
      }
      ui.rLat.textContent = formatDecimal(p.lat, p.lon, 5);
      ui.rDms.textContent = toDMS(p.lat, 'lat', 1) + '  ' + toDMS(p.lon, 'lon', 1);
      ui.rMgrs.textContent = toMGRS(p.lat, p.lon, 5) || 'MGRS: polar cap (UPS)';
      ui.rElev.textContent = terrainLive()
        ? 'Elev ' + fixed(p.height / exaggeration(), 0) + ' m / ' + fixed(p.height / exaggeration() / FT, 0) + ' ft'
        : 'Elev: terrain off';
    }

    function updateScale() {
      if (!prefs.readout) return;
      var s = canvasSize();
      var scale = null;
      if (s.w > 120 && s.h > 0) {
        var a = pickCartesian(new C.Cartesian2(s.w / 2 - 50, s.h / 2));
        var b = pickCartesian(new C.Cartesian2(s.w / 2 + 50, s.h / 2));
        if (a && b) {
          var ga = C.Cartographic.fromCartesian(a);
          var gb = C.Cartographic.fromCartesian(b);
          var d = geodesicDistance(ga.latitude * R2D, ga.longitude * R2D, gb.latitude * R2D, gb.longitude * R2D);
          scale = niceScale(d / 100, 110);
        }
      }
      [['metric', ui.scaleM], ['imperial', ui.scaleI]].forEach(function (pair) {
        var part = scale && scale[pair[0]];
        pair[1].row.style.display = part ? '' : 'none';
        if (part) {
          pair[1].bar.style.width = Math.max(1, Math.round(part.px)) + 'px';
          pair[1].label.textContent = part.label;
        }
      });
    }

    function updateCompass() {
      var hd = isNum(camera.heading) ? camera.heading * R2D : 0;
      ui.needle.style.transform = 'rotate(' + (-hd).toFixed(1) + 'deg)';
    }

    // -------- drawings on the globe

    function centroid(points) {
      var lon0 = points[0][0];
      var sx = 0;
      var sy = 0;
      points.forEach(function (p) {
        var dl = p[0] - lon0;
        if (dl > 180) dl -= 360;
        if (dl < -180) dl += 360;
        sx += lon0 + dl;
        sy += p[1];
      });
      return [wrapLon(sx / points.length), sy / points.length];
    }

    function displayName(d) { return d.name || drawingMetric(d, prefs.units) || d.kind; }

    function unrender(id) {
      (m.rendered[id] || []).forEach(function (e) { try { ds.entities.remove(e); } catch (err) { /* gone */ } });
      delete m.rendered[id];
    }

    function renderDrawing(d) {
      unrender(d.id);
      var list = [];
      var n = 0;
      var text = displayName(d);
      function add(spec) {
        spec.id = 'mt-' + d.id + '-' + (n++);
        spec.name = text;
        spec.properties = { type: 'annotation', kind: d.kind, drawingId: d.id, tool: d.tool };
        list.push(ds.entities.add(spec));
      }
      if (d.kind === 'pin') {
        var p = d.points[0];
        add({ position: deg(p[0], p[1]), point: pointGraphics(d.color, 9), label: labelGraphics(text, d.color) });
      } else if (d.kind === 'line') {
        add({ polyline: lineGraphics(d.points, d.color) });
        var last = d.points[d.points.length - 1];
        add({ position: deg(last[0], last[1]), point: pointGraphics(d.color, 6), label: labelGraphics(text, d.color) });
        if (d.tool) {
          d.points.slice(0, -1).forEach(function (q) { add({ position: deg(q[0], q[1]), point: pointGraphics(d.color, 5) }); });
        }
      } else if (d.kind === 'polygon') {
        add({ polygon: { hierarchy: new C.PolygonHierarchy(degArray(d.points)), material: color(d.color, 0.22), arcType: C.ArcType.GEODESIC } });
        add({ polyline: lineGraphics(closeRing(d.points), d.color) });
        var c = centroid(d.points);
        add({ position: deg(c[0], c[1]), label: labelGraphics(text, d.color) });
      } else if (d.kind === 'circle') {
        var o = d.points[0];
        add({ position: deg(o[0], o[1]), point: pointGraphics(d.color, 7), label: labelGraphics(text, d.color) });
        d.radii.forEach(function (r) {
          add({ polyline: lineGraphics(ringPoints(o[1], o[0], r, 120), d.color, 2) });
          var top = vincentyDirect(o[1], o[0], 0, r);
          add({ position: deg(top.lon, top.lat), label: labelGraphics(formatDistance(r, prefs.units), d.color, true) });
        });
      }
      m.rendered[d.id] = list;
      return list;
    }

    function renderAll() {
      Object.keys(m.rendered).forEach(unrender);
      store.list().forEach(function (d) { try { renderDrawing(d); } catch (err) { /* skip one bad drawing */ } });
    }

    function boundsOf(list) {
      var b = { w: 180, s: 90, e: -180, n: -90 };
      list.forEach(function (d) {
        var pad = 0;
        if (d.kind === 'circle') pad = (d.radii[d.radii.length - 1] || 0) / 111320;
        d.points.forEach(function (p) {
          var lonPad = pad / Math.max(0.05, Math.cos(p[1] * D2R));
          b.w = Math.min(b.w, p[0] - lonPad);
          b.e = Math.max(b.e, p[0] + lonPad);
          b.s = Math.min(b.s, p[1] - pad);
          b.n = Math.max(b.n, p[1] + pad);
        });
      });
      if (b.w > b.e) return null;
      var dx = Math.max(0.01, (b.e - b.w) * 0.2);
      var dy = Math.max(0.01, (b.n - b.s) * 0.2);
      return { w: clamp(b.w - dx, -180, 180), s: clamp(b.s - dy, -89.9, 89.9), e: clamp(b.e + dx, -180, 180), n: clamp(b.n + dy, -89.9, 89.9) };
    }

    function flyToDrawings(list) {
      if (!list.length) return false;
      if (list.length === 1 && list[0].kind === 'pin') {
        var p = list[0].points[0];
        camera.flyTo({ destination: deg(p[0], p[1], clamp(cameraHeight(), 1500, 30000)), duration: 1.2 });
        return true;
      }
      var b = boundsOf(list);
      if (!b) return false;
      camera.flyTo({ destination: C.Rectangle.fromDegrees(b.w, b.s, b.e, b.n), duration: 1.2 });
      return true;
    }

    function refreshList() {
      var items = store.list();
      ui.listHead.textContent = 'Drawings: ' + items.length + (items.length ? '  (click a name to fly there)' : '');
      clearChildren(ui.list);
      items.slice().reverse().slice(0, 60).forEach(function (d) {
        var r = el('div', 'mt-item');
        var dot = el('span', 'mt-dot');
        dot.style.background = d.color;
        var name = el('button', 'mt-name', d.name || d.kind);
        name.type = 'button';
        name.title = 'Fly to ' + (d.name || d.kind);
        name.addEventListener('click', function () { flyToDrawings([d]); });
        var metric = el('span', 'mt-metric', d.kind === 'pin' || d.tool ? '' : drawingMetric(d, prefs.units));
        var x = el('button', 'mt-x', 'x');
        x.type = 'button';
        x.title = 'Delete ' + (d.name || d.kind);
        x.addEventListener('click', function () { removeDrawing(d.id); });
        r.appendChild(dot);
        r.appendChild(name);
        r.appendChild(metric);
        r.appendChild(x);
        ui.list.appendChild(r);
      });
      if (items.length > 60) ui.list.appendChild(el('div', 'mt-hint', '+' + (items.length - 60) + ' older'));
    }

    function removeDrawing(id) {
      var gone = store.remove(id);
      if (!gone) return false;
      unrender(id);
      refreshList();
      status('Deleted ' + (gone.name || gone.kind) + '.');
      return true;
    }

    function removeLast() {
      var gone = store.removeLast();
      if (!gone) { status('No drawings to remove.'); return null; }
      unrender(gone.id);
      refreshList();
      status('Removed ' + (gone.name || gone.kind) + '.');
      return gone;
    }

    function clearAll() {
      var now = Date.now();
      if (!store.list().length) { status('No drawings to clear.'); return false; }
      if (now - m.clearArmed > 4000) {
        m.clearArmed = now;
        ui.clearBtn.textContent = 'SURE?';
        setTimeout(function () {
          if (m.destroyed || m.clearArmed !== now) return;
          m.clearArmed = 0;
          ui.clearBtn.textContent = 'CLEAR';
        }, 4000);
        status('Click CLEAR again to remove all ' + store.list().length + ' drawings.');
        return false;
      }
      m.clearArmed = 0;
      ui.clearBtn.textContent = 'CLEAR';
      store.list().forEach(function (d) { unrender(d.id); });
      store.clear();
      refreshList();
      status('All drawings removed.');
      return true;
    }

    // -------- drawing sessions

    function setBodyDrawing(onFlag) {
      var b = doc.body;
      if (!b) return;
      var cls = String(b.className || '').split(/\s+/).filter(function (c) { return c && c !== 'mt-drawing'; });
      if (onFlag) cls.push('mt-drawing');
      b.className = cls.join(' ');
    }

    // The viewer's own double-click tracks an entity; while a drawing is open a
    // double-click finishes it instead.
    function suppressDoubleClick(onFlag) {
      var h = viewer.screenSpaceEventHandler;
      var T = C.ScreenSpaceEventType;
      if (!h || typeof h.getInputAction !== 'function') return;
      try {
        if (onFlag && !m.savedDbl) {
          m.savedDbl = { fn: h.getInputAction(T.LEFT_DOUBLE_CLICK) || null };
          if (m.savedDbl.fn) h.removeInputAction(T.LEFT_DOUBLE_CLICK);
        } else if (!onFlag && m.savedDbl) {
          if (m.savedDbl.fn) h.setInputAction(m.savedDbl.fn, T.LEFT_DOUBLE_CLICK);
          m.savedDbl = null;
        }
      } catch (err) { m.savedDbl = null; }
    }

    function updateToolButtons() {
      var id = m.session ? m.session.tool.id : null;
      TOOLS.forEach(function (t) { setActive(ui.tools[t.id], t.id === id); });
      ui.sessionRow.style.display = m.session ? '' : 'none';
      var multi = m.session && m.session.tool.kind !== 'pin' && m.session.tool.kind !== 'circle';
      ui.finish.disabled = !multi;
      ui.undo.disabled = !multi;
    }

    function sessionPoints(withCursor, closed) {
      var s = m.session;
      if (!s) return [];
      var pts = s.points.slice();
      if (withCursor && m.cursor && (!s.tool.max || pts.length < s.tool.max)) pts.push([m.cursor.lon, m.cursor.lat]);
      if (closed && pts.length > 2) pts.push(pts[0]);
      return pts;
    }

    function liveText() {
      var s = m.session;
      if (!s) return '';
      var t = s.tool;
      var pts = sessionPoints(true, false);
      var units = prefs.units;
      if (t.kind === 'pin') return 'Click to drop a pin. Esc to stop.';
      if (t.kind === 'circle') return 'Click to place rings: ' + (parseRadii(ui.rings.value, units) || []).map(function (r) { return formatDistance(r, units); }).join(' / ');
      if (pts.length < 2) return t.id === 'los' ? 'Click the observer, then the target.' : 'Click the first point.';
      var a = pts[pts.length - 2];
      var b = pts[pts.length - 1];
      var seg = vincentyInverse(a[1], a[0], b[1], b[0]);
      if (t.kind === 'polygon') {
        var perim = pathLength(pts) + (pts.length > 2 ? geodesicDistance(b[1], b[0], pts[0][1], pts[0][0]) : 0);
        return (pts.length > 2 ? 'Area ' + formatArea(polygonArea(pts), units) + '  ' : '') + 'Perimeter ' + formatDistance(perim, units);
      }
      var text = 'Total ' + formatDistance(pathLength(pts), units);
      if (pts.length > 2) text += '  Leg ' + formatDistance(seg.distance, units);
      return text + '  ' + formatBearing(seg.azimuth1);
    }

    function updateLive() {
      if (!m.session) { ui.live.textContent = ''; return; }
      ui.live.textContent = liveText();
      if (typeof scene.requestRender === 'function') scene.requestRender();
    }

    function sessionColor() { return prefs.color; }

    function makePreview() {
      var s = m.session;
      var css = sessionColor();
      var t = s.tool;
      s.entities.push(ds.entities.add({
        id: 'mt-preview-line',
        polyline: {
          positions: new C.CallbackProperty(function () {
            var pts = sessionPoints(true, t.kind === 'polygon');
            return pts.length > 1 ? degArray(pts) : [];
          }, false),
          width: 2,
          material: color(css),
          depthFailMaterial: color(css, 0.45),
          arcType: C.ArcType.GEODESIC
        }
      }));
      if (t.kind === 'polygon') {
        s.entities.push(ds.entities.add({
          id: 'mt-preview-fill',
          polygon: {
            hierarchy: new C.CallbackProperty(function () {
              var pts = sessionPoints(true, false);
              return pts.length > 2 ? new C.PolygonHierarchy(degArray(pts)) : undefined;
            }, false),
            material: color(css, 0.18)
          }
        }));
      }
      s.entities.push(ds.entities.add({
        id: 'mt-preview-label',
        position: new C.CallbackProperty(function () {
          return m.cursor ? deg(m.cursor.lon, m.cursor.lat) : undefined;
        }, false),
        label: {
          text: new C.CallbackProperty(liveText, false),
          font: '11px "JetBrains Mono", monospace',
          fillColor: color(css),
          outlineColor: C.Color.BLACK,
          outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new C.Cartesian2(14, -14),
          heightReference: C.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY
        }
      }));
    }

    function addVertex(p) {
      var s = m.session;
      s.vertices.push(ds.entities.add({
        id: 'mt-vertex-' + s.vertices.length,
        position: deg(p[0], p[1]),
        point: pointGraphics(sessionColor(), 6)
      }));
    }

    function startTool(id) {
      var t = toolById(id);
      if (!t) return false;
      if (m.session) endSession();
      closePopup();
      m.session = { tool: t, points: [], entities: [], vertices: [], label: '' };
      suppressDoubleClick(true);
      setBodyDrawing(true);
      updateToolButtons();
      if (t.kind !== 'pin' && t.kind !== 'circle') makePreview();
      status(t.title);
      updateLive();
      return true;
    }

    function endSession() {
      var s = m.session;
      if (!s) return;
      s.entities.concat(s.vertices).forEach(function (e) { try { ds.entities.remove(e); } catch (err) { /* gone */ } });
      m.session = null;
      suppressDoubleClick(false);
      setBodyDrawing(false);
      updateToolButtons();
      updateLive();
    }

    function cancel() {
      if (!m.session) return false;
      var multi = m.session.tool.kind !== 'pin' && m.session.tool.kind !== 'circle';
      endSession();
      status(multi ? 'Drawing cancelled.' : 'Stopped.');
      return true;
    }

    function toggleTool(id) {
      if (m.session && m.session.tool.id === id) {
        if (m.session.points.length >= m.session.tool.min && m.session.tool.kind !== 'pin' && m.session.tool.kind !== 'circle') return finish();
        cancel();
        return null;
      }
      return startTool(id);
    }

    function undoPoint() {
      var s = m.session;
      if (!s || !s.points.length) return false;
      s.points.pop();
      var v = s.vertices.pop();
      if (v) { try { ds.entities.remove(v); } catch (err) { /* gone */ } }
      updateLive();
      return true;
    }

    function nameFor(t, pts, label) {
      var u = prefs.units;
      var prefix = label ? label + ': ' : '';
      if (t.id === 'dist') {
        var len = pathLength(pts);
        return prefix + formatDistance(len, u) + (pts.length === 2 ? ' ' + formatBearing(initialBearing(pts[0][1], pts[0][0], pts[1][1], pts[1][0])) : '');
      }
      if (t.id === 'area') return prefix + formatArea(polygonArea(pts), u);
      if (t.id === 'line') return label || 'Line ' + formatDistance(pathLength(pts), u);
      if (t.id === 'poly') return label || 'Polygon ' + formatArea(polygonArea(pts), u);
      if (t.id === 'profile') return (label || 'Profile') + ' ' + formatDistance(pathLength(pts), u);
      return (label || 'LOS') + ' ' + formatDistance(pathLength(pts), u);
    }

    function finish() {
      var s = m.session;
      if (!s) return null;
      var t = s.tool;
      if (t.kind === 'pin' || t.kind === 'circle') { cancel(); return null; }
      if (s.points.length < t.min) {
        status(t.min === 3 ? 'An area needs at least three points.' : 'Needs at least two points.');
        return null;
      }
      var pts = s.points.slice();
      var label = cleanText(ui.label.value, 80);
      endSession();
      var saved = store.add({ kind: t.kind, tool: t.tag, color: sessionColor(), points: pts, name: nameFor(t, pts, label) });
      if (!saved) { status('That drawing could not be saved.'); return null; }
      renderDrawing(saved);
      refreshList();
      status(saved.name);
      if (t.id === 'profile') runProfile(saved);
      if (t.id === 'los') runLos(saved, label);
      return saved;
    }

    function addPin(lat, lon, name) {
      var d = store.add({
        kind: 'pin', color: sessionColor(), points: [[lon, lat]],
        name: cleanText(name, 120) || cleanText(ui.label.value, 120) || formatHemi(lat, lon, 4)
      });
      if (!d) return null;
      renderDrawing(d);
      refreshList();
      status('Pin: ' + d.name);
      return d;
    }

    function addRings(lat, lon, radiiText) {
      var radii = parseRadii(radiiText != null ? radiiText : ui.rings.value, prefs.units);
      if (!radii || !radii.length) { status('Rings: write radii such as 1, 5, 10 km'); return null; }
      var label = cleanText(ui.label.value, 80);
      var d = store.add({
        kind: 'circle', color: sessionColor(), points: [[lon, lat]], radii: radii,
        name: label || 'Rings ' + radii.map(function (r) { return formatDistance(r, prefs.units); }).join(' / ')
      });
      if (!d) return null;
      renderDrawing(d);
      refreshList();
      status(d.name);
      return d;
    }

    function addPoint(p) {
      var s = m.session;
      if (!s || !p) return false;
      var t = s.tool;
      if (t.kind === 'pin') { addPin(p.lat, p.lon); return true; }
      if (t.kind === 'circle') { addRings(p.lat, p.lon); return true; }
      var ll = [round7(p.lon), round7(p.lat)];
      var last = s.points[s.points.length - 1];
      if (last && geodesicDistance(last[1], last[0], ll[1], ll[0]) < 0.5) return false;
      if (s.points.length >= MAX_POINTS) return false;
      s.points.push(ll);
      addVertex(ll);
      if (t.max && s.points.length >= t.max) finish();
      else updateLive();
      return true;
    }

    // -------- terrain sampling, profile and line of sight

    function sampleHeights(points) {
      var provider = viewer.terrainProvider;
      if (!terrainLive() || !provider) return Promise.resolve(null);
      var cartos = points.map(function (q) { return C.Cartographic.fromDegrees(q.lon, q.lat); });
      var p;
      try { p = Promise.resolve(C.sampleTerrainMostDetailed(provider, cartos)); } catch (err) { p = Promise.reject(err); }
      return p.catch(function () { return C.sampleTerrain(provider, 11, cartos); }).then(function (res) {
        return (res || cartos).map(function (c) { return c && isNum(c.height) ? c.height : 0; });
      });
    }

    function drawChart(samples, los, css) {
      ui.chartWrap.style.display = '';
      var g = typeof ui.chart.getContext === 'function' ? ui.chart.getContext('2d') : null;
      if (!g || samples.length < 2) return;
      var W = ui.chart.width;
      var H = ui.chart.height;
      var D = samples[samples.length - 1].d || 1;
      var R = MEAN_RADIUS_M / (1 - (los ? los.k : 0));
      var ys = samples.map(function (s) { return s.h + (los ? s.d * (D - s.d) / (2 * R) : 0); });
      var a = los ? ys[0] + los.h0 : null;
      var b = los ? ys[ys.length - 1] + los.h1 : null;
      var lo = Math.min.apply(null, ys.concat(los ? [a, b] : []));
      var hi = Math.max.apply(null, ys.concat(los ? [a, b] : []));
      if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
      var padY = (hi - lo) * 0.08;
      lo -= padY;
      hi += padY;
      var X = function (d) { return d / D * (W - 2) + 1; };
      var Y = function (h) { return H - 12 - (h - lo) / (hi - lo) * (H - 16); };
      g.clearRect(0, 0, W, H);
      g.beginPath();
      g.moveTo(X(0), H - 12);
      samples.forEach(function (s, i) { g.lineTo(X(s.d), Y(ys[i])); });
      g.lineTo(X(D), H - 12);
      g.closePath();
      g.fillStyle = 'rgba(255,176,0,0.22)';
      g.fill();
      g.beginPath();
      samples.forEach(function (s, i) { if (i) g.lineTo(X(s.d), Y(ys[i])); else g.moveTo(X(s.d), Y(ys[i])); });
      g.strokeStyle = '#ffb000';
      g.lineWidth = 1;
      g.stroke();
      if (los) {
        g.beginPath();
        g.moveTo(X(0), Y(a));
        g.lineTo(X(D), Y(b));
        g.strokeStyle = css;
        g.lineWidth = 1.5;
        g.stroke();
      }
      g.fillStyle = '#ffb000';
      g.font = '9px monospace';
      g.fillText(elevText(Math.max.apply(null, samples.map(function (s) { return s.h; }))), 3, 9);
      g.fillText(formatDistance(D, prefs.units), W - 60, H - 2);
    }

    function runProfile(d) {
      var pts = profilePoints(d.points, 200);
      if (!terrainLive()) {
        ui.chartWrap.style.display = 'none';
        status('Elevation profile needs terrain: turn TERRAIN on in Map view, then draw it again.');
        return Promise.resolve(null);
      }
      status('Sampling terrain along ' + formatDistance(pathLength(d.points), prefs.units) + '...');
      return sampleHeights(pts).then(function (hs) {
        if (!hs) return null;
        var samples = pts.map(function (q, i) { return { d: q.d, h: hs[i] }; });
        var st = profileStats(samples);
        drawChart(samples, null, d.color);
        ui.chartInfo.textContent = 'Min ' + elevText(st.min) + '  max ' + elevText(st.max) +
          '  climb ' + elevText(st.gain) + '  descent ' + elevText(st.loss);
        status(d.name);
        return { samples: samples, stats: st };
      }, function (err) {
        status('Terrain sampling failed: ' + errMsg(err));
        return null;
      });
    }

    function runLos(d, label) {
      var pts = profilePoints(d.points, 240);
      var eyes = parseEyeHeights(ui.eyes.value) || [2, 2];
      var heights = terrainLive() ? sampleHeights(pts) : Promise.resolve(null);
      return heights.then(function (hs) {
        var samples = pts.map(function (q, i) { return { d: q.d, h: hs ? hs[i] : 0 }; });
        var res = lineOfSight(samples, eyes[0], eyes[1]);
        if (!res) return null;
        var css = res.visible ? '#00e676' : '#ff4040';
        var name = (label ? label + ': ' : 'LOS ') + (res.visible ? 'clear ' : 'blocked ') + formatDistance(res.distance, prefs.units) +
          (res.visible ? '' : ' (at ' + formatDistance(res.blockedAt, prefs.units) + ')');
        var upd = store.update(d.id, { color: css, name: name });
        if (upd) { renderDrawing(upd); refreshList(); }
        drawChart(samples, { h0: eyes[0], h1: eyes[1], k: res.k }, css);
        ui.chartInfo.textContent = 'Eyes ' + eyes[0] + ' m / ' + eyes[1] + ' m, k ' + res.k +
          ', min clearance ' + elevText(res.minClearance) + (terrainLive() ? '' : ' (terrain off: earth curve only)');
        status(name);
        return res;
      }, function (err) {
        status('Terrain sampling failed: ' + errMsg(err));
        return null;
      });
    }

    // -------- graticule and terminator

    function viewRect() {
      var r = null;
      try { r = camera.computeViewRectangle(scene.globe ? scene.globe.ellipsoid : undefined); } catch (err) { r = null; }
      if (!r || !isNum(r.west)) return { west: -180, south: -90, east: 180, north: 90 };
      return { west: r.west * R2D, south: r.south * R2D, east: r.east * R2D, north: r.north * R2D };
    }

    function drawGraticule() {
      gridDs.entities.removeAll();
      if (!prefs.grid) return 0;
      var r = viewRect();
      var east = r.east <= r.west ? r.east + 360 : r.east;
      var midLat = clamp((r.south + r.north) / 2, -85, 85);
      var span = Math.max(r.north - r.south, (east - r.west) * Math.cos(midLat * D2R));
      var g = graticuleLines(r, graticuleStep(span, 8));
      var lineColor = color('#ffb000', 0.35);
      var midLon = r.west + (east - r.west) / 2;
      // A whole-globe view rectangle puts every label on the equator and the
      // central meridian of the full map, half of them on the far side; anchor
      // them under the camera instead and skip the ones beyond the horizon.
      var cam = camera.positionCartographic;
      var camLon = cam && isNum(cam.longitude) ? cam.longitude * R2D : null;
      var camLat = cam && isNum(cam.latitude) ? cam.latitude * R2D : null;
      var camH = cam && isNum(cam.height) ? cam.height : null;
      if (east - r.west > 180 && camLon !== null && camLat !== null) {
        midLon = camLon;
        midLat = clamp(camLat, -80, 80);
      }
      var near = function (lon, lat) { return labelFacesCamera(lon, lat, camLon, camLat, camH); };
      var clampLines = terrainLive();
      // A grid line names itself on a click, and anything drawn under it still answers first.
      var gridLine = function (spec, name) {
        spec.name = name;
        spec.properties = { type: 'grid line', detail_rows: [['MEANING', 'Latitude and longitude graticule (GRID button)']] };
        gridDs.entities.add(spec).sdaBackground = true;
      };
      var count = 0;
      g.meridians.forEach(function (mm) {
        var pts = [];
        var stepLat = Math.max((mm.north - mm.south) / 6, 1e-6);
        for (var lat = mm.south; lat < mm.north; lat += stepLat) pts.push([mm.lon, lat]);
        pts.push([mm.lon, mm.north]);
        gridLine({ polyline: { positions: degArray(pts), width: 1, material: lineColor, arcType: C.ArcType.GEODESIC, clampToGround: clampLines } }, 'Meridian ' + mm.label);
        if (near(mm.lon, midLat)) gridLine({ position: deg(mm.lon, midLat), label: labelGraphics(mm.label, '#ffb000', true) }, 'Meridian ' + mm.label);
        count++;
      });
      g.parallels.forEach(function (pp) {
        var pts = [];
        var stepLon = Math.min(10, Math.max((pp.east - pp.west) / 8, 1e-6));
        for (var lon = pp.west; lon < pp.east; lon += stepLon) pts.push([lon, pp.lat]);
        pts.push([pp.east, pp.lat]);
        gridLine({ polyline: { positions: degArray(pts), width: 1, material: lineColor, arcType: C.ArcType.RHUMB, clampToGround: clampLines } }, 'Parallel ' + pp.label);
        if (near(midLon, pp.lat)) gridLine({ position: deg(midLon, pp.lat), label: labelGraphics(pp.label, '#ffb000', true) }, 'Parallel ' + pp.label);
        count++;
      });
      return count;
    }

    function drawTerminator() {
      sunDs.entities.removeAll();
      if (!prefs.sun) return 0;
      var t = simTime();
      m.sunTime = t.getTime();
      TWILIGHT.forEach(function (tw, i) {
        var ring = terminatorPoints(t, tw.depression, 180);
        sunDs.entities.add({
          id: 'mt-sun-' + i,
          name: tw.name + (i ? ' twilight edge' : ' line'),
          polyline: {
            positions: degArray(ring),
            width: i ? 1 : 2,
            material: i ? new C.PolylineDashMaterialProperty({ color: color('#6aa8ff', tw.alpha), dashLength: 12 }) : color('#ffd060', tw.alpha),
            arcType: C.ArcType.GEODESIC
          }
        });
      });
      var s = subsolarPoint(t);
      // Depth tested, unlike the other labels: this one is only redrawn as
      // the clock moves, so a camera check would go stale, and with the test
      // off it shows through the globe whenever the sun is on the far side.
      var sunLabel = labelGraphics('SUN ' + formatHemi(s.lat, s.lon, 1), '#ffd060', true);
      sunLabel.heightReference = C.HeightReference.NONE;
      sunLabel.disableDepthTestDistance = 0;
      sunDs.entities.add({
        id: 'mt-sun-point',
        name: 'Subsolar point',
        position: deg(s.lon, s.lat, 3000),
        point: { pixelSize: 8, color: color('#ffd060'), outlineColor: C.Color.BLACK, outlineWidth: 1 },
        label: sunLabel
      });
      return TWILIGHT.length;
    }

    // -------- geocoding

    function fetchJson(url) {
      if (typeof global.fetch !== 'function') return Promise.reject(new Error('fetch is not available'));
      return global.fetch(url, { headers: { Accept: 'application/json' } }).then(function (r) {
        if (!r.ok) {
          var e = new Error('HTTP ' + r.status);
          e.status = r.status;
          throw e;
        }
        return r.json();
      });
    }

    // Nominatim allows one request a second; queue direct calls behind that.
    function politeFetch(url) {
      var g = m.geo;
      function run() {
        var wait = Math.max(0, g.last + NOMINATIM_GAP_MS - Date.now());
        return new Promise(function (resolve) { setTimeout(resolve, wait); }).then(function () {
          g.last = Date.now();
          return fetchJson(url);
        });
      }
      var p = g.queue.then(run, run);
      g.queue = p.then(function () {}, function () {});
      return p;
    }

    function zoomForHeight() {
      var h = cameraHeight();
      return h > 2e6 ? 5 : h > 5e5 ? 8 : h > 1e5 ? 10 : h > 2e4 ? 13 : 16;
    }

    function reverseGeocode(lat, lon) {
      var g = m.geo;
      var key = lat.toFixed(4) + ',' + lon.toFixed(4);
      if (Object.prototype.hasOwnProperty.call(g.cache, key)) return Promise.resolve(g.cache[key]);
      function direct() { return politeFetch(nominatimReverseUrl(lat, lon, zoomForHeight())).then(placeName); }
      var p = g.serverMissing ? direct() : fetchJson(reverseGeocodeUrl(api, lat, lon)).then(placeName, function (err) {
        if (err && (err.status === 404 || err.status === 405 || err.status === 501)) {
          g.serverMissing = true;
          return direct();
        }
        throw err;
      });
      return p.then(function (name) {
        g.cache[key] = name || '';
        return name || '';
      }, function () { return ''; });
    }

    function firstPlace(json) {
      var list = Array.isArray(json) ? json : json && Array.isArray(json.results) ? json.results : [];
      for (var i = 0; i < list.length; i++) {
        var r = list[i] || {};
        var lat = Number(r.lat);
        var lon = Number(r.lon);
        if (validLatLon(lat, lon)) return { lat: lat, lon: lon, name: cleanText(r.display_name || r.name || '', 160) };
      }
      return null;
    }

    function flyToPoint(lat, lon, label, kind) {
      if (m.gotoEntity) { try { ds.entities.remove(m.gotoEntity); } catch (err) { /* gone */ } }
      m.gotoEntity = ds.entities.add({
        id: 'mt-goto',
        name: label,
        position: deg(lon, lat),
        point: pointGraphics('#00d4ff', 10),
        label: labelGraphics(label.length > 48 ? label.slice(0, 47) + '...' : label, '#00d4ff', true),
        properties: { type: 'annotation', kind: 'goto' }
      });
      var target = kind === 'place' ? 30000 : 15000;
      camera.flyTo({ destination: deg(lon, lat, Math.max(500, Math.min(cameraHeight(), target))), duration: 1.6 });
      status('Go to ' + label + '  ' + (toMGRS(lat, lon, 5) || ''));
      intel('Go to: ' + label);
    }

    function goTo(text) {
      var q = String(text == null ? '' : text).trim();
      if (!q) { status('Type a coordinate or a place.'); return Promise.resolve(null); }
      var c = parseCoordinate(q);
      if (c) {
        flyToPoint(c.lat, c.lon, c.label, c.kind);
        return Promise.resolve(c);
      }
      status('Searching for ' + q + '...');
      var req = isStatic ? politeFetch(nominatimSearchUrl(q, 1)) : fetchJson(api + '/api/geocode?q=' + encodeURIComponent(q));
      return req.then(function (json) {
        var hit = firstPlace(json);
        if (!hit) { status('No coordinate or place matched ' + q + '.'); return null; }
        hit.kind = 'place';
        flyToPoint(hit.lat, hit.lon, hit.name || q, 'place');
        return hit;
      }, function (err) {
        status('Place search failed: ' + errMsg(err));
        return null;
      });
    }

    // -------- right-click popup

    function closePopup() {
      if (m.popup) detach(m.popup);
      m.popup = null;
    }

    function copyText(text) {
      var clip = global.navigator && global.navigator.clipboard;
      if (clip && typeof clip.writeText === 'function') {
        return clip.writeText(text).then(function () { status('Copied: ' + text); return true; },
          function () { status('Clipboard refused; text: ' + text); return false; });
      }
      status('No clipboard here; text: ' + text);
      return Promise.resolve(false);
    }

    function showPopup(pos) {
      closePopup();
      var p = pickLatLon(pos);
      if (!p) return null;
      var box = el('div', 'mt-pop');
      var vw = global.innerWidth || 1200;
      var vh = global.innerHeight || 800;
      box.style.left = clamp(pos.x + 12, 4, vw - 290) + 'px';
      box.style.top = clamp(pos.y + 12, 46, vh - 230) + 'px';
      var decimal = formatDecimal(p.lat, p.lon, 6);
      var mgrs = toMGRS(p.lat, p.lon, 5);
      [decimal, toDMS(p.lat, 'lat', 2) + '  ' + toDMS(p.lon, 'lon', 2),
        mgrs ? 'MGRS ' + mgrs : 'MGRS: polar cap (UPS)', formatUTM(p.lat, p.lon) ? 'UTM ' + formatUTM(p.lat, p.lon) : '',
        terrainLive() ? 'Elev ' + elevText(p.height / exaggeration()) : 'Elev: terrain off'
      ].forEach(function (line) { if (line) box.appendChild(el('div', null, line)); });
      var place = el('div', null, 'Place: looking up...');
      box.appendChild(place);
      var actions = el('div', 'mt-row');
      actions.appendChild(button('COPY', 'Copy decimal, DMS and MGRS', function () {
        copyText(decimal + ' | ' + toDMS(p.lat, 'lat', 1) + ' ' + toDMS(p.lon, 'lon', 1) + (mgrs ? ' | ' + mgrs : ''));
      }));
      actions.appendChild(button('PIN', 'Drop a pin here', function () {
        var nm = place.textContent.indexOf('Place: ') === 0 && !/looking up|none found/.test(place.textContent)
          ? place.textContent.slice(7).split(',')[0] : '';
        addPin(p.lat, p.lon, nm || null);
        closePopup();
      }));
      actions.appendChild(button('RINGS', 'Range rings here, radii from the Rings box', function () { addRings(p.lat, p.lon); closePopup(); }));
      actions.appendChild(button('MEASURE', 'Measure distance starting here', function () {
        closePopup();
        startTool('dist');
        addPoint(p);
      }));
      if (global.SDASummit && typeof global.SDASummit.open === 'function') {
        actions.appendChild(button('CONDITIONS', 'Summit conditions here for the next seven days', function () {
          closePopup();
          global.SDASummit.open({ lat: p.lat, lon: p.lon });
        }));
      }
      actions.appendChild(button('X', 'Close', function () { closePopup(); }));
      box.appendChild(actions);
      if (doc.body) doc.body.appendChild(box);
      m.popup = box;
      m.popupPlace = reverseGeocode(p.lat, p.lon).then(function (name) {
        if (m.popup === box) place.textContent = name ? 'Place: ' + name : 'Place: none found';
        return name;
      });
      return p;
    }

    // -------- camera controls

    function centreTarget() {
      var s = canvasSize();
      return s.w && s.h ? pickCartesian(new C.Cartesian2(s.w / 2, s.h / 2)) : null;
    }
    function zoomIn() { if (typeof camera.zoomIn === 'function') camera.zoomIn(Math.max(cameraHeight() * 0.5, 5)); }
    function zoomOut() { if (typeof camera.zoomOut === 'function') camera.zoomOut(Math.max(cameraHeight(), 5)); }
    function orbitTo(heading, pitch) {
      var target = centreTarget();
      if (target && scene.mode === C.SceneMode.SCENE3D && typeof camera.flyToBoundingSphere === 'function') {
        var range = C.Cartesian3.distance(camera.positionWC, target);
        camera.flyToBoundingSphere(new C.BoundingSphere(target, 0), { offset: new C.HeadingPitchRange(heading, pitch, range), duration: 0.8 });
      } else if (typeof camera.setView === 'function') {
        camera.setView({ orientation: { heading: heading, pitch: pitch, roll: 0 } });
      }
    }
    function resetNorth() { orbitTo(0, isNum(camera.pitch) ? camera.pitch : -Math.PI / 2); }
    function topDown() { orbitTo(isNum(camera.heading) ? camera.heading : 0, -Math.PI / 2); }
    function home() { if (typeof camera.flyHome === 'function') camera.flyHome(1.5); }

    function currentMode() {
      var M = C.SceneMode || {};
      return scene.mode === M.SCENE2D ? '2d' : scene.mode === M.COLUMBUS_VIEW ? 'cv' : '3d';
    }
    function updateModeButtons(mode) {
      Object.keys(ui.modes).forEach(function (k) { setActive(ui.modes[k], k === mode); });
    }
    function setSceneMode(mode) {
      var mm = mode === '2d' || mode === 'cv' ? mode : '3d';
      if (mm === '2d' && typeof scene.morphTo2D === 'function') scene.morphTo2D(1.2);
      else if (mm === 'cv' && typeof scene.morphToColumbusView === 'function') scene.morphToColumbusView(1.2);
      else if (typeof scene.morphTo3D === 'function') scene.morphTo3D(1.2);
      updateModeButtons(mm);
      return mm;
    }

    // -------- terrain and view toggles

    // The page may load its own terrain after this module starts (ion token from
    // /api/config), so the flag is refreshed from the viewer when read.
    function terrainLive() {
      if (!m.terrainOn && !m.terrainWant && hasRealTerrain()) {
        m.terrainOn = true;
        m.terrainWant = true;
        m.terrainSource = 'page';
        updateToggles();
      }
      return m.terrainOn;
    }

    function hasRealTerrain() {
      var tp = viewer.terrainProvider;
      return !!(tp && typeof C.EllipsoidTerrainProvider === 'function' && !(tp instanceof C.EllipsoidTerrainProvider));
    }

    function setTerrain(onFlag) {
      var want = !!onFlag;
      m.terrainWant = want;
      prefs.terrain = want;
      savePrefs();
      if (!want) {
        viewer.terrainProvider = new C.EllipsoidTerrainProvider();
        m.terrainOn = false;
        m.terrainSource = '';
        updateToggles();
        status('Terrain off.');
        return Promise.resolve(false);
      }
      var ion = !!(C.Ion && C.Ion.defaultAccessToken) && typeof C.createWorldTerrainAsync === 'function';
      updateToggles();
      status('Loading terrain...');
      var p;
      try {
        p = ion ? C.createWorldTerrainAsync() : C.ArcGISTiledElevationTerrainProvider.fromUrl(ARCGIS_ELEVATION_URL);
      } catch (err) {
        p = Promise.reject(err);
      }
      return Promise.resolve(p).then(function (provider) {
        if (!m.terrainWant || m.destroyed) return false;
        viewer.terrainProvider = provider;
        m.terrainOn = true;
        m.terrainSource = ion ? 'Cesium World Terrain' : 'Esri World Elevation 3D';
        updateToggles();
        status('Terrain on: ' + m.terrainSource + '.');
        if (prefs.grid) drawGraticule();
        return true;
      }, function (err) {
        m.terrainWant = false;
        m.terrainOn = false;
        prefs.terrain = false;
        savePrefs();
        updateToggles();
        status('Terrain did not load: ' + errMsg(err));
        return false;
      });
    }

    function setExaggeration(v) {
      var x = clamp(Number(v) || 1, 1, 10);
      if ('verticalExaggeration' in scene) scene.verticalExaggeration = x;
      else if (scene.globe) scene.globe.terrainExaggeration = x;
      prefs.exaggeration = x;
      savePrefs();
      ui.exag.value = String(x);
      ui.exagVal.textContent = 'x' + x;
      return x;
    }

    var UNIT_SHORT = { metric: 'KM', imperial: 'MI', nautical: 'NM' };

    function updateToggles() {
      setActive(ui.terrain, m.terrainWant);
      setActive(ui.gridBtn, prefs.grid);
      setActive(ui.sunBtn, prefs.sun);
      setActive(ui.hudBtn, prefs.readout);
      setActive(ui.navBtn, prefs.nav);
      ui.unitsBtn.textContent = 'UNITS ' + UNIT_SHORT[prefs.units];
      ui.readout.style.display = prefs.readout ? '' : 'none';
      ui.nav.style.display = prefs.nav ? '' : 'none';
      ui.swatches.forEach(function (s) { setActive(s.el, s.color === prefs.color, 'mt-swatch'); });
    }

    function setUnits(u) {
      if (UNITS.indexOf(u) < 0) return prefs.units;
      prefs.units = u;
      savePrefs();
      updateToggles();
      refreshList();
      updateLive();
      return u;
    }
    function setReadout(onFlag) { prefs.readout = !!onFlag; savePrefs(); updateToggles(); return prefs.readout; }
    function setNav(onFlag) { prefs.nav = !!onFlag; savePrefs(); updateToggles(); return prefs.nav; }
    function setGraticule(onFlag) { prefs.grid = !!onFlag; savePrefs(); updateToggles(); return drawGraticule(); }
    function setTerminator(onFlag) { prefs.sun = !!onFlag; savePrefs(); updateToggles(); return drawTerminator(); }
    function setColor(c) {
      var css = cleanColor(c);
      if (!css) return prefs.color;
      prefs.color = css;
      savePrefs();
      updateToggles();
      return css;
    }

    // -------- files: screenshot, export, import

    function stamp() { return new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '').replace('T', '-'); }

    function download(name, data, mime) {
      try {
        var url;
        var revoke = false;
        if (typeof data === 'string' && data.indexOf('data:') === 0) {
          url = data;
        } else if (global.Blob && global.URL && typeof global.URL.createObjectURL === 'function') {
          url = global.URL.createObjectURL(new global.Blob([data], { type: mime }));
          revoke = true;
        } else {
          url = 'data:' + mime + ';charset=utf-8,' + encodeURIComponent(data);
        }
        var a = doc.createElement('a');
        a.href = url;
        a.download = name;
        a.style.display = 'none';
        doc.body.appendChild(a);
        a.click();
        doc.body.removeChild(a);
        if (revoke) setTimeout(function () { global.URL.revokeObjectURL(url); }, 4000);
        return true;
      } catch (err) {
        status('Download failed: ' + errMsg(err));
        return false;
      }
    }

    function footerText() {
      var parts = ['SDA WorldView', simTime().toISOString().replace(/\.\d+Z$/, 'Z')];
      var c = camera.positionCartographic;
      if (c && isNum(c.height)) parts.push('camera ' + formatHemi(c.latitude * R2D, c.longitude * R2D, 3) + ' alt ' + formatDistance(c.height, 'metric'));
      var credit = doc.getElementById('basemap-credit');
      var ct = credit ? cleanText(credit.textContent, 160) : '';
      if (ct) parts.push(ct);
      return parts.join('   ');
    }

    // The globe's WebGL buffer is valid only inside the frame that drew it
    // (preserveDrawingBuffer is off), so the copy happens in postRender.
    function screenshot() {
      return new Promise(function (resolve, reject) {
        var done = false;
        var remove = null;
        function grab() {
          if (done) return;
          done = true;
          if (typeof remove === 'function') remove();
          try {
            var src = scene.canvas;
            var W = src.width;
            var H = src.height;
            var band = 22;
            var out = doc.createElement('canvas');
            out.width = W;
            out.height = H + band;
            var g = out.getContext('2d');
            g.drawImage(src, 0, 0);
            g.fillStyle = '#000';
            g.fillRect(0, H, W, band);
            g.fillStyle = '#ffb000';
            g.font = '12px monospace';
            g.textBaseline = 'middle';
            g.fillText(footerText(), 8, H + band / 2);
            var url = out.toDataURL('image/png');
            download('sda-globe-' + stamp() + '.png', url, 'image/png');
            status('Screenshot saved.');
            resolve(url);
          } catch (err) {
            status('Screenshot failed: ' + errMsg(err));
            reject(err);
          }
        }
        remove = scene.postRender.addEventListener(grab);
        if (typeof scene.requestRender === 'function') scene.requestRender();
      });
    }

    function toggleFullscreen() {
      var el2 = doc.documentElement;
      try {
        if (doc.fullscreenElement || doc.webkitFullscreenElement) {
          var exit = doc.exitFullscreen || doc.webkitExitFullscreen;
          if (exit) exit.call(doc);
          return false;
        }
        var req = el2 && (el2.requestFullscreen || el2.webkitRequestFullscreen);
        if (!req) { status('Fullscreen is not available here.'); return false; }
        var r = req.call(el2);
        if (r && typeof r.catch === 'function') r.catch(function (err) { status('Fullscreen refused: ' + errMsg(err)); });
        return true;
      } catch (err) {
        status('Fullscreen failed: ' + errMsg(err));
        return false;
      }
    }

    function exportFile(kind) {
      var list = store.list();
      if (!list.length) { status('Nothing to export yet.'); return null; }
      var isKml = kind === 'kml';
      var text = isKml ? drawingsToKML(list) : JSON.stringify(drawingsToGeoJSON(list), null, 1);
      download('sda-annotations-' + stamp() + (isKml ? '.kml' : '.geojson'), text,
        isKml ? 'application/vnd.google-earth.kml+xml' : 'application/geo+json');
      status('Exported ' + list.length + ' drawing' + (list.length === 1 ? '' : 's') + ' as ' + (isKml ? 'KML' : 'GeoJSON') + '.');
      return text;
    }

    function importText(text, filename) {
      var res;
      try {
        res = importDrawings(text, filename);
      } catch (err) {
        status('Import failed: ' + errMsg(err));
        return null;
      }
      var added = store.addMany(res.drawings);
      added.forEach(function (d) { try { renderDrawing(d); } catch (err) { /* keep the rest */ } });
      refreshList();
      var msg = 'Imported ' + added.length + ' from ' + (filename || res.format.toUpperCase()) +
        (res.skipped ? ', skipped ' + res.skipped : '') + '.';
      status(msg);
      intel(msg);
      if (added.length) flyToDrawings(added);
      return { added: added.length, skipped: res.skipped, format: res.format };
    }

    function importFile(file) {
      if (file.size > MAX_IMPORT_CHARS) { status('That file is larger than 20 MB.'); return Promise.resolve(null); }
      var read = typeof file.text === 'function' ? file.text() : new Promise(function (resolve, reject) {
        var r = new global.FileReader();
        r.onload = function () { resolve(String(r.result)); };
        r.onerror = function () { reject(r.error); };
        r.readAsText(file);
      });
      return read.then(function (t) { return importText(t, file.name); }, function (err) {
        status('Could not read the file: ' + errMsg(err));
        return null;
      });
    }

    // -------- saved views

    function currentView() {
      var c = camera.positionCartographic;
      if (!c) return null;
      return { lat: c.latitude * R2D, lon: c.longitude * R2D, height: c.height, heading: camera.heading, pitch: camera.pitch, roll: camera.roll };
    }

    function refreshViews() {
      clearChildren(ui.views);
      var items = marks.list();
      if (!items.length) ui.views.appendChild(el('div', 'mt-hint', 'No saved views yet.'));
      items.slice().reverse().forEach(function (b) {
        var r = el('div', 'mt-item');
        var name = el('button', 'mt-name', b.name);
        name.type = 'button';
        name.title = 'Fly to ' + b.name;
        name.addEventListener('click', function () { flyToBookmark(b.id); });
        var x = el('button', 'mt-x', 'x');
        x.type = 'button';
        x.title = 'Delete ' + b.name;
        x.addEventListener('click', function () { marks.remove(b.id); refreshViews(); });
        r.appendChild(name);
        r.appendChild(x);
        ui.views.appendChild(r);
      });
    }

    function saveView(name) {
      var b = marks.add(name, currentView());
      if (!b) { status('Could not read the camera view.'); return null; }
      ui.viewName.value = '';
      refreshViews();
      status('Saved view ' + b.name + '.');
      return b;
    }

    function flyToBookmark(id) {
      var b = marks.get(id);
      if (!b) return false;
      var v = b.view;
      camera.flyTo({ destination: deg(v.lon, v.lat, v.height), orientation: { heading: v.heading, pitch: v.pitch, roll: v.roll }, duration: 1.5 });
      return true;
    }

    // -------- imagery layers

    function layerLabel(layer, idx) {
      if (layer && typeof layer.sdaLabel === 'string' && layer.sdaLabel) return cleanText(layer.sdaLabel, 60);
      var prov = layer && layer.imageryProvider;
      var url = prov && (prov.url || prov._url || (prov._resource && prov._resource.url));
      var host = imageryLabelFromUrl(url);
      var kind = prov && prov.constructor && prov.constructor.name ? prov.constructor.name.replace(/ImageryProvider$/, '') : '';
      return (idx === 0 ? 'Base: ' : 'Layer ' + idx + ': ') + (host || kind || 'imagery');
    }

    function refreshLayers() {
      clearChildren(ui.layers);
      var layers = viewer.imageryLayers;
      if (!layers || !isNum(layers.length) || !layers.length) {
        ui.layers.appendChild(el('div', 'mt-hint', 'No imagery layers.'));
        return 0;
      }
      var shown = 0;
      for (var i = layers.length - 1; i >= 0 && shown < 20; i--, shown++) {
        (function (layer, idx) {
          var r = el('div', 'mt-layer');
          var cb = el('input');
          cb.type = 'checkbox';
          cb.checked = layer.show !== false;
          cb.title = 'Show or hide';
          cb.addEventListener('change', function () { layer.show = !!cb.checked; });
          var name = el('span', null, layerLabel(layer, idx));
          name.title = name.textContent;
          var sl = el('input');
          sl.type = 'range';
          sl.min = '0';
          sl.max = '1';
          sl.step = '0.05';
          sl.value = String(isNum(layer.alpha) ? layer.alpha : 1);
          sl.title = 'Opacity';
          sl.addEventListener('input', function () { layer.alpha = clamp(Number(sl.value), 0, 1); });
          r.appendChild(cb);
          r.appendChild(name);
          r.appendChild(sl);
          ui.layers.appendChild(r);
        })(layers.get(i), i);
      }
      return shown;
    }

    function scheduleLayers() {
      if (m.layerTimer) clearTimeout(m.layerTimer);
      m.layerTimer = setTimeout(function () { m.layerTimer = null; if (!m.destroyed) refreshLayers(); }, 300);
    }

    // -------- events

    var T = C.ScreenSpaceEventType;
    var handler = new C.ScreenSpaceEventHandler(scene.canvas);
    cleanup.push(function () { try { handler.destroy(); } catch (err) { /* gone */ } });
    handler.setInputAction(function (mv) {
      var now = Date.now();
      if (now - m.lastMove < 40) return;
      m.lastMove = now;
      m.cursor = pickLatLon(mv && mv.endPosition);
      updateReadout(m.cursor);
      if (m.session) updateLive();
    }, T.MOUSE_MOVE);
    handler.setInputAction(function (ev) {
      if (!m.session) return;
      var p = pickLatLon(ev && ev.position);
      if (!p) { status('That click missed the globe.'); return; }
      addPoint(p);
    }, T.LEFT_CLICK);
    handler.setInputAction(function (ev) {
      if (m.session) {
        if (m.session.tool.kind === 'pin' || m.session.tool.kind === 'circle') cancel();
        else finish();
        return;
      }
      showPopup(ev && ev.position);
    }, T.RIGHT_CLICK);
    handler.setInputAction(function () {
      if (m.session && m.session.tool.kind !== 'pin' && m.session.tool.kind !== 'circle') finish();
    }, T.LEFT_DOUBLE_CLICK);

    onCesium(scene.postRender, function () {
      var now = Date.now();
      if (now - m.lastFrame < 150) return;
      m.lastFrame = now;
      updateCompass();
      updateScale();
      if (prefs.sun && Math.abs(simTime().getTime() - m.sunTime) > 60000) drawTerminator();
    });
    onCesium(camera.moveStart, function () { closePopup(); });
    onCesium(camera.moveEnd, function () { if (prefs.grid) drawGraticule(); });
    onCesium(scene.morphComplete, function () {
      updateModeButtons(currentMode());
      if (prefs.grid) drawGraticule();
    });
    ['layerAdded', 'layerRemoved', 'layerMoved', 'layerShownOrHidden'].forEach(function (k) {
      onCesium(viewer.imageryLayers && viewer.imageryLayers[k], scheduleLayers);
    });
    cleanup.push(function () { if (m.layerTimer) clearTimeout(m.layerTimer); });

    function handleKey(key) {
      if (m.session) {
        if (key === 'Escape') { cancel(); return true; }
        if (key === 'Enter') { finish(); return true; }
        if (key === 'Backspace' || key === 'Delete') { undoPoint(); return true; }
        return false;
      }
      if (m.popup && key === 'Escape') { closePopup(); return true; }
      return false;
    }
    on(doc, 'keydown', function (e) {
      var tgt = e.target;
      var tag = tgt && tgt.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (tgt && tgt.isContentEditable)) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (handleKey(e.key)) {
        e.preventDefault();
        if (typeof e.stopPropagation === 'function') e.stopPropagation();
      }
    }, true);

    var ACTIONS = {
      zoomIn: zoomIn, zoomOut: zoomOut, resetNorth: resetNorth, topDown: topDown, home: home,
      screenshot: function () { return screenshot().catch(function () { return null; }); },
      fullscreen: toggleFullscreen,
      graticule: function () { return setGraticule(!prefs.grid); },
      terminator: function () { return setTerminator(!prefs.sun); },
      terrain: function () { return setTerrain(!m.terrainWant); },
      readout: function () { return setReadout(!prefs.readout); },
      units: function () { return setUnits(UNITS[(UNITS.indexOf(prefs.units) + 1) % UNITS.length]); },
      measure: function () { return toggleTool('dist'); },
      area: function () { return toggleTool('area'); },
      pin: function () { return toggleTool('pin'); },
      goto: function () { if (typeof ui.goto.focus === 'function') ui.goto.focus(); return true; },
      mode2d: function () { return setSceneMode('2d'); },
      mode3d: function () { return setSceneMode('3d'); },
      modecv: function () { return setSceneMode('cv'); }
    };

    function destroy() {
      if (m.destroyed) return;
      if (m.session) endSession();
      m.destroyed = true;
      closePopup();
      cleanup.splice(0).forEach(function (fn) { try { fn(); } catch (err) { /* keep going */ } });
      [ds, gridDs, sunDs].forEach(function (d) { try { viewer.dataSources.remove(d, true); } catch (err) { /* gone */ } });
      detach(ui.readout);
      detach(ui.nav);
      detach(rootEl);
      setBodyDrawing(false);
    }

    // -------- start

    m.terrainOn = hasRealTerrain();
    m.terrainWant = m.terrainOn;
    if (m.terrainOn) m.terrainSource = 'page';
    if (prefs.exaggeration !== 1) setExaggeration(prefs.exaggeration);
    renderAll();
    refreshList();
    refreshViews();
    refreshLayers();
    updateToggles();
    updateModeButtons(currentMode());
    if (prefs.grid) drawGraticule();
    if (prefs.sun) drawTerminator();
    if (prefs.terrain && !m.terrainOn) setTerrain(true);

    return {
      root: rootEl,
      ui: ui,
      state: m,
      prefs: prefs,
      store: store,
      marks: marks,
      dataSources: { drawings: ds, grid: gridDs, sun: sunDs },
      handler: handler,
      action: function (name) { var fn = ACTIONS[name]; return fn ? fn() : undefined; },
      actions: Object.keys(ACTIONS),
      startTool: startTool,
      toggleTool: toggleTool,
      addPoint: addPoint,
      finish: finish,
      cancel: cancel,
      undo: undoPoint,
      addPin: addPin,
      addRings: addRings,
      removeDrawing: removeDrawing,
      removeLast: removeLast,
      clearAll: clearAll,
      goTo: goTo,
      reverseGeocode: reverseGeocode,
      showPopup: showPopup,
      closePopup: closePopup,
      screenshot: screenshot,
      toggleFullscreen: toggleFullscreen,
      setTerrain: setTerrain,
      setExaggeration: setExaggeration,
      setSceneMode: setSceneMode,
      setGraticule: setGraticule,
      setTerminator: setTerminator,
      setReadout: setReadout,
      setNav: setNav,
      setUnits: setUnits,
      setColor: setColor,
      saveView: saveView,
      flyToBookmark: flyToBookmark,
      exportGeoJSON: function () { return drawingsToGeoJSON(store.list()); },
      exportKML: function () { return drawingsToKML(store.list()); },
      exportFile: exportFile,
      importText: importText,
      refreshLayers: refreshLayers,
      updateScale: updateScale,
      runProfile: runProfile,
      runLos: runLos,
      handleKey: handleKey,
      isActive: function () { return !!m.session; },
      destroy: destroy
    };
  }

  // ---------------------------------------------------------------- page entry

  var current = null;
  var lastError = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.viewer || !ctx.viewer.scene || !global.Cesium || !global.document) return false;
      if (current) current.destroy();
      current = null;
      current = createModule(ctx);
      lastError = null;
      return true;
    } catch (err) {
      lastError = err;
      current = null;
      if (ctx && typeof ctx.addIntelItem === 'function') {
        try { ctx.addIntelItem('Map tools did not start: ' + errMsg(err)); } catch (e2) { /* page feed only */ }
      }
      return false;
    }
  }

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    if (!current || typeof current[name] !== 'function') return undefined;
    return current[name].apply(null, args);
  }

  global.SDAMapTools = {
    init: init,
    instance: function () { return current; },
    lastError: function () { return lastError; },
    action: function (name) { return call('action', name); },
    startTool: function (id) { return call('startTool', id); },
    finish: function () { return call('finish'); },
    cancel: function () { return call('cancel'); },
    undo: function () { return call('undo'); },
    goTo: function (text) { return call('goTo', text); },
    screenshot: function () { return current ? current.screenshot() : Promise.reject(new Error('Map tools are not running')); },
    setTerrain: function (on) { return call('setTerrain', on); },
    setExaggeration: function (v) { return call('setExaggeration', v); },
    setSceneMode: function (mode) { return call('setSceneMode', mode); },
    setGraticule: function (on) { return call('setGraticule', on); },
    setTerminator: function (on) { return call('setTerminator', on); },
    setUnits: function (u) { return call('setUnits', u); },
    drawings: function () { return current ? current.store.list() : []; },
    bookmarks: function () { return current ? current.marks.list() : []; },
    saveView: function (name) { return call('saveView', name); },
    flyToBookmark: function (id) { return call('flyToBookmark', id); },
    exportGeoJSON: function () { return call('exportGeoJSON'); },
    exportKML: function () { return call('exportKML'); },
    importText: function (text, name) { return call('importText', text, name); },
    handleKey: function (key) { return !!call('handleKey', key); },
    isActive: function () { return !!(current && current.isActive()); },
    destroy: function () { if (current) current.destroy(); current = null; },

    STYLE_ID: STYLE_ID,
    STORE_KEYS: STORE_KEYS,
    ARCGIS_ELEVATION_URL: ARCGIS_ELEVATION_URL,
    NOMINATIM_URL: NOMINATIM_URL,
    NOMINATIM_GAP_MS: NOMINATIM_GAP_MS,
    ELLIPSOID: ELL,
    MEAN_RADIUS_M: MEAN_RADIUS_M,
    REFRACTION_K: REFRACTION_K,
    COLORS: COLORS,
    KINDS: KINDS,
    TOOLS: TOOLS.map(function (t) { return t.id; }),
    TWILIGHT: TWILIGHT,
    GRID_STEPS: GRID_STEPS,

    haversineDistance: haversineDistance,
    angularDistance: angularDistance,
    vincentyInverse: vincentyInverse,
    vincentyDirect: vincentyDirect,
    geodesicDistance: geodesicDistance,
    initialBearing: initialBearing,
    pathLength: pathLength,
    polygonArea: polygonArea,
    ringPoints: ringPoints,
    toDMS: toDMS,
    formatDecimal: formatDecimal,
    formatHemi: formatHemi,
    formatDistance: formatDistance,
    formatArea: formatArea,
    formatBearing: formatBearing,
    compassPoint: compassPoint,
    niceScale: niceScale,
    parseRadii: parseRadii,
    parseEyeHeights: parseEyeHeights,
    utmZone: utmZone,
    bandLetter: bandLetter,
    latLonToUtm: latLonToUtm,
    utmToLatLon: utmToLatLon,
    formatUTM: formatUTM,
    toMGRS: toMGRS,
    parseMGRS: parseMGRS,
    parseUTM: parseUTM,
    parsePlusCode: parsePlusCode,
    parseCoordinate: parseCoordinate,
    parseXml: parseXml,
    drawingsToGeoJSON: drawingsToGeoJSON,
    geoJSONToDrawings: geoJSONToDrawings,
    drawingsToKML: drawingsToKML,
    kmlToDrawings: kmlToDrawings,
    gpxToDrawings: gpxToDrawings,
    detectFormat: detectFormat,
    importDrawings: importDrawings,
    sanitizeDrawing: sanitizeDrawing,
    sanitizeView: sanitizeView,
    drawingMetric: drawingMetric,
    createBookmarkStore: createBookmarkStore,
    createDrawingStore: createDrawingStore,
    memoryStorage: memoryStorage,
    graticuleStep: graticuleStep,
    graticuleLines: graticuleLines,
    formatGraticuleLabel: formatGraticuleLabel,
    labelFacesCamera: labelFacesCamera,
    subsolarPoint: subsolarPoint,
    terminatorPoints: terminatorPoints,
    lineOfSight: lineOfSight,
    profilePoints: profilePoints,
    profileStats: profileStats,
    nominatimReverseUrl: nominatimReverseUrl,
    nominatimSearchUrl: nominatimSearchUrl,
    reverseGeocodeUrl: reverseGeocodeUrl,
    placeName: placeName,
    imageryLabelFromUrl: imageryLabelFromUrl,
    cleanPrefs: cleanPrefs
  };
})(typeof window !== 'undefined' ? window : this);
