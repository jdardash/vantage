"""Maritime routes: live vessels with dark-ship estimates, chokepoint monitors,
and sanctioned vessels flagged, all over the shared AISStream subscription.

* GET /api/maritime/chokepoints          the nine monitors: polygons always, live
                                         counts and transits when the key is set
* GET /api/maritime/chokepoints/{cp_id}  vessels inside one monitor now
* GET /api/maritime/vessels              vessels in a box, plus dark-ship estimates

AIS needs ``AISSTREAM_API_KEY`` (free from aisstream.io), read here server-side
and never returned. Without it every endpoint still answers 200: the chokepoint
polygons are served and the vessel lists are empty, with ``key_configured:
false`` and an error naming the variable. The rules (dark threshold, transit
method) are described in sda.maritime and returned with each answer.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from sda import maritime, sanctions

router = APIRouter(tags=["Maritime"])


def _base(key: str) -> dict:
    if not key:
        return {"key_configured": False, "connected": False, "error": maritime.KEY_MISSING,
                "source": "AISStream"}
    status = maritime.ais_status()
    return {"key_configured": True, "connected": status["connected"],
            "error": status["error"], "cached_vessels": status["cached_vessels"],
            "source": "AISStream"}


def _sample(key: str) -> sanctions.SanctionsStore:
    """Keep the stream open, fold the current cache in, and wake the sanctions list."""
    maritime.touch_stream(key)
    maritime.TRACKER.ingest(maritime.ais_rows())
    store = sanctions.get_store()
    store.ensure_loaded()
    return store


def _rules() -> dict:
    return {
        "dark_after_s": maritime.DARK_AFTER_S, "dark_max_s": maritime.DARK_MAX_S,
        "dark_min_sog_kn": maritime.DARK_MIN_SOG_KN, "live_s": maritime.LIVE_S,
        "transit_span": maritime.TRANSIT_SPAN, "dark_rule": maritime.DARK_RULE,
        "transit_rule": maritime.METHOD,
    }


@router.get("/api/maritime/chokepoints",
            summary="Chokepoint monitors: polygons, vessels inside now, transits")
async def maritime_chokepoints() -> dict:
    key = maritime.ais_key()
    out = _base(key)
    now = time.time()
    if key:
        store = _sample(key)
        tracker = maritime.TRACKER
        stats = tracker.chokepoint_stats(now)
        for cp in stats:
            cp["sanctioned_now"] = sum(
                1 for t in tracker.inside(cp["id"], now)
                if store.vessel(mmsi=t["mmsi"]))
        out.update(counting_since=maritime.iso(tracker.counting_since),
                   server_started=maritime.iso(tracker.started_at))
    else:
        stats = [{**cp.as_dict(), "inside_now": None, "under_way_now": None,
                  "transits_total": None, "transits_today": None, "by_day": {},
                  "sanctioned_now": None} for cp in maritime.CHOKEPOINTS]
        out.update(counting_since="", server_started=maritime.iso(maritime.TRACKER.started_at))
    out.update(chokepoints=stats, day=maritime.utc_day(now), rules=_rules())
    return out


@router.get("/api/maritime/chokepoints/{cp_id}",
            summary="Vessels inside one chokepoint monitor now")
async def maritime_chokepoint(cp_id: str, limit: int = Query(500, ge=1, le=2000)) -> dict:
    cp = maritime.CHOKEPOINT_BY_ID.get(cp_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="Unknown chokepoint")
    key = maritime.ais_key()
    out = _base(key)
    out["chokepoint"] = cp.as_dict()
    if not key:
        out.update(vessels=[], count=0)
        return out
    store = _sample(key)
    now = time.time()
    rows = [maritime.public_track(t, now, store) for t in maritime.TRACKER.inside(cp_id, now)]
    rows.sort(key=lambda r: (0 if r.get("sanction") else 1, r["name"] or r["mmsi"]))
    stats = next(s for s in maritime.TRACKER.chokepoint_stats(now) if s["id"] == cp_id)
    out.update(vessels=rows[:limit], count=len(rows),
               transits_total=stats["transits_total"], transits_today=stats["transits_today"],
               by_day=stats["by_day"])
    return out


@router.get("/api/maritime/vessels",
            summary="Vessels in a box with sanctions flags, plus dark-ship estimates")
async def maritime_vessels(
    south: float = Query(-90.0, ge=-90.0, le=90.0),
    west: float = Query(-180.0, ge=-180.0, le=180.0),
    north: float = Query(90.0, ge=-90.0, le=90.0),
    east: float = Query(180.0, ge=-180.0, le=180.0),
    limit: int = Query(2000, ge=1, le=5000),
) -> dict:
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="Empty bounding box")
    key = maritime.ais_key()
    out = _base(key)
    out["rules"] = _rules()
    if not key:
        out.update(vessels=[], dark=[], count=0, dark_count=0, sanctioned_count=0,
                   tracked=0, sanctions_loaded=False)
        return out
    store = _sample(key)
    now = time.time()
    tracker = maritime.TRACKER

    def in_box(lat: float, lon: float) -> bool:
        return south <= lat <= north and west <= lon <= east

    live = [maritime.public_track(t, now, store) for t in tracker.live(now)
            if in_box(t["lat"], t["lon"])]
    # Listed vessels first, so a full view never drops one under the limit.
    live.sort(key=lambda r: 0 if r.get("sanction") else (1 if r.get("name_match") else 2))
    dark = [maritime.public_dark(d, now, store) for d in tracker.dark(now)
            if in_box(d["est_lat"], d["est_lon"]) or in_box(d["lat"], d["lon"])]
    dark.sort(key=lambda r: r["silent_s"])
    vessels = live[:limit]
    dark = dark[:limit]
    out.update(
        vessels=vessels, dark=dark, count=len(vessels), dark_count=len(dark),
        sanctioned_count=sum(1 for r in vessels + dark if r.get("sanction")),
        capped=len(live) > limit, tracked=len(tracker.tracks),
        sanctions_loaded=bool(store.records),
    )
    return out
