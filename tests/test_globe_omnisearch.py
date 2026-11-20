"""Tests for the universal search box (static/js/omnisearch.js), run under Node.

The real satsearch.js (rank) and maptools.js (parseCoordinate) are loaded, so
the module is tested against the code it reuses, not against stand-ins.
"""

from __future__ import annotations

import pytest

from tests import globe_ux_harness as h

pytestmark = pytest.mark.skipif(h.NODE is None, reason="node is not installed")

EXTRA = r"""
// The page's "/" box as worldview.html builds it, with the page's own listener.
function pageBox() {
  var box = new El('div'); box.id = 'search-box'; box.className = 'visible';
  var inp = new El('input'); inp.id = 'search-input';
  inp.setAttribute('placeholder', 'Search location... (ESC to close)');
  var calls = [];
  inp.addEventListener('input', function () { calls.push('page input'); });
  var res = new El('div'); res.id = 'search-results';
  box.appendChild(inp); box.appendChild(res); document.body.appendChild(box);
  return { box: box, old: inp, res: res, calls: calls };
}
var SATS = [
  { noradId: 25544, name: 'ISS (ZARYA)', lastLat: 10, lastLon: 20, lastAlt: 420, regime: 'LEO' },
  { noradId: 48274, name: 'CSS (TIANHE)', lastLat: -5, lastLon: 100, lastAlt: 390, regime: 'LEO' },
  { noradId: 20580, name: 'HST', lastLat: 1, lastLon: 2, lastAlt: 530, regime: 'LEO' }
];
function engine() {
  var by = {}; SATS.forEach(function (s) { by[s.noradId] = s; });
  return { sats: SATS, byNoradId: by, selectedId: null };
}
function row(kind, id, name, tier, extra) {
  return { kind: kind, id: String(id), name: name, detail: '', tier: tier, lat: 39.74,
    lon: -104.99,
    alt_m: null, extra: extra || {} };
}
// Server answers: places only when asked for them.
function serve(extraRows) {
  route('/api/search', function (url) {
    var rows = [row('camera', 42, 'Issaquah Hwy 900', 1, { rid: 42 }),
                row('satellite', 25544, 'ISS (ZARYA)', 1, { norad_id: 25544 }),
                row('camera', 7, 'Denver Union Station', 1, { rid: 7 })].concat(extraRows || []);
    if (String(url).indexOf('places=1') >= 0) rows.push(row('place', 'place-0', 'Denver', 0));
    var q = decodeURIComponent(String(url).split('q=')[1].split('&')[0]).toLowerCase();
    rows = rows.filter(function (r) { return r.name.toLowerCase().indexOf(q) >= 0; });
    return { query: q, results: rows, count: rows.length, sources: {} };
  });
}
function names(list) {
  return list.children.map(function (c) {
    var t = byClass(c, 'sr-type')[0];
    return byClass(c, 'sr-name')[0].textContent + (t ? ' [' + t.textContent + ']' : '');
  });
}
async function type(input, text) { input.value = text; input.fire('input'); await wait(230);
  await settle(); }
async function press(input, k) { var ev = input.fire('keydown', { key: k }); await settle();
  return ev; }
function boxInput() { return document.getElementById('search-input'); }
"""

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> h.Ran:
    return h.run(tmp_path_factory.mktemp("omnisearch"),
                 ["satsearch.js", "maptools.js", "omnisearch.js"], SCENARIOS, extra_prelude=EXTRA)


scenario("helpers", r"""
  var O = window.SDAOmniSearch;
  var ac = { icao24: 'a1b2c3', callsign: 'UAL123', registration: 'N12345' };
  var many = [];
  for (var i = 0; i < 10; i++) many.push({ kind: 'place', id: 'p' + i, tier: 0 });
  many.push({ kind: 'satellite', id: 's', tier: 3 });
  return {
    fold: [O.fold('Zuerich-Flughafen'), O.fold('Z\u00fcrich  Hbf.'), O.fold(null)],
    tiers: [['iss zarya', 'iss zarya'], ['iss zarya', 'iss'], ['iss zarya', 'zar'],
            ['iss zarya', 'ary'], ['iss', 'hst']].map(function (p) { return O.nameTier(p[0],
              p[1]); }),
    shapes: ['25544', 'A0001', 'UAL123', 'N12345', 'a1b2c3', 'Denver'].map(function (q) {
      var s = O.queryShape(q);
      return Object.keys(s).filter(function (k) { return s[k]; }); }),
    orders: ['25544', 'UAL123', 'Denver'].map(function (q) { return O.kindOrder(q)[1]; }),
    aircraft: ['A1B2C3', 'ual123', 'ual1', 'L12', 'n12345', 'n1', 'zz'].map(function (q) {
      return O.aircraftTier(ac, q); }),
    merged: O.mergeResults(many, 'denver', 4).map(function (r) { return r.kind; }),
    url: O.searchUrl('', 'a b&c', { full: true }), typingUrl: O.searchUrl('/x', 'iss', {}),
    standoff: [O.standoffKm(420), O.standoffKm(35786), O.standoffKm(20200)]
  };
""")


