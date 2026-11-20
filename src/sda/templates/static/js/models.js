// 3D models on the Vantage globe, and the ride-along controls for the camera.
//
// Models: once the camera is close, the nearest aircraft and ships the page
// already draws as points get a glTF model of their class, turned to their
// reported track, and the point is hidden; zoom back out and the points come
// back. The ISS and Hubble get their own models when the camera is within a
// few thousand kilometres, and so does any other satellite while it is the
// selected one. Nothing here fetches data: the module reads the Cesium
// entities other layers already drew (viewer.entities and every data source)
// and the satellite engine's point positions.
//
// What an entity needs to carry (Cesium entity properties):
//   type 'aircraft' | 'military' (ids 'flight-*' and 'mil-*' also match),
//     aircraft_type (ICAO designator, e.g. B738), category (ADS-B emitter
//     category 'A1'..'B7' or the OpenSky extended integer), heading (deg true),
//     velocity (m/s)
//   type 'vessel' | 'ship' or feed 'ais', ship_type (AIS type code 0..99),
//     heading / cog_deg (deg true), sog_kn, mmsi; layers.js detail_rows
//     (MMSI, SPEED, COURSE, HEADING) are read when the fields are missing
//   model_class: forces one of AIRCRAFT_MODELS / SHIP_MODELS
//   state 'estimated' or estimated true: never modelled, so a model never
//     presents an estimate as a report
// Modules that draw primitives rather than entities (aviation.js, maritime.js)
// hand plain rows to SDAModels.addSource; a chosen row gets an entity here in
// the 'sdamodels-feeds' data source, and the source is told which keys are
// modelled so it can hide those points.
//
// Smooth motion: between two reports a modelled aircraft or ship is carried
// forward along its reported track at its reported speed, for at most 45 s
// (aircraft) or 10 min (ships), then held; the next report blends in over 2 s.
// The point layers themselves are left as reported.
//
// Ride along: the page already owns the camera modes (setCameraMode 'chase',
// 'cockpit', 'orbit', Escape to leave). This module adds the on-screen bar
// for them (target readout, mode buttons, EXIT), hides the target's own model
// in the first-person mode, and re-acquires a ship when layers.js redraws its
// feed with new entities.
//
// Sources: the ICAO type-designator sets and the emitter-category table are
// adapted from gev-worldcams src/data/aircraftClass.js (MIT, Copyright (c) 2026
// Bilawal Sidhu), which adapts skylight web/src/display/aircraftGlyph.ts (MIT,
// cpaczek/skylight). The model-swap radii, the hysteresis and the nose -X
// heading offset follow gev-worldcams src/data/flights.js (MIT). Aircraft GLBs
// are CC BY 4.0 (static/models/NOTICE.txt); the space and ship GLBs are built
// by scripts/build_globe_models.py.
//
// Frames: Cesium fixed-frame Cartesians from entity positions only; no
// propagation of our own unless the page opts in (useSmoothSatellites).
// Text is only ever set with textContent.

