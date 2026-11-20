"""Tests for the camera wall (static/js/camwall.js) and the star in cameras.js, under Node."""

from __future__ import annotations

import pytest

from tests import globe_ux_harness as h

pytestmark = pytest.mark.skipif(h.NODE is None, reason="node is not installed")

# The DOM stub keeps `src` as a plain property; a browser reflects it into the
# attribute, which is what removeAttribute('src') (pausing an MJPEG still) clears.
EXTRA = r"""
Object.defineProperty(El.prototype, 'src', {
  get: function () { return this.attrs.src || ''; },
  set: function (v) { this.attrs.src = String(v); }
});
var FEEDS = {
  image: function (rid) { return { feed: 'image',
    feed_url: 'https://cams.example.org/' + rid + '.jpg',
    refresh_s: 30 }; },
  mjpeg: function (rid) { return { feed: 'mjpeg',
    feed_url: 'https://cams.example.org/' + rid + '.mjpg' }; },
  youtube: function () { return { feed: 'youtube', feed_url: 'dQw4w9WgXcQ' }; },
  mp4: function (rid) { return { feed: 'mp4',
    feed_url: 'https://cams.example.org/' + rid + '.mp4' }; }
};
// Seed the starred list the way cameras.js stores it, and serve each camera.
function seed(kinds) {
  var favs = kinds.map(function (k, i) {
    return { rid: i + 1, name: 'Cam ' + (i + 1), lat: 39 + i / 10, lon: -105 + i / 10,
      group: 'traffic', city: 'Denver', country: 'US' };
  });
  window.localStorage = new FakeStorage();
  window.localStorage.setItem('sda.cameras.v1.favourites', JSON.stringify(favs));
  route('/api/cameras/', function (url) {
    var rid = Number(String(url).split('/').pop());
    var rec = FEEDS[kinds[rid - 1]](rid);
    rec.rid = rid; rec.name = 'Cam ' + rid; rec.lat = 39; rec.lon = -105; rec.city = 'Denver';
    rec.country = 'US'; rec.type = 'traffic'; rec.source = 'test';
    return rec;
  });
  return favs;
}
function wallEl() { return byClass(document.body, 'cw-wall')[0] || null; }
function tiles() { return byClass(document.body, 'cw-tile'); }
function stored() {
  return JSON.parse(window.localStorage.getItem('sda.cameras.v1.favourites') || '[]')
    .map(function (f) { return f.rid; });
}
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> h.Ran:
    return h.run(tmp_path_factory.mktemp("camwall"), ["cameras.js", "camwall.js"], SCENARIOS,
                 extra_prelude=EXTRA)


scenario("helpers", r"""
  var W = window.SDACamWall;
  var twenty = []; for (var i = 0; i < 20; i++) twenty.push(i);
  var s = W.pageSlice(twenty, 2);
  return {
    grids: [0, 1, 2, 3, 4, 5, 9, 12].map(function (n) { var g = W.gridFor(n); return [g.cols,
      g.rows]; }),
    pages: [0, 9, 10, 18, 19].map(W.pageCount),
    slice: [s.page, s.pages, s.items],
    clampHigh: W.pageSlice(twenty, 99).page, clampLow: W.pageSlice(twenty, -1).page,
    max: W.MAX_TILES
  };
""")


def test_grid_and_paging_helpers(ran):
    r = ran["helpers"]
    assert r["grids"] == [[0, 0], [1, 1], [2, 1], [2, 2], [2, 2], [3, 2], [3, 3], [3, 3]]
    assert r["pages"] == [1, 1, 2, 2, 3]
    assert r["slice"] == [2, 3, [18, 19]]
    assert r["clampHigh"] == 2 and r["clampLow"] == 0
    assert r["max"] == 9


scenario("panel", r"""
  seed(['image', 'mjpeg', 'youtube']);
  var k = makeCtx({});
  var opened = [];
  window.SDACameras.open = function (rid) { opened.push(rid); };
  var ok = window.SDACamWall.init(k.ctx);
  var names = byClass(k.ctx.mount, 'cw-item-name').map(function (b) { return b.textContent; });
  var summary = byClass(k.ctx.mount, 'cw-summary')[0].textContent;
  var openDisabled = byText(k.ctx.mount, 'OPEN WALL')[0].disabled;
  byClass(k.ctx.mount, 'cw-item-name')[1].fire('click');
  var flown = k.ctx.viewer.camera.flown.destination;
  byClass(k.ctx.mount, 'cw-x')[0].fire('click');
  return { ok: ok, names: names, summary: summary, openDisabled: openDisabled, opened: opened,
    flown: [flown.lon, flown.lat],
    after: byClass(k.ctx.mount, 'cw-item-name').map(function (b) { return b.textContent; }),
    stored: stored(), wallOpen: window.SDACamWall.isOpen() };
