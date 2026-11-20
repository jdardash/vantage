"""Tests for the globe view-mode switch (static/js/viewmode.js), run under Node."""

from __future__ import annotations

import pytest

from tests import globe_ux_harness as h

pytestmark = pytest.mark.skipif(h.NODE is None, reason="node is not installed")

SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> h.Ran:
    return h.run(tmp_path_factory.mktemp("viewmode"), ["viewmode.js"], SCENARIOS)


scenario("helpers", r"""
  var V = window.SDAViewMode;
  var s = V.memoryStorage();
  var wrote = [V.writeSaved(s, 'Columbus'), V.writeSaved(s, 'sideways')];
  var afterGood = V.readSaved(s);
  s.setItem(V.STORE_KEY, 'junk');
  return {
    names: ['3D', 'globe', '2d', 'MAP', 'flat', 'cv', 'Columbus View', 'columbus-view', '',
            null, 7, 'sideways'].map(V.normalizeMode),
    scene: [3, 2, 1, 0, undefined].map(function (m) { return V.modeOfScene(m,
      Cesium.SceneMode); }),
    next: ['3d', '2d', 'cv', 'junk'].map(V.nextMode),
    wrote: wrote, afterGood: afterGood, junk: V.readSaved(s), key: V.STORE_KEY
  };
""")


def test_helpers(ran):
    r = ran["helpers"]
    assert r["names"] == ["3d", "3d", "2d", "2d", "2d", "cv", "cv", "cv", None, None, None, None]
    assert r["scene"] == ["3d", "2d", "cv", None, None]
    assert r["next"] == ["2d", "cv", "3d", "2d"]
    assert r["wrote"] == [True, False] and r["afterGood"] == "cv"
    assert r["junk"] is None
    assert r["key"] == "sda.viewmode.v1"


scenario("restore_saved", r"""
  var store = new FakeStorage();
  store.setItem('sda.viewmode.v1', '2d');
  var k = makeCtx({ storage: store });
  var ok = window.SDAViewMode.init(k.ctx);
  var sw = byClass(document.body, 'vm-float')[0];
  return {
    ok: ok, morphs: k.ctx.viewer.scene.morphs, mode: window.SDAViewMode.mode(),
    on: byClass(sw, 'vm-on').map(function (b) { return b.textContent; }),
    labels: byTag(sw, 'BUTTON').map(function (b) { return b.textContent; }),
    feed: k.feed, mountKids: k.ctx.mount.children.length
  };
""")


def test_restores_saved_mode_without_animation(ran):
    r = ran["restore_saved"]
    assert r["ok"] is True
    assert r["morphs"] == [["2d", 0]]
    assert r["mode"] == "2d" and r["on"] == ["2D"]
    assert r["labels"] == ["3D", "2D", "CV"]
    assert r["feed"] == []  # a restore is silent
    assert r["mountKids"] == 0  # the switch floats; the mount stays empty


scenario("click_saves", r"""
  var store = new FakeStorage();
  var k = makeCtx({ storage: store });
  window.SDAViewMode.init(k.ctx);
  var before = { morphs: k.ctx.viewer.scene.morphs.slice(),
    saved: store.getItem('sda.viewmode.v1') };
  byText(document.body, 'CV')[0].fire('click');
  var afterCv = store.getItem('sda.viewmode.v1');
  window.SDAViewMode.cycle();
  return { before: before, morphs: k.ctx.viewer.scene.morphs, afterCv: afterCv,
    afterCycle: store.getItem('sda.viewmode.v1'), mode: window.SDAViewMode.mode(), feed: k.feed,
    bad: window.SDAViewMode.setMode('sideways') };
""")


def test_click_and_cycle_save(ran):
    r = ran["click_saves"]
    assert r["before"] == {"morphs": [], "saved": None}
    assert r["morphs"] == [["cv", 1.2], ["3d", 1.2]]
    assert r["afterCv"] == "cv" and r["afterCycle"] == "3d" and r["mode"] == "3d"
    assert r["feed"] == ["View: Columbus view", "View: 3D globe"]
    assert r["bad"] is None


scenario("maptools_drives_morph", r"""
  var store = new FakeStorage();
  store.setItem('sda.viewmode.v1', 'cv');
  var k = makeCtx({ storage: store });
  var mtCalls = [];
  window.SDAMapTools = { instance: function () { return {}; },
    setSceneMode: function (m) { mtCalls.push(m);
      k.ctx.viewer.scene.morphTo2D(1.2); } };
  window.SDAViewMode.init(k.ctx);
  var startMorphs = k.ctx.viewer.scene.morphs.slice();
  byText(document.body, '2D')[0].fire('click');
  // Another module (or the map tools panel) morphs the scene on its own.
  k.ctx.viewer.scene.morphTo3D(1.2);
  return { startMorphs: startMorphs, mtCalls: mtCalls, saved: store.getItem('sda.viewmode.v1'),
    on: byClass(document.body, 'vm-on').map(function (b) { return b.textContent; }) };
""")


def test_map_tools_morph_and_external_switches_are_saved(ran):
    r = ran["maptools_drives_morph"]
    assert r["startMorphs"] == [["cv", 0]]  # restore goes straight to the scene, no animation
    assert r["mtCalls"] == ["2d"]  # an interactive switch goes through map tools
    assert r["saved"] == "3d"  # the external morph to 3D was remembered
    assert r["on"] == ["3D"]


scenario("broken_storage", r"""
  window.localStorage = { getItem: function () { throw new Error('denied'); },
    setItem: function () { throw new Error('denied'); }, removeItem: function () {} };
  var k = makeCtx({});
  var ok = window.SDAViewMode.init(k.ctx);
  window.SDAViewMode.setMode('2d');
  return { ok: ok, mode: window.SDAViewMode.mode(), saved: window.SDAViewMode.saved() };
""")


def test_falls_back_to_memory_when_storage_throws(ran):
    r = ran["broken_storage"]
    assert r == {"ok": True, "mode": "2d", "saved": "2d"}


scenario("no_viewer", r"""
  var a = window.SDAViewMode.init({});
  var b = window.SDAViewMode.init(null);
  return { a: a, b: b, cycle: window.SDAViewMode.cycle(), mode: window.SDAViewMode.mode() };
""")


def test_init_without_a_viewer_is_harmless(ran):
    assert ran["no_viewer"] == {"a": False, "b": False, "cycle": None, "mode": None}


scenario("destroy", r"""
  var k = makeCtx({ storage: new FakeStorage() });
  window.SDAViewMode.init(k.ctx);
  window.SDAViewMode.destroy();
  k.ctx.viewer.scene.morphTo2D(1);
  return { floats: byClass(document.body, 'vm-float').length,
    listeners: k.ctx.viewer.scene.morphComplete.fns.length };
""")


def test_destroy_removes_switch_and_listener(ran):
    assert ran["destroy"] == {"floats": 0, "listeners": 0}
