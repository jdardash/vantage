// 3D cities on the WorldView globe.
//
// The PHOTOREAL 3D basemap turns this on. With a Google Maps key, or with a
// free Cesium ion token (CESIUM_ION_TOKEN: Cesium ion serves Google's
// Photorealistic 3D Tiles through its own Google asset, so no Google account
// is needed), it loads Google's photorealistic city mesh and hides the globe
// surface under it. Without either, or when Google's tiles fail to load, it
// extrudes OpenStreetMap building footprints from GET /api/buildings, coloured
// by kind, whenever the camera is below 3,000 m.
//
// Imagery overlays drape on the globe surface, so they vanish under the
// photorealistic mesh; the module names the ones it hides instead of letting
// them disappear silently. Ground marks (pins, camera icons, labels at height
// 0) are clamped to the mesh while it is on, so they sit on the roofs and
// streets rather than inside them.
//
//   SDABuildings3D.chooseCityLayer(config) -> 'google-key' | 'google-ion' | 'osm'
//   SDABuildings3D.visibleAt(cameraHeightM) -> true below 3,000 m
//   SDABuildings3D.hiddenBy3D(layerState)  -> names of the imagery overlays hidden
//   SDABuildings3D.setConfig(config)        (the page's /api/config answer)
//   SDABuildings3D.setCityMode(on) / setEnabled(on)
(function (global) {
  'use strict';

  var PANEL_TITLE = '3D city';
  var VISIBLE_BELOW_M = 3000;
  var BOX_STEP_DEG = 0.005;   // box centres snap to this grid so nearby views share a box
  var BOX_HALF_DEG = 0.0075;  // a 0.015 degree box, inside the server's 0.02 limit
  var KEEP_BOXES = 4;
  // Google tiles that fail this many times with no tile loaded in between mean the
  // session or the network is gone; one stray failure does not end the mesh.
  var TILE_FAILS_TO_FALL_BACK = 3;
  var R2D = 180 / Math.PI;

  var FAIL_TEXT = 'Google 3D tiles did not load (free monthly allowance used, or no network); ' +
    'showing OpenStreetMap buildings.';
  var TOKEN_TEXT = 'A free Cesium ion token (CESIUM_ION_TOKEN) adds Google\'s photorealistic 3D ' +
    'cities. Without one the buildings are OpenStreetMap footprints, raised by their height ' +
    'tag, else 3.2 m a level, else 8 m.';
  var OSM_CREDIT = 'Imagery: Esri, Maxar, Earthstar Geographics; buildings: OpenStreetMap ' +
    'contributors (ODbL)';
  var CREDITS = {
    'google-key': 'Imagery: Google Photorealistic 3D Tiles',
    'google-ion': 'Imagery: Google Photorealistic 3D Tiles through Cesium ion'
  };

  // Draped imagery overlays, under the names their own panels show.
  var IMAGERY_OVERLAYS = [
    { key: 'nasa_imagery', name: 'Dated imagery (NASA GIBS)' },
    { key: 'radar', name: 'Radar (RainViewer)' },
    { key: 'clouds', name: 'Live clouds' }
  ];

  var KINDS = [
    { group: 'Homes', color: '#d9c4a0', kinds: ['house', 'residential', 'apartments', 'detached',
      'semidetached_house', 'terrace', 'hut', 'bungalow', 'dormitory', 'farm', 'cabin'] },
    { group: 'Shops and offices', color: '#7fb0d6', kinds: ['commercial', 'retail', 'office', 'shop',
      'supermarket', 'kiosk', 'hotel', 'marketplace', 'bank', 'restaurant'] },
    { group: 'Industry and storage', color: '#a59cc6', kinds: ['industrial', 'warehouse', 'factory',
      'manufacture', 'storage_tank', 'hangar', 'garage', 'garages', 'shed', 'service', 'barn'] },
    { group: 'Worship', color: '#f0c75e', kinds: ['mosque', 'church', 'cathedral', 'chapel', 'temple',
      'shrine', 'synagogue', 'religious', 'place_of_worship'] },
    { group: 'Schools, health, public', color: '#e8926a', kinds: ['school', 'university', 'college',
      'kindergarten', 'hospital', 'clinic', 'public', 'government', 'civic', 'townhall',
      'fire_station', 'police', 'train_station', 'transportation', 'stadium'] },
    { group: 'Other buildings', color: '#c3c8cf', kinds: [] }
  ];

  function chooseCityLayer(config) {
    var c = config && typeof config === 'object' ? config : {};
    if (typeof c.google_maps_api_key === 'string' && c.google_maps_api_key) return 'google-key';
    if (typeof c.cesium_ion_token === 'string' && c.cesium_ion_token) return 'google-ion';
    return 'osm';
  }

  function visibleAt(cameraHeightM) {
    return typeof cameraHeightM === 'number' && isFinite(cameraHeightM) && cameraHeightM < VISIBLE_BELOW_M;
  }

  function hiddenBy3D(layerState) {
    var s = layerState && typeof layerState === 'object' ? layerState : {};
    return IMAGERY_OVERLAYS.filter(function (o) { return s[o.key] === true; })
      .map(function (o) { return o.name; });
  }

  function kindGroup(kind) {
    var k = String(kind || '').toLowerCase();
    for (var i = 0; i < KINDS.length - 1; i++) {
      if (KINDS[i].kinds.indexOf(k) >= 0) return KINDS[i];
    }
    return KINDS[KINDS.length - 1];
  }

  function round4(x) { return Math.round(x * 1e4) / 1e4; }

  // The box to ask for around a ground point: centred on a 0.005 degree grid.
  function boxAround(lat, lon) {
    var clat = Math.round(lat / BOX_STEP_DEG) * BOX_STEP_DEG;
    var clon = Math.round(lon / BOX_STEP_DEG) * BOX_STEP_DEG;
    var west = clon - BOX_HALF_DEG;
    var east = clon + BOX_HALF_DEG;
    if (west < -180) west += 360;   // east < west: the server splits it at the antimeridian
    if (east > 180) east -= 360;
    return {
      key: round4(clat) + ',' + round4(clon),
      south: round4(Math.max(-90, clat - BOX_HALF_DEG)), north: round4(Math.min(90, clat + BOX_HALF_DEG)),
      west: round4(west), east: round4(east)
    };
  }

  function joinNames(names) {
    if (names.length < 2) return names.join('');
    return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  }

  function titleKind(kind) {
    var k = String(kind || 'building').replace(/_/g, ' ');
    return k.charAt(0).toUpperCase() + k.slice(1);
  }

  function fmtHeight(h) {
    return (Math.round(h * 10) / 10) + ' m';
  }

  function detailRows(b) {
    var h = Number(b.height_m);
    var how = b.height_from === 'height' ? ' (tagged)'
      : b.height_from === 'levels' ? ' (from ' + b.levels + ' levels)' : ' (not tagged, assumed)';
    return [
      ['KIND', titleKind(b.kind)],
      ['HEIGHT', fmtHeight(h) + how],
      ['LEVELS', b.levels != null ? String(b.levels) : 'not tagged'],
      ['OSM ID', String(b.id)],
      ['SOURCE', 'OpenStreetMap contributors (ODbL)']
    ];
  }

  // ------------------------------------------------------------------ module

  var config = null;     // the page's /api/config answer, via setConfig
  var wanted = false;    // city mode asked for, possibly before init
  var current = null;

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var viewer = ctx.viewer;
    var scene = viewer.scene;
    var camera = viewer.camera;
    var base = typeof ctx.api === 'string' ? ctx.api : '';
    // route: what this entry asked for; layer: what is on screen (osm after a fallback).
    // tileset is kept, hidden, between entries so each page spends one root load;
    // pending is a tileset still on its way.
    var m = { on: false, route: null, layer: null, gen: 0, tileset: null, tileRoute: null,
      tileFails: 0, tileUnhook: [], pending: null, ds: null, boxes: [], refs: {},
      loading: null, clamped: [], status: '', destroyed: false };
    var ui = {};
    var unhook = [];

    function el(tag, cls, text) {
      var e = doc.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }

    function intel(text) {
      if (typeof ctx.addIntelItem === 'function') {
        try { ctx.addIntelItem(text); } catch (err) { /* the feed is optional */ }
      }
    }

    function setStatus(text) {
      m.status = text || '';
      if (ui.status) ui.status.textContent = m.status;
    }

    function say(text) { setStatus(text); intel(text); }

    function setCredit(text) {
      var box = doc.getElementById ? doc.getElementById('basemap-credit') : null;
      if (box) box.textContent = text;
    }

    function setNote(on) { if (ui.note) ui.note.style.display = on ? '' : 'none'; }

    function buildPanel() {
      if (!ctx.mount || typeof ctx.mount.appendChild !== 'function') return;
      var root = el('div', 'panel');
      root.appendChild(el('h3', null, PANEL_TITLE));
      var body = el('div', 'panel-body');
      ui.status = el('div', 'b3d-status', 'Choose PHOTOREAL 3D under Basemap to see cities in 3D.');
      ui.status.style.fontSize = '10px';
      body.appendChild(ui.status);
      var legend = el('div', 'b3d-legend');
      legend.style.marginTop = '6px';
      KINDS.forEach(function (k) {
        var row = el('div', 'b3d-legend-row');
        row.style.fontSize = '9px';
        var swatch = el('span', 'b3d-swatch', ' ');
        swatch.style.display = 'inline-block';
        swatch.style.width = '8px';
        swatch.style.height = '8px';
        swatch.style.marginRight = '6px';
        swatch.style.background = k.color;
        row.appendChild(swatch);
        row.appendChild(el('span', null, k.group));
        legend.appendChild(row);
      });
      body.appendChild(legend);
      ui.note = el('div', 'b3d-note', TOKEN_TEXT);
      ui.note.style.fontSize = '9px';
      ui.note.style.opacity = '0.7';
      ui.note.style.marginTop = '6px';
      body.appendChild(ui.note);
      setNote(chooseCityLayer(config) === 'osm');
      root.appendChild(body);
      ctx.mount.appendChild(root);
      ui.root = root;
    }

    // -------- where the camera is

    function cameraHeight() {
      var c = camera.positionCartographic;
      if (!c || typeof c.height !== 'number') return NaN;
      var ground = 0;
      try {
        if (scene.globe && typeof scene.globe.getHeight === 'function') {
          var g = scene.globe.getHeight(c);
          if (typeof g === 'number' && isFinite(g)) ground = g;
        }
      } catch (err) { ground = 0; }
      return c.height - ground;
    }

    function viewTarget() {
      var cv = scene.canvas;
      if (cv && typeof camera.pickEllipsoid === 'function' && C.Cartesian2) {
        try {
          var hit = camera.pickEllipsoid(new C.Cartesian2((cv.clientWidth || cv.width) / 2,
            (cv.clientHeight || cv.height) / 2));
          if (hit) {
            var g = C.Cartographic.fromCartesian(hit);
            if (g) return { lat: g.latitude * R2D, lon: g.longitude * R2D };
          }
        } catch (err) { /* fall through to the point under the camera */ }
      }
      var c = camera.positionCartographic;
      return c ? { lat: c.latitude * R2D, lon: c.longitude * R2D } : null;
    }

    // -------- Google Photorealistic 3D Tiles

    function loadGoogle(route) {
      if (typeof C.createGooglePhotorealistic3DTileset !== 'function') {
        return Promise.reject(new Error('this Cesium build has no Google 3D tiles'));
      }
      var key;
      if (route === 'google-key') {
        key = config.google_maps_api_key;
      } else if (C.Ion && !C.Ion.defaultAccessToken) {
        C.Ion.defaultAccessToken = config.cesium_ion_token;
      }
      try {
        return Promise.resolve(C.createGooglePhotorealistic3DTileset(key, { showCreditsOnScreen: true }));
      } catch (err) {
        return Promise.reject(err);
      }
    }

    // Cesium's own credit box is detached by the page; Google's tiles must show
    // their logo and data credits, so it is put on screen while they are.
    function showCesiumCredits(on) {
      var box = viewer.creditDisplay && viewer.creditDisplay.container;
      if (!box) return;
      if (on && !box.parentNode && doc.body) {
        box.style.position = 'fixed';
        box.style.left = '8px';
        box.style.bottom = '30px';
        box.style.zIndex = '20';
        box.style.fontSize = '10px';
        box.style.maxWidth = '60%';
        doc.body.appendChild(box);
      } else if (!on && box.parentNode) {
        box.parentNode.removeChild(box);
      }
    }

    function overlayState() {
      var s = {};
      var visible = typeof ctx.getVisibleLayers === 'function' ? ctx.getVisibleLayers() : [];
      (visible || []).forEach(function (k) { s[k] = true; });
      try {
        var nasa = global.SDANasaImagery;
        if (nasa && typeof nasa.isImageryOn === 'function') s.nasa_imagery = !!nasa.isImageryOn();
        var wx = global.SDAWeather;
        if (wx && typeof wx.isEnabled === 'function' && typeof wx.isLayerOn === 'function') {
          s.radar = !!(wx.isEnabled() && wx.isLayerOn('radar'));
        }
        var clouds = global.SDAClouds;
        if (clouds && typeof clouds.isEnabled === 'function') s.clouds = !!clouds.isEnabled();
      } catch (err) { /* a module that throws hides nothing we can name */ }
      return s;
    }

    // -------- ground marks on the mesh

    function eachEntity(fn) {
      var lists = [viewer.entities];
      var sources = viewer.dataSources;
      if (sources && typeof sources.get === 'function') {
        for (var i = 0; i < sources.length; i++) {
          var ds = sources.get(i);
          if (ds && ds !== m.ds && ds.entities) lists.push(ds.entities);
        }
      }
      lists.forEach(function (list) {
        if (list && list.values) list.values.forEach(fn);
      });
    }

    function groundLevel(e, time) {
      var p = e.position;
      if (!p) return false;
      try {
        if (typeof p.getValue === 'function') p = p.getValue(time);
        if (!p) return false;
        var g = C.Cartographic.fromCartesian(p);
        return !!g && Math.abs(g.height) < 1;
      } catch (err) { return false; }
    }

    function clampGround(on) {
      if (!on) {
        m.clamped.forEach(function (c) {
          c.g.heightReference = c.was;
          delete c.g.sdaClamped;
        });
        m.clamped = [];
        return;
      }
      if (!C.HeightReference) return;
      var time = viewer.clock ? viewer.clock.currentTime : undefined;
      eachEntity(function (e) {
        ['point', 'billboard', 'label'].forEach(function (k) {
          var g = e[k];
          if (!g || g.sdaClamped || !groundLevel(e, time)) return;
          m.clamped.push({ g: g, was: g.heightReference });
          g.sdaClamped = true;
          g.heightReference = C.HeightReference.CLAMP_TO_GROUND;
        });
      });
    }

    // -------- OpenStreetMap buildings

    function ensureSource() {
      if (m.ds) return m.ds;
      m.ds = new C.CustomDataSource('sda-buildings3d');
      viewer.dataSources.add(m.ds);
      return m.ds;
    }

    function buildingEntity(b) {
      var ring = Array.isArray(b.ring) ? b.ring : [];
      var flat = [];
      ring.forEach(function (p) { flat.push(Number(p[0]), Number(p[1])); });
      if (flat.length < 8 || !(Number(b.height_m) > 0)) return null;
      var group = kindGroup(b.kind);
      return m.ds.entities.add({
        id: 'bld-' + b.id,
        name: b.name || titleKind(b.kind),
        polygon: {
          hierarchy: new C.PolygonHierarchy(C.Cartesian3.fromDegreesArray(flat)),
          height: 0,
          heightReference: C.HeightReference.CLAMP_TO_GROUND,
          extrudedHeight: Number(b.height_m),
          extrudedHeightReference: C.HeightReference.RELATIVE_TO_GROUND,
          material: C.Color.fromCssColorString(group.color),
          outline: false
        },
        properties: { type: 'Building', detail_rows: detailRows(b) }
      });
    }

    function addBox(key, list) {
      var ids = [];
      (list || []).forEach(function (b) {
        if (!b || b.id == null) return;
        var id = 'bld-' + b.id;
        if (!m.refs[id]) {
          if (!buildingEntity(b)) return;
          m.refs[id] = 0;
        }
        m.refs[id] += 1;
        ids.push(id);
      });
      m.boxes.push({ key: key, ids: ids });
      while (m.boxes.length > KEEP_BOXES) dropBox(m.boxes.shift());
    }

    function dropBox(box) {
      box.ids.forEach(function (id) {
        m.refs[id] -= 1;
        if (m.refs[id] > 0) return;
        delete m.refs[id];
        var e = m.ds.entities.getById(id);
        if (e) m.ds.entities.remove(e);
      });
    }

    function hasBox(key) {
      for (var i = 0; i < m.boxes.length; i++) if (m.boxes[i].key === key) return true;
      return false;
    }

    function buildingCount() { return Object.keys(m.refs).length; }

    function refreshOsm() {
      if (!m.on || m.layer !== 'osm' || !m.ds) return Promise.resolve(null);
      var h = cameraHeight();
      if (!visibleAt(h)) {
        m.ds.show = false;
        setStatus('OpenStreetMap buildings appear below 3,000 m above the ground.');
        return Promise.resolve(null);
      }
      m.ds.show = true;
      var target = viewTarget();
      if (!target) return Promise.resolve(null);
      var box = boxAround(target.lat, target.lon);
      if (hasBox(box.key) || m.loading === box.key) return Promise.resolve(null);
      if (ctx.isStatic) {
        setStatus('OpenStreetMap buildings need the live server.');
        return Promise.resolve(null);
      }
      m.loading = box.key;
      var gen = m.gen;
      var url = base + '/api/buildings?south=' + box.south + '&west=' + box.west +
        '&north=' + box.north + '&east=' + box.east;
      setStatus('Loading OpenStreetMap buildings...');
      return global.fetch(url).then(function (resp) {
        return resp.json().then(function (body) {
          if (!resp.ok) return { error: (body && body.detail) || 'The buildings request failed.' };
          return body;
        });
      }).then(function (data) {
        m.loading = null;
        if (gen !== m.gen || m.layer !== 'osm') return null;
        var list = data && Array.isArray(data.buildings) ? data.buildings : [];
        if (data && data.error && !list.length) {
          setStatus(String(data.error));
          return null;
        }
        addBox(box.key, list);
        var n = buildingCount();
        setStatus(n + ' OpenStreetMap buildings in view' +
          (data && data.truncated ? ' (the first 3,000 of a dense area)' : '') +
          '. Click one for its height.');
        if (scene.requestRender) scene.requestRender();
        return n;
      }).catch(function () {
        m.loading = null;
        if (gen === m.gen) setStatus('OpenStreetMap buildings did not load (no answer from this server).');
        return null;
      });
    }

    function startOsm() {
      m.layer = 'osm';
      scene.globe.show = true;
      ensureSource().show = true;
      setCredit(OSM_CREDIT);
      setNote(chooseCityLayer(config) === 'osm');
      return refreshOsm();
    }

    // -------- the Google tileset's life

    function destroyTileset(t) {
      try {
        if (t && typeof t.destroy === 'function' &&
            !(typeof t.isDestroyed === 'function' && t.isDestroyed())) t.destroy();
      } catch (err) { /* already gone */ }
    }

    // Off the scene and destroyed, so the next entry asks Google for a new one.
    function dropTileset() {
      var t = m.tileset;
      if (!t) return;
      m.tileUnhook.forEach(function (fn) { try { fn(); } catch (err) { /* gone */ } });
      m.tileUnhook = [];
      m.tileset = null;
      m.tileRoute = null;
      m.tileFails = 0;
      // Cesium's primitive collection destroys what it removes; destroyTileset covers the rest.
      try { scene.primitives.remove(t); } catch (err) { /* never added */ }
      destroyTileset(t);
    }

    function listen(evt, fn) {
      if (!evt || typeof evt.addEventListener !== 'function') return;
      var off = evt.addEventListener(fn);
      m.tileUnhook.push(typeof off === 'function' ? off : function () {
        if (typeof evt.removeEventListener === 'function') evt.removeEventListener(fn);
      });
    }

    function adoptTileset(tileset, route) {
      m.tileset = tileset;
      m.tileRoute = route;
      m.tileFails = 0;
      listen(tileset.tileLoad, function () { m.tileFails = 0; });
      listen(tileset.tileFailed, function () { onTileFailed(tileset); });
      scene.primitives.add(tileset);
    }

    // A tile that fails after the root has loaded (network gone, Google's session
    // expired on a page left open, 429s) would leave black ground under a hidden
    // globe; past a few failures in a row the city goes back to OpenStreetMap.
    // Cesium can raise tileFailed from inside the tileset's own update, so the
    // tileset is dropped on the next turn rather than destroyed under its caller.
    function onTileFailed(tileset) {
      if (tileset !== m.tileset || m.tileFails >= TILE_FAILS_TO_FALL_BACK) return;
      m.tileFails++;
      if (m.tileFails < TILE_FAILS_TO_FALL_BACK) return;
      global.setTimeout(function () {
        if (tileset !== m.tileset) return;
        dropTileset();
        if (m.on && m.layer !== 'osm') fallBack();
      }, 0);
    }

    function fallBack() {
      showCesiumCredits(false);
      clampGround(false);
      say(FAIL_TEXT);
      return startOsm().then(function () {
        if (m.status.indexOf(FAIL_TEXT) < 0) setStatus(FAIL_TEXT + ' ' + m.status);
        return m.layer;
      });
    }

    function showGoogle() {
      var route = m.tileRoute;
      m.tileset.show = true;
      scene.globe.show = false;   // the tiles cover the ground; the globe under them would z-fight
      m.layer = route;
      setCredit(CREDITS[route]);
      showCesiumCredits(true);
      clampGround(true);
      setStatus(CREDITS[route].replace('Imagery: ', '') + '. Click two points with the map ' +
        'tools HGT measure for a height.');
      var hidden = hiddenBy3D(overlayState());
      if (hidden.length) {
        intel('Photorealistic 3D hides ' + joinNames(hidden) + ': draped imagery lies on the ' +
          'globe surface, which the 3D tiles replace.');
      }
      if (scene.requestRender) scene.requestRender();
      return route;
    }

    // One root request per page: a kept tileset is shown again, a load already on
    // its way is waited for, and a tileset that lands after the city was left is
    // destroyed rather than kept.
    function startGoogle(route) {
      if (m.tileset && m.tileRoute === route) return Promise.resolve(showGoogle());
      dropTileset();
      setStatus('Loading Google Photorealistic 3D Tiles...');
      if (m.pending && m.pending.route === route) return m.pending.done;
      var job = { route: route };
      job.done = loadGoogle(route).then(function (tileset) {
        if (m.pending === job) m.pending = null;
        if (m.destroyed || !m.on || m.route !== route) {
          destroyTileset(tileset);
          return m.layer;
        }
        adoptTileset(tileset, route);
        return showGoogle();
      }, function () {
        if (m.pending === job) m.pending = null;
        if (m.destroyed || !m.on || m.route !== route) return m.layer;
        return fallBack();
      });
      m.pending = job;
      return job.done;
    }

    // -------- city mode on and off

    function enter() {
      ++m.gen;
      m.on = true;
      var route = chooseCityLayer(config);
      m.route = route;
      setNote(route === 'osm');
      if (route === 'osm') return startOsm().then(function () { return m.layer; });
      return startGoogle(route);
    }

    function leave() {
      m.gen++;
      m.on = false;
      m.route = null;
      m.loading = null;
      if (m.tileset) m.tileset.show = false;   // kept for the next entry
      scene.globe.show = true;
      showCesiumCredits(false);
      clampGround(false);
      if (m.ds) {
        m.ds.entities.removeAll();
        m.ds.show = false;
      }
      m.boxes = [];
      m.refs = {};
      m.layer = null;
      setStatus('Choose PHOTOREAL 3D under Basemap to see cities in 3D.');
      return Promise.resolve(null);
    }

    function setCityMode(on) {
      if (on) return m.on ? Promise.resolve(m.layer) : enter();
      return m.on ? leave() : Promise.resolve(null);
    }

    function onMoveEnd() {
      if (!m.on) return;
      if (m.layer === 'osm') refreshOsm();
      else if (m.layer) clampGround(true);
    }

    function destroy() {
      m.destroyed = true;
      leave();
      dropTileset();
      unhook.forEach(function (fn) { try { fn(); } catch (err) { /* gone */ } });
      if (m.ds) { try { viewer.dataSources.remove(m.ds, true); } catch (err) { /* gone */ } }
      if (ui.root && ui.root.parentNode) ui.root.parentNode.removeChild(ui.root);
    }

    buildPanel();
    if (camera.moveEnd && typeof camera.moveEnd.addEventListener === 'function') {
      unhook.push(camera.moveEnd.addEventListener(onMoveEnd));
    }

    return {
      m: m,
      setCityMode: setCityMode,
      refresh: refreshOsm,
      configChanged: function () {
        var route = chooseCityLayer(config);
        setNote(route === 'osm');
        if (m.tileset && m.tileRoute !== route) dropTileset();
        if (!m.on) return Promise.resolve(null);
        // Same route as this entry asked for: nothing to redo (no second Overpass box).
        if (route === m.route) return Promise.resolve(m.layer);
        return leave().then(enter);
      },
      destroy: destroy
    };
  }

  function init(ctx) {
    if (!ctx || !ctx.viewer || !ctx.viewer.scene || !global.Cesium) return false;
    if (current) { current.destroy(); current = null; }
    current = createModule(ctx);
    if (wanted) current.setCityMode(true);
    return true;
  }

  function setCityMode(on) {
    wanted = !!on;
    return current ? current.setCityMode(wanted) : Promise.resolve(null);
  }

  global.SDABuildings3D = {
    init: init,
    setEnabled: setCityMode,
    setCityMode: setCityMode,
    setConfig: function (cfg) {
      config = cfg && typeof cfg === 'object' ? cfg : null;
      return current ? current.configChanged() : Promise.resolve(null);
    },
    isEnabled: function () { return wanted; },
    state: function () {
      if (!current) return { on: wanted, layer: null, buildings: 0, status: '' };
      var m = current.m;
      return { on: m.on, layer: m.layer, buildings: Object.keys(m.refs).length, status: m.status };
    },
    refresh: function () { return current ? current.refresh() : Promise.resolve(null); },
    chooseCityLayer: chooseCityLayer,
    visibleAt: visibleAt,
    hiddenBy3D: hiddenBy3D,
    kindGroup: kindGroup,
    boxAround: boxAround,
    FAIL_TEXT: FAIL_TEXT,
    VISIBLE_BELOW_M: VISIBLE_BELOW_M,
    IMAGERY_OVERLAYS: IMAGERY_OVERLAYS,
    KINDS: KINDS
  };
})(typeof window !== 'undefined' ? window : this);