""")


def test_panel_lists_starred_cameras(ran):
    r = ran["panel"]
    assert r["ok"] is True
    assert r["names"] == ["Cam 1", "Cam 2", "Cam 3"]
    assert r["summary"].startswith("3 starred cameras; the wall plays 3 at a time")
    assert r["openDisabled"] is False
    assert r["opened"] == [2] and r["flown"] == [-104.9, 39.1]
    assert r["after"] == ["Cam 2", "Cam 3"] and r["stored"] == [2, 3]
    assert r["wallOpen"] is False


scenario("open_wall", r"""
  seed(['image', 'mjpeg', 'youtube', 'image', 'mjpeg', 'youtube', 'image', 'mjpeg', 'youtube',
        'image']);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  var opened = window.SDACamWall.open();
  await settle();
  var wall = wallEl();
  var grid = byClass(wall, 'cw-grid')[0];
  var imgs = byTag(wall, 'IMG').map(function (i) { return i.getAttribute('src'); });
  var frames = byTag(wall, 'IFRAME').map(function (f) { return f.getAttribute('src'); });
  var first = {
    opened: opened, tiles: tiles().length, title: byClass(wall, 'cw-title')[0].textContent,
    cols: grid.style.gridTemplateColumns, rows: grid.style.gridTemplateRows,
    prevDisabled: byText(wall, 'PREV')[0].disabled, nextDisabled: byText(wall, 'NEXT')[0].disabled,
    fetched: requested('/api/cameras/').length, imgs: imgs, frames: frames,
    links: byTag(wall, 'A').length, feed: k.feed.slice()
  };
  byText(wall, 'NEXT')[0].fire('click');
  await settle();
  var second = { walls: byClass(document.body, 'cw-wall').length, tiles: tiles().length,
    title: byClass(wallEl(), 'cw-title')[0].textContent,
    cols: byClass(wallEl(), 'cw-grid')[0].style.gridTemplateColumns,
    fetched: requested('/api/cameras/').length };
  byText(wallEl(), 'FULL')[0].fire('click');
  var full = wallEl().has('cw-full');
  byText(wallEl(), 'PREV')[0].fire('click');
  await settle();
  return { first: first, second: second, full: full, keptFull: wallEl().has('cw-full'),
    backTiles: tiles().length };
""")


def test_wall_plays_nine_tiles_and_pages(ran):
    r = ran["open_wall"]
    f = r["first"]
    assert f["opened"] is True and f["tiles"] == 9
    assert f["title"] == "CAMERA WALL  9 of 10  page 1/2"
    assert f["cols"] == "repeat(3, minmax(0, 1fr))" and f["rows"] == "repeat(3, minmax(0, 1fr))"
    assert f["prevDisabled"] is True and f["nextDisabled"] is False
    assert f["fetched"] == 9
    # Stills are cache-busted, MJPEG streams are used as given, YouTube is the nocookie embed.
    assert len(f["imgs"]) == 6
    assert sum(1 for s in f["imgs"] if s.endswith(".mjpg")) == 3
    assert sum(1 for s in f["imgs"] if ".jpg?sda_t=" in s) == 3
    assert len(f["frames"]) == 3
    embed = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert all(s.startswith(embed) for s in f["frames"])
    assert f["links"] == 9  # every tile keeps its source link
    assert f["feed"] == ["Camera wall: 9 starred cameras"]
    s = r["second"]
    assert s["walls"] == 1 and s["tiles"] == 1
    assert s["title"] == "CAMERA WALL  1 of 10  page 2/2"
    assert s["cols"] == "repeat(1, minmax(0, 1fr))"
    assert s["fetched"] == 10
    assert r["full"] is True and r["keptFull"] is True and r["backTiles"] == 9


scenario("hidden_pauses", r"""
  seed(['image', 'mjpeg', 'youtube', 'mp4']);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  window.SDACamWall.open();
  await settle();
  function state() {
    var t = tiles();
    return {
      still: byTag(t[0], 'IMG')[0].hasAttribute('src'),
      mjpeg: byTag(t[1], 'IMG')[0].getAttribute('src'),
      yt: byTag(t[2], 'IFRAME')[0].getAttribute('src'),
      videoPaused: !!byTag(t[3], 'VIDEO')[0].paused
    };
  }
  var shown = state();
  setHidden(true);
  var hidden = state();
  setHidden(false);
  var back = state();
  // A wall opened while the tab is hidden starts paused.
  window.SDACamWall.close();
  setHidden(true);
  window.SDACamWall.open();
  await settle();
  var openedHidden = state();
  setHidden(false);
  return { shown: shown, hidden: hidden, back: back, openedHidden: openedHidden };
