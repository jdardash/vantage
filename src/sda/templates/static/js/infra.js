// Critical infrastructure on the WorldView globe: power plants (nuclear as its own
// toggle), airports, seaports, dams, data centres, submarine cables with their
// landing points, and radiation monitoring stations. One layer, a toggle per
// kind, a distinct shape and colour per kind, points clustered per kind on a
// grid that follows the view, a card per item on click, cables as polylines.
//
// Data: the server's catalog (sda/infra.py, built by `python -m sda.infra
// import`): WRI Global Power Plant Database (CC BY 4.0), OurAirports (public
// domain), NGA World Port Index (public domain), Wikidata dams (CC0), PeeringDB
// facilities, BfS ODL and Safecast radiation stations, and the TeleGeography
// submarine cable map (CC BY-NC-SA 3.0, fetched and cached by the server, never
// bundled). Airport cards link to LiveATC's feed search for the ICAO code. A
// cable card lists the countries it lands in and, for each, how many other cables
// land there; 0 means the country hangs on this one cable.
//
// Endpoints: /api/infra/summary, /api/infra/items?bbox=&kinds=&limit=,
// /api/infra/search?q=, /api/infra/item/<id>, /api/infra/cables,
// /api/infra/cables/<id>. On the static demo (no backend) the panel says the
// layer needs the live server and does nothing else.
//
// Frames: geodetic degrees only. No text is ever set as HTML; marker images come
// from the constant shape table below, links are http(s) only.
//
// Page wiring: the page loads this file, lists it in EXTENSIONS with a mount div,
// the Infrastructure layer toggle calls SDAInfra.setEnabled(on), and the page's
// click handler calls picked.id.sdaPick() for anything that carries one.