def test_helpers_mirror_the_server(ran):
    r = ran["helpers"]
    assert r["fold"] == ["zuerich flughafen", "zurich hbf", ""]
    assert r["tiers"] == [0, 1, 2, 3, -1]
    assert r["shapes"] == [["digits"], ["alpha5"], ["callsign"], ["registration"], ["hex"], []]
    assert r["orders"] == ["satellite", "aircraft", "place"]
    assert r["aircraft"] == [0, 0, 1, 3, 0, 1, -1]
    # Ten exact places cannot push the one satellite out: each kind keeps a share.
    assert r["merged"] == ["place", "place", "place", "satellite"]
    assert r["url"] == "/api/search?q=a%20b%26c&limit=12&places=1&live=1"
    assert r["typingUrl"] == "/x/api/search?q=iss&limit=12&places=0&live=0"
    assert r["standoff"] == [1200, 21471.6, 12120]


scenario("adopts_page_box", r"""
  var p = pageBox();
  var k = makeCtx({ satEngine: engine() });
  var ok = window.SDAOmniSearch.init(k.ctx);
  var fresh = boxInput();
  var swapped = fresh !== p.old && fresh.parentNode === p.box;
  fresh.value = 'iss'; fresh.fire('input');
  await wait(230); await settle();
  var during = { pageCalls: p.calls.slice(), placeholder: fresh.getAttribute('placeholder'),
    panel: byClass(k.ctx.mount, 'os-input').length };
  window.SDAOmniSearch.destroy();
  return { ok: ok, swapped: swapped, during: during, restored: boxInput() === p.old,
    panelGone: byClass(document.body, 'os-input').length,
    statusGone: byClass(p.box, 'os-box-status').length };
""")


def test_adopts_the_page_box_and_gives_it_back(ran):
    r = ran["adopts_page_box"]
    assert r["ok"] is True and r["swapped"] is True
    assert r["during"]["pageCalls"] == []  # the page's per-keystroke geocoder is gone
    assert r["during"]["placeholder"].startswith("Places, satellites, aircraft")
    assert r["during"]["panel"] == 1
    assert r["restored"] is True and r["panelGone"] == 0 and r["statusGone"] == 0


scenario("typing", r"""
  var p = pageBox();
  serve();
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  await type(boxInput(), 'iss');
  return { requests: requested('/api/search'), rows: names(p.res),
    active: byClass(p.res, 'os-active').length,
    status: byClass(p.box, 'os-box-status')[0].textContent,
    oneChar: (await (async function () { REQUESTS.length = 0; await type(boxInput(), 'i');
      return { requests: REQUESTS.length, rows: p.res.children.length }; })()) };
""")


def test_typing_asks_without_places_and_prefers_live_satellites(ran):
    r = ran["typing"]
    assert r["requests"] == ["/api/search?q=iss&limit=12&places=0&live=0"]
    # Camera and satellite tie on tier (prefix); a plain word puts cameras first.
    # The server's ISS is dropped: the globe's own record has the live position.
    assert r["rows"] == ["Issaquah Hwy 900 [CAMERA]", "ISS (ZARYA) [SAT]",
      'Search places for "iss"']
    assert r["active"] == 1
    assert r["status"] == "2 results"
    assert r["oneChar"] == {"requests": 0, "rows": 0}


scenario("enter_widens_then_flies", r"""
  var p = pageBox();
  serve();
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  var input = boxInput();
  await type(input, 'denver');
  var typed = names(p.res);
  var ev = await press(input, 'Enter');
  var widened = names(p.res);
  await press(input, 'Enter');
  var d = k.ctx.viewer.camera.flown.destination;
  return { typed: typed, widened: widened, prevented: !!ev.defaultPrevented,
    stopped: !!ev.stopped, requests: requested('/api/search'),
    flown: [d.lon, d.lat, d.h], visible: p.box.has('visible'), feed: k.feed };
""")