""")


def test_hidden_tab_pauses_every_tile(ran):
    r = ran["hidden_pauses"]
    assert r["shown"]["mjpeg"] == "https://cams.example.org/2.mjpg"
    assert r["shown"]["yt"].startswith("https://www.youtube-nocookie.com/embed/")
    assert r["shown"]["videoPaused"] is False
    assert r["hidden"] == {"still": True, "mjpeg": None, "yt": "about:blank", "videoPaused": True}
    assert r["back"] == r["shown"]
    assert r["openedHidden"]["mjpeg"] is None and r["openedHidden"]["yt"] == "about:blank"
    assert r["openedHidden"]["videoPaused"] is True


scenario("unstar", r"""
  seed(['image', 'mjpeg', 'youtube', 'image']);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  window.SDACamWall.open();
  await settle();
  var star = byText(tiles()[1], '\u2605')[0];
  star.fire('click');
  await settle();
  var afterTile = { tiles: tiles().map(function (t) { return byClass(t,
    'cw-tile-name')[0].textContent; }),
    stored: stored(), panel: byClass(k.ctx.mount, 'cw-item').length,
    walls: byClass(document.body, 'cw-wall').length };
  window.SDACameras.favourites().clear();
  return { afterTile: afterTile, open: window.SDACamWall.isOpen(),
    walls: byClass(document.body, 'cw-wall').length,
    openDisabled: byText(k.ctx.mount, 'OPEN WALL')[0].disabled,
    reopen: window.SDACamWall.open(),
    status: byClass(k.ctx.mount, 'cw-status')[0].textContent };
""")


def test_unstar_takes_a_tile_off_and_an_empty_list_closes(ran):
    r = ran["unstar"]
    a = r["afterTile"]
    assert a["tiles"] == ["Cam 1", "Cam 3", "Cam 4"]
    assert a["stored"] == [1, 3, 4] and a["panel"] == 3 and a["walls"] == 1
    assert r["open"] is False and r["walls"] == 0
    assert r["openDisabled"] is True and r["reopen"] is False
    assert r["status"].startswith("Star a camera first")


scenario("keys", r"""
  seed(['image', 'mjpeg']);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  var idle = key('Escape');
  var toggledOn = window.SDACamWall.toggle();
  await settle();
  var esc = key('Escape');
  var afterEsc = window.SDACamWall.isOpen();
  window.SDACamWall.toggle();
  var toggledOff = window.SDACamWall.toggle();
  byText(k.ctx.mount, 'OPEN WALL')[0].fire('click');
  var byButton = window.SDACamWall.isOpen();
  byText(wallEl(), 'CLOSE')[0].fire('click');
  return { idlePrevented: !!idle.defaultPrevented, toggledOn: toggledOn,
    escPrevented: !!esc.defaultPrevented, afterEsc: afterEsc, toggledOff: toggledOff,
    byButton: byButton, closed: window.SDACamWall.isOpen(),
    walls: byClass(document.body, 'cw-wall').length };
""")


def test_escape_toggle_and_buttons(ran):
    assert ran["keys"] == {
        "idlePrevented": False, "toggledOn": True, "escPrevented": True, "afterEsc": False,
        "toggledOff": False, "byButton": True, "closed": False, "walls": 0,
    }


scenario("solo", r"""
  seed(['image', 'mjpeg', 'youtube']);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  window.SDACamWall.open();
  await settle();
  byText(tiles()[2], 'SOLO')[0].fire('click');
  var on = { wall: wallEl().has('cw-solo-mode'),
    solo: tiles().map(function (t) { return t.has('cw-solo'); }) };
  byText(tiles()[2], 'SOLO')[0].fire('click');
  byText(tiles()[0], 'GO')[0].fire('click');
  var d = k.ctx.viewer.camera.flown.destination;
  return { on: on, off: wallEl().has('cw-solo-mode'), go: [d.lon, d.lat, d.h] };