(function (global) {
  'use strict';

  var PANEL_TITLE = 'Infrastructure';
  var STYLE_ID = 'sda-infra-style';
  var LAYER_KEY = 'infra';

  var VIEW_LIMIT = 1500;
  var CLUSTER_ACROSS = 28;
  var CLOSE_ALT_M = 1500000;
  var LABEL_ALT_M = 600000;
  var LABEL_MAX = 60;
  var MOVE_DEBOUNCE_MS = 400;
  var SEARCH_DEBOUNCE_MS = 300;
  var CABLE_RETRY_MS = 60000;
  var CABLE_RETRY_MAX = 10;

  // Kind -> marker. Shape and colour both differ between kinds.
  var KINDS = [
    { key: 'power', label: 'Power plants', shape: 'square', color: '#ffd23f' },
    { key: 'nuclear', label: 'Nuclear plants', shape: 'hexagon', color: '#9be15d' },
    { key: 'airport', label: 'Airports', shape: 'triangle', color: '#4fc3f7' },
    { key: 'port', label: 'Seaports', shape: 'pentagon', color: '#00bfa5' },
    { key: 'dam', label: 'Dams', shape: 'invtriangle', color: '#7986cb' },
    { key: 'datacenter', label: 'Data centres', shape: 'diamond', color: '#f06292' },
    { key: 'landing', label: 'Cable landings', shape: 'circle', color: '#ff8a65' },
    { key: 'radiation', label: 'Radiation', shape: 'star', color: '#ef5350' }
  ];
  var CABLE_COLOR = '#ff7043';
  var KIND_BY_KEY = {};
  KINDS.forEach(function (k) { KIND_BY_KEY[k.key] = k; });

  var SHAPES = {
    square: '<rect x="5" y="5" width="14" height="14"',
    hexagon: '<polygon points="12,2 21,7 21,17 12,22 3,17 3,7"',
    triangle: '<polygon points="12,3 22,20 2,20"',
    pentagon: '<polygon points="12,2 22,9.5 18,21 6,21 2,9.5"',
    invtriangle: '<polygon points="2,4 22,4 12,21"',
    diamond: '<polygon points="12,2 22,12 12,22 2,12"',
    circle: '<circle cx="12" cy="12" r="8"',
    star: '<polygon points="12,2 14.9,8.6 22,9.3 16.6,14 18.2,21 12,17.3 5.8,21 7.4,14 2,9.3 9.1,8.6"'
  };

  function iconUrl(shape, color, cluster) {
    var stroke = cluster ? '#ffffff' : '#000000';
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
      'viewBox="0 0 24 24">' + SHAPES[shape] + ' fill="' + color +
      '" stroke="' + stroke + '" stroke-width="' + (cluster ? 2 : 1.4) + '"/></svg>';
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }

  var ICONS = {};
  var CLUSTER_ICONS = {};
  KINDS.forEach(function (k) {
    ICONS[k.key] = iconUrl(k.shape, k.color, false);
    CLUSTER_ICONS[k.key] = iconUrl(k.shape, k.color, true);
  });

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  function isNum(x) { return typeof x === 'number' && isFinite(x); }
  function isStr(x) { return typeof x === 'string' && x.length > 0; }

  function query(pairs) {
    var parts = [];
    for (var i = 0; i < pairs.length; i++) {
      var v = pairs[i][1];
      if (v === null || v === undefined || v === '') continue;
      parts.push(encodeURIComponent(pairs[i][0]) + '=' + encodeURIComponent(String(v)));
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  function round3(x) { return Math.round(x * 1000) / 1000; }

  function boxFromRect(rect, padFrac) {
    if (!rect || !isNum(rect.west) || !isNum(rect.south) || !isNum(rect.east) ||
        !isNum(rect.north)) return null;
    var d = 180 / Math.PI;
    var w = rect.west * d, s = rect.south * d, e = rect.east * d, n = rect.north * d;
    var pad = isNum(padFrac) ? padFrac : 0.1;
    var lonSpan = e >= w ? e - w : e + 360 - w;
    var latSpan = n - s;
    if (lonSpan >= 359) { w = -180; e = 180; } else {
      w -= lonSpan * pad; e += lonSpan * pad;
      if (w < -180) w += 360;
      if (e > 180) e -= 360;
    }
    s = Math.max(-90, s - latSpan * pad);
    n = Math.min(90, n + latSpan * pad);
    return { west: round3(w), south: round3(s), east: round3(e), north: round3(n) };
  }

  function boxAround(lat, lon, km) {
    var dLat = km / 111.195;
    var dLon = km / (111.195 * Math.max(0.05, Math.cos(lat * Math.PI / 180)));
    var w = lon - dLon, e = lon + dLon;
    if (dLon >= 180) { w = -180; e = 180; } else {
      if (w < -180) w += 360;
      if (e > 180) e -= 360;
    }
    return {
      west: round3(w), south: round3(Math.max(-90, lat - dLat)),
      east: round3(e), north: round3(Math.min(90, lat + dLat))
    };
  }

  function bboxParam(box) {
    return [box.west, box.south, box.east, box.north].join(',');
  }

  function withCommas(n) {
    return String(Math.round(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function formatCount(n) {
    if (!isNum(n)) return '0';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e4) return Math.round(n / 1e3) + 'k';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  // The kinds switched on, in display order, as the server's kinds parameter.
  function kindsParam(on) {
    return KINDS.filter(function (k) { return on && on[k.key]; })
      .map(function (k) { return k.key; }).join(',');
  }

  function safeUrl(u) {
    return (typeof u === 'string' && /^https?:\/\/[^\s]+$/i.test(u)) ? u : '';
  }

  function liveAtcUrl(icao) {
    var code = typeof icao === 'string' ? icao.trim().toUpperCase() : '';
    return /^[A-Z]{4}$/.test(code) ? 'https://www.liveatc.net/search/?icao=' + code : '';
  }

  // Group items per kind on a grid of about `across` cells over the view width.
  // A cell holding one item yields that item; two or more yield a cluster at
  // their mean position, carrying the count and the highest-scoring member.
  function cluster(items, box, across) {
    var singles = [];
    var clusters = [];
    if (!items || !items.length) return { singles: singles, clusters: clusters };
    var lonSpan = box ? (box.east >= box.west ? box.east - box.west
      : box.east + 360 - box.west) : 360;
    var size = Math.max(1e-4, lonSpan / Math.max(1, across || CLUSTER_ACROSS));
    var cells = {};
    var order = [];
    items.forEach(function (it) {
      if (!it || !isNum(it.lat) || !isNum(it.lon) || !KIND_BY_KEY[it.kind]) return;
      var lon = it.lon;
      if (box && box.east < box.west && lon < box.west) lon += 360;
      var key = it.kind + '|' + Math.floor((it.lat + 90) / size) + '|' +
        Math.floor((lon + 180) / size);
      if (!cells[key]) { cells[key] = []; order.push(key); }
      cells[key].push(it);
    });
    order.forEach(function (key) {
      var group = cells[key];
      if (group.length === 1) { singles.push(group[0]); return; }
      var lat = 0, x = 0, y = 0, best = group[0];
      group.forEach(function (it) {
        lat += it.lat;
        var r = it.lon * Math.PI / 180;
        x += Math.cos(r); y += Math.sin(r);
        if ((it.score || 0) > (best.score || 0)) best = it;
      });
      clusters.push({
        kind: group[0].kind, count: group.length, best: best,
        lat: lat / group.length, lon: Math.atan2(y, x) * 180 / Math.PI
      });
    });
    return { singles: singles, clusters: clusters };
  }

  function clusterSize(count) {
    if (!isNum(count) || count < 2) return 16;
    return Math.min(34, 16 + 6 * Math.log(count) / Math.LN10);
  }

  function fmtNum(v, digits) {
    if (!isNum(v)) return '';
    return digits ? v.toFixed(digits) : withCommas(v);
  }

  // Rows of [label, value] for an item card, by kind, skipping empty values.
  function cardModel(d) {
    if (!d || typeof d !== 'object') return null;
    var f = (d.fields && typeof d.fields === 'object') ? d.fields : {};
    var rows = [];
    function add(label, value) {
      if (value === null || value === undefined || value === '') return;
      rows.push([label, String(value)]);
    }
    var kind = d.kind;
    if (kind === 'power' || kind === 'nuclear') {
      add('Capacity', isNum(f.capacity_mw) ? withCommas(f.capacity_mw) + ' MW' : '');
      var fuels = [f.fuel].concat(Array.isArray(f.other_fuels) ? f.other_fuels : []);
      add('Fuel', fuels.filter(isStr).join(', '));
      add('Commissioned', isNum(f.commissioning_year) ? Math.round(f.commissioning_year) : '');
      add('Owner', f.owner);
      if (isNum(f.generation_gwh)) {
        add('Generation', withCommas(f.generation_gwh) + ' GWh in ' + f.generation_year +
          (f.generation_estimated ? ' (estimated)' : ''));
      }
      add('Country', d.country);
    } else if (kind === 'airport') {
      var codes = [];
      if (isStr(f.icao)) codes.push('ICAO ' + f.icao);
      if (isStr(f.iata)) codes.push('IATA ' + f.iata);
      add('Codes', codes.join(' / ') || f.ident);
      add('Type', f.type);
      add('Place', [f.municipality, f.region].filter(isStr).join(', '));
      add('Elevation', isNum(f.elevation_ft) ? withCommas(f.elevation_ft) + ' ft' : '');
      add('Scheduled service', f.scheduled_service ? 'yes' : 'no');
    } else if (kind === 'port') {
      add('Harbor', [f.harbor_size, f.harbor_type].filter(isStr).join(', '));
      add('Use', f.harbor_use);
      add('UN/LOCODE', f.un_locode);
      add('Water body', f.water_body);
      add('Channel depth', isNum(f.channel_depth_m) ? fmtNum(f.channel_depth_m, 1) + ' m' : '');
      add('Cargo pier depth', isNum(f.cargo_pier_depth_m)
        ? fmtNum(f.cargo_pier_depth_m, 1) + ' m' : '');
      add('Max vessel length', isNum(f.max_vessel_length_m)
        ? withCommas(f.max_vessel_length_m) + ' m' : '');
      var fac = [];
      if (f.container === 'Yes') fac.push('container');
      if (f.oil_terminal === 'Yes') fac.push('oil terminal');
      if (f.lng_terminal === 'Yes') fac.push('LNG terminal');
      add('Facilities', fac.join(', '));
      add('Country', d.country);
    } else if (kind === 'dam') {
      add('Height', isNum(f.height_m) ? fmtNum(f.height_m, 1) + ' m' : '');
      add('Built', isNum(f.year) ? f.year : '');
      add('River', f.river);
      add('Type', Array.isArray(f.types) ? f.types.filter(isStr).join(', ') : '');
      add('Country', d.country);
    } else if (kind === 'datacenter') {
      add('Operator', f.operator);
      add('Place', [f.city, f.state, d.country].filter(isStr).join(', '));
      add('Networks', isNum(f.networks) ? f.networks : '');
      add('Exchanges', isNum(f.exchanges) ? f.exchanges : '');
      add('Carriers', isNum(f.carriers) ? f.carriers : '');
      add('CLLI', f.clli);
    } else if (kind === 'radiation') {
      add('Network', f.network);
      if (isNum(f.dose_rate_usvh)) add('Dose rate', f.dose_rate_usvh.toFixed(3) + ' uSv/h');
      if (isNum(f.dose_rate_usvh_approx)) {
        add('Dose rate', 'about ' + f.dose_rate_usvh_approx.toFixed(3) + ' uSv/h');
      }
      if (isNum(f.cpm)) add('Count rate', fmtNum(f.cpm, 0) + ' cpm (' + (f.tube || '') + ')');
      add('Measured', f.measured_at);
      add('Status', f.status);
      add('Position', f.position_note);
    } else if (kind === 'landing') {
      add('Country', d.country);
      var n = Array.isArray(f.cables) ? f.cables.length : 0;
      add('Cables landing here', n);
      if (isNum(f.country_cables) && d.country) {
        add('Cables landing in ' + d.country, f.country_cables);
      }
    }
    var links = (Array.isArray(d.links) ? d.links : []).map(function (l) {
      return { label: l && isStr(l.label) ? l.label : 'Link', url: safeUrl(l && l.url) };
    }).filter(function (l) { return l.url; });
    if (kind === 'airport' && !links.some(function (l) { return /liveatc/.test(l.url); })) {
      var atc = liveAtcUrl(f.icao);
      if (atc) links.unshift({ label: 'LiveATC feeds for ' + f.icao, url: atc });
    }
    var src = d.source && isStr(d.source.name)
      ? d.source.name + (isStr(d.source.licence) ? ' (' + d.source.licence + ')' : '') : '';
    return {
      title: isStr(d.name) ? d.name : 'Unnamed',
      subtitle: [d.label || (KIND_BY_KEY[kind] ? KIND_BY_KEY[kind].label : ''), d.brief]
        .filter(isStr).join(' | '),
      rows: rows, links: links, source: src,
      cables: kind === 'landing' && Array.isArray(f.cables) ? f.cables.filter(function (c) {
        return c && isStr(c.id) && isStr(c.name);
      }) : []
    };
  }

  // Card model for a cable: facts, then one row per landing country with how
  // many other cables land there, fewest first (the server's order).
  function cableCardModel(c) {
    if (!c || typeof c !== 'object') return null;
    var rows = [];
    function add(label, value) {
      if (value === null || value === undefined || value === '') return;
      rows.push([label, String(value)]);
    }
    add('Length', c.length);
    add('Ready for service', isNum(c.rfs_year)
      ? c.rfs_year + (c.is_planned ? ' (planned)' : '') : (c.is_planned ? 'planned' : ''));
    add('Owners', c.owners);
    add('Suppliers', c.suppliers);
    add('Landing points', Array.isArray(c.landing_points) ? c.landing_points.length : '');
    var countries = (Array.isArray(c.countries_detail) ? c.countries_detail : [])
      .filter(function (r) { return r && isStr(r.country) && isNum(r.other_cables); })
      .map(function (r) {
        var other = r.other_cables;
        return {
          country: r.country,
          text: (isNum(r.landings) ? r.landings + ' landing' + (r.landings === 1 ? '' : 's') +
            ', ' : '') + (other === 0 ? 'no other cable lands there'
            : other + ' other cable' + (other === 1 ? '' : 's') + ' land there'),
          sole: other === 0
        };
      });
    var links = [];
    if (safeUrl(c.url)) links.push({ label: 'Cable website', url: c.url });
    if (safeUrl(c.map_url)) links.push({ label: 'Submarine Cable Map', url: c.map_url });
    return {
      title: isStr(c.name) ? c.name : 'Submarine cable',
      subtitle: 'Submarine cable | ' + (countries.length || 0) + ' countries',
      rows: rows, countries: countries, links: links,
      source: isStr(c.attribution) ? c.attribution : 'TeleGeography submarinecablemap.com'
    };
  }

  // Polyline vertex lists from a cable feature: arrays of [lon, lat], each with at
  // least two valid points.
  function cableLines(feature) {
    var g = feature && feature.geometry;
    if (!g) return [];
    var lines = g.type === 'MultiLineString' ? g.coordinates
      : (g.type === 'LineString' ? [g.coordinates] : []);
    var out = [];
    (Array.isArray(lines) ? lines : []).forEach(function (line) {
      if (!Array.isArray(line)) return;
      var pts = line.filter(function (p) {
        return Array.isArray(p) && isNum(p[0]) && isNum(p[1]) && Math.abs(p[0]) <= 180 &&
          Math.abs(p[1]) <= 90;
      });
      if (pts.length >= 2) out.push(pts);
    });
    return out;
  }

  function cableColor(feature) {
    var c = feature && feature.properties && feature.properties.color;
    return (typeof c === 'string' && /^#[0-9a-fA-F]{6}$/.test(c)) ? c : CABLE_COLOR;
  }

  // ---------------------------------------------------------------------
  // DOM helpers and style
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.infra-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 4px;',
    '  line-height: 1.4; }',
    '.infra-attr { font-size: 8px; color: var(--hud-dim, #7a5500); margin-bottom: 6px;',
    '  line-height: 1.3; opacity: 0.8; }',
    '.infra-kinds { display: grid; grid-template-columns: 1fr 1fr; gap: 3px; margin-bottom: 5px; }',
    '.infra-kind { display: flex; align-items: center; gap: 4px; background: rgba(0,0,0,0.4);',
    '  border: 1px solid rgba(255,176,0,0.25); color: var(--hud-dim, #7a5500); font: inherit;',
    '  font-size: 8px; padding: 3px 4px; cursor: pointer; text-align: left; }',
    '.infra-kind.on { color: var(--hud-color, #ffb000); border-color: rgba(255,176,0,0.6); }',
    '.infra-kind .infra-n { margin-left: auto; opacity: 0.8; }',
    '.infra-dot { width: 10px; height: 10px; flex: none; }',
    '.infra-line { width: 12px; height: 2px; flex: none; }',
    '.infra-search { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.6);',
    '  border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000); font: inherit;',
    '  font-size: 9px; padding: 3px 5px; margin-bottom: 5px; }',
    '.infra-view { font-size: 9px; color: var(--hud-dim, #7a5500); min-height: 11px; }',
    '.infra-status { font-size: 9px; color: var(--hud-text, #ffb000); min-height: 11px; }',
    '.infra-card { margin-top: 5px; border-top: 1px solid rgba(255,176,0,0.25); padding-top: 4px;',
    '  font-size: 9px; color: var(--hud-color, #ffb000); }',
    '.infra-card:empty { display: none; }',
    '.infra-card .infra-title { font-size: 10px; font-weight: bold; }',
    '.infra-card .infra-sub { color: var(--hud-dim, #7a5500); margin-bottom: 3px; }',
    '.infra-card .infra-row { display: flex; gap: 6px; line-height: 1.35; }',
    '.infra-card .infra-row span:first-child { color: var(--hud-dim, #7a5500); min-width: 76px; }',
    '.infra-card .infra-sole { color: #ff6b6b; }',
    '.infra-card a { color: var(--hud-color, #ffb000); display: block; margin-top: 2px; }',
    '.infra-card .infra-src { font-size: 8px; color: var(--hud-dim, #7a5500); margin-top: 3px; }',
    '.infra-list { max-height: 150px; overflow-y: auto; margin-top: 4px; }',
    '.infra-item { display: block; width: 100%; text-align: left; background: none; border: 0;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 3px 2px; cursor: pointer; overflow: hidden;',
    '  text-overflow: ellipsis; white-space: nowrap; }',
    '.infra-item:hover { background: rgba(255,176,0,0.08); }',
    '.infra-item .infra-meta { color: var(--hud-dim, #7a5500); margin-left: 4px; }'
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

  function link(label, url) {
    var a = el('a', null, label);
    a.setAttribute('href', url);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  // ---------------------------------------------------------------------
  // The module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var viewer = ctx.viewer;
    var api = ctx.api || '';
    var m = {
      ctx: ctx, enabled: false, summary: null, available: false, cablesOn: true,
      on: {}, seq: 0, timer: null, searchTimer: null, cableTimer: null, cableTries: 0,
      marks: null, labels: null, lines: null, items: [], box: null, lastView: null,
      cablesLoaded: false, cablesPending: false, cableLinesById: {}, selectedCable: null,
      counts: {}
    };
    KINDS.forEach(function (k) { m.on[k.key] = true; });

    function note(msg) { try { ctx.addIntelItem(msg); } catch (e) { /* keep going */ } }

    function getJson(path) {
      if (ctx.isStatic) return Promise.reject(new Error('static build'));
      return global.fetch(api + path).then(function (r) {
        if (!r.ok) { var err = new Error('HTTP ' + r.status); err.status = r.status; throw err; }
        return r.json();
      });
    }

    // -- panel ----------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.summaryEl = el('div', 'infra-summary', 'Turn on the Infrastructure layer to load it.');
    m.attrEl = el('div', 'infra-attr');
    var grid = el('div', 'infra-kinds');
    m.kindBtns = {};
    m.kindCounts = {};
    KINDS.forEach(function (k) {
      var b = button('infra-kind on', null, 'Show or hide ' + k.label.toLowerCase());
      var dot = el('img', 'infra-dot');
      dot.setAttribute('src', ICONS[k.key]);
      dot.setAttribute('alt', '');
      b.appendChild(dot);
      b.appendChild(el('span', null, k.label));
      var n = el('span', 'infra-n', '');
      b.appendChild(n);
      b.setAttribute('aria-pressed', 'true');
      b.addEventListener('click', function () { toggleKind(k.key); });
      grid.appendChild(b);
      m.kindBtns[k.key] = b;
      m.kindCounts[k.key] = n;
    });
    m.cableBtn = button('infra-kind on', null, 'Show or hide submarine cables');
    var swatch = el('span', 'infra-line');
    swatch.style.background = CABLE_COLOR;
    m.cableBtn.appendChild(swatch);
    m.cableBtn.appendChild(el('span', null, 'Submarine cables'));
    m.cableCount = el('span', 'infra-n', '');
    m.cableBtn.appendChild(m.cableCount);
    m.cableBtn.setAttribute('aria-pressed', 'true');
    m.cableBtn.addEventListener('click', function () { setCables(!m.cablesOn); });
    grid.appendChild(m.cableBtn);
    m.search = el('input', 'infra-search');
    m.search.setAttribute('type', 'search');
    m.search.setAttribute('placeholder', 'Search: name, ICAO, IATA, UN/LOCODE...');
    m.search.setAttribute('maxlength', '100');
    m.view = el('div', 'infra-view');
    m.view.setAttribute('aria-live', 'polite');
    m.status = el('div', 'infra-status');
    m.status.setAttribute('role', 'status');
    m.card = el('div', 'infra-card');
    m.list = el('div', 'infra-list');
    [m.summaryEl, m.attrEl, grid, m.search, m.view, m.status, m.card, m.list]
      .forEach(function (n) { body.appendChild(n); });
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(msg) { m.status.textContent = msg; }
    function setView(msg) { m.view.textContent = msg; }

    function setCount(n) {
      try {
        var c = global.document.getElementById('count-' + LAYER_KEY);
        if (c) c.textContent = formatCount(n);
      } catch (e) { /* keep going */ }
    }

    function showKindCounts(counts) {
      KINDS.forEach(function (k) {
        var n = counts && isNum(counts[k.key]) ? counts[k.key] : null;
        m.kindCounts[k.key].textContent = (m.on[k.key] && n !== null) ? formatCount(n) : '';
      });
    }

    // -- globe layers ---------------------------------------------------

    function ensureLayers() {
      if (m.marks) return;
      m.lines = viewer.scene.primitives.add(new C.PolylineCollection());
      m.marks = viewer.scene.primitives.add(new C.BillboardCollection());
      m.labels = viewer.scene.primitives.add(new C.LabelCollection());
    }

    function flyTo(lat, lon, height) {
      try {
        viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(lon, lat, height || 60000), duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function cameraHeight() {
      var carto = viewer.camera.positionCartographic;
      return carto && isNum(carto.height) ? carto.height : 2e7;
    }

    function drawItems() {
      if (!m.marks) return;
      m.marks.removeAll();
      m.labels.removeAll();
      var height = cameraHeight();
      var close = height <= CLOSE_ALT_M;
      var depth = close ? Number.POSITIVE_INFINITY : 0;
      var shown = m.items.filter(function (it) { return m.on[it.kind]; });
      var groups = cluster(shown, m.box, CLUSTER_ACROSS);
      groups.clusters.forEach(function (c) {
        var pos = C.Cartesian3.fromDegrees(c.lon, c.lat, 50);
        var size = clusterSize(c.count);
        var pick = {
          type: 'infra-cluster', kind: c.kind, count: c.count,
          sdaPick: function () { flyTo(c.lat, c.lon, Math.max(20000, cameraHeight() / 3)); }
        };
        m.marks.add({
          position: pos, image: CLUSTER_ICONS[c.kind], width: size, height: size,
          verticalOrigin: C.VerticalOrigin.CENTER, disableDepthTestDistance: depth, id: pick
        });
        m.labels.add({
          position: pos, text: String(c.count), font: 'bold 10px monospace',
          fillColor: C.Color.WHITE, outlineColor: C.Color.BLACK, outlineWidth: 3,
          style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.CENTER,
          verticalOrigin: C.VerticalOrigin.CENTER, disableDepthTestDistance: depth, id: pick
        });
      });
      var named = height <= LABEL_ALT_M ? groups.singles.slice().sort(function (a, b) {
        return (b.score || 0) - (a.score || 0);
      }).slice(0, LABEL_MAX) : [];
      var withLabel = {};
      named.forEach(function (it) { withLabel[it.id] = true; });
      groups.singles.forEach(function (it) {
        var pos = C.Cartesian3.fromDegrees(it.lon, it.lat, 50);
        var pick = { type: 'infra', id: it.id, kind: it.kind,
          sdaPick: function () { openItem(it); } };
        m.marks.add({
          position: pos, image: ICONS[it.kind], width: 14, height: 14,
          verticalOrigin: C.VerticalOrigin.CENTER,
          scaleByDistance: new C.NearFarScalar(5000, 1.3, 8000000, 0.7),
          disableDepthTestDistance: depth, id: pick
        });
        if (!withLabel[it.id]) return;
        m.labels.add({
          position: pos, text: it.name, font: '10px monospace',
          fillColor: C.Color.WHITE, outlineColor: C.Color.BLACK, outlineWidth: 2,
          style: C.LabelStyle.FILL_AND_OUTLINE, horizontalOrigin: C.HorizontalOrigin.LEFT,
          verticalOrigin: C.VerticalOrigin.CENTER, pixelOffset: new C.Cartesian2(10, 0),
          disableDepthTestDistance: depth, id: pick
        });
      });
      return groups;
    }

    function viewState() {
      var cam = viewer.camera;
      var carto = cam.positionCartographic;
      var height = carto ? carto.height : 2e7;
      var rect = null;
      try { rect = cam.computeViewRectangle(viewer.scene.globe.ellipsoid); } catch (e) { rect = null; }
      var box = boxFromRect(rect, 0.1);
      if (!box && carto) {
        box = boxAround(carto.latitude * 180 / Math.PI, carto.longitude * 180 / Math.PI,
          Math.min(3000, Math.max(50, height / 1000)));
      }
      return { height: height, box: box || { west: -180, south: -90, east: 180, north: 90 } };
    }

    function refresh(force) {
      if (!m.enabled || ctx.isStatic || !m.available) return null;
      var kinds = kindsParam(m.on);
      if (!kinds) {
        m.seq++;  // drop answers still in flight for the kinds just switched off
        m.lastView = null;
        m.items = [];
        drawItems();
        setCount(0);
        showKindCounts({});
        setView('Every kind is switched off.');
        return null;
      }
      var v = viewState();
      var key = bboxParam(v.box) + '|' + kinds;
      if (!force && key === m.lastView) return null;
      m.lastView = key;
      var seq = ++m.seq;
      return getJson('/api/infra/items' + query([
        ['bbox', bboxParam(v.box)], ['kinds', kinds], ['limit', VIEW_LIMIT]
      ])).then(function (data) {
        if (seq !== m.seq || !m.enabled) return;
        m.items = Array.isArray(data.items) ? data.items : [];
        m.box = v.box;
        m.counts = data.counts || {};
        var groups = drawItems();
        setCount(data.total || 0);
        showKindCounts(m.counts);
        var n = groups ? groups.singles.length + groups.clusters.length : 0;
        setView(data.capped
          ? 'Showing the ' + withCommas(m.items.length) + ' largest of ' +
            withCommas(data.total) + ' sites in view (' + n + ' markers). Zoom in for more.'
          : withCommas(m.items.length) + ' sites in view. Click a marker for its card.');
      }).catch(function () { if (seq === m.seq) setView('Infrastructure did not load.'); });
    }

    function scheduleRefresh() {
      if (!m.enabled) return;
      if (m.timer) global.clearTimeout(m.timer);
      m.timer = global.setTimeout(function () { m.timer = null; refresh(false); },
        MOVE_DEBOUNCE_MS);
    }

    // -- cables -------------------------------------------------------------

    function clearCables() {
      if (m.lines) m.lines.removeAll();
      m.cableLinesById = {};
      m.cablesLoaded = false;
    }

    function drawCables(geo) {
      clearCables();
      var feats = geo && Array.isArray(geo.features) ? geo.features : [];
      feats.forEach(function (f) {
        var id = f && (f.id || (f.properties && f.properties.id));
        if (typeof id !== 'string') return;
        var name = f.properties && typeof f.properties.name === 'string' ? f.properties.name : id;
        var color = C.Color.fromCssColorString(cableColor(f)).withAlpha(0.85);
        var pick = { type: 'infra-cable', id: id, name: name,
          sdaPick: function () { openCable(id); } };
        m.cableLinesById[id] = [];
        cableLines(f).forEach(function (pts) {
          var flat = [];
          pts.forEach(function (p) { flat.push(p[0], p[1]); });
          var line = m.lines.add({
            positions: C.Cartesian3.fromDegreesArray(flat), width: 1.5,
            material: C.Material.fromType('Color', { color: color }), id: pick
          });
          m.cableLinesById[id].push(line);
        });
      });
      m.cablesLoaded = true;
      m.cableCount.textContent = feats.length ? formatCount(feats.length) : '';
      return feats.length;
    }

    function loadCables() {
      if (!m.enabled || !m.cablesOn || ctx.isStatic || m.cablesLoaded || m.cablesPending) {
        return null;
      }
      if (m.cableTimer) { global.clearTimeout(m.cableTimer); m.cableTimer = null; }
      m.cablesPending = true;
      return getJson('/api/infra/cables').then(function (geo) {
        m.cablesPending = false;
        if (!m.enabled || !m.cablesOn || m.cablesLoaded) return;
        if (geo && geo.available && Array.isArray(geo.features) && geo.features.length) {
          var n = drawCables(geo);
          m.cableTries = 0;
          note('Infrastructure: ' + n + ' submarine cables');
          return;
        }
        if (geo && geo.loading && m.cableTries < CABLE_RETRY_MAX) {
          m.cableTries++;
          setStatus('The server is fetching the submarine cable map; it appears in a few ' +
            'minutes.');
          m.cableTimer = global.setTimeout(function () { m.cableTimer = null; loadCables(); },
            CABLE_RETRY_MS);
          return;
        }
        setStatus('No submarine cable map on the server yet' +
          (geo && geo.error ? ' (' + geo.error + ').' : '.'));
      }).catch(function () {
        m.cablesPending = false;
        setStatus('Submarine cables did not load.');
      });
    }

    function setCables(on) {
      m.cablesOn = !!on;
      m.cableBtn.className = 'infra-kind' + (m.cablesOn ? ' on' : '');
      m.cableBtn.setAttribute('aria-pressed', m.cablesOn ? 'true' : 'false');
      if (m.lines) m.lines.show = m.enabled && m.cablesOn;
      if (m.cablesOn) loadCables();
    }

    function highlightCable(id) {
      if (m.selectedCable && m.cableLinesById[m.selectedCable]) {
        m.cableLinesById[m.selectedCable].forEach(function (l) { l.width = 1.5; });
      }
      m.selectedCable = id;
      if (id && m.cableLinesById[id]) {
        m.cableLinesById[id].forEach(function (l) { l.width = 4; });
      }
    }

    // -- cards ----------------------------------------------------------------

    function renderCard(model, extra) {
      m.card.replaceChildren();
      if (!model) return;
      m.card.appendChild(el('div', 'infra-title', model.title));
      if (model.subtitle) m.card.appendChild(el('div', 'infra-sub', model.subtitle));
      model.rows.forEach(function (r) {
        var row = el('div', 'infra-row');
        row.appendChild(el('span', null, r[0]));
        row.appendChild(el('span', null, r[1]));
        m.card.appendChild(row);
      });
      if (extra) extra(m.card);
      model.links.forEach(function (l) { m.card.appendChild(link(l.label, l.url)); });
      if (model.source) m.card.appendChild(el('div', 'infra-src', 'Source: ' + model.source));
    }

    function openItem(it) {
      if (!it || typeof it.id !== 'string') return null;
      setStatus('Loading ' + (it.name || it.id) + '...');
      return getJson('/api/infra/item/' + encodeURIComponent(it.id)).then(function (d) {
        var model = cardModel(d);
        renderCard(model, model.cables.length ? function (card) {
          card.appendChild(el('div', 'infra-sub', 'Cables landing here:'));
          model.cables.forEach(function (c) {
            var b = button('infra-item', c.name, 'Open the card for ' + c.name);
            b.addEventListener('click', function () { openCable(c.id); });
            card.appendChild(b);
          });
        } : null);
        setStatus('');
        note('Infrastructure: ' + model.title + (model.subtitle ? ', ' + model.subtitle : ''));
      }).catch(function () { setStatus('The card did not load.'); });
    }

    function openCable(id) {
      if (typeof id !== 'string' || !id) return null;
      highlightCable(id);
      setStatus('Loading cable...');
      return getJson('/api/infra/cables/' + encodeURIComponent(id)).then(function (c) {
        var model = cableCardModel(c);
        renderCard(model, function (card) {
          if (!model.countries.length) return;
          card.appendChild(el('div', 'infra-sub', 'Lands in (other cables in that country):'));
          model.countries.forEach(function (r) {
            var row = el('div', 'infra-row' + (r.sole ? ' infra-sole' : ''));
            row.appendChild(el('span', null, r.country));
            row.appendChild(el('span', null, r.text));
            card.appendChild(row);
          });
        });
        setStatus('');
        var sole = model.countries.filter(function (r) { return r.sole; })
          .map(function (r) { return r.country; });
        note('Cable: ' + model.title + (sole.length ? ', only cable for ' + sole.join(', ') : ''));
      }).catch(function () { setStatus('The cable card did not load.'); });
    }

    // -- actions --------------------------------------------------------------

    function toggleKind(key) {
      if (!KIND_BY_KEY[key]) return;
      m.on[key] = !m.on[key];
      var b = m.kindBtns[key];
      b.className = 'infra-kind' + (m.on[key] ? ' on' : '');
      b.setAttribute('aria-pressed', m.on[key] ? 'true' : 'false');
      if (!m.enabled) { setEnabled(true, true); return; }
      refresh(true);
    }

    function showList(items) {
      m.list.replaceChildren();
      items.forEach(function (it) {
        var b = button('infra-item');
        var dot = el('img', 'infra-dot');
        dot.setAttribute('src', ICONS[it.kind] || ICONS.power);
        dot.setAttribute('alt', '');
        b.appendChild(dot);
        b.appendChild(el('span', null, ' ' + (it.name || it.id)));
        b.appendChild(el('span', 'infra-meta', [it.brief, it.country].filter(isStr).join(' | ')));
        b.addEventListener('click', function () {
          if (isNum(it.lat) && isNum(it.lon)) flyTo(it.lat, it.lon, 60000);
          openItem(it);
        });
        m.list.appendChild(b);
      });
    }

    m.search.addEventListener('input', function () {
      if (m.searchTimer) global.clearTimeout(m.searchTimer);
      var q = String(m.search.value || '').trim();
      if (q.length < 2) { m.list.replaceChildren(); return; }
      if (ctx.isStatic) { setStatus('The infrastructure catalog needs the live server.'); return; }
      m.searchTimer = global.setTimeout(function () {
        m.searchTimer = null;
        getJson('/api/infra/search' + query([['q', q], ['limit', 20]])).then(function (data) {
          var list = Array.isArray(data.items) ? data.items : [];
          showList(list);
          setStatus(list.length ? list.length + ' matches for "' + q + '".'
            : 'Nothing matches "' + q + '".');
        }).catch(function () { setStatus('Search did not run.'); });
      }, SEARCH_DEBOUNCE_MS);
    });

    // -- enable / disable -------------------------------------------------------

    function loadSummary() {
      return getJson('/api/infra/summary').then(function (s) {
        m.summary = s;
        var cables = s && s.cables && s.cables.available;
        m.available = !!(s && (s.available || cables));
        if (!s || !s.available) {
          m.summaryEl.textContent = 'No infrastructure catalog yet. On the server run: ' +
            'python -m sda.infra import';
        } else {
          var kinds = s.kinds || {};
          var parts = KINDS.map(function (k) {
            var n = kinds[k.key] && isNum(kinds[k.key].count) ? kinds[k.key].count : 0;
            return n ? withCommas(n) + ' ' + k.label.toLowerCase() : '';
          }).filter(Boolean);
          if (s.cables && s.cables.count) parts.push(withCommas(s.cables.count) + ' cables');
          m.summaryEl.textContent = parts.join(', ') + '.';
        }
        m.attrEl.textContent = s && typeof s.attribution === 'string' ? s.attribution : '';
        return s;
      });
    }

    function setEnabled(on, fromPanel) {
      on = !!on;
      if (ctx.isStatic) {
        m.summaryEl.textContent = 'The infrastructure catalog needs the live server.';
        return;
      }
      if (on === m.enabled) return;
      m.enabled = on;
      if (fromPanel && on) {
        try {
          var box = global.document.getElementById('layer-' + LAYER_KEY);
          if (box && !box.checked) {
            box.checked = true;
            if (typeof global.toggleLayer === 'function') {
              m.enabled = false;
              global.toggleLayer(LAYER_KEY, true);
              return;
            }
          }
        } catch (e) { /* keep going */ }
      }
      if (on) {
        ensureLayers();
        m.marks.show = m.labels.show = true;
        m.lines.show = m.cablesOn;
        m.lastView = null;
        (m.summary ? Promise.resolve(m.summary) : loadSummary()).then(function (s) {
          if (!m.enabled) return;
          if (m.available) {
            if (s && s.available) note('Infrastructure: ' + withCommas(s.count) + ' sites');
            refresh(true);
            loadCables();
          }
        }).catch(function (err) {
          m.summaryEl.textContent = err && err.status === 404
            ? 'This server has no infrastructure endpoints.'
            : 'The infrastructure catalog did not load.';
        });
      } else {
        if (m.timer) { global.clearTimeout(m.timer); m.timer = null; }
        if (m.searchTimer) { global.clearTimeout(m.searchTimer); m.searchTimer = null; }
        if (m.cableTimer) { global.clearTimeout(m.cableTimer); m.cableTimer = null; }
        m.seq++;
        if (m.marks) {
          m.marks.show = m.labels.show = m.lines.show = false;
          m.marks.removeAll();
          m.labels.removeAll();
        }
        clearCables();
        m.items = [];
        setCount(0);
        showKindCounts({});
        setView('');
        setStatus('');
      }
    }

    m.setEnabled = setEnabled;
    m.refresh = refresh;
    m.openItem = openItem;
    m.openCable = openCable;
    m.toggleKind = toggleKind;
    m.setCables = setCables;
    m.loadCables = loadCables;

    try {
      viewer.camera.moveEnd.addEventListener(scheduleRefresh);
    } catch (e) { /* keep going */ }

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
      if (ctx.isStatic) {
        current.summaryEl.textContent = 'The infrastructure catalog needs the live server.';
      }
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAInfra = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    refresh: function () { return current ? current.refresh(true) : null; },
    open: function (item) { return current ? current.openItem(item) : null; },
    openCable: function (id) { return current ? current.openCable(id) : null; },
    toggleKind: function (key) { if (current) current.toggleKind(key); },
    setCables: function (on) { if (current) current.setCables(on); },
    KINDS: KINDS,
    ICONS: ICONS,
    CLUSTER_ICONS: CLUSTER_ICONS,
    VIEW_LIMIT: VIEW_LIMIT,
    query: query,
    boxFromRect: boxFromRect,
    boxAround: boxAround,
    kindsParam: kindsParam,
    cluster: cluster,
    clusterSize: clusterSize,
    cardModel: cardModel,
    cableCardModel: cableCardModel,
    cableLines: cableLines,
    cableColor: cableColor,
    liveAtcUrl: liveAtcUrl,
    safeUrl: safeUrl,
    formatCount: formatCount
  };
})(typeof window !== 'undefined' ? window : this);
