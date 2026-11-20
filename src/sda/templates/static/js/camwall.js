// Camera wall for the WorldView globe: the cameras you starred, up to nine at
// once in a grid over the globe, each playing its feed the way the operator
// publishes it.
//
// Nothing here plays a feed itself. Favourites, the per-camera record and the
// player all come from the cameras module (window.SDACameras):
//
//   SDACameras.favourites()     the starred list (localStorage), newest first
//   SDACameras.fetchCamera(rid) -> Promise of the full record from /api/cameras/{rid}
//   SDACameras.playFeed(media, foot, cam) -> { stop, pause, resume }
//
// so refreshing stills, MJPEG, HLS (hls.js loaded on demand), MP4 and YouTube
// (youtube-nocookie) behave exactly as in the single viewer, with the same URL
// checks. When the tab is hidden every tile is paused (stills stop refreshing,
// MJPEG and YouTube unload, video stops loading) and resumes when shown.
//
// The grid layout idea (several live feeds docked together) comes from
// WebcamDock.jsx in godseye (Apache-2.0); this file shares no code with it.
//
// Page wiring: list it in EXTENSIONS after SDACameras with a mount div; the
// panel lists the starred cameras and opens the wall. A key handler can call
// SDACamWall.toggle(). Escape closes the wall.