def test_enter_widens_to_places_then_flies(ran):
    r = ran["enter_widens_then_flies"]
    assert r["typed"] == ["Denver Union Station [CAMERA]", 'Search places for "denver"']
    assert r["widened"] == ["Denver [PLACE]", "Denver Union Station [CAMERA]"]
    assert r["prevented"] is True and r["stopped"] is True
    assert r["requests"] == ["/api/search?q=denver&limit=12&places=0&live=0",
                             "/api/search?q=denver&limit=12&places=1&live=1"]
    assert r["flown"] == [-104.99, 39.74, 50000]
    assert r["visible"] is False
    assert r["feed"] == ["Search: flying to Denver"]


scenario("exact_id_flies_at_once", r"""
  var p = pageBox();
  serve();
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  var input = boxInput();
  await type(input, '25544');
  var rows = names(p.res);
  await press(input, 'Enter');
  var d = k.ctx.viewer.camera.flown.destination;
  return { rows: rows, selected: k.ctx.selected, flown: [d.lon, d.lat, d.h],
    cancelled: k.ctx.viewer.camera.cancelled, feed: k.feed,
      requests: requested('/api/search').length };
""")


def test_exact_norad_id_selects_and_flies_on_enter(ran):
    r = ran["exact_id_flies_at_once"]
    assert r["rows"][0] == "ISS (ZARYA) [SAT]"
    assert r["selected"] == [25544]
    assert r["flown"] == [20, 10, 1620000]  # 420 km plus the 1,200 km standoff
    assert r["cancelled"] == 1
    assert r["feed"] == ["Search: ISS (ZARYA) (NORAD 25544), 420 km"]
    assert r["requests"] == 1  # no widening round trip for an exact hit


scenario("arrows_open_camera", r"""
  var p = pageBox();
  serve();
  var opened = [];
  window.SDACameras = { open: function (rid) { opened.push(rid); } };
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  var input = boxInput();
  await type(input, 'iss');
  await press(input, 'ArrowDown');
  await press(input, 'ArrowUp');
  var active = byClass(p.res, 'os-active').map(function (c) { return byClass(c,
    'sr-name')[0].textContent; });
  await press(input, 'Enter');
  var d = k.ctx.viewer.camera.flown.destination;
  return { active: active, opened: opened, flown: [d.lon, d.lat, d.h], feed: k.feed };
""")


def test_arrow_keys_choose_and_a_camera_opens(ran):
    r = ran["arrows_open_camera"]
    assert r["active"] == ["Issaquah Hwy 900"]
    assert r["opened"] == [42]
    assert r["flown"] == [-104.99, 39.74, 3000]
    assert r["feed"] == ["Search: camera Issaquah Hwy 900"]


scenario("peak_and_aircraft", r"""
  var summit = [];
  window.SDASummit = { open: function (p) { summit.push(p); } };
  var plane = { id: 'flight-a1b2c3', name: 'UAL123 ',
    properties: { type: { getValue: function () { return 'aircraft'; } }, icao24: 'a1b2c3',
      origin_country: 'United States' },
    position: { getValue: function () { return { lon: -104.7, lat: 39.9, h: 10000 }; } } };
  var other = { id: 'sat-1', name: 'UAL123 lookalike', properties: { type: 'satellite' },
    position: { getValue: function () { return { lon: 0, lat: 0, h: 0 }; } } };
  var k = makeCtx({ isStatic: true, entities: [plane, other] });
  window.SDAOmniSearch.init(k.ctx);
  var res = await window.SDAOmniSearch.search('ual12');
  window.SDAOmniSearch.pick(res.results[0]);
  var d1 = k.ctx.viewer.camera.flown.destination;
  window.SDAOmniSearch.pick({ kind: 'peak', id: '9', name: 'Mount Elbert', tier: 0, lat: 39.1178,
    lon: -106.4454, extra: { peak_id: 9, elev_m: 4401, prom_m: 2772 } });
  var d2 = k.ctx.viewer.camera.flown;
  return { rows: res.results.map(function (r) { return [r.kind, r.name, r.tier, r.detail]; }),
    selected: k.ctx.viewer.selectedEntity && k.ctx.viewer.selectedEntity.id,
    plane: [d1.lon, d1.lat, d1.h], peak: [d2.destination.h,
      Math.round(d2.orientation.pitch * 180 / Math.PI)],
    summit: summit, feed: k.feed, requests: REQUESTS.length };
""")


def test_aircraft_on_the_globe_and_peaks(ran):
    r = ran["peak_and_aircraft"]
    assert r["rows"] == [["aircraft", "UAL123", 1, "A1B2C3 | United States"]]
    assert r["selected"] == "flight-a1b2c3"
    assert r["plane"] == [-104.7, 39.9, 50000]
    assert r["peak"] == [13401, -35]
    assert r["summit"] == [{"id": 9, "name": "Mount Elbert", "lat": 39.1178, "lon": -106.4454,
                            "elev_m": 4401, "prom_m": 2772}]
    assert r["feed"] == ["Search: aircraft UAL123", "Search: peak Mount Elbert (4401 m)"]
    assert r["requests"] == 0  # static demo: no server