(function (global) {
  'use strict';

  var PANEL_TITLE = '3D models';
  var STYLE_ID = 'sda-models-style';
  var HUD_ID = 'sda-ride';
  var MODEL_BASE = 'static/models/';
  var SCAN_MS = 500;
  var HUD_MS = 250;
  var ISS_ID = 25544;
  var HUBBLE_ID = 20580;
  var SPACE_RANGE_M = 2500000;
  var KEEP_FACTOR = 1.25;
  var MIN_PX = 26;
  var SPACE_MIN_PX = 64;
  var MAX_SCALE = 600;
  var DR_MAX_S = { aircraft: 45, vessel: 600 };
  var BLEND_S = 2.0;
  var BLEND_MAX_M = 5000;
  var KNOT_MS = 0.514444;
  var FT_PER_M = 1 / 0.3048;
  var MS_TO_KT = 1 / KNOT_MS;
  var ATTACHED = { chase: true, cockpit: true, orbit: true };

  var BUDGETS = {
    low: { max: 16, addM: 90000 },
    medium: { max: 40, addM: 150000 },
    high: { max: 80, addM: 220000 }
  };

  function toSet(list) {
    var o = {};
    for (var i = 0; i < list.length; i++) o[list[i]] = true;
    return o;
  }

  // ICAO type designators by class (gev-worldcams / skylight, MIT; see header).
  var HELI = toSet(['EC20', 'EC25', 'EC30', 'EC35', 'EC45', 'EC55', 'AS50', 'AS55', 'AS65',
    'AS32', 'A109', 'A119', 'A139', 'A169', 'A189', 'B06', 'B06T', 'B407', 'B412', 'B427',
    'B429', 'B430', 'B505', 'S76', 'S92', 'S61', 'S64', 'H60', 'H500', 'MD52', 'MD60', 'R22',
    'R44', 'R66', 'EXEC', 'EXPL', 'GAZL', 'LYNX', 'NH90', 'PUMA', 'SCAV', 'UH1', 'B105',
    'B212', 'B214', 'B222', 'H47', 'H64', 'V22']);
  var QUAD = toSet(['B741', 'B742', 'B743', 'B744', 'B748', 'B74S', 'B74R', 'B74D', 'A388',
    'A342', 'A343', 'A345', 'A346', 'A124', 'C5M', 'A225', 'IL96', 'B52', 'E3TF', 'E3CF',
    'E6', 'K35E']);
  var WIDE = toSet(['A306', 'A30B', 'A310', 'A332', 'A333', 'A338', 'A339', 'A359', 'A35K',
    'B762', 'B763', 'B764', 'B772', 'B77L', 'B773', 'B77W', 'B778', 'B779', 'B788', 'B789',
    'B78X', 'MD11', 'IL86', 'DC10', 'L101', 'A337', 'C17', 'K35R', 'KC10', 'KC46', 'A3ST',
    'IL76', 'P8', 'E767']);
  var TPROP = toSet(['DH8A', 'DH8B', 'DH8C', 'DH8D', 'AT43', 'AT44', 'AT45', 'AT46', 'AT72',
    'AT73', 'AT75', 'AT76', 'SF34', 'SB20', 'SW3', 'SW4', 'E110', 'E120', 'C208', 'C212',
    'C408', 'PC12', 'B190', 'BE20', 'B350', 'B300', 'JS31', 'JS32', 'JS41', 'D228', 'D328',
    'F50', 'F27', 'ATP', 'TBM7', 'TBM8', 'TBM9', 'TBM0', 'PC6', 'C441', 'C425', 'DHC6',
    'DHC7', 'C130', 'C30J', 'AN12', 'AN26', 'AN32', 'SH36', 'CVLT', 'SAAB', 'A400', 'P3',
    'E2', 'C27J', 'CN35', 'C295']);
  var GLIDER = toSet(['DISC', 'DUOD', 'VENT', 'NIMB', 'NIM3', 'NIM4', 'JANS', 'ARCE', 'DG40',
    'DG80', 'DG1T', 'DG30', 'DG50', 'LS3', 'LS4', 'LS6', 'LS7', 'LS8', 'STD3', 'G103', 'G102',
    'G104', 'PW5', 'PW6', 'L13', 'L23', 'L33', 'PIK', 'PEGA', 'KEST', 'TWIN', 'AS33', 'ASW',
    'ASG', 'ASK', 'VENS', 'GLID', 'MOSQ', 'DIMO']);
  var LIGHT = toSet(['C150', 'C152', 'C162', 'C172', 'C72R', 'C175', 'C177', 'C180', 'C182',
    'C185', 'C188', 'C206', 'C207', 'C210', 'C310', 'C337', 'SR20', 'SR22', 'S22T', 'PA18',
    'PA24', 'PA28', 'P28A', 'P28B', 'P28R', 'PA32', 'P32R', 'PA34', 'PA38', 'PA44', 'PA46',
    'DA20', 'DA40', 'DA42', 'DA62', 'BE33', 'BE35', 'BE36', 'BE58', 'BE76', 'BE19', 'BE23',
    'BE24', 'M20P', 'M20T', 'AA1', 'AA5', 'GLAS', 'COL4', 'RV4', 'RV6', 'RV7', 'RV8', 'RV9',
    'RV10', 'RV14', 'GA8', 'G115', 'BL8', 'CH7', 'T6', 'TEX2', 'PC21', 'PC9', 'PC7']);
  var BIZJET = toSet(['C500', 'C501', 'C510', 'C525', 'C25A', 'C25B', 'C25C', 'C25M', 'C550',
    'C551', 'C560', 'C56X', 'C650', 'C680', 'C68A', 'C700', 'C750', 'CL30', 'CL35', 'CL60',
    'GLF2', 'GLF3', 'GLF4', 'GLF5', 'GLF6', 'GA5C', 'GA6C', 'G150', 'G280', 'GL5T', 'GL7T',
    'GLEX', 'LJ23', 'LJ24', 'LJ25', 'LJ31', 'LJ35', 'LJ40', 'LJ45', 'LJ55', 'LJ60', 'LJ70',
    'LJ75', 'FA10', 'FA20', 'FA50', 'FA7X', 'FA8X', 'F900', 'F2TH', 'H25A', 'H25B', 'H25C',
    'HDJT', 'E50P', 'E55P', 'E545', 'E550', 'PC24', 'PRM1', 'BE40', 'ASTR', 'WW24', 'SF50',
    'C21', 'C37A', 'C37B']);
  var UAV = toSet(['Q1', 'Q4', 'Q9', 'MQ1', 'MQ4', 'MQ9', 'RQ4', 'TB2', 'SHDW', 'HERN',
    'MQ1C', 'RQ7']);
  var FASTJET = toSet(['F16', 'F15', 'F18', 'FA18', 'F18S', 'F14', 'F22', 'F35', 'F4', 'F5',
    'A10', 'AV8B', 'TYPH', 'EUFI', 'RFAL', 'RAFL', 'GRIP', 'JAS39', 'TOR', 'MIR2', 'M2000',
    'SU27', 'SU30', 'SU33', 'SU34', 'SU35', 'SU57', 'MG29', 'MIG29', 'MG31', 'J20', 'T38',
    'HAWK', 'L39', 'M346', 'T7A', 'B1', 'B2', 'EA18']);
  var BIG_UAV = toSet(['Q4', 'RQ4', 'MQ4']);

  // OpenSky extended-states integer category and ADS-B emitter categories.
  var OPENSKY_CATEGORY = { 2: 'light', 3: 'airliner', 4: 'airliner', 5: 'airliner',
    6: 'widebody', 7: 'fastjet', 8: 'helicopter', 9: 'glider', 14: 'drone' };
  var EMITTER_CATEGORY = { A1: 'light', A2: 'bizjet', A3: 'airliner', A4: 'airliner',
    A5: 'widebody', A6: 'fastjet', A7: 'helicopter', B1: 'glider', B6: 'drone' };

  // offset: degrees added to the compass track for Cesium's heading. The
  // gev-worldcams aircraft point their nose down local -X in glTF (Cesium -Y
  // after the axis correction), so +180; the generated models point +X, so -90.
  var AIRCRAFT_MODELS = {
    airliner: { file: 'airplane.glb', scale: 0.74, offset: 180, label: 'airliner' },
    widebody: { file: 'airplane.glb', scale: 1.25, offset: 180, label: 'wide-body airliner' },
    quadjet: { file: 'airplane.glb', scale: 1.4, offset: 180, label: 'four-engine heavy' },
    bizjet: { file: 'jet.glb', scale: 0.5, offset: 180, label: 'business jet' },
    fastjet: { file: 'jet.glb', scale: 0.42, offset: 180, label: 'fast jet' },
    turboprop: { file: 'atr72.glb', scale: 1.0, offset: 180, label: 'turboprop' },
    light: { file: 'c172.glb', scale: 1.0, offset: 180, label: 'light aircraft' },
    glider: { file: 'c172.glb', scale: 1.1, offset: 180, label: 'glider' },
    helicopter: { file: 'bell206.glb', scale: 1.0, offset: 180, label: 'helicopter' },
    drone: { file: 'mq9.glb', scale: 1.0, offset: 180, label: 'drone' }
  };
  var SHIP_MODELS = {
    cargo: { file: 'ship_cargo.glb', length: 190, offset: -90, label: 'cargo ship' },
    container: { file: 'ship_container.glb', length: 300, offset: -90, label: 'container ship' },
    tanker: { file: 'ship_tanker.glb', length: 250, offset: -90, label: 'tanker' },
    passenger: { file: 'ship_passenger.glb', length: 290, offset: -90, label: 'passenger ship' },
    fishing: { file: 'ship_fishing.glb', length: 34, offset: -90, label: 'fishing vessel' },
    tug: { file: 'ship_tug.glb', length: 32.8, offset: -90, label: 'tug or service vessel' },
    military: { file: 'ship_military.glb', length: 150, offset: -90, label: 'naval vessel' },
    sailing: { file: 'ship_sailing.glb', length: 14, offset: -90, label: 'sailing vessel' },
    pleasure: { file: 'ship_pleasure.glb', length: 22, offset: -90, label: 'pleasure craft' },
    highspeed: { file: 'ship_pleasure.glb', length: 22, scale: 1.8, offset: -90,
      label: 'high-speed craft' }
  };
  var SPACE_MODELS = {
    iss: { file: 'iss.glb', length: 109, label: 'ISS' },
    hubble: { file: 'hubble.glb', length: 13.2, label: 'Hubble' },
    satellite: { file: 'satellite.glb', length: 14, label: 'satellite' }
  };
  var CONTAINER_LINES = /\b(MAERSK|MSC|CMA CGM|COSCO|EVER|EVERGREEN|ONE|HAPAG|HAPAG-LLOYD|YM|YANG MING|ZIM|HMM|OOCL|APL|PIL|WAN HAI|SEASPAN|NYK|MOL|KOTA|SITC|TS LINES|X-PRESS|CSCL)\b/;
  var TANKER_NAMES = /\b(TANKER|LNG|LPG|GAS|CRUDE|PETRO|OIL)\b/;

  var clockFn = function () { return Date.now(); };

  function finite(v) { return typeof v === 'number' && isFinite(v); }

  function num(v) {
    if (finite(v)) return v;
    if (typeof v === 'string' && v.trim() !== '') {
      var n = parseFloat(v);
      return isFinite(n) ? n : null;
    }
    return null;
  }

  // ---------------------------------------------------------------------
  // Classification (pure)
  // ---------------------------------------------------------------------

  function classifyAircraft(info) {
    info = info || {};
    var forced = String(info.modelClass || '').toLowerCase();
    if (AIRCRAFT_MODELS[forced]) return { cls: forced, basis: 'forced' };
    var code = String(info.typeCode || '').trim().toUpperCase();
    if (code === 'MIL' || code === 'UNKNOWN') code = '';
    if (code) {
      if (FASTJET[code]) return { cls: 'fastjet', basis: 'type' };
      if (UAV[code]) return { cls: 'drone', basis: 'type' };
      if (HELI[code]) return { cls: 'helicopter', basis: 'type' };
      if (QUAD[code]) return { cls: 'quadjet', basis: 'type' };
      if (WIDE[code]) return { cls: 'widebody', basis: 'type' };
      if (TPROP[code]) return { cls: 'turboprop', basis: 'type' };
      if (GLIDER[code]) return { cls: 'glider', basis: 'type' };
      if (BIZJET[code]) return { cls: 'bizjet', basis: 'type' };
      if (LIGHT[code]) return { cls: 'light', basis: 'type' };
      return { cls: 'airliner', basis: 'type' };
    }
    var cat = info.category;
    if (finite(cat) && OPENSKY_CATEGORY[cat]) return { cls: OPENSKY_CATEGORY[cat], basis: 'category' };
    var catText = String(cat == null ? '' : cat).trim().toUpperCase();
    if (EMITTER_CATEGORY[catText]) return { cls: EMITTER_CATEGORY[catText], basis: 'category' };
    // No type and no category: a guess from speed and height, said as such.
    var v = num(info.velocity);
    var alt = num(info.altM);
    if (v !== null && alt !== null && v > 0) {
      if (v < 50 && alt < 1500) return { cls: 'light', basis: 'speed' };
      if (v < 120 && alt < 7000) return { cls: 'turboprop', basis: 'speed' };
    }
    return { cls: 'airliner', basis: 'default' };
  }

  function classifyShip(info) {
    info = info || {};
    var forced = String(info.modelClass || '').toLowerCase();
    if (SHIP_MODELS[forced]) return { cls: forced, basis: 'forced' };
    var t = num(info.shipType);
    var name = String(info.name || '').toUpperCase();
    if (t !== null) {
      t = Math.round(t);
      if (t === 30) return { cls: 'fishing', basis: 'type' };
      if (t === 35 || t === 55) return { cls: 'military', basis: 'type' };
      if (t === 36) return { cls: 'sailing', basis: 'type' };
      if (t === 37) return { cls: 'pleasure', basis: 'type' };
      if ((t >= 31 && t <= 34) || (t >= 50 && t <= 59)) return { cls: 'tug', basis: 'type' };
      if ((t >= 40 && t <= 49) || (t >= 20 && t <= 29)) return { cls: 'highspeed', basis: 'type' };
      if (t >= 60 && t <= 69) return { cls: 'passenger', basis: 'type' };
      if (t >= 80 && t <= 89) return { cls: 'tanker', basis: 'type' };
      if (t >= 70 && t <= 79) {
        return CONTAINER_LINES.test(name) ? { cls: 'container', basis: 'type and name' }
          : { cls: 'cargo', basis: 'type' };
      }
    }
    if (CONTAINER_LINES.test(name)) return { cls: 'container', basis: 'name' };
    if (TANKER_NAMES.test(name)) return { cls: 'tanker', basis: 'name' };
    return { cls: 'cargo', basis: 'default' };
  }

  function modelSpec(kind, cls) {
    if (kind === 'aircraft') return AIRCRAFT_MODELS[cls] || AIRCRAFT_MODELS.airliner;
    if (kind === 'vessel') return SHIP_MODELS[cls] || SHIP_MODELS.cargo;
    return SPACE_MODELS[cls] || SPACE_MODELS.satellite;
  }

  // Scale for a ship: its reported length when there is one, else its class.
  function shipScale(spec, lengthM) {
    var base = spec.scale || 1;
    var L = num(lengthM);
    if (L === null || L <= 2) return base;
    return Math.max(0.25, Math.min(2.2, L / spec.length));
  }

  function aircraftScale(spec, typeCode) {
    var code = String(typeCode || '').trim().toUpperCase();
    return BIG_UAV[code] ? 1.6 : spec.scale;
  }

  // Cesium heading for a compass track: model +X bearing is 90 deg + heading.
  function headingFor(trackDeg, offsetDeg) {
    var h = (trackDeg + offsetDeg) % 360;
    return h < 0 ? h + 360 : h;
  }

  // ENU displacement of a dead-reckoned track, metres.
  function drOffset(speedMs, trackDeg, seconds, maxS) {
    var t = Math.max(0, Math.min(maxS, seconds));
    var a = trackDeg * Math.PI / 180;
    return { east: speedMs * Math.sin(a) * t, north: speedMs * Math.cos(a) * t, t: t };
  }

  function budgetFor(name) { return BUDGETS[name] || BUDGETS.medium; }

  // Which candidates get a model: the target first, then nearest, capped. A
  // candidate already modelled keeps its model out to the wider keep radius.
  function pickModels(cands, budget) {
    var add = budget.addM;
    var keep = add * KEEP_FACTOR;
    var ok = cands.filter(function (c) {
      if (c.target) return true;
      return c.d <= (c.has ? keep : add);
    });
    ok.sort(function (a, b) {
      if (a.target !== b.target) return a.target ? -1 : 1;
      return a.d - b.d;
    });
    return ok.slice(0, budget.max);
  }

  // The camera range the chase mode should hold for a target of this size.
  function rideRangeFor(kind, lengthM, altM) {
    var L = num(lengthM) || 30;
    if (kind === 'satellite') return Math.max(60, L * 3.2);
    if (kind === 'vessel') return Math.max(80, L * 2.4);
    return Math.max(90, L * 3.6) + Math.min(400, Math.max(0, num(altM) || 0) * 0.01);
  }

  function formatRide(r) {
    var parts = [r.mode.toUpperCase()];
    if (r.name) parts.push(r.name);
    if (r.label) parts.push(r.label);
    if (finite(r.altM)) {
      if (r.kind === 'satellite') parts.push(Math.round(r.altM / 1000) + ' km');
      else if (r.kind === 'aircraft') {
        var ft = r.altM * FT_PER_M;
        parts.push(ft >= 18000 ? 'FL' + Math.round(ft / 100) : Math.round(ft).toLocaleString('en-US') + ' ft');
      }
    }
    if (finite(r.speedMs)) {
      if (r.kind === 'satellite') parts.push((r.speedMs / 1000).toFixed(2) + ' km/s');
      else parts.push(Math.round(r.speedMs * MS_TO_KT) + ' kt');
    }
    if (finite(r.track)) parts.push('HDG ' + ('00' + Math.round(r.track) % 360).slice(-3));
    return parts.join(' | ');
  }

  // ---------------------------------------------------------------------
  // Entity reading
  // ---------------------------------------------------------------------

  function prop(entity, name, time) {
    var bag = entity && entity.properties;
    if (!bag) return undefined;
    var v = bag[name];
    if (v && typeof v.getValue === 'function') {
      try { return v.getValue(time); } catch (e) { return undefined; }
    }
    return v;
  }

  function rowValue(entity, label, time) {
    var rows = prop(entity, 'detail_rows', time);
    if (!Array.isArray(rows)) return null;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i][0] === label) return rows[i][1];
    }
    return null;
  }

  function kindOf(entity, time) {
    if (!entity) return null;
    if (prop(entity, 'state', time) === 'estimated' || prop(entity, 'estimated', time) === true) {
      return null;
    }
    var type = prop(entity, 'type', time);
    if (type === 'aircraft' || type === 'military') return 'aircraft';
    if (type === 'vessel' || type === 'ship' || prop(entity, 'feed', time) === 'ais') return 'vessel';
    var id = String(entity.id || '');
    if (id.indexOf('flight-') === 0 || id.indexOf('mil-') === 0) return 'aircraft';
    return null;
  }

  // Track and speed in degrees and m/s, from fields first, rows second.
  function motionOf(entity, kind, time) {
    var track = null;
    var speed = null;
    if (kind === 'aircraft') {
      track = num(prop(entity, 'heading', time));
      if (track === null) track = num(prop(entity, 'track', time));
      speed = num(prop(entity, 'velocity', time));
    } else {
      // True heading first (the hull points along it), then course over ground.
      // AIS sends 511 for no heading and 360 for no course, so both fail the range.
      var tries = [prop(entity, 'heading', time), prop(entity, 'heading_deg', time),
        rowValue(entity, 'HEADING', time), prop(entity, 'cog_deg', time),
        rowValue(entity, 'COURSE', time)];
      for (var i = 0; i < tries.length && track === null; i++) {
        var n = num(tries[i]);
        if (n !== null && n >= 0 && n < 360) track = n;
      }
      var kn = num(prop(entity, 'sog_kn', time));
      if (kn === null) kn = num(rowValue(entity, 'SPEED', time));
      if (kn !== null && kn >= 0 && kn < 102.3) speed = kn * KNOT_MS;
      if (speed === null) speed = num(prop(entity, 'velocity', time));
    }
    if (track !== null && (track < 0 || track > 360)) track = null;
    if (speed !== null && speed < 0) speed = null;
    return { track: track, speed: speed };
  }

  function describe(entity, kind, time) {
    var name = entity.name || '';
    if (kind === 'aircraft') {
      var typeCode = prop(entity, 'aircraft_type', time) || prop(entity, 'type_code', time) || '';
      var pos = null;
      try { pos = entity.position && entity.position.getValue(time); } catch (e) { pos = null; }
      var c = classifyAircraft({
        modelClass: prop(entity, 'model_class', time), typeCode: typeCode,
        category: prop(entity, 'category', time), velocity: prop(entity, 'velocity', time),
        altM: prop(entity, 'alt_m', time)
      });
      var spec = modelSpec('aircraft', c.cls);
      return { cls: c.cls, basis: c.basis, spec: spec, scale: aircraftScale(spec, typeCode),
        typeCode: String(typeCode || ''), name: name, pos: pos,
        military: prop(entity, 'type', time) === 'military' };
    }
    var s = classifyShip({
      modelClass: prop(entity, 'model_class', time),
      shipType: prop(entity, 'ship_type', time), name: name
    });
    var sspec = modelSpec('vessel', s.cls);
    return { cls: s.cls, basis: s.basis, spec: sspec,
      scale: shipScale(sspec, prop(entity, 'length_m', time)), name: name };
  }

  function idKey(entity, time) {
    var mmsi = prop(entity, 'mmsi', time) || rowValue(entity, 'MMSI', time);
    return mmsi ? 'mmsi:' + mmsi : 'name:' + (entity.name || '');
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var CSS = [
    '.sdamodels-row { display: flex; align-items: center; gap: 6px; margin: 4px 0; font-size: 10px; }',
    '.sdamodels-row label { display: flex; align-items: center; gap: 6px; cursor: pointer; }',
    '.sdamodels-row select { background: rgba(0,0,0,0.6); color: inherit; border: 1px solid rgba(255,176,0,0.3); font: inherit; font-size: 10px; }',
    '.sdamodels-status { font-size: 9px; color: var(--hud-dim, #7a5500); margin-top: 6px; min-height: 2.4em; }',
    '#' + HUD_ID + ' { position: fixed; left: 50%; bottom: 74px; transform: translateX(-50%); z-index: 30;',
    '  display: none; align-items: center; gap: 8px; padding: 6px 10px; pointer-events: auto;',
    '  background: rgba(0,0,0,0.78); border: 1px solid rgba(255,176,0,0.35); border-radius: 4px;',
    '  color: var(--hud-color, #ffb000); font: 11px "JetBrains Mono", monospace; letter-spacing: 1px;',
    '  max-width: calc(100vw - 20px); flex-wrap: wrap; justify-content: center; }',
    '#' + HUD_ID + '.on { display: flex; }',
    '#' + HUD_ID + ' .sdaride-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 60vw; }',
    '#' + HUD_ID + ' button { background: rgba(255,176,0,0.08); color: inherit; border: 1px solid rgba(255,176,0,0.35);',
    '  font: inherit; font-size: 10px; padding: 3px 8px; cursor: pointer; border-radius: 2px; }',
    '#' + HUD_ID + ' button.active { background: rgba(255,176,0,0.3); }',
    '#' + HUD_ID + ' button.sdaride-exit { border-color: rgba(255,90,60,0.7); color: #ff7a5c; }',
    '#' + HUD_ID + ' .sdaride-hint { font-size: 9px; color: var(--hud-dim, #7a5500); }'
  ].join('\n');

  function injectStyle(doc) {
    if (!doc || doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    (doc.head || doc.body).appendChild(s);
  }

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
    var scene = viewer.scene;
    var satEngine = ctx.satEngine || null;
    var m = {
      enabled: true, smooth: true, budget: 'medium', smoothSats: false,
      recs: new Map(), space: {}, feed: new Map(), sources: {},
      lastScan: 0, lastHud: 0, counts: { aircraft: 0, vessel: 0, space: 0 },
      ride: { coll: null, key: null, entity: null }
    };

    function now() { return clockFn(); }
    function time() { return viewer.clock ? viewer.clock.currentTime : undefined; }

    function cesiumPositionProperty(fn) {
      if (typeof C.CallbackPositionProperty === 'function') {
        return new C.CallbackPositionProperty(fn, false);
      }
      return new C.CallbackProperty(fn, false);
    }

    function selectedEntity() { return global.selectedEntity || null; }

    function cameraTarget() {
      var cs = global.camState;
      if (!cs || !ATTACHED[cs.mode]) return null;
      return cs;
    }

    // ---- aircraft and ship models -----------------------------------

    function collections() {
      var out = [];
      if (viewer.entities) out.push({ coll: viewer.entities, show: true });
      var dss = viewer.dataSources;
      if (dss && typeof dss.get === 'function') {
        for (var i = 0; i < dss.length; i++) {
          var ds = dss.get(i);
          if (!ds || ds === m.spaceDs) continue;
          out.push({ coll: ds.entities, show: ds.show !== false });
        }
      }
      return out;
    }

    function orient(rec, pos) {
      var mo = motionOf(rec.entity, rec.kind, time());
      if (mo.track !== null) rec.track = mo.track;
      if (rec.track === undefined || rec.track === null || !pos) return;
      var hpr = new C.HeadingPitchRoll(C.Math.toRadians(headingFor(rec.track, rec.spec.offset)), 0, 0);
      rec.entity.orientation = new C.ConstantProperty(C.Transforms.headingPitchRollQuaternion(pos, hpr));
    }

    function drPosition(rec, result) {
      var f = rec.fix;
      var t = (now() - f.t) / 1000;
      var o = drOffset(f.speed, f.track, t, f.maxS);
      var out = C.Matrix4.multiplyByPoint(f.enu, new C.Cartesian3(o.east, o.north, 0),
        result || new C.Cartesian3());
      if (rec.blend) {
        var k = 1 - (now() - rec.blend.t) / 1000 / BLEND_S;
        if (k > 0) {
          out = C.Cartesian3.add(out, C.Cartesian3.multiplyByScalar(rec.blend.off, k,
            new C.Cartesian3()), out);
        } else {
          rec.blend = null;
        }
      }
      rec.drawn = C.Cartesian3.clone(out, rec.drawn);
      return out;
    }

    // Take the position the feed just wrote as a new fix and carry it forward.
    function takeFix(rec) {
      var e = rec.entity;
      var t = time();
      var pos = null;
      try { pos = e.position && e.position.getValue(t); } catch (err) { pos = null; }
      if (!pos) return;
      rec.reported = C.Cartesian3.clone(pos);
      rec.reportedProp = e.position;
      orient(rec, pos);
      var mo = motionOf(e, rec.kind, t);
      if (!m.smooth || mo.speed === null || mo.track === null || mo.speed < 0.5) {
        rec.fix = null;
        rec.posProp = null;
        return;
      }
      var off = rec.drawn ? C.Cartesian3.subtract(rec.drawn, pos, new C.Cartesian3()) : null;
      rec.blend = (off && C.Cartesian3.magnitude(off) < BLEND_MAX_M) ? { off: off, t: now() } : null;
      rec.fix = { pos: rec.reported, t: now(), enu: C.Transforms.eastNorthUpToFixedFrame(pos),
        speed: mo.speed, track: mo.track, maxS: DR_MAX_S[rec.kind] || 30 };
      rec.posProp = cesiumPositionProperty(function (tm, result) { return drPosition(rec, result); });
      e.position = rec.posProp;
    }

    function attach(entity, kind) {
      var d = describe(entity, kind, time());
      var rec = {
        entity: entity, kind: kind, cls: d.cls, basis: d.basis, spec: d.spec, info: d,
        savedModel: entity.model, savedOrientation: entity.orientation,
        savedPoint: entity.point ? entity.point.show : undefined,
        savedBillboard: entity.billboard ? entity.billboard.show : undefined,
        fix: null, posProp: null, drawn: null, blend: null, track: null
      };
      var opts = {
        uri: MODEL_BASE + d.spec.file, scale: d.scale, minimumPixelSize: MIN_PX,
        maximumScale: MAX_SCALE, runAnimations: false
      };
      if (C.ShadowMode) opts.shadows = C.ShadowMode.DISABLED;
      if (kind === 'aircraft' && d.military && C.ColorBlendMode) {
        opts.color = C.Color.fromCssColorString('#a3ab8f');
        opts.colorBlendMode = C.ColorBlendMode.MIX;
        opts.colorBlendAmount = 0.45;
      }
      if (C.Color && C.Color.fromCssColorString) {
        opts.silhouetteColor = C.Color.fromCssColorString('#ffb000');
        opts.silhouetteSize = 0;
      }
      entity.model = new C.ModelGraphics(opts);
      if (entity.point) entity.point.show = false;
      if (entity.billboard) entity.billboard.show = false;
      m.recs.set(entity, rec);
      takeFix(rec);
      return rec;
    }

    function detach(rec) {
      var e = rec.entity;
      if (rec.posProp && e.position === rec.posProp && rec.reported) {
        e.position = new C.ConstantPositionProperty(rec.reported);
      }
      e.model = rec.savedModel;
      e.orientation = rec.savedOrientation;
      if (e.point && rec.savedPoint !== undefined) e.point.show = rec.savedPoint;
      if (e.billboard && rec.savedBillboard !== undefined) e.billboard.show = rec.savedBillboard;
      m.recs.delete(e);
    }

    function detachAll() {
      Array.from(m.feed.values()).forEach(dropFeed);
      Array.from(m.recs.values()).forEach(detach);
    }

    // ---- rows from modules that draw primitives, not entities ----------
    // A source gives plain rows (see addSource below); a chosen row gets an
    // entity of its own here, carrying the same properties a page entity would.

    function ensureFeedDs() {
      if (m.feedDs) return m.feedDs;
      m.feedDs = new C.CustomDataSource('sdamodels-feeds');
      var added = viewer.dataSources.add(m.feedDs);
      if (added && typeof added.then === 'function') added.then(null, function () {});
      return m.feedDs;
    }

    function rowProps(row) {
      var kind = row.kind === 'vessel' ? 'vessel' : 'aircraft';
      var speed = num(row.speed_ms);
      if (speed === null && num(row.gs_kt) !== null) speed = num(row.gs_kt) * KNOT_MS;
      if (speed === null && num(row.sog_kn) !== null) speed = num(row.sog_kn) * KNOT_MS;
      var track = num(row.track);
      if (track === null) track = num(row.cog_deg);
      var p = { type: kind === 'vessel' ? 'vessel' : (row.military ? 'military' : 'aircraft'),
        heading: track, velocity: speed, model_source: row.source || '' };
      if (kind === 'vessel') {
        p.ship_type = row.ship_type;
        p.length_m = row.length_m;
        p.mmsi = row.mmsi || row.key;
      } else {
        p.aircraft_type = row.type_code || row.type || '';
        p.category = row.category;
        p.alt_m = row.alt_m;
      }
      if (row.model_class) p.model_class = row.model_class;
      return p;
    }

    function rowPosition(row) {
      var lat = num(row.lat);
      var lon = num(row.lon);
      if (lat === null || lon === null || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
      var alt = row.kind === 'vessel' ? 0 : Math.max(0, num(row.alt_m) || 0);
      return C.Cartesian3.fromDegrees(lon, lat, alt);
    }

    function rowEstimated(row) {
      return row.estimated === true || row.estimate === true || row.state === 'estimated';
    }

    function feedEntity(src, row, pos) {
      var key = src + ':' + row.key;
      var f = m.feed.get(key);
      var sig = [row.lat, row.lon, row.alt_m, row.track, row.cog_deg].join(',');
      if (!f) {
        var e = ensureFeedDs().entities.add({
          id: 'sdamodels-' + key, name: row.name || String(row.key),
          position: new C.ConstantPositionProperty(pos), properties: rowProps(row)
        });
        if (typeof row.pick === 'function') e.sdaPick = row.pick;
        f = { key: key, entity: e, sig: sig, src: src, rowKey: row.key };
        m.feed.set(key, f);
        return f;
      }
      if (f.sig !== sig) {
        f.sig = sig;
        f.entity.properties = rowProps(row);
        f.entity.position = new C.ConstantPositionProperty(pos);  // read as a new fix
      }
      return f;
    }

    function dropFeed(f) {
      var rec = m.recs.get(f.entity);
      if (rec) detach(rec);
      if (m.feedDs) m.feedDs.entities.remove(f.entity);
      m.feed.delete(f.key);
    }

    function sourceCands(camPos, reach, height, target, skipHex) {
      var out = [];
      Object.keys(m.sources).forEach(function (name) {
        var rows = [];
        try { rows = m.sources[name].rows() || []; } catch (e) { rows = []; }
        for (var i = 0; i < rows.length; i++) {
          var row = rows[i];
          if (!row || row.key == null || rowEstimated(row)) continue;
          if (row.kind !== 'vessel' && skipHex[String(row.key).toLowerCase()]) continue;
          var f = m.feed.get(name + ':' + row.key);
          var isTarget = !!(f && f.entity === target);
          if (!isTarget && height > reach) continue;
          var pos = rowPosition(row);
          if (!pos) continue;
          out.push({ row: row, src: name, pos: pos,
            kind: row.kind === 'vessel' ? 'vessel' : 'aircraft',
            d: C.Cartesian3.distance(camPos, pos), has: !!f, target: isTarget });
        }
      });
      return out;
    }

    function scanEntities() {
      var cam = viewer.camera;
      var camPos = cam.positionWC || cam.position;
      var height = cam.positionCartographic ? cam.positionCartographic.height : Infinity;
      var budget = budgetFor(m.budget);
      var target = selectedEntity();
      var t = time();
      var cands = [];
      var hexes = {};
      if (m.enabled && camPos) {
        var reach = budget.addM * KEEP_FACTOR;
        collections().forEach(function (c) {
          if (!c.show || !c.coll) return;
          if (m.feedDs && c.coll === m.feedDs.entities) return;
          var values = c.coll.values || [];
          for (var i = 0; i < values.length; i++) {
            var e = values[i];
            var id = String(e.id || '');
            var hex = id.indexOf('flight-') === 0 ? id.slice(7)
              : (id.indexOf('mil-') === 0 ? id.slice(4) : '');
            if (hex) hexes[hex.toLowerCase()] = true;
            var isTarget = (e === target);
            if (!isTarget && height > reach) continue;
            if (e.isShowing === false || e.show === false) continue;
            var kind = kindOf(e, t);
            if (!kind) continue;
            var p = null;
            try { p = e.position && e.position.getValue(t); } catch (err) { p = null; }
            if (!p) continue;
            cands.push({ e: e, kind: kind, d: C.Cartesian3.distance(camPos, p),
              has: m.recs.has(e), target: isTarget });
          }
        });
        // An aircraft the page already draws as flight-<hex> is not modelled twice.
        cands = cands.concat(sourceCands(camPos, reach, height, target, hexes));
      }
      var chosen = pickModels(cands, budget);
      var keepSet = new Set();
      var keepFeed = {};
      chosen.forEach(function (c) {
        if (c.row) {
          var f = feedEntity(c.src, c.row, c.pos);
          keepFeed[f.key] = true;
          c.e = f.entity;
        }
        keepSet.add(c.e);
        if (!m.recs.has(c.e)) attach(c.e, c.kind);
      });
      Array.from(m.feed.values()).forEach(function (f) {
        if (!keepFeed[f.key]) dropFeed(f);
      });
      Array.from(m.recs.values()).forEach(function (rec) {
        if (!keepSet.has(rec.entity)) detach(rec);
      });
      Object.keys(m.sources).forEach(function (name) {
        var cb = m.sources[name].modelled;
        if (typeof cb !== 'function') return;
        var keys = [];
        m.feed.forEach(function (f) { if (f.src === name) keys.push(f.rowKey); });
        try { cb(keys); } catch (e) { /* the source keeps its points */ }
      });
      var counts = { aircraft: 0, vessel: 0 };
      m.recs.forEach(function (rec) {
        counts[rec.kind] += 1;
        var g = rec.entity.model;
        if (g) {
          var sel = rec.entity === target;
          g.silhouetteSize = sel ? 2 : 0;
          var cs = cameraTarget();
          g.show = !(sel && cs && cs.mode === 'cockpit' && cs.targetKind === 'entity');
        }
        if (!rec.fix) {
          var p = null;
          try { p = rec.entity.position.getValue(t); } catch (err) { p = null; }
          if (p) orient(rec, p);
        }
      });
      m.counts.aircraft = counts.aircraft;
      m.counts.vessel = counts.vessel;
    }

    // Every frame: a feed that wrote a new position replaces our callback;
    // take that as the next fix.
    function checkFixes() {
      m.recs.forEach(function (rec) {
        var e = rec.entity;
        if (rec.posProp) {
          if (e.position !== rec.posProp) takeFix(rec);
        } else if (rec.reportedProp && e.position !== rec.reportedProp) {
          takeFix(rec);
        }
      });
    }

    // ---- space models -----------------------------------------------

    function satRecord(id) {
      return satEngine && satEngine.byNoradId ? satEngine.byNoradId[id] : null;
    }

    function satPosition(id) {
      var s = satRecord(id);
      if (!s) return null;
      var sat = global.satellite;
      if (m.smoothSats && sat && s.satrec && typeof ctx.simNow === 'function') {
        try {
          var when = ctx.simNow();
          var pv = sat.propagate(s.satrec, when);
          if (pv && pv.position) {
            var gd = sat.eciToGeodetic(pv.position, sat.gstime(when));
            return C.Cartesian3.fromRadians(gd.longitude, gd.latitude, gd.height * 1000);
          }
        } catch (e) { /* fall back to the engine's point */ }
      }
      if (!s.point || s.point.show === false || !s.point.position) return null;
      return s.point.position;
    }

    function spaceClass(id) {
      if (id === ISS_ID) return 'iss';
      if (id === HUBBLE_ID) return 'hubble';
      return 'satellite';
    }

    function spaceOrientation(item) {
      var p = item.lastPos;
      var prev = item.prevPos;
      if (!p || !prev) return item.quat || undefined;
      var fwd = C.Cartesian3.subtract(p, prev, new C.Cartesian3());
      if (C.Cartesian3.magnitude(fwd) < 1) return item.quat || undefined;
      var up = C.Cartesian3.normalize(p, new C.Cartesian3());
      var along = C.Cartesian3.multiplyByScalar(up, C.Cartesian3.dot(fwd, up), new C.Cartesian3());
      var x = C.Cartesian3.normalize(C.Cartesian3.subtract(fwd, along, new C.Cartesian3()),
        new C.Cartesian3());
      var y = C.Cartesian3.cross(up, x, new C.Cartesian3());
      var mat = C.Matrix3.fromColumnMajorArray([x.x, x.y, x.z, y.x, y.y, y.z, up.x, up.y, up.z]);
      item.quat = C.Quaternion.fromRotationMatrix(mat);
      return item.quat;
    }

    function ensureSpaceDs() {
      if (m.spaceDs) return m.spaceDs;
      m.spaceDs = new C.CustomDataSource('sdamodels-space');
      var added = viewer.dataSources.add(m.spaceDs);
      if (added && typeof added.then === 'function') added.then(null, function () {});
      return m.spaceDs;
    }

    function spaceItem(id) {
      var item = m.space[id];
      if (item) return item;
      var cls = spaceClass(id);
      var spec = SPACE_MODELS[cls];
      item = m.space[id] = { id: id, cls: cls, spec: spec, lastPos: null, prevPos: null, quat: null };
      var opts = { uri: MODEL_BASE + spec.file, minimumPixelSize: SPACE_MIN_PX,
        maximumScale: 20000, runAnimations: false };
      if (C.ShadowMode) opts.shadows = C.ShadowMode.DISABLED;
      item.entity = ensureSpaceDs().entities.add({
        id: 'sdamodels-sat-' + id,
        name: (satRecord(id) && satRecord(id).name) || spec.label,
        position: cesiumPositionProperty(function () {
          var p = satPosition(id);
          if (p && (!item.lastPos || !C.Cartesian3.equals(p, item.lastPos))) {
            item.prevPos = item.lastPos;
            item.lastPos = C.Cartesian3.clone(p);
          }
          return p || undefined;
        }),
        orientation: new C.CallbackProperty(function () { return spaceOrientation(item); }, false),
        model: opts
      });
      // The page's click and hover handlers route anything whose id carries
      // type 'satellite' to selectSatellite, so the model selects its object.
      item.entity.type = 'satellite';
      item.entity.noradId = id;
      return item;
    }

    function scanSpace() {
      var wanted = {};
      var cam = viewer.camera;
      var camPos = cam.positionWC || cam.position;
      var cs = cameraTarget();
      if (m.enabled && satEngine && satEngine.byNoradId && camPos) {
        var ids = [ISS_ID, HUBBLE_ID];
        var sel = satEngine.selectedId;
        if (sel != null && ids.indexOf(sel) < 0) ids.push(sel);
        ids.forEach(function (id) {
          var p = satPosition(id);
          if (!p) return;
          var isSel = (id === sel);
          if (!isSel && C.Cartesian3.distance(camPos, p) > SPACE_RANGE_M) return;
          wanted[id] = true;
        });
      }
      var n = 0;
      Object.keys(m.space).forEach(function (k) {
        var item = m.space[k];
        if (!wanted[k]) {
          // A selected-only generic model is dropped; ISS and Hubble are kept hidden.
          if (item.cls === 'satellite') {
            m.spaceDs.entities.remove(item.entity);
            delete m.space[k];
          } else {
            item.entity.show = false;
          }
        }
      });
      Object.keys(wanted).forEach(function (k) {
        var item = spaceItem(Number(k));
        var riding = cs && cs.targetKind === 'satellite' && String(cs.targetId) === String(k);
        item.entity.show = !(riding && cs.mode === 'cockpit');
        n += 1;
      });
      m.counts.space = n;
    }

    // ---- ride-along bar ------------------------------------------------

    function buildHud() {
      var hud = doc.getElementById(HUD_ID);
      if (hud) return hud;
      hud = el(doc, 'div', '');
      hud.id = HUD_ID;
      hud.setAttribute('role', 'toolbar');
      hud.setAttribute('aria-label', 'Ride along');
      m.hudText = el(doc, 'span', 'sdaride-text', '');
      hud.appendChild(m.hudText);
      m.hudButtons = {};
      [['chase', 'CHASE'], ['cockpit', 'FIRST PERSON'], ['orbit', 'ORBIT']].forEach(function (b) {
        var btn = el(doc, 'button', '', b[1]);
        btn.type = 'button';
        btn.addEventListener('click', function () { setMode(b[0]); });
        m.hudButtons[b[0]] = btn;
        hud.appendChild(btn);
      });
      var exit = el(doc, 'button', 'sdaride-exit', 'EXIT');
      exit.type = 'button';
      exit.title = 'Leave the ride (Escape)';
      exit.addEventListener('click', function () { setMode('free'); });
      hud.appendChild(exit);
      hud.appendChild(el(doc, 'span', 'sdaride-hint', 'ESC TO EXIT'));
      doc.body.appendChild(hud);
      return hud;
    }

    function setMode(mode) {
      if (typeof global.setCameraMode === 'function') {
        try { global.setCameraMode(mode); } catch (e) { /* page keeps its own state */ }
      }
      m.lastHud = 0;
      hudTick(true);
    }

    function rideInfo(cs) {
      var info = { mode: cs.mode, kind: null, name: '', label: '', altM: null, speedMs: null,
        track: null };
      if (cs.targetKind === 'satellite') {
        var s = satRecord(cs.targetId);
        info.kind = 'satellite';
        info.name = s ? (s.name || String(cs.targetId)) : String(cs.targetId);
        info.label = spaceClass(cs.targetId) === 'satellite' ? '' : SPACE_MODELS[spaceClass(cs.targetId)].label;
        if (s && finite(s.lastAlt)) info.altM = s.lastAlt * 1000;
        if (s && finite(s.lastVel)) info.speedMs = s.lastVel * 1000;
        return info;
      }
      var e = selectedEntity();
      if (!e) return info;
      var t = time();
      var kind = kindOf(e, t);
      info.kind = kind || 'entity';
      info.name = e.name || '';
      var rec = m.recs.get(e);
      if (rec) info.label = rec.spec.label + (rec.info && rec.info.typeCode ? ' ' + rec.info.typeCode : '');
      if (kind) {
        var mo = motionOf(e, kind, t);
        info.speedMs = mo.speed;
        info.track = mo.track;
      }
      try {
        var p = e.position && e.position.getValue(t);
        var carto = p && C.Cartographic.fromCartesian(p);
        if (carto) info.altM = carto.height;
      } catch (err) { /* no altitude */ }
      return info;
    }

    // layers.js redraws a feed with new entities; follow the same ship.
    function reacquire(cs) {
      if (!cs || cs.targetKind !== 'entity') { m.ride.entity = null; return; }
      var e = selectedEntity();
      if (!e) return;
      var t = time();
      if (m.ride.entity !== e) {
        m.ride.entity = e;
        m.ride.coll = e.entityCollection || null;
        m.ride.key = idKey(e, t);
        return;
      }
      var coll = m.ride.coll;
      if (!coll || typeof coll.contains !== 'function' || coll.contains(e)) return;
      var values = coll.values || [];
      for (var i = 0; i < values.length; i++) {
        if (values[i] !== e && idKey(values[i], t) === m.ride.key) {
          if (typeof global.selectEntity === 'function') {
            try { global.selectEntity(values[i]); } catch (err) { return; }
            m.ride.entity = values[i];
            // cameraTargetPosition() matches on the id, so move the target too.
            cs.targetKind = 'entity';
            cs.targetId = values[i].id;
            cs.lastPos = null;
          }
          return;
        }
      }
    }

    function hudTick(force) {
      var t = now();
      if (!force && t - m.lastHud < HUD_MS) return;
      m.lastHud = t;
      var hud = m.hud || (m.hud = buildHud());
      var cs = cameraTarget();
      if (!cs) {
        hud.classList.remove('on');
        m.ride.entity = null;
        return;
      }
      reacquire(cs);
      hud.classList.add('on');
      m.hudText.textContent = formatRide(rideInfo(cs));
      Object.keys(m.hudButtons).forEach(function (k) {
        m.hudButtons[k].classList.toggle('active', cs.mode === k);
      });
    }

    // ---- panel -----------------------------------------------------------

    function setStatus() {
      if (!m.statusEl) return;
      var c = m.counts;
      var parts = [];
      if (!m.enabled) {
        m.statusEl.textContent = 'Models are off; every object is a point.';
        return;
      }
      if (c.aircraft) parts.push(c.aircraft + ' aircraft');
      if (c.vessel) parts.push(c.vessel + ' ships');
      if (c.space) parts.push(c.space + ' spacecraft');
      var reach = Math.round(budgetFor(m.budget).addM / 1000);
      m.statusEl.textContent = parts.length
        ? 'Modelled: ' + parts.join(', ') + '.'
        : 'Aircraft and ships turn into models within ' + reach + ' km of the camera; ' +
          'the ISS and Hubble within ' + Math.round(SPACE_RANGE_M / 1000) + ' km.';
    }

    function buildPanel() {
      var root = el(doc, 'div', 'panel sdamodels');
      var h = el(doc, 'h3', '', PANEL_TITLE);
      root.appendChild(h);
      var body = el(doc, 'div', 'panel-body');
      root.appendChild(body);

      function check(label, value, onChange, title) {
        var row = el(doc, 'div', 'sdamodels-row');
        var lab = el(doc, 'label', '');
        var box = el(doc, 'input', '');
        box.type = 'checkbox';
        box.checked = value;
        box.addEventListener('change', function () { onChange(!!box.checked); });
        lab.appendChild(box);
        lab.appendChild(doc.createTextNode(label));
        if (title) lab.title = title;
        row.appendChild(lab);
        body.appendChild(row);
        return box;
      }
      m.enabledBox = check('3D models when zoomed in', m.enabled, function (on) { setEnabled(on); },
        'Aircraft by type, ships by class, the ISS and Hubble');
      m.smoothBox = check('Smooth motion between reports', m.smooth, function (on) { setSmooth(on); },
        'Carry each modelled object along its reported track until the next report (45 s for aircraft, 10 min for ships)');
      var row = el(doc, 'div', 'sdamodels-row');
      row.appendChild(el(doc, 'span', '', 'Detail'));
      var sel = el(doc, 'select', '');
      ['low', 'medium', 'high'].forEach(function (k) {
        var o = el(doc, 'option', '', k.toUpperCase() + ' (' + BUDGETS[k].max + ' models)');
        o.value = k;
        sel.appendChild(o);
      });
      sel.value = m.budget;
      sel.addEventListener('change', function () { setBudget(sel.value); });
      m.budgetSel = sel;
      row.appendChild(sel);
      body.appendChild(row);
      m.statusEl = el(doc, 'div', 'sdamodels-status', '');
      body.appendChild(m.statusEl);
      h.addEventListener('click', function (evt) {
        if (evt && evt.target && evt.target.tagName === 'INPUT') return;
        root.classList.toggle('collapsed');
      });
      return root;
    }

    function setEnabled(on) {
      m.enabled = !!on;
      if (m.enabledBox) m.enabledBox.checked = m.enabled;
      if (!m.enabled) detachAll();
      m.lastScan = 0;
      frame();
      setStatus();
    }

    function setSmooth(on) {
      m.smooth = !!on;
      if (m.smoothBox) m.smoothBox.checked = m.smooth;
      m.recs.forEach(function (rec) {
        if (!m.smooth && rec.posProp && rec.entity.position === rec.posProp && rec.reported) {
          rec.entity.position = new C.ConstantPositionProperty(rec.reported);
          rec.posProp = null;
          rec.fix = null;
        }
        takeFix(rec);
      });
    }

    function setBudget(name) {
      if (!BUDGETS[name]) return;
      m.budget = name;
      if (m.budgetSel) m.budgetSel.value = name;
      m.lastScan = 0;
    }

    function frame() {
      var t = now();
      checkFixes();
      if (t - m.lastScan >= SCAN_MS) {
        m.lastScan = t;
        try { scanEntities(); } catch (e) { /* next scan */ }
        try { scanSpace(); } catch (e) { /* next scan */ }
        setStatus();
      }
      hudTick(false);
    }

    injectStyle(doc);
    m.root = buildPanel();
    m.hud = buildHud();
    if (scene.preUpdate && scene.preUpdate.addEventListener) {
      scene.preUpdate.addEventListener(frame);
    }
    setStatus();

    m.frame = frame;
    m.setEnabled = setEnabled;
    m.setSmooth = setSmooth;
    m.setBudget = setBudget;
    m.satPosition = satPosition;
    m.addSource = function (name, src) {
      if (!name || !src || typeof src.rows !== 'function') return false;
      m.sources[name] = src;
      m.lastScan = 0;
      return true;
    };
    m.removeSource = function (name) {
      delete m.sources[name];
      Array.from(m.feed.values()).forEach(function (f) { if (f.src === name) dropFeed(f); });
    };
    m.rideInfo =function () { var cs = cameraTarget(); return cs ? rideInfo(cs) : null; };
    m.rideRange = function (kind, id, altM) {
      if (kind === 'satellite') {
        return rideRangeFor('satellite', SPACE_MODELS[spaceClass(Number(id))].length, altM);
      }
      var e = selectedEntity();
      var rec = e && m.recs.get(e);
      if (!rec) return null;
      var len = rec.kind === 'vessel' ? rec.spec.length * (rec.info.scale || 1)
        : 40 * (rec.info.scale || 1);
      return rideRangeFor(rec.kind, len, altM);
    };
    return m;
  }

  var current = null;
  var pending = {};

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function') return false;
      if (!ctx.viewer || !global.Cesium) return false;
      current = createModule(ctx);
      Object.keys(pending).forEach(function (k) { current.addSource(k, pending[k]); });
      ctx.mount.appendChild(current.root);
      return true;
    } catch (err) {
      return false;
    }
  }

  global.SDAModels = {
    init: init,
    setEnabled: function (on) { if (current) current.setEnabled(on); },
    isEnabled: function () { return !!(current && current.enabled); },
    setSmooth: function (on) { if (current) current.setSmooth(on); },
    // 'low' | 'medium' | 'high'; static/js/visuals.js calls this from its preset.
    setBudget: function (name) { if (current) current.setBudget(name); },
    // Once the page reads satPosition() for its chase camera too, the space
    // models may propagate per frame instead of following the 20 Hz points.
    useSmoothSatellites: function (on) { if (current) current.smoothSats = !!on; },
    satPosition: function (id) { return current ? current.satPosition(id) : null; },
    // Modules that draw primitives (aviation.js, maritime.js) offer rows here:
    // addSource('maritime', { rows: function () { return [{ key: mmsi, kind: 'vessel',
    //   lat, lon, cog_deg, sog_kn, ship_type, length_m, name, estimate, pick }] },
    //   modelled: function (keys) { hide those points } }). Aircraft rows carry
    // kind 'aircraft', alt_m, track, gs_kt or speed_ms, type_code, category,
    // military, state. Estimated rows are never modelled.
    addSource: function (name, src) {
      if (current) return current.addSource(name, src);
      if (!name || !src || typeof src.rows !== 'function') return false;
      pending[name] = src;  // applied at init, so load order does not matter
      return true;
    },
    removeSource: function (name) { delete pending[name]; if (current) current.removeSource(name); },
    rideRange: function (kind, id, altM) { return current ? current.rideRange(kind, id, altM) : null; },
    rideInfo: function () { return current ? current.rideInfo() : null; },
    counts: function () { return current ? Object.assign({}, current.counts) : null; },
    _frame: function () { if (current) current.frame(); },
    _setClock: function (fn) { clockFn = typeof fn === 'function' ? fn : function () { return Date.now(); }; },
    AIRCRAFT_MODELS: AIRCRAFT_MODELS,
    SHIP_MODELS: SHIP_MODELS,
    SPACE_MODELS: SPACE_MODELS,
    BUDGETS: BUDGETS,
    DR_MAX_S: DR_MAX_S,
    classifyAircraft: classifyAircraft,
    classifyShip: classifyShip,
    modelSpec: modelSpec,
    shipScale: shipScale,
    headingFor: headingFor,
    drOffset: drOffset,
    pickModels: pickModels,
    rideRangeFor: rideRangeFor,
    formatRide: formatRide,
    kindOf: kindOf,
    motionOf: motionOf
  };
})(typeof window !== 'undefined' ? window : this);
