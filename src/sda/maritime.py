"""Maritime intelligence over the AIS stream: dark ships, chokepoint monitors and
sanctioned vessels.

The AIS feed is the AISStream subscription held by sda.routes.worldview (one
WebSocket, latest report per MMSI, at most AIS_CACHE_LIMIT vessels, oldest
evicted first). That cache forgets a vessel soon after it stops reporting, which
is exactly the vessel this module cares about, so a MaritimeTracker samples the
cache every SAMPLE_EVERY_S seconds and keeps its own memory per MMSI.

Dark ships
    A vessel whose last report had it under way (speed at least DARK_MIN_SOG_KN,
    a valid course, not at anchor, moored or aground) and that has sent nothing
    for DARK_AFTER_S (30 minutes) is kept on the map at a dead-reckoned position:
    its last position moved along its last course over ground at its last speed
    for the time since, on a sphere of mean Earth radius. It is dropped after
    DARK_MAX_S (6 hours), when the estimate is no longer worth drawing. AISStream
    is mostly terrestrial receivers, so a ship that leaves coastal coverage looks
    the same as one that switched its transponder off; the label says estimate,
    never intent.

Chokepoint monitors
    Nine polygons (CHOKEPOINTS). A vessel inside one now is one whose latest
    report, under LIVE_S old, falls inside. A transit is counted once per
    passage, when the vessel's reports inside the polygon span at least
    TRANSIT_SPAN of the polygon's length along its axis (the shipping direction).
    Ferries crossing the strait and ships at anchor do not move along the axis
    and are not counted. A passage ends after PASSAGE_GAP_S without a report
    inside. Counts run from the first AIS sample after the server started and
    are kept per UTC day for DAY_HISTORY days; they cover only the time the AIS
    stream was open, which is while the maritime layer is on.

Sanctioned vessels
    Matched through sda.sanctions by MMSI, which the OFAC SDN remarks give for
    about half its listed vessels. AIS position reports carry no IMO number, so
    an IMO match is not possible from this feed; a vessel whose broadcast name
    equals a listed vessel's name is reported separately as unconfirmed.

Frames: geodetic degrees, speeds in knots, distances in km, times in UTC.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sda import sanctions

EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852

LIVE_S = 1800.0
DARK_AFTER_S = 1800.0
DARK_MAX_S = 6 * 3600.0
DARK_MIN_SOG_KN = 3.0
MAX_SOG_KN = 50.0
STATIONARY_NAV_STATUS = frozenset({1, 5, 6})  # at anchor, moored, aground
TRANSIT_SPAN = 0.6
PASSAGE_GAP_S = 3600.0
DAY_HISTORY = 14
TRACK_LIMIT = 20000
SAMPLE_EVERY_S = 2.0
IDLE_STOP_S = 300.0
REMARK_CHARS = 600

KEY_ENV = "AISSTREAM_API_KEY"
KEY_MISSING = "AISSTREAM_API_KEY is needed for live vessels (a free key from aisstream.io)"


# ---------------------------------------------------------------------------
# Geometry on a sphere of mean Earth radius
# ---------------------------------------------------------------------------


def destination(lat: float, lon: float, bearing_deg: float, distance_km: float
                ) -> tuple[float, float]:
    """Point reached from (lat, lon) along an initial bearing over a great circle."""
    d = distance_km / EARTH_RADIUS_KM
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    sin_phi2 = (math.sin(phi1) * math.cos(d)
                + math.cos(phi1) * math.sin(d) * math.cos(theta))
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lam2 = lam1 + math.atan2(math.sin(theta) * math.sin(d) * math.cos(phi1),
                             math.cos(d) - math.sin(phi1) * sin_phi2)
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0
    return math.degrees(phi2), lon2


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def point_in_polygon(lon: float, lat: float, poly: tuple[tuple[float, float], ...]) -> bool:
    """Even-odd ray test on lon/lat. No monitor polygon crosses the antimeridian."""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Chokepoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chokepoint:
    id: str
    name: str
    polygon: tuple[tuple[float, float], ...]   # (lon, lat), not closed
    axis: tuple[tuple[float, float], tuple[float, float]]  # shipping direction, end to end
    note: str = ""

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        lons = [p[0] for p in self.polygon]
        lats = [p[1] for p in self.polygon]
        return min(lons), min(lats), max(lons), max(lats)

    def contains(self, lon: float, lat: float) -> bool:
        w, s, e, n = self.bbox
        if not (w <= lon <= e and s <= lat <= n):
            return False
        return point_in_polygon(lon, lat, self.polygon)

    def along(self, lon: float, lat: float) -> float:
        """Fraction of the way from the axis start to its end (0..1 inside)."""
        (lon_a, lat_a), (lon_b, lat_b) = self.axis
        k = math.cos(math.radians((lat_a + lat_b) / 2.0))
        bx, by = (lon_b - lon_a) * k, lat_b - lat_a
        px, py = (lon - lon_a) * k, lat - lat_a
        length2 = bx * bx + by * by
        return (px * bx + py * by) / length2 if length2 else 0.0

    @property
    def centroid(self) -> tuple[float, float]:
        n = len(self.polygon)
        return (sum(p[0] for p in self.polygon) / n, sum(p[1] for p in self.polygon) / n)

    def as_dict(self) -> dict:
        lon, lat = self.centroid
        return {
            "id": self.id, "name": self.name,
            "polygon": [list(p) for p in self.polygon],
            "axis": [list(p) for p in self.axis],
            "center": [round(lon, 4), round(lat, 4)],
            "note": self.note,
        }


# Straits are rectangles along the shipping axis, sized to reach both shores;
# the two canals and the Bosporus follow the waterway. Approximate monitors, not
# legal boundaries. Coordinates are (lon, lat) degrees.
CHOKEPOINTS: tuple[Chokepoint, ...] = (
    Chokepoint(
        "gibraltar", "Strait of Gibraltar",
        ((-6.058, 36.056), (-5.308, 36.086), (-5.292, 35.834), (-6.042, 35.804)),
        ((-6.05, 35.93), (-5.30, 35.96)),
        "Atlantic to Mediterranean between Tarifa and Tangier.",
    ),
    Chokepoint(
        "hormuz", "Strait of Hormuz",
        ((56.074, 26.698), (56.874, 26.618), (56.826, 26.222), (56.026, 26.302)),
        ((56.05, 26.50), (56.85, 26.42)),
        "Persian Gulf to the Gulf of Oman, past the Musandam peninsula.",
    ),
    Chokepoint(
        "bab_el_mandeb", "Bab-el-Mandeb",
        ((43.398, 12.247), (43.048, 12.747), (43.352, 12.953), (43.702, 12.453)),
        ((43.55, 12.35), (43.20, 12.85)),
        "Gulf of Aden to the Red Sea, either side of Perim.",
    ),
    Chokepoint(
        "suez", "Suez Canal",
        ((32.20, 31.40), (32.42, 31.40), (32.42, 30.50), (32.65, 30.05), (32.65, 29.85),
         (32.45, 29.85), (32.25, 30.30)),
        ((32.31, 31.30), (32.56, 29.93)),
        "Port Said to Suez, the Bitter Lakes included; the outer anchorages are not.",
    ),
    Chokepoint(
        "malacca", "Strait of Malacca",
        ((101.831, 2.925), (102.131, 2.665), (101.369, 1.775), (101.069, 2.035)),
        ((101.45, 2.48), (101.75, 2.22)),
        "A section across the strait off Port Dickson.",
    ),
    Chokepoint(
        "panama", "Panama Canal",
        ((-80.00, 9.40), (-79.84, 9.40), (-79.62, 9.10), (-79.50, 8.88), (-79.60, 8.84),
         (-79.78, 9.02), (-80.00, 9.20)),
        ((-79.92, 9.36), (-79.56, 8.93)),
        "Colon to Balboa through Gatun Lake; the offshore anchorages are not included.",
    ),
    Chokepoint(
        "bosporus", "Bosporus",
        ((28.96, 40.99), (29.06, 40.99), (29.10, 41.10), (29.18, 41.24), (29.07, 41.24),
         (29.02, 41.10)),
        ((29.00, 41.00), (29.12, 41.23)),
        "Sea of Marmara to the Black Sea through Istanbul.",
    ),
    Chokepoint(
        "dover", "Strait of Dover",
        ((1.25, 51.072), (1.61, 51.272), (1.95, 51.028), (1.59, 50.828)),
        ((1.42, 50.95), (1.78, 51.15)),
        "English Channel to the North Sea between Dover and Calais.",
    ),
    Chokepoint(
        "taiwan_strait", "Taiwan Strait",
        ((119.487, 25.133), (119.787, 25.533), (121.013, 24.767), (120.713, 24.367)),
        ((120.10, 24.75), (120.40, 25.15)),
        "A section across the strait between Fujian and Hsinchu.",
    ),
)
CHOKEPOINT_BY_ID = {cp.id: cp for cp in CHOKEPOINTS}

METHOD = (
    f"Inside now: latest AIS report under {int(LIVE_S // 60)} min old falls in the polygon. "
    f"Transit: one passage whose reports inside span at least {int(TRANSIT_SPAN * 100)}% of "
    f"the polygon's length along the shipping axis; crossings and anchored ships are not "
    f"counted. Counted only while the AIS stream is open."
)
DARK_RULE = (
    f"No AIS report for {int(DARK_AFTER_S // 60)} min after the last one showed the vessel "
    f"under way (at least {DARK_MIN_SOG_KN:g} kn, not anchored or moored). Position is "
    f"estimated from the last course and speed, and dropped after "
    f"{int(DARK_MAX_S // 3600)} h. Leaving receiver coverage looks the same as switching "
    f"AIS off."
)


# ---------------------------------------------------------------------------
# AIS field cleaning
# ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def clean_sog(value: object) -> float | None:
    """Speed over ground in knots; 102.3 means not available."""
    sog = _num(value)
    if sog is None or sog < 0 or sog > MAX_SOG_KN:
        return None
    return sog


def clean_cog(value: object) -> float | None:
    """Course over ground in degrees; 360 means not available."""
    cog = _num(value)
    if cog is None or cog < 0 or cog >= 360:
        return None
    return cog


def clean_nav(value: object) -> int | None:
    nav = _num(value)
    return int(nav) if nav is not None and 0 <= nav <= 15 else None


def utc_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d")


def iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def under_way(track: dict) -> bool:
    sog = track.get("sog_kn")
    return (sog is not None and sog >= DARK_MIN_SOG_KN and track.get("cog_deg") is not None
            and track.get("nav_status") not in STATIONARY_NAV_STATUS)


def dead_reckon(track: dict, now: float) -> tuple[float, float, float] | None:
    """(lat, lon, km run) after the silence, or None when the vessel was not under way."""
    if not under_way(track):
        return None
    silent = max(0.0, min(now - float(track["received"]), DARK_MAX_S))
    run_km = float(track["sog_kn"]) * KM_PER_NM * silent / 3600.0
    lat, lon = destination(float(track["lat"]), float(track["lon"]),
                           float(track["cog_deg"]), run_km)
    return lat, lon, run_km


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


class MaritimeTracker:
    """Per-MMSI memory of the AIS cache, chokepoint membership and transit counts."""

    def __init__(self) -> None:
        self.tracks: dict[str, dict] = {}
        self.transits_total: dict[str, int] = {cp.id: 0 for cp in CHOKEPOINTS}
        self.transits_by_day: dict[str, dict[str, int]] = {cp.id: {} for cp in CHOKEPOINTS}
        self.started_at = time.time()
        self.counting_since: float | None = None
        self.last_sample: float | None = None
        self.samples = 0

    # -- ingest ---------------------------------------------------------------

    def ingest(self, rows: Iterable[dict], now: float | None = None) -> int:
        """Fold the AIS cache rows in; returns how many were new reports."""
        now = time.time() if now is None else now
        fresh = 0
        for row in rows:
            if self._ingest_row(row):
                fresh += 1
        self.samples += 1
        self.last_sample = now
        if fresh and self.counting_since is None:
            self.counting_since = now
        self._prune(now)
        return fresh

    def _ingest_row(self, row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        mmsi = str(row.get("mmsi") or "").strip()
        lat = _num(row.get("lat"))
        lon = _num(row.get("lon"))
        received = _num(row.get("received"))
        if not mmsi or lat is None or lon is None or received is None:
            return False
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return False
        prev = self.tracks.get(mmsi)
        if prev is not None and received <= float(prev["received"]):
            return False
        track = {
            "mmsi": mmsi,
            "name": str(row.get("name") or "").strip(),
            "lat": lat, "lon": lon,
            "sog_kn": clean_sog(row.get("sog_kn")),
            "cog_deg": clean_cog(row.get("cog_deg")),
            "heading_deg": _num(row.get("heading_deg")),
            "nav_status": clean_nav(row.get("nav_status")),
            "time_utc": str(row.get("time_utc") or ""),
            "received": received,
            "cp": None,
            "passage": prev.get("passage") if prev else None,
        }
        if prev is not None:
            self.tracks.pop(mmsi)  # re-insert so dict order follows the last report
        self.tracks[mmsi] = track
        self._chokepoint_step(track)
        return True

    def _chokepoint_step(self, track: dict) -> None:
        lon, lat, received = track["lon"], track["lat"], float(track["received"])
        for cp in CHOKEPOINTS:
            if not cp.contains(lon, lat):
                continue
            track["cp"] = cp.id
            t = cp.along(lon, lat)
            passage = track.get("passage")
            if (not passage or passage["cp"] != cp.id
                    or received - passage["last_in"] > PASSAGE_GAP_S):
                passage = {"cp": cp.id, "t_min": t, "t_max": t, "last_in": received,
                           "counted": False}
                track["passage"] = passage
            passage["t_min"] = min(passage["t_min"], t)
            passage["t_max"] = max(passage["t_max"], t)
            passage["last_in"] = received
            if not passage["counted"] and passage["t_max"] - passage["t_min"] >= TRANSIT_SPAN:
                passage["counted"] = True
                self.transits_total[cp.id] += 1
                days = self.transits_by_day[cp.id]
                day = utc_day(received)
                days[day] = days.get(day, 0) + 1
            return

    def _prune(self, now: float) -> None:
        cutoff = now - DARK_MAX_S - PASSAGE_GAP_S
        while self.tracks:
            first = next(iter(self.tracks.values()))
            if len(self.tracks) <= TRACK_LIMIT and float(first["received"]) >= cutoff:
                break
            self.tracks.pop(first["mmsi"], None)
        for days in self.transits_by_day.values():
            for day in sorted(days)[:-DAY_HISTORY]:
                days.pop(day, None)

    # -- views ---------------------------------------------------------------

    def live(self, now: float) -> list[dict]:
        return [t for t in self.tracks.values() if now - float(t["received"]) < LIVE_S]

    def dark(self, now: float) -> list[dict]:
        out = []
        for t in self.tracks.values():
            silent = now - float(t["received"])
            if silent < DARK_AFTER_S or silent > DARK_MAX_S:
                continue
            est = dead_reckon(t, now)
            if est is None:
                continue
            lat, lon, run_km = est
            out.append({**t, "est_lat": lat, "est_lon": lon, "run_km": run_km,
                        "silent_s": silent})
        return out

    def chokepoint_stats(self, now: float) -> list[dict]:
        inside: dict[str, list[dict]] = {cp.id: [] for cp in CHOKEPOINTS}
        for t in self.live(now):
            if t.get("cp") in inside:
                inside[t["cp"]].append(t)
        today = utc_day(now)
        out = []
        for cp in CHOKEPOINTS:
            rows = inside[cp.id]
            out.append({
                **cp.as_dict(),
                "inside_now": len(rows),
                "under_way_now": sum(1 for t in rows if under_way(t)),
                "transits_total": self.transits_total[cp.id],
                "transits_today": self.transits_by_day[cp.id].get(today, 0),
                "by_day": dict(sorted(self.transits_by_day[cp.id].items())),
            })
        return out

    def inside(self, cp_id: str, now: float) -> list[dict]:
        return [t for t in self.live(now) if t.get("cp") == cp_id]


TRACKER = MaritimeTracker()


def reset_tracker() -> MaritimeTracker:
    """Start counting again from nothing (tests). Callers read maritime.TRACKER."""
    global TRACKER
    TRACKER = MaritimeTracker()
    return TRACKER


# ---------------------------------------------------------------------------
# Sanctions on a track
# ---------------------------------------------------------------------------


def _short(text: str) -> str:
    return text if len(text) <= REMARK_CHARS else text[:REMARK_CHARS - 3].rstrip() + "..."


def sanction_flags(track: dict, store: sanctions.SanctionsStore) -> dict:
    """``sanction`` for an MMSI match (listed), ``name_match`` for a name-only lead."""
    out: dict[str, Any] = {}
    hits = store.vessel(mmsi=track.get("mmsi", ""))
    if hits:
        h = hits[0]
        out["sanction"] = {
            "listed": True, "matched_on": "mmsi", "source": h["source"],
            "programme": h["programme"], "name": h["name"], "imo": h["imo"],
            "remarks": _short(h["remarks"]), "url": h["url"], "listings": len(hits),
        }
        return out
    leads = store.vessel_name_match(track.get("name", ""))
    if leads:
        h = leads[0]
        out["name_match"] = {
            "listed": False, "matched_on": "name", "source": h["source"],
            "programme": h["programme"], "name": h["name"], "imo": h["imo"],
            "remarks": _short(h["remarks"]), "url": h["url"], "listings": len(leads),
            "note": "Name only. AIS position reports carry no IMO number, so this is "
                    "not confirmed; check the IMO before relying on it.",
        }
    return out


def public_track(track: dict, now: float, store: sanctions.SanctionsStore) -> dict:
    row = {
        "mmsi": track["mmsi"], "name": track["name"],
        "lat": round(track["lat"], 5), "lon": round(track["lon"], 5),
        "sog_kn": track["sog_kn"], "cog_deg": track["cog_deg"],
        "heading_deg": track["heading_deg"], "nav_status": track["nav_status"],
        "time_utc": track["time_utc"], "age_s": round(now - float(track["received"]), 1),
        "chokepoint": track.get("cp"),
    }
    row.update(sanction_flags(track, store))
    return row


def public_dark(item: dict, now: float, store: sanctions.SanctionsStore) -> dict:
    row = public_track(item, now, store)
    minutes = int(item["silent_s"] // 60)
    row.update({
        "estimate": True,
        "lat": round(item["est_lat"], 5), "lon": round(item["est_lon"], 5),
        "last_lat": round(item["lat"], 5), "last_lon": round(item["lon"], 5),
        "silent_s": round(item["silent_s"], 1), "run_km": round(item["run_km"], 2),
        "label": f"EST {item['name'] or item['mmsi']} (no AIS {minutes} min)",
    })
    return row


# ---------------------------------------------------------------------------
# The AIS stream (sda.routes.worldview) and the sampler
# ---------------------------------------------------------------------------

_SAMPLER: dict[str, Any] = {"task": None, "last_request": 0.0}


def ais_key() -> str:
    return (os.environ.get(KEY_ENV) or "").strip()


def ais_rows() -> list[dict]:
    from sda.routes import worldview

    return list(worldview._ais_vessels().values())


def ais_status() -> dict:
    from sda.routes import worldview

    state = worldview._AIS_STATE
    return {"connected": bool(state.get("connected")), "error": state.get("error"),
            "cached_vessels": len(worldview._ais_vessels())}


def touch_stream(key: str) -> None:
    """Keep the shared AISStream subscription open and the sampler running."""
    from sda.routes import worldview

    worldview._AIS_STATE["last_request"] = time.monotonic()
    worldview._ais_ensure_stream(key)
    _SAMPLER["last_request"] = time.monotonic()
    task = _SAMPLER.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        return
    with contextlib.suppress(RuntimeError):
        _SAMPLER["task"] = asyncio.get_running_loop().create_task(_sample_loop())


async def _sample_loop() -> None:
    try:
        while time.monotonic() - float(_SAMPLER["last_request"]) < IDLE_STOP_S:
            TRACKER.ingest(ais_rows())
            await asyncio.sleep(SAMPLE_EVERY_S)
    finally:
        _SAMPLER["task"] = None


def stop_sampler() -> None:
    """Cancel the sampler. Used by the tests."""
    task = _SAMPLER.get("task")
    if isinstance(task, asyncio.Task):
        task.cancel()
    _SAMPLER["task"] = None
    _SAMPLER["last_request"] = 0.0