scenario("static_coordinates_and_places", r"""
  var p = pageBox();
  var went = [];
  window.SDAMapTools.instance = function () { return {}; };
  window.SDAMapTools.goTo = function (q) { went.push(q); return Promise.resolve(null); };
  var k = makeCtx({ isStatic: true, satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  var coord = await window.SDAOmniSearch.search('39.7392, -104.9903');
  var mgrs = await window.SDAOmniSearch.search('13S EE 0129 0050');
  var input = boxInput();
  await type(input, 'Denver');
  var rows = names(p.res);
  await press(input, 'Enter');
  return { coord: coord.results.map(function (r) { return [r.kind, r.lat, r.lon]; }),
    mgrs: mgrs.results.length && mgrs.results[0].kind, rows: rows, went: went,
    visible: p.box.has('visible'), requests: REQUESTS.length };
""")


def test_static_demo_uses_map_tools_for_coordinates_and_places(ran):
    r = ran["static_coordinates_and_places"]
    assert r["coord"] == [["coordinate", 39.7392, -104.9903]]
    assert r["mgrs"] == "coordinate"
    assert r["rows"] == ['Search places for "Denver"']
    assert r["went"] == ["Denver"]
    assert r["visible"] is False
    assert r["requests"] == 0


scenario("server_missing_or_failing", r"""
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  var a = await window.SDAOmniSearch.search('hst');
  var b = await window.SDAOmniSearch.search('css');
  var gone = requested('/api/search').length;
  window.SDAOmniSearch.destroy();
  REQUESTS.length = 0;
  route('/api/search', {}, 500);
  var p = pageBox();
  var k2 = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k2.ctx);
  await type(boxInput(), 'hst');
  return { a: a.results.map(function (r) { return r.name; }), aServer: a.server,
    b: b.results.map(function (r) { return r.name; }), gone: gone,
    failRows: names(p.res), failStatus: byClass(p.box, 'os-box-status')[0].textContent };
""")


def test_without_the_server_local_sources_still_answer(ran):
    r = ran["server_missing_or_failing"]
    assert r["a"] == ["HST"] and r["aServer"] is False
    assert r["b"] == ["CSS (TIANHE)"]
    assert r["gone"] == 1  # a 404 means the static build: never asked again
    assert r["failRows"][0] == "HST [SAT]"
    assert r["failStatus"] == "1 result; the search service did not answer"


scenario("escape_and_panel", r"""
  var p = pageBox();
  serve();
  var k = makeCtx({ satEngine: engine() });
  window.SDAOmniSearch.init(k.ctx);
  await type(boxInput(), 'iss');
  var esc = await press(boxInput(), 'Escape');
  var boxClosed = !p.box.has('visible');
  var panelInput = byClass(k.ctx.mount, 'os-input')[0];
  await type(panelInput, 'hst');
  var panelRows = names(byClass(k.ctx.mount, 'os-list')[0]);
  await press(panelInput, 'Escape');
  var afterFirstEsc = [panelInput.value, byClass(k.ctx.mount, 'os-list')[0].children.length];
  var focused = window.SDAOmniSearch.focus();
  return { escPrevented: !!esc.defaultPrevented, boxClosed: boxClosed, panelRows: panelRows,
    afterFirstEsc: afterFirstEsc, focused: focused, reopened: p.box.has('visible'),
    active: document.activeElement === boxInput(), bad: window.SDAOmniSearch.init({
      mount: new El('div') }) };
""")


def test_escape_closes_box_and_clears_panel(ran):
    r = ran["escape_and_panel"]
    assert r["escPrevented"] is True and r["boxClosed"] is True
    assert r["panelRows"] == ["HST [SAT]", 'Search places for "hst"']
    assert r["afterFirstEsc"] == ["", 0]
    assert r["focused"] is True and r["reopened"] is True and r["active"] is True
    assert r["bad"] is False


scenario("panel_collapse", r"""
  var k = makeCtx({});
  window.SDAOmniSearch.init(k.ctx);
  var panel = byClass(k.ctx.mount, 'panel')[0];
  var head = byTag(panel, 'H3')[0];
  // The page binds its own collapse toggle after init; the panel must still toggle once.
  head.addEventListener('click', function () { panel.classList.toggle('collapsed'); });
  head.fire('click');
  return panel.has('collapsed');
""")


def test_panel_head_toggles_once_alongside_the_page_binding(ran):
    assert ran["panel_collapse"] is True