(function (global) {
  'use strict';

  var STYLE_ID = 'sda-camwall-style';
  var PANEL_TITLE = 'Camera wall';
  var MAX_TILES = 9;

  // ---------------------------------------------------------------------
  // Pure helpers (exported for the Node tests)
  // ---------------------------------------------------------------------

  // Columns and rows for n tiles: as square as the count allows, wider than tall.
  function gridFor(n) {
    var k = Math.max(0, Math.min(MAX_TILES, Math.floor(n) || 0));
    if (k === 0) return { cols: 0, rows: 0 };
    var cols = Math.ceil(Math.sqrt(k));
    return { cols: cols, rows: Math.ceil(k / cols) };
  }

  function pageCount(n) {
    return Math.max(1, Math.ceil((Math.floor(n) || 0) / MAX_TILES));
  }

  function pageSlice(list, page) {
    var pages = pageCount(list.length);
    var p = Math.max(0, Math.min(pages - 1, Math.floor(page) || 0));
    return { page: p, pages: pages, items: list.slice(p * MAX_TILES, (p + 1) * MAX_TILES) };
  }

  function isNum(x) { return typeof x === 'number' && isFinite(x); }

  function textOr(v, fallback) {
    return (typeof v === 'string' && v.trim()) ? v.trim() : fallback;
  }

  function placeLine(cam) {
    return [cam.city, cam.country].filter(function (x) { return typeof x === 'string' && x; })
      .join(', ');
  }

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------

  var STYLE_CSS = [
    '.cw-summary { font-size: 9px; color: var(--hud-dim, #7a5500); margin-bottom: 6px; line-height: 1.4; }',
    '.cw-row { display: flex; gap: 3px; margin-bottom: 5px; }',
    '.cw-row .mode-btn { flex: 1; font-size: 8px; padding: 3px 2px; }',
    '.cw-list { max-height: 160px; overflow-y: auto; }',
    '.cw-item { display: flex; align-items: center; gap: 4px; font-size: 9px;',
    '  border-bottom: 1px solid rgba(255,176,0,0.08); padding: 2px 0; }',
    '.cw-item-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
    '  cursor: pointer; background: none; border: 0; color: var(--hud-color, #ffb000); font: inherit;',
    '  text-align: left; padding: 0; }',
    '.cw-item-name:hover { text-decoration: underline; }',
    '.cw-x { background: none; border: 1px solid rgba(255,176,0,0.25); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 8px; padding: 0 4px; cursor: pointer; border-radius: 2px; }',
    '.cw-status { font-size: 9px; min-height: 11px; color: var(--hud-text, #ffb000); }',
    '.cw-wall { position: fixed; left: 262px; top: 60px; right: 12px; bottom: 72px; z-index: 47;',
    '  display: flex; flex-direction: column; background: rgba(0,0,0,0.94);',
    '  border: 1px solid var(--hud-color, #ffb000); border-radius: 4px;',
    '  font-family: "JetBrains Mono", monospace; color: var(--hud-color, #ffb000); }',
    '.cw-wall.cw-full { left: 6px; top: 6px; right: 6px; bottom: 6px; }',
    '.cw-head { display: flex; align-items: center; gap: 6px; height: 28px; flex: 0 0 auto;',
    '  padding: 0 8px; border-bottom: 1px solid rgba(255,176,0,0.15); font-size: 10px; letter-spacing: 1px; }',
    '.cw-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
    '  font-weight: 700; }',
    '.cw-btn { background: none; border: 1px solid rgba(255,176,0,0.3); color: var(--hud-color, #ffb000);',
    '  font: inherit; font-size: 9px; padding: 2px 8px; cursor: pointer; border-radius: 2px; }',
    '.cw-btn:disabled { opacity: 0.35; cursor: default; }',
    '.cw-grid { flex: 1; min-height: 0; display: grid; gap: 4px; padding: 4px; }',
    '.cw-tile { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: #000;',
    '  border: 1px solid rgba(255,176,0,0.25); }',
    '.cw-solo-mode .cw-tile:not(.cw-solo) { display: none; }',
    '.cw-solo-mode .cw-grid { grid-template-columns: 1fr !important; grid-template-rows: 1fr !important; }',
    '.cw-tile-head { display: flex; align-items: center; gap: 4px; padding: 2px 4px; font-size: 9px;',
    '  flex: 0 0 auto; border-bottom: 1px solid rgba(255,176,0,0.12); }',
    '.cw-tile-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
    '.cw-media { position: relative; flex: 1; min-height: 0; display: flex; align-items: center;',
    '  justify-content: center; overflow: hidden; }',
    '.cw-media img, .cw-media video { max-width: 100%; max-height: 100%; object-fit: contain; }',
    '.cw-media iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }',
    '.cw-media .cams-msg { padding: 8px; text-align: center; font-size: 9px; color: var(--hud-dim, #7a5500); }',
    '.cw-foot { display: flex; gap: 6px; padding: 1px 4px; font-size: 8px; flex: 0 0 auto;',
    '  color: var(--hud-dim, #7a5500); overflow: hidden; white-space: nowrap; }',
    '.cw-foot a { color: var(--hud-color, #ffb000); }',
    '@media (max-width: 760px) { .cw-wall { left: 4px; right: 4px; top: 48px; bottom: 70px; } }'
  ].join('\n');

  function injectStyle(doc) {
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
    var b = el('button', className, text);
    b.setAttribute('type', 'button');
    if (title) b.setAttribute('title', title);
    return b;
  }

  // ---------------------------------------------------------------------
  // Module
  // ---------------------------------------------------------------------

  function createModule(ctx) {
    var C = global.Cesium;
    var doc = global.document;
    var m = { ctx: ctx, wall: null, tiles: [], page: 0, seq: 0, cleanup: [] };

    function cams() { return global.SDACameras || null; }
    function favs() {
      var c = cams();
      try { return c && typeof c.favourites === 'function' ? c.favourites() : null; } catch (e) {
        return null;
      }
    }
    function note(text) { try { ctx.addIntelItem(text); } catch (e) { /* page feed only */ } }
    function on(target, type, fn, opts) {
      target.addEventListener(type, fn, opts);
      m.cleanup.push(function () {
        try { target.removeEventListener(type, fn, opts); } catch (e) { /* gone */ }
      });
    }

    // -- panel ------------------------------------------------------------

    m.root = el('div', 'panel');
    var head = el('h3', null, PANEL_TITLE);
    var body = el('div', 'panel-body');
    m.summary = el('div', 'cw-summary');
    var row = el('div', 'cw-row');
    m.openBtn = button('mode-btn', 'OPEN WALL', 'Play up to nine starred cameras at once');
    row.appendChild(m.openBtn);
    m.status = el('div', 'cw-status');
    m.status.setAttribute('role', 'status');
    m.list = el('div', 'cw-list');
    body.appendChild(m.summary);
    body.appendChild(row);
    body.appendChild(m.status);
    body.appendChild(m.list);
    m.root.appendChild(head);
    m.root.appendChild(body);
    head.addEventListener('click', function (e) {
      try { e.stopImmediatePropagation(); } catch (err) { /* keep going */ }
      m.root.classList.toggle('collapsed');
    });

    function setStatus(text) { m.status.textContent = text || ''; }

    function flyTo(cam) {
      if (!isNum(cam.lat) || !isNum(cam.lon) || !C) return;
      try {
        ctx.viewer.camera.flyTo({
          destination: C.Cartesian3.fromDegrees(cam.lon, cam.lat, 3000), duration: 1.6
        });
      } catch (e) { /* keep going */ }
    }

    function renderPanel() {
      var store = favs();
      m.list.replaceChildren();
      if (ctx.isStatic) {
        m.summary.textContent = 'The camera wall needs the live server (camera catalog).';
        m.openBtn.disabled = true;
        return;
      }
      if (!store) {
        m.summary.textContent = 'The camera wall needs the cameras module.';
        m.openBtn.disabled = true;
        return;
      }
      var list = store.list();
      m.openBtn.disabled = !list.length;
      m.summary.textContent = list.length
        ? list.length + ' starred camera' + (list.length === 1 ? '' : 's') + '; the wall plays ' +
          Math.min(MAX_TILES, list.length) + ' at a time.'
        : 'No starred cameras yet. Open a camera from the CCTV layer and press its star.';
      list.forEach(function (cam) {
        var item = el('div', 'cw-item');
        var name = button('cw-item-name', textOr(cam.name, 'Camera'),
          'Fly there and open this camera');
        var where = placeLine(cam);
        if (where) name.setAttribute('title', where);
        name.addEventListener('click', function () {
          flyTo(cam);
          var c = cams();
          if (c && typeof c.open === 'function') c.open(cam.rid);
        });
        var x = button('cw-x', 'X', 'Unstar');
        x.setAttribute('aria-label', 'Unstar ' + textOr(cam.name, 'camera'));
        x.addEventListener('click', function () { store.remove(cam.rid); });
        item.appendChild(name);
        item.appendChild(x);
        m.list.appendChild(item);
      });
    }

    // -- wall -------------------------------------------------------------

    function stopTiles() {
      m.tiles.forEach(function (t) { if (t.player) { try { t.player.stop(); } catch (e) { /* gone */ } } });
      m.tiles = [];
    }

    function close() {
      m.seq++;
      stopTiles();
      if (m.wall && m.wall.parentNode) m.wall.parentNode.removeChild(m.wall);
      m.wall = null;
    }

    function buildTile(fav, wallSeq) {
      var tile = el('div', 'cw-tile');
      var bar = el('div', 'cw-tile-head');
      var name = el('span', 'cw-tile-name', textOr(fav.name, 'Camera'));
      var where = placeLine(fav);
      if (where) name.setAttribute('title', where);
      var go = button('cw-btn', 'GO', 'Fly the globe to this camera');
      var solo = button('cw-btn', 'SOLO', 'Show only this camera; press again for the grid');
      var unstar = button('cw-btn', '\u2605', 'Unstar and take it off the wall');
      unstar.setAttribute('aria-label', 'Unstar ' + textOr(fav.name, 'camera'));
      bar.appendChild(name);
      bar.appendChild(go);
      bar.appendChild(solo);
      bar.appendChild(unstar);
      var media = el('div', 'cw-media');
      media.appendChild(el('div', 'cams-msg', 'Loading...'));
      var foot = el('div', 'cw-foot');
      tile.appendChild(bar);
      tile.appendChild(media);
      tile.appendChild(foot);
      var t = { rid: fav.rid, root: tile, player: null };
      go.addEventListener('click', function () { flyTo(fav); });
      solo.addEventListener('click', function () {
        var was = tile.classList.contains('cw-solo');
        m.tiles.forEach(function (o) { o.root.classList.remove('cw-solo'); });
        if (!was) tile.classList.add('cw-solo');
        if (m.wall) m.wall.classList.toggle('cw-solo-mode', !was);
      });
      unstar.addEventListener('click', function () {
        var store = favs();
        if (store) store.remove(fav.rid);
      });
      var c = cams();
      var fetched = c && typeof c.fetchCamera === 'function'
        ? c.fetchCamera(fav.rid) : Promise.reject(new Error('no cameras module'));
      fetched.then(function (cam) {
        if (wallSeq !== m.seq || m.tiles.indexOf(t) < 0) return;
        var place = placeLine(cam);
        if (place || cam.type) foot.appendChild(el('span', null, [place, cam.type].filter(Boolean).join(' | ')));
        t.player = c.playFeed(media, foot, cam, ctx.api || '');
        if (doc.hidden) t.player.pause();
      }).catch(function () {
        if (wallSeq !== m.seq) return;
        media.replaceChildren(el('div', 'cams-msg', 'This camera did not load.'));
      });
      return t;
    }

    function open(page) {
      var store = favs();
      if (ctx.isStatic) { setStatus('The camera wall needs the live server.'); return false; }
      if (!store) { setStatus('The camera wall needs the cameras module.'); return false; }
      var list = store.list();
      if (!list.length) {
        setStatus('Star a camera first: open one from the CCTV layer and press its star.');
        return false;
      }
      var full = !!(m.wall && m.wall.classList.contains('cw-full'));
      close();
      var slice = pageSlice(list, isNum(page) ? page : m.page);
      m.page = slice.page;
      var seq = m.seq;

      var wall = el('div', 'cw-wall' + (full ? ' cw-full' : ''));
      wall.setAttribute('role', 'dialog');
      wall.setAttribute('aria-label', 'Camera wall');
      var bar = el('div', 'cw-head');
      bar.appendChild(el('span', 'cw-title', 'CAMERA WALL  ' + slice.items.length + ' of ' +
        list.length + (slice.pages > 1 ? '  page ' + (slice.page + 1) + '/' + slice.pages : '')));
      var prev = button('cw-btn', 'PREV', 'The previous nine starred cameras');
      var next = button('cw-btn', 'NEXT', 'The next nine starred cameras');
      prev.disabled = slice.page === 0;
      next.disabled = slice.page >= slice.pages - 1;
      prev.addEventListener('click', function () { open(m.page - 1); });
      next.addEventListener('click', function () { open(m.page + 1); });
      var size = button('cw-btn', full ? 'SHRINK' : 'FULL', 'Wall over the whole window, or back');
      size.addEventListener('click', function () {
        var big = wall.classList.toggle('cw-full');
        size.textContent = big ? 'SHRINK' : 'FULL';
      });
      var shut = button('cw-btn', 'CLOSE', 'Close the wall (Esc)');
      shut.addEventListener('click', close);
      if (slice.pages > 1) { bar.appendChild(prev); bar.appendChild(next); }
      bar.appendChild(size);
      bar.appendChild(shut);

      var grid = el('div', 'cw-grid');
      var shape = gridFor(slice.items.length);
      grid.style.gridTemplateColumns = 'repeat(' + shape.cols + ', minmax(0, 1fr))';
      grid.style.gridTemplateRows = 'repeat(' + shape.rows + ', minmax(0, 1fr))';
      wall.appendChild(bar);
      wall.appendChild(grid);
      doc.body.appendChild(wall);
      m.wall = wall;
      slice.items.forEach(function (fav) {
        var t = buildTile(fav, seq);
        m.tiles.push(t);
        grid.appendChild(t.root);
      });
      note('Camera wall: ' + slice.items.length + ' starred camera' +
        (slice.items.length === 1 ? '' : 's'));
      return true;
    }

    // A favourite removed while the wall is open leaves it; the rest keep playing.
    function onFavsChanged(list) {
      renderPanel();
      if (!m.wall) return;
      var keep = {};
      list.forEach(function (f) { keep[f.rid] = true; });
      var gone = m.tiles.filter(function (t) { return !keep[t.rid]; });
      if (!gone.length) return;
      if (!list.length) { close(); return; }
      open(m.page);
    }

    function onVisibility() {
      m.tiles.forEach(function (t) {
        if (!t.player) return;
        try { if (doc.hidden) t.player.pause(); else t.player.resume(); } catch (e) { /* next */ }
      });
    }

    function onKey(e) {
      if (!m.wall || e.key !== 'Escape') return;
      close();
      try { e.preventDefault(); e.stopPropagation(); } catch (err) { /* keep going */ }
    }

    m.openBtn.addEventListener('click', function () { open(0); });
    on(doc, 'visibilitychange', onVisibility);
    on(doc, 'keydown', onKey, true);
    var store = favs();
    if (store && typeof store.onChange === 'function') {
      var off = store.onChange(onFavsChanged);
      m.cleanup.push(off);
    }
    renderPanel();

    function destroy() {
      close();
      m.cleanup.splice(0).forEach(function (fn) { try { fn(); } catch (e) { /* gone */ } });
      if (m.root.parentNode) m.root.parentNode.removeChild(m.root);
    }

    return {
      root: m.root,
      open: open,
      close: close,
      isOpen: function () { return !!m.wall; },
      tiles: function () { return m.tiles.slice(); },
      refresh: renderPanel,
      destroy: destroy
    };
  }

  var instance = null;

  function init(ctx) {
    try {
      if (!ctx || !ctx.mount || typeof ctx.mount.appendChild !== 'function' || !global.document) {
        return false;
      }
      if (instance) instance.destroy();
      injectStyle(global.document);
      instance = createModule(ctx);
      ctx.mount.appendChild(instance.root);
      return true;
    } catch (err) {
      instance = null;
      return false;
    }
  }

  global.SDACamWall = {
    init: init,
    open: function (page) { return instance ? instance.open(page) : false; },
    close: function () { if (instance) instance.close(); },
    toggle: function () {
      if (!instance) return false;
      if (instance.isOpen()) { instance.close(); return false; }
      return instance.open();
    },
    isOpen: function () { return !!(instance && instance.isOpen()); },
    refresh: function () { if (instance) instance.refresh(); },
    destroy: function () { if (instance) instance.destroy(); instance = null; },
    MAX_TILES: MAX_TILES,
    gridFor: gridFor,
    pageCount: pageCount,
    pageSlice: pageSlice
  };
})(typeof window !== 'undefined' ? window : this);