""")


def test_solo_and_go(ran):
    r = ran["solo"]
    assert r["on"] == {"wall": True, "solo": [False, False, True]}
    assert r["off"] is False
    assert r["go"] == [-105, 39, 3000]


scenario("tile_fails", r"""
  seed(['image', 'mjpeg']);
  route('/api/cameras/2', {}, 500);
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  window.SDACamWall.open();
  await settle();
  return tiles().map(function (t) { return byClass(t, 'cams-msg').map(function (m) {
    return m.textContent; }).concat(byTag(t, 'IMG').length ? ['img'] : []); });
""")


def test_a_camera_that_fails_says_so_and_the_rest_play(ran):
    assert ran["tile_fails"] == [["img"], ["This camera did not load."]]


scenario("static_mode", r"""
  seed(['image']);
  var k = makeCtx({ isStatic: true });
  window.SDACamWall.init(k.ctx);
  var summary = byClass(k.ctx.mount, 'cw-summary')[0].textContent;
  var opened = window.SDACamWall.open();
  return { summary: summary, opened: opened, requests: REQUESTS.length,
    disabled: byText(k.ctx.mount, 'OPEN WALL')[0].disabled };
""")


def test_static_demo_needs_the_server(ran):
    r = ran["static_mode"]
    assert r["summary"] == "The camera wall needs the live server (camera catalog)."
    assert r["opened"] is False and r["requests"] == 0 and r["disabled"] is True


scenario("no_cameras_module", r"""
  delete window.SDACameras;
  var k = makeCtx({});
  var ok = window.SDACamWall.init(k.ctx);
  return { ok: ok, summary: byClass(k.ctx.mount, 'cw-summary')[0].textContent,
    opened: window.SDACamWall.open(), bad: window.SDACamWall.init({}) };
""")


def test_without_the_cameras_module_the_wall_says_so(ran):
    r = ran["no_cameras_module"]
    assert r["ok"] is True
    assert r["summary"] == "The camera wall needs the cameras module."
    assert r["opened"] is False and r["bad"] is False


scenario("star_on_card", r"""
  seed(['youtube', 'image']);
  window.localStorage.setItem('sda.cameras.v1.favourites', '[]');
  var k = makeCtx({});
  var camOk = window.SDACameras.init(k.ctx);
  var wallMount = new El('div'); document.body.appendChild(wallMount);
  var wallCtx = Object.assign({}, k.ctx, { mount: wallMount });
  window.SDACamWall.init(wallCtx);
  window.SDACameras.open(2);
  await settle();
  var frame = byClass(document.body, 'cams-frame')[0];
  var star = byClass(frame, 'cams-star')[0];
  var before = [star.textContent, star.getAttribute('aria-pressed')];
  star.fire('click');
  var after = [star.textContent, star.getAttribute('aria-pressed')];
  var starred = byText(k.ctx.mount, 'STARRED 1').length;
  var panel = byClass(wallMount, 'cw-item-name').map(function (b) { return b.textContent; });
  var saved = JSON.parse(window.localStorage.getItem('sda.cameras.v1.favourites'));
  byText(k.ctx.mount, 'WALL')[0].fire('click');
  await settle();
  var wallOpen = window.SDACamWall.isOpen();
  star.fire('click');
  return { camOk: camOk, before: before, after: after, starred: starred, panel: panel,
    saved: saved, wallOpen: wallOpen, unstarred: star.textContent,
    wallAfterUnstar: window.SDACamWall.isOpen() };
""")


def test_star_on_the_camera_card_feeds_the_wall(ran):
    r = ran["star_on_card"]
    assert r["camOk"] is True
    assert r["before"] == ["\u2606", "false"] and r["after"] == ["\u2605", "true"]
    assert r["starred"] == 1 and r["panel"] == ["Cam 2"]
    # Only identity and place are kept; never a feed URL.
    assert r["saved"] == [{"rid": 2, "name": "Cam 2", "lat": 39, "lon": -105, "group": "",
                           "city": "Denver", "country": "US"}]
    assert r["wallOpen"] is True
    assert r["unstarred"] == "\u2606" and r["wallAfterUnstar"] is False


scenario("panel_collapse", r"""
  var k = makeCtx({});
  window.SDACamWall.init(k.ctx);
  var panel = byClass(k.ctx.mount, 'panel')[0];
  var head = byTag(panel, 'H3')[0];
  // The page binds its own collapse toggle after init; the panel must still toggle once.
  head.addEventListener('click', function () { panel.classList.toggle('collapsed'); });
  head.fire('click');
  return panel.has('collapsed');
""")


def test_panel_head_toggles_once_alongside_the_page_binding(ran):
    assert ran["panel_collapse"] is True
