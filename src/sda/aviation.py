"""Aviation intelligence for the globe, built on adsb.lol.

What it gives the routes in ``sda.routes.aviation``:

* military aircraft (``/v2/mil``) and emergency squawks 7500, 7600 and 7700
  (``/v2/sqk/{code}``), parsed into one flat record shape;
* an aircraft card: registration, type, operator, position state, and the
  scheduled route for the callsign from the VRS standing data (CC0) with the
  leg the aircraft is actually on, plus LiveATC search links;
* dead reckoning: a record older than REPORT_FRESH_S is carried along its last
  track at its last ground speed for at most DEAD_RECKON_MAX_S, and is always
  labelled as an estimate;
* GPS interference inferred from aircraft reports, the GPSJam method: per hex,
  over the last hour, the share of aircraft whose own position report carries
  low integrity (NIC below 7 or NACp below 8) or that readsb flagged as having
  lost GPS (``gpsOkBefore``). MLAT and TIS-B positions are skipped because their
  NIC and NACp describe the ground network, not the aircraft.

Rate limits. adsb.lol meters every ``/v2`` path. Without a key an IP gets 500
requests a day (``x-ratelimit-limit: 500, 500;w=86400``, measured 2026-11-19)
and a 429 with Retry-After past that. Every metered call here goes through one
token bucket sized from SDA_ADSBLOL_BUDGET (default 350 a day, leaving room for
the older /api/military-flights proxy on the same IP), feed intervals stretch to
fit the budget, the upstream ``x-ratelimit-remaining`` header caps the bucket,
and a 429 stops every call until Retry-After has passed. One request token is
held back for the emergency feeds. ADSBLOL_API_KEY, when set, is sent as
``x-api-key`` from this process only and never reaches the browser.

Coordinates are geodetic degrees on a sphere of mean Earth radius; altitudes
are metres (adsb.lol reports feet, converted here). Data: adsb.lol, ODbL 1.0.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import httpx

ADSBLOL_BASE = "https://api.adsb.lol"
ROUTE_URL = "https://vrs-standing-data.adsb.lol/routes/{prefix}/{callsign}.json"
LIVEATC_URL = "https://www.liveatc.net/search/?icao={icao}"
USER_AGENT = "vantage/0.2 (worldview aviation)"

ADSBLOL_SOURCE = "adsb.lol (ODbL 1.0)"
ROUTE_SOURCE = "VRS standing data via adsb.lol (CC0)"
SITE_SOURCE = "OpenStreetMap via Overpass (ODbL)"

EARTH_MEAN_RADIUS_KM = 6371.0088
NM_KM = 1.852
FT_TO_M = 0.3048

EMERGENCY_SQUAWKS: dict[str, str] = {
    "7700": "general emergency",
    "7600": "radio failure",
    "7500": "unlawful interference",
}

#: A position younger than this is drawn as a report; older ones are estimates.
REPORT_FRESH_S = 120.0
#: Dead reckoning stops this long after the last position report.
DEAD_RECKON_MAX_S = 1200.0
#: Below this ground speed a track is not worth extrapolating.
DEAD_RECKON_MIN_KT = 30.0

#: GPS interference window, and integrity thresholds (DO-260B categories).
GPS_WINDOW_S = 3600.0
NIC_LOW = 7  # NIC 7 and up: containment radius under 0.2 NM
NACP_LOW = 8  # NACp 8 and up: position uncertainty under 0.05 NM (92.6 m)
#: Hex circumradius in Mercator degrees: about 380 km across at the equator and
#: 220 km at 55 N. One adsb.lol sample holds tens of aircraft, so cells are wide.
HEX_SIZE_DEG = 2.0
MIN_HEX_AIRCRAFT = 2
MAX_HEX_LAT = 84.0
#: GPSJam colour breaks, percent of aircraft with low integrity.
LEVEL_MEDIUM_PCT = 2.0
LEVEL_HIGH_PCT = 10.0

#: View-centre samples for the interference map: snapped to this grid, 250 NM
#: radius (the /v2/point maximum), each cell at most once per half window.
SAMPLE_GRID_DEG = 4.0
SAMPLE_RADIUS_NM = 250
SAMPLE_REPEAT_S = GPS_WINDOW_S / 2

DEFAULT_DAILY_BUDGET = 350
KEYLESS_DAILY_LIMIT = 500
LOOKUP_CACHE_S = 60.0
ROUTE_CACHE_S = 6 * 3600.0
SITE_CACHE_S = 6 * 3600.0
SITE_ERROR_CACHE_S = 300.0
SITE_HALF_SPAN_DEG = 2.5

MAX_STORE = 20000
MAX_OBSERVATIONS = 60000

#: Target refresh intervals with an unconstrained budget, in seconds. The real
#: intervals are these stretched until the whole set fits the daily budget.
FAST_INTERVALS_S: dict[str, float] = {
    "sqk7700": 60.0,
    "sqk7600": 120.0,
    "sqk7500": 120.0,
    "mil": 60.0,
    "point": 120.0,
}
#: Share of the daily budget the feeds plan for; the rest is card lookups.
FEED_SHARE = 0.9

_HEX_RE = re.compile(r"^~?[0-9a-f]{6}$")
_ICAO_AIRPORT_RE = re.compile(r"^[A-Z0-9]{4}$")
_AIRLINE_CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9][0-9A-Z]{0,4}$")
_V2_PATH_RE = re.compile(r"^/v2/[A-Za-z0-9_.~/-]{1,80}$")

# Words that mark an operator string as an organisation. A civil operator
# without one is most likely a private person and is withheld: the card never
# names private individuals.
_ORG_WORDS = re.compile(
    r"\b(AIR|AIRLINES?|AIRWAYS|AVIATION|AERO|AEROSPACE|JETS?|CHARTER|CARGO|EXPRESS|"
    r"LLC|INC|CORP|CORPORATION|COMPANY|LTD|LIMITED|PLC|GMBH|AG|SA|SAS|SPA|BV|NV|"
    r"AB|AS|OY|KG|PTY|TRUST|TRUSTEE|BANK|LEASING|LEASE|CAPITAL|HOLDINGS?|GROUP|"
    r"SERVICES?|SYSTEMS|UNIVERSITY|COLLEGE|SCHOOL|ACADEMY|COUNTY|STATE|CITY|POLICE|"
    r"SHERIFF|FIRE|DEPARTMENT|DEPT|GOVERNMENT|MINISTRY|FORCES?|NAVY|ARMY|GUARD|"
    r"COAST|FEDERAL|NATIONAL|ROYAL|HOSPITAL|MEDICAL|HEALTH|FLIGHT|FLYING|CLUB|"
    r"HELICOPTERS?|AMBULANCE|RESCUE|AUTHORITY|AGENCY)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    """A finite float from an untyped JSON value, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _int(value: object) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


def _wrap_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a sphere of mean Earth radius."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MEAN_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def dead_reckon(
    lat: float, lon: float, track_deg: float, speed_kt: float, dt_s: float
) -> tuple[float, float]:
    """Position after flying *dt_s* seconds along a great circle from (lat, lon)."""
    d = speed_kt * NM_KM * dt_s / 3600.0 / EARTH_MEAN_RADIUS_KM
    p1, l1, th = math.radians(lat), math.radians(lon), math.radians(track_deg)
    p2 = math.asin(
        max(-1.0, min(1.0, math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(th)))
    )
    l2 = l1 + math.atan2(
        math.sin(th) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), _wrap_lon(math.degrees(l2))


def cross_track_km(
    lat: float, lon: float, a_lat: float, a_lon: float, b_lat: float, b_lon: float
) -> tuple[float, float]:
    """(cross-track, along-track) distance of a point from the great circle A to B."""
    d13 = haversine_km(a_lat, a_lon, lat, lon) / EARTH_MEAN_RADIUS_KM
    t13 = math.radians(bearing_deg(a_lat, a_lon, lat, lon))
    t12 = math.radians(bearing_deg(a_lat, a_lon, b_lat, b_lon))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    cos_xt = math.cos(xt)
    ratio = math.cos(d13) / cos_xt if cos_xt > 1e-12 else 1.0
    at = math.acos(max(-1.0, min(1.0, ratio)))
    if math.cos(t13 - t12) < 0:
        at = -at
    return xt * EARTH_MEAN_RADIUS_KM, at * EARTH_MEAN_RADIUS_KM


# ---------------------------------------------------------------------------
# Parsing adsb.lol records
# ---------------------------------------------------------------------------


def normalize_hex(value: object) -> str | None:
    """Lower-case 24-bit address, keeping adsb.lol's ``~`` for non-ICAO ones."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _HEX_RE.match(text) else None


def is_private(ac: Mapping[str, Any]) -> bool:
    """True for PIA addresses (dbFlags 4) and FAA LADD aircraft (dbFlags 8)."""
    flags = _int(ac.get("dbFlags")) or 0
    return bool(flags & 12)


def public_operator(name: object, military: bool) -> str:
    """The operator string when it names an organisation, else empty."""
    if not isinstance(name, str):
        return ""
    text = " ".join(name.split())[:80]
    if not text:
        return ""
    if military or _ORG_WORDS.search(text):
        return text
    return ""


def _uses_ground_position(ac: Mapping[str, Any]) -> bool:
    """True when NIC and NACp came from MLAT or TIS-B rather than the aircraft."""
    src = str(ac.get("type") or "")
    if src.startswith(("mlat", "tisb", "mode_s", "adsc", "other", "unknown")):
        return True
    for key in ("mlat", "tisb"):
        fields = ac.get(key)
        if isinstance(fields, list) and ("nic" in fields or "nac_p" in fields):
            return True
    return False


def integrity_state(ac: Mapping[str, Any]) -> str | None:
    """'bad', 'good', or None when the record says nothing about the aircraft's GPS.

    GPSJam counts aircraft reporting low navigation accuracy. Here an aircraft
    counts as degraded when readsb marked its GPS as lost (``gpsOkBefore``), or
    when its own ADS-B report carries NIC below 7 or NACp below 8. Aircraft on
    the ground, and positions from MLAT or TIS-B, are not counted at all.
    """
    if ac.get("alt_baro") == "ground":
        return None
    if _num(ac.get("gpsOkBefore")) is not None:
        return "bad"
    if _uses_ground_position(ac):
        return None
    nic = _int(ac.get("nic"))
    nacp = _int(ac.get("nac_p"))
    if nic is None or nacp is None:
        return None
    return "bad" if (nic < NIC_LOW or nacp < NACP_LOW) else "good"


def parse_aircraft(ac: object, now_s: float) -> dict[str, Any] | None:
    """One adsb.lol ``ac`` entry as a flat record, or None without an address.

    ``lat``/``lon`` come from the live fix, else from ``lastPosition`` with its
    age. The receiver-based rough position (``rr_lat``) is never used. For a
    private aircraft the callsign, registration and operator are blanked.
    """
    if not isinstance(ac, dict):
        return None
    hex_id = normalize_hex(ac.get("hex"))
    if hex_id is None:
        return None
    lat = _num(ac.get("lat"))
    lon = _num(ac.get("lon"))
    seen_pos = _num(ac.get("seen_pos"))
    from_last = False
    if lat is None or lon is None:
        last = ac.get("lastPosition")
        if isinstance(last, dict):
            lat, lon = _num(last.get("lat")), _num(last.get("lon"))
            seen_pos = _num(last.get("seen_pos"))
            from_last = lat is not None and lon is not None
    if lat is not None and lon is not None and (abs(lat) > 90 or abs(lon) > 180):
        lat = lon = None
    seen = _num(ac.get("seen"))
    age = seen_pos if seen_pos is not None else (seen if seen is not None else 0.0)
    alt_baro = ac.get("alt_baro")
    on_ground = alt_baro == "ground"
    alt_ft = _num(alt_baro) if not on_ground else 0.0
    if alt_ft is None:
        alt_ft = _num(ac.get("alt_geom"))
    flags = _int(ac.get("dbFlags")) or 0
    military = bool(flags & 1)
    private = bool(flags & 12)
    squawk = ac.get("squawk") if isinstance(ac.get("squawk"), str) else ""
    track = _num(ac.get("track"))
    if track is None:
        track = _num(ac.get("true_heading"))
    callsign = str(ac.get("flight") or "").strip()[:8]
    reg = str(ac.get("r") or "").strip()[:12]
    emergency = ac.get("emergency")
    rec: dict[str, Any] = {
        "hex": hex_id,
        "callsign": "" if private else callsign,
        "registration": "" if private else reg,
        "type": str(ac.get("t") or "").strip()[:8],
        "description": str(ac.get("desc") or "").strip()[:80],
        "operator": "" if private else public_operator(ac.get("ownOp"), military),
        "military": military,
        "private": private,
        "lat": round(lat, 5) if lat is not None else None,
        "lon": round(lon, 5) if lon is not None else None,
        "alt_m": round(alt_ft * FT_TO_M, 1) if alt_ft is not None else None,
        "on_ground": on_ground,
        "gs_kt": _num(ac.get("gs")),
        "track": round(track, 1) if track is not None else None,
        "squawk": squawk,
        "emergency_status": emergency if isinstance(emergency, str) else "",
        "category": str(ac.get("category") or "")[:3],
        "nic": _int(ac.get("nic")),
        "nac_p": _int(ac.get("nac_p")),
        "gps": integrity_state(ac),
        "position_source": (
            "last" if from_last else ("mlat" if _uses_ground_position(ac) else "adsb")
        ),
        "seen_at": round(now_s - max(0.0, age), 1),
    }
    return rec


def aircraft_with_position(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in records if r.get("lat") is not None and r.get("lon") is not None]


# ---------------------------------------------------------------------------
# Dead reckoning on a record
# ---------------------------------------------------------------------------


def position_state(rec: Mapping[str, Any], now_s: float) -> dict[str, Any]:
    """Where to draw *rec* at *now_s*: reported, estimated, or lost.

    ``reported`` is a position under REPORT_FRESH_S old. ``estimated`` carries
    the last position along the last track at the last ground speed, for at most
    DEAD_RECKON_MAX_S; it is never presented as a report. Anything older, on the
    ground, slower than DEAD_RECKON_MIN_KT or without a track is ``lost``.
    """
    lat, lon = _num(rec.get("lat")), _num(rec.get("lon"))
    seen_at = _num(rec.get("seen_at"))
    if lat is None or lon is None or seen_at is None:
        return {"state": "lost", "age_s": None}
    age = max(0.0, now_s - seen_at)
    if age <= REPORT_FRESH_S:
        return {"state": "reported", "lat": lat, "lon": lon, "age_s": round(age, 1)}
    track, gs = _num(rec.get("track")), _num(rec.get("gs_kt"))
    if (
        age > DEAD_RECKON_MAX_S
        or rec.get("on_ground")
        or track is None
        or gs is None
        or gs < DEAD_RECKON_MIN_KT
    ):
        return {"state": "lost", "age_s": round(age, 1)}
    elat, elon = dead_reckon(lat, lon, track, gs, age)
    return {
        "state": "estimated",
        "lat": round(elat, 5),
        "lon": round(elon, 5),
        "age_s": round(age, 1),
        "from": {"lat": lat, "lon": lon},
    }


# ---------------------------------------------------------------------------
# Hex grid (GPS interference)
# ---------------------------------------------------------------------------
#
# Pointy-top hexagons on the Mercator plane (axial coordinates, as in Amit
# Patel's hexagon guide at redblobgames.com). Mercator keeps angles, so every
# cell is a regular hexagon on the globe; cells shrink toward the poles the way
# web-map tiles do. Cells stop at 84 degrees of latitude.


def _merc_y(lat_deg: float) -> float:
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2)))


def _merc_lat(y_deg: float) -> float:
    return math.degrees(2 * math.atan(math.exp(math.radians(y_deg))) - math.pi / 2)


def _cube_round(q: float, r: float) -> tuple[int, int]:
    s = -q - r
    rq, rr, rs = round(q), round(r), round(s)
    dq, dr, ds = abs(rq - q), abs(rr - r), abs(rs - s)
    if dq > dr and dq > ds:
        rq = -rr - rs
    elif dr > ds:
        rr = -rq - rs
    return int(rq), int(rr)


def hex_cell(lat: float, lon: float, size_deg: float = HEX_SIZE_DEG) -> tuple[int, int] | None:
    """Axial (q, r) of the hex holding the point, or None past MAX_HEX_LAT."""
    if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > MAX_HEX_LAT:
        return None
    x = _wrap_lon(lon)
    y = _merc_y(lat)
    q = (math.sqrt(3) / 3 * x - y / 3) / size_deg
    r = (2.0 / 3.0 * y) / size_deg
    return _cube_round(q, r)


def hex_center(q: int, r: int, size_deg: float = HEX_SIZE_DEG) -> tuple[float, float]:
    """(lat, lon) of a hex centre."""
    x = size_deg * math.sqrt(3) * (q + r / 2)
    y = size_deg * 1.5 * r
    return _merc_lat(y), x


def hex_boundary(q: int, r: int, size_deg: float = HEX_SIZE_DEG) -> list[list[float]]:
    """The six corners as [lon, lat] pairs, counter-clockwise."""
    x0 = size_deg * math.sqrt(3) * (q + r / 2)
    y0 = size_deg * 1.5 * r
    out = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        out.append([
            round(x0 + size_deg * math.cos(a), 4),
            round(_merc_lat(y0 + size_deg * math.sin(a)), 4),
        ])
    return out


def gpsjam_percent(bad: int, total: int) -> float:
    """GPSJam's share: 100 * (bad - 1) / total, the minus one damping thin hexes."""
    if total <= 0:
        return 0.0
    return max(0.0, 100.0 * (bad - 1) / total)


def interference_level(pct: float) -> str:
    if pct > LEVEL_HIGH_PCT:
        return "high"
    if pct >= LEVEL_MEDIUM_PCT:
        return "medium"
    return "low"


Observation = tuple[float, float, float, str, bool]  # (t, lat, lon, hex, degraded)


def interference_hexes(
    observations: Iterable[Observation],
    now_s: float,
    window_s: float = GPS_WINDOW_S,
    size_deg: float = HEX_SIZE_DEG,
    min_aircraft: int = MIN_HEX_AIRCRAFT,
) -> list[dict[str, Any]]:
    """Hexes with at least *min_aircraft* distinct aircraft in the window.

    An aircraft counts once per hex, as degraded if any of its reports there
    was. Highest share first.
    """
    cells: dict[tuple[int, int], dict[str, bool]] = {}
    for t, lat, lon, hex_id, bad in observations:
        if now_s - t > window_s or t - now_s > 300:
            continue
        key = hex_cell(lat, lon, size_deg)
        if key is None:
            continue
        seen = cells.setdefault(key, {})
        seen[hex_id] = seen.get(hex_id, False) or bad
    out: list[dict[str, Any]] = []
    for (q, r), aircraft in cells.items():
        total = len(aircraft)
        if total < min_aircraft:
            continue
        bad_count = sum(1 for v in aircraft.values() if v)
        pct = gpsjam_percent(bad_count, total)
        clat, clon = hex_center(q, r, size_deg)
        out.append({
            "id": f"{q},{r}",
            "lat": round(clat, 4),
            "lon": round(clon, 4),
            "aircraft": total,
            "degraded": bad_count,
            "pct": round(pct, 1),
            "level": interference_level(pct),
            "boundary": hex_boundary(q, r, size_deg),
        })
    out.sort(key=lambda h: (-h["pct"], -h["aircraft"], h["id"]))
    return out


# ---------------------------------------------------------------------------
# Routes (VRS standing data) and LiveATC
# ---------------------------------------------------------------------------


def liveatc_url(icao: object) -> str | None:
    if not isinstance(icao, str) or not _ICAO_AIRPORT_RE.match(icao.strip().upper()):
        return None
    return LIVEATC_URL.format(icao=icao.strip().upper())


def airline_callsign(callsign: object) -> str | None:
    """The callsign upper-cased when it has the airline shape (ICAO code + number)."""
    if not isinstance(callsign, str):
        return None
    text = callsign.strip().upper()
    return text if _AIRLINE_CALLSIGN_RE.match(text) else None


def _airport(entry: Mapping[str, Any]) -> dict[str, Any]:
    icao = str(entry.get("icao") or "").upper()
    return {
        "icao": icao,
        "iata": str(entry.get("iata") or "")[:3],
        "name": str(entry.get("name") or "")[:80],
        "location": str(entry.get("location") or "")[:60],
        "country": str(entry.get("countryiso2") or "")[:2],
        "lat": _num(entry.get("lat")),
        "lon": _num(entry.get("lon")),
        "liveatc": liveatc_url(icao),
    }


def leg_fit(
    lat: float, lon: float, a: Mapping[str, Any], b: Mapping[str, Any]
) -> tuple[bool, float]:
    """(plausible, detour km) for a position on the leg A to B.

    Plausible within max(50 NM, 20 percent of the leg) of either airport or of
    the great circle between them, the tolerance adsb.lol uses for its own
    route check (adsblol/api, BSD-3-Clause), with a proper cross-track test.
    """
    a_lat, a_lon = float(a["lat"]), float(a["lon"])
    b_lat, b_lon = float(b["lat"]), float(b["lon"])
    d_ab = haversine_km(a_lat, a_lon, b_lat, b_lon)
    d_ap = haversine_km(a_lat, a_lon, lat, lon)
    d_pb = haversine_km(lat, lon, b_lat, b_lon)
    tol = max(50 * NM_KM, 0.2 * d_ab)
    detour = d_ap + d_pb - d_ab
    if d_ap <= tol or d_pb <= tol:
        return True, detour
    xt, at = cross_track_km(lat, lon, a_lat, a_lon, b_lat, b_lon)
    return (abs(xt) <= tol and -tol <= at <= d_ab + tol), detour


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def route_summary(
    route: Mapping[str, Any],
    lat: float | None,
    lon: float | None,
    track: float | None = None,
) -> dict[str, Any] | None:
    """Origin and destination of the leg the aircraft is on, from a VRS route.

    Legs are ranked by plausibility, then by whether the track points within 90
    degrees of the leg's destination (which tells an out-and-back apart), then
    by the smallest detour.
    """
    airports = [
        a for a in (route.get("_airports") or [])
        if isinstance(a, dict)
        and _num(a.get("lat")) is not None
        and _num(a.get("lon")) is not None
        and isinstance(a.get("icao"), str)
        and _ICAO_AIRPORT_RE.match(a["icao"].upper())
    ]
    if len(airports) < 2:
        return None
    leg, plausible = 0, None
    if lat is not None and lon is not None:
        best: tuple[bool, bool, float, int] | None = None
        for i in range(len(airports) - 1):
            a, b = airports[i], airports[i + 1]
            ok, detour = leg_fit(lat, lon, a, b)
            heading_ok = track is None or _angle_diff(
                bearing_deg(lat, lon, float(b["lat"]), float(b["lon"])), track
            ) <= 90.0
            cand = (ok, heading_ok, -detour, -i)
            if best is None or cand > best:
                best = cand
        if best is not None:
            leg, plausible = -best[3], best[0]
    return {
        "origin": _airport(airports[leg]),
        "destination": _airport(airports[leg + 1]),
        "legs": len(airports) - 1,
        "codes": str(route.get("airport_codes") or "")[:60],
        "airline_code": str(route.get("airline_code") or "")[:4],
        "plausible": plausible,
        "source": ROUTE_SOURCE,
        "note": "Scheduled route filed for this callsign, not reported by the aircraft.",
    }


def nearest_site(
    sites: Iterable[Mapping[str, Any]], lat: float, lon: float
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for s in sites:
        slat, slon = _num(s.get("lat")), _num(s.get("lon"))
        if slat is None or slon is None:
            continue
        d = haversine_km(lat, lon, slat, slon)
        if best is None or d < best["distance_km"]:
            best = {
                "site": {
                    "id": str(s.get("id") or ""),
                    "name": str(s.get("name") or "UNNAMED")[:80],
                    "kind": str(s.get("kind") or "military")[:40],
                    "operator": str(s.get("operator") or "")[:80],
                    "lat": slat,
                    "lon": slon,
                },
                "distance_km": d,
                "bearing_deg": round(bearing_deg(lat, lon, slat, slon), 0),
            }
    if best is not None:
        best["distance_km"] = round(best["distance_km"], 1)
    return best


# ---------------------------------------------------------------------------
# Request budget
# ---------------------------------------------------------------------------


def api_key() -> str | None:
    key = (os.environ.get("ADSBLOL_API_KEY") or "").strip()
    return key or None


def daily_budget() -> int:
    raw = (os.environ.get("SDA_ADSBLOL_BUDGET") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_DAILY_BUDGET
    except ValueError:
        value = DEFAULT_DAILY_BUDGET
    return max(24, min(1_000_000, value))


def feed_intervals(budget: int) -> dict[str, float]:
    """FAST_INTERVALS_S stretched so the feeds fit FEED_SHARE of *budget* a day."""
    per_day = sum(86400.0 / s for s in FAST_INTERVALS_S.values())
    scale = max(1.0, per_day / max(1.0, budget * FEED_SHARE))
    return {k: round(v * scale, 1) for k, v in FAST_INTERVALS_S.items()}


def _retry_after(headers: Mapping[str, str]) -> float:
    try:
        return max(1.0, min(86400.0, float(headers.get("retry-after", "60"))))
    except ValueError:
        return 60.0


class TokenBucket:
    """Daily request budget: refills evenly, holds one token for emergencies."""

    def __init__(self, per_day: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_day = per_day
        self.rate = per_day / 86400.0
        self.capacity = float(max(8, min(60, per_day // 40)))
        self.tokens = self.capacity
        self.clock = clock
        self.updated = clock()
        self.blocked_until = 0.0
        self.spent = 0
        self.denied = 0
        self.upstream_remaining: int | None = None
        self.upstream_limit = ""

    def _refill(self, now: float) -> None:
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, priority: str = "normal") -> bool:
        now = self.clock()
        self._refill(now)
        floor = 1.0 if priority == "emergency" else 2.0
        if now < self.blocked_until or self.tokens < floor:
            self.denied += 1
            return False
        self.tokens -= 1.0
        self.spent += 1
        return True

    def block_for(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, self.clock() + seconds)
        self.tokens = 0.0

    def note_headers(self, headers: Mapping[str, str]) -> None:
        limit = headers.get("x-ratelimit-limit")
        if limit:
            self.upstream_limit = limit[:60]
        try:
            remaining = int(headers.get("x-ratelimit-remaining", ""))
        except ValueError:
            return
        self.upstream_remaining = remaining
        if remaining <= 0:
            try:
                reset = float(headers.get("x-ratelimit-reset", "3600"))
            except ValueError:
                reset = 3600.0
            self.block_for(max(60.0, min(86400.0, reset)))
        else:
            self.tokens = min(self.tokens, float(remaining))

    def status(self) -> dict[str, Any]:
        now = self.clock()
        self._refill(now)
        return {
            "per_day": self.per_day,
            "tokens": round(self.tokens, 2),
            "capacity": self.capacity,
            "spent": self.spent,
            "denied": self.denied,
            "blocked_s": round(max(0.0, self.blocked_until - now), 0),
            "upstream_remaining": self.upstream_remaining,
            "upstream_limit": self.upstream_limit,
        }


# ---------------------------------------------------------------------------
# Process state: store, observations, feeds, caches
# ---------------------------------------------------------------------------


class _Feed:
    def __init__(self) -> None:
        self.attempted_at: float | None = None  # monotonic
        self.fetched_at: float | None = None  # unix seconds
        self.aircraft: list[dict[str, Any]] = []
        self.error = ""
        self.inflight = False


class _State:
    def __init__(self) -> None:
        self.bucket = TokenBucket(daily_budget())
        self.store: dict[str, dict[str, Any]] = {}
        self.observations: deque[Observation] = deque(maxlen=MAX_OBSERVATIONS)
        self.feeds: dict[str, _Feed] = {}
        self.lookups: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self.routes: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self.sites: dict[str, tuple[float, dict[str, Any]]] = {}
        self.samples: dict[str, float] = {}
        self.last_sample_at: float | None = None
        self.sample_areas: deque[dict[str, Any]] = deque(maxlen=200)


_STATE = _State()


def state() -> _State:
    return _STATE


def reset_state() -> None:
    """Forget every cached feed, record, budget and sample. Used by the tests."""
    global _STATE
    _STATE = _State()


def ingest(data: Mapping[str, Any], now_s: float | None = None) -> list[dict[str, Any]]:
    """Parse a /v2 answer, update the store and the GPS observations, return the records."""
    st = _STATE
    server_ms = _num(data.get("now"))
    now = now_s if now_s is not None else (server_ms / 1000.0 if server_ms else time.time())
    out: list[dict[str, Any]] = []
    for ac in data.get("ac") or []:
        rec = parse_aircraft(ac, now)
        if rec is None:
            continue
        out.append(rec)
        old = st.store.get(rec["hex"])
        newer = rec["lat"] is not None and (
            old is None or old.get("lat") is None or rec["seen_at"] >= old["seen_at"]
        )
        if newer or old is None:
            st.store[rec["hex"]] = rec
        if rec["gps"] is not None and rec["lat"] is not None and rec["position_source"] != "last":
            st.observations.append(
                (rec["seen_at"], rec["lat"], rec["lon"], rec["hex"], rec["gps"] == "bad")
            )
        elif rec["gps"] == "bad" and isinstance(ac, dict):
            # GPS lost: count it where the GPS was last good.
            glat, glon = _num(ac.get("gpsOkLat")), _num(ac.get("gpsOkLon"))
            if glat is not None and glon is not None:
                st.observations.append((rec["seen_at"], glat, glon, rec["hex"], True))
    if len(st.store) > MAX_STORE:
        _prune_store(now)
    return out


def _prune_store(now_s: float) -> None:
    st = _STATE
    cutoff = now_s - GPS_WINDOW_S
    for hex_id in [h for h, r in st.store.items() if r["seen_at"] < cutoff]:
        del st.store[hex_id]
    if len(st.store) > MAX_STORE:
        oldest = sorted(st.store.items(), key=lambda kv: kv[1]["seen_at"])
        for hex_id, _ in oldest[: len(st.store) - MAX_STORE]:
            del st.store[hex_id]


def aircraft_snapshot(max_age_s: float = GPS_WINDOW_S) -> list[dict[str, Any]]:
    """Every aircraft adsb.lol returned to this process in the last *max_age_s*.

    For other modules (the universal search): icao24, callsign, registration,
    type, lat, lon, alt_m, gs_kt, track, squawk, military, seen_at (unix s).
    Private aircraft come with callsign and registration blank.
    """
    now = time.time()
    out = []
    for rec in _STATE.store.values():
        if rec.get("lat") is None or now - rec["seen_at"] > max_age_s:
            continue
        out.append({
            "icao24": rec["hex"],
            "callsign": rec["callsign"],
            "registration": rec["registration"],
            "type": rec["type"],
            "lat": rec["lat"],
            "lon": rec["lon"],
            "alt_m": rec["alt_m"],
            "gs_kt": rec["gs_kt"],
            "track": rec["track"],
            "squawk": rec["squawk"],
            "military": rec["military"],
            "seen_at": rec["seen_at"],
        })
    return out


# ---------------------------------------------------------------------------
# Upstream calls
# ---------------------------------------------------------------------------


def _lower_headers(headers: object) -> dict[str, str]:
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return {}
    return {str(k).lower(): str(v) for k, v in items}


async def _get_adsblol(path: str, priority: str) -> tuple[dict[str, Any] | None, str]:
    """GET one metered adsb.lol path through the shared budget: (payload, error)."""
    st = _STATE
    if not st.bucket.take(priority):
        return None, "budget"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = api_key()
    if key:
        headers["x-api-key"] = key
    try:
        async with httpx.AsyncClient(
            timeout=12.0, headers=headers, follow_redirects=True
        ) as client:
            resp = await client.get(ADSBLOL_BASE + path)
            hdrs = _lower_headers(getattr(resp, "headers", None))
            st.bucket.note_headers(hdrs)
            if resp.status_code == 429:
                st.bucket.block_for(_retry_after(hdrs))
                return None, "adsb.lol rate limit reached"
            if resp.status_code != 200:
                return None, f"adsb.lol answered {resp.status_code}"
            data = resp.json()
    except Exception:
        return None, "adsb.lol request failed"
    if not isinstance(data, dict) or not isinstance(data.get("ac", []), list):
        return None, "adsb.lol sent an unexpected answer"
    return data, ""


async def fetch_v2(path: str, purpose: str = "lookup") -> dict[str, Any] | None:
    """GET an adsb.lol /v2 path through the shared budget, cached LOOKUP_CACHE_S.

    For other modules: returns the parsed answer, or None when the path is not
    a /v2 path, the budget is spent or the upstream failed. Records it returns
    are added to the store behind aircraft_snapshot().
    """
    if not isinstance(path, str) or not _V2_PATH_RE.match(path) or ".." in path:
        return None
    st = _STATE
    now = time.monotonic()
    hit = st.lookups.get(path)
    if hit is not None and now - hit[0] < LOOKUP_CACHE_S:
        return hit[1]
    priority = "emergency" if purpose == "emergency" else "lookup"
    data, err = await _get_adsblol(path, priority)
    if err == "budget":
        return hit[1] if hit is not None else None
    if data is not None:
        ingest(data)
    if len(st.lookups) > 500:
        st.lookups.clear()
    st.lookups[path] = (time.monotonic(), data)
    return data


async def refresh_feed(name: str, path: str, priority: str) -> _Feed:
    """Refresh one feed when its interval has passed and the budget allows."""
    st = _STATE
    feed = st.feeds.setdefault(name, _Feed())
    interval = feed_intervals(st.bucket.per_day).get(name, 600.0)
    now = time.monotonic()
    if feed.inflight or (feed.attempted_at is not None and now - feed.attempted_at < interval):
        return feed
    feed.inflight = True
    try:
        data, err = await _get_adsblol(path, priority)
        if err == "budget":
            feed.error = "Waiting for the adsb.lol request budget."
            return feed
        feed.attempted_at = time.monotonic()
        if data is None:
            feed.error = err
            return feed
        feed.aircraft = ingest(data)
        feed.fetched_at = time.time()
        feed.error = ""
    finally:
        feed.inflight = False
    return feed


def _feed_meta(feeds: Mapping[str, _Feed]) -> dict[str, Any]:
    st = _STATE
    intervals = feed_intervals(st.bucket.per_day)
    now = time.monotonic()
    meta: dict[str, Any] = {}
    for name, feed in feeds.items():
        wait = 0.0
        if feed.attempted_at is not None:
            wait = max(0.0, intervals.get(name, 600.0) - (now - feed.attempted_at))
        meta[name] = {
            "fetched_at": feed.fetched_at,
            "error": feed.error,
            "interval_s": intervals.get(name),
            "next_refresh_s": round(wait, 0),
        }
    return meta


def _public(rec: Mapping[str, Any], now_s: float) -> dict[str, Any]:
    out = {k: rec[k] for k in (
        "hex", "callsign", "registration", "type", "operator", "military", "private",
        "lat", "lon", "alt_m", "on_ground", "gs_kt", "track", "squawk", "category",
        "gps", "position_source", "seen_at",
    )}
    out["age_s"] = round(max(0.0, now_s - rec["seen_at"]), 1)
    if rec.get("squawk") in EMERGENCY_SQUAWKS:
        out["meaning"] = EMERGENCY_SQUAWKS[rec["squawk"]]
    return out


async def military() -> dict[str, Any]:
    feed = await refresh_feed("mil", "/v2/mil", "normal")
    now = time.time()
    rows = [_public(r, now) for r in aircraft_with_position(feed.aircraft)]
    return {
        "aircraft": rows,
        "count": len(rows),
        "reported_total": len(feed.aircraft),
        "fetched_at": feed.fetched_at,
        "server_now": now,
        "feeds": _feed_meta({"mil": feed}),
        "error": feed.error,
        "source": ADSBLOL_SOURCE,
    }


async def emergencies() -> dict[str, Any]:
    """Aircraft squawking 7500, 7600 or 7700, from the squawk feeds and the store."""
    feeds = {}
    for code in EMERGENCY_SQUAWKS:
        name = f"sqk{code}"
        feeds[name] = await refresh_feed(name, f"/v2/sqk/{code}", "emergency")
    now = time.time()
    merged: dict[str, dict[str, Any]] = {}
    pools: list[Iterable[dict[str, Any]]] = [f.aircraft for f in feeds.values()]
    pools.append(list(_STATE.store.values()))
    for pool in pools:
        for rec in pool:
            if rec.get("squawk") not in EMERGENCY_SQUAWKS or rec.get("lat") is None:
                continue
            if now - rec["seen_at"] > DEAD_RECKON_MAX_S:
                continue
            old = merged.get(rec["hex"])
            if old is None or rec["seen_at"] > old["seen_at"]:
                merged[rec["hex"]] = rec
    rows = [_public(r, now) for r in merged.values()]
    rows.sort(key=lambda r: (list(EMERGENCY_SQUAWKS).index(r["squawk"]), -r["seen_at"]))
    errors = sorted({f.error for f in feeds.values() if f.error})
    return {
        "aircraft": rows,
        "count": len(rows),
        "airborne": sum(1 for r in rows if not r["on_ground"]),
        "codes": dict(EMERGENCY_SQUAWKS),
        "server_now": now,
        "feeds": _feed_meta(feeds),
        "error": "; ".join(errors),
        "source": ADSBLOL_SOURCE,
    }


def _sample_key(lat: float, lon: float) -> tuple[str, float, float]:
    g = SAMPLE_GRID_DEG
    clat = max(-80.0, min(80.0, round(lat / g) * g))
    clon = _wrap_lon(round(lon / g) * g)
    if clon == -180.0:
        clon = 180.0
    return f"{clat:g},{clon:g}", clat, clon


async def sample_area(lat: float, lon: float) -> str:
    """Ask /v2/point around the view centre when that area has not been sampled lately.

    Returns 'sampled', 'recent', 'waiting' (the point interval has not passed),
    'budget' or 'failed'.
    """
    st = _STATE
    key, clat, clon = _sample_key(lat, lon)
    mono = time.monotonic()
    last = st.samples.get(key)
    if last is not None and mono - last < SAMPLE_REPEAT_S:
        return "recent"
    gap = feed_intervals(st.bucket.per_day)["point"]
    if st.last_sample_at is not None and mono - st.last_sample_at < gap:
        return "waiting"
    data, err = await _get_adsblol(f"/v2/point/{clat:g}/{clon:g}/{SAMPLE_RADIUS_NM}", "normal")
    if err == "budget":
        return "budget"
    st.last_sample_at = time.monotonic()
    st.samples[key] = st.last_sample_at
    if data is None:
        return "failed"
    ingest(data)
    st.sample_areas.append({
        "lat": clat, "lon": clon, "radius_km": round(SAMPLE_RADIUS_NM * NM_KM, 0),
        "at": time.time(),
    })
    return "sampled"


async def gps_interference(
    lat: float | None, lon: float | None, size_deg: float = HEX_SIZE_DEG
) -> dict[str, Any]:
    sample = "none"
    if lat is not None and lon is not None:
        sample = await sample_area(lat, lon)
    st = _STATE
    now = time.time()
    obs = [o for o in st.observations if now - o[0] <= GPS_WINDOW_S]
    hexes = interference_hexes(obs, now, GPS_WINDOW_S, size_deg)
    counted = len({o[3] for o in obs})
    areas = [a for a in st.sample_areas if now - a["at"] <= GPS_WINDOW_S]
    return {
        "hexes": hexes,
        "count": len(hexes),
        "aircraft_counted": counted,
        "window_s": GPS_WINDOW_S,
        "size_deg": size_deg,
        "sample": sample,
        "sampled_areas": areas,
        "levels": {
            "low": f"under {LEVEL_MEDIUM_PCT:g}% degraded",
            "medium": f"{LEVEL_MEDIUM_PCT:g} to {LEVEL_HIGH_PCT:g}% degraded",
            "high": f"over {LEVEL_HIGH_PCT:g}% degraded",
        },
        "method": (
            "Inferred from aircraft reports, not measured: per hex over the last hour, "
            "100 x (degraded - 1) / aircraft, where degraded means the aircraft reported "
            f"NIC below {NIC_LOW} or NACp below {NACP_LOW} in its own ADS-B position, or "
            "adsb.lol marked its GPS as lost. MLAT and TIS-B positions are not counted. "
            "Only areas adsb.lol was asked about appear: the military and emergency "
            "feeds and the view centres sampled."
        ),
        "server_now": now,
        "source": ADSBLOL_SOURCE,
    }


async def lookup_route(callsign: str) -> dict[str, Any] | None:
    """The VRS standing-data route for an airline callsign, cached ROUTE_CACHE_S."""
    cs = airline_callsign(callsign)
    if cs is None:
        return None
    st = _STATE
    now = time.monotonic()
    hit = st.routes.get(cs)
    if hit is not None and now - hit[0] < ROUTE_CACHE_S:
        return hit[1]
    route: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(
            timeout=8.0, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(ROUTE_URL.format(prefix=cs[:2], callsign=cs))
            if resp.status_code == 200:
                body = resp.json()
                route = body if isinstance(body, dict) else None
            elif resp.status_code != 404:
                return None  # transient: do not cache
    except Exception:
        return None
    if len(st.routes) > 2000:
        st.routes.clear()
    st.routes[cs] = (time.monotonic(), route)
    return route


async def aircraft_card(
    hex_id: str,
    callsign: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Everything the card shows for one aircraft. *hex_id* must be normalised."""
    st = _STATE
    rec = st.store.get(hex_id)
    lookup = "store"
    now = time.time()
    if rec is None or now - rec["seen_at"] > REPORT_FRESH_S:
        data = await fetch_v2(f"/v2/hex/{hex_id}", "lookup")
        lookup = "adsb.lol" if data is not None else ("unavailable" if rec is None else "store")
        rec = st.store.get(hex_id) or rec
    now = time.time()
    out: dict[str, Any] = {"hex": hex_id, "found": rec is not None, "lookup": lookup,
                           "server_now": now, "sources": [ADSBLOL_SOURCE]}
    if rec is not None:
        out.update(_public(rec, now))
        out["description"] = rec.get("description", "")
        out["nic"] = rec.get("nic")
        out["nac_p"] = rec.get("nac_p")
        out["emergency_status"] = rec.get("emergency_status", "")
        out["position"] = position_state(rec, now)
    else:
        out["position"] = {"state": "lost", "age_s": None}
    private = bool(rec and rec.get("private"))
    out["private"] = private
    if private:
        out["note"] = ("The owner asked for privacy (FAA LADD or a PIA address); "
                       "identity and route are withheld.")
        out["route"] = None
        return out
    cs = (rec.get("callsign") if rec else "") or (callsign or "").strip().upper()
    out["callsign"] = cs
    plat = rec.get("lat") if rec else lat
    plon = rec.get("lon") if rec else lon
    route = None
    if airline_callsign(cs):
        raw = await lookup_route(cs)
        if raw is not None:
            route = route_summary(raw, plat, plon, rec.get("track") if rec else None)
    out["route"] = route
    if route is not None:
        out["sources"].append(ROUTE_SOURCE)
        if not out.get("operator") and route["airline_code"]:
            out["operator"] = route["airline_code"]
            out["operator_source"] = "airline code from the callsign"
    elif out.get("operator"):
        out["operator_source"] = "adsb.lol aircraft database"
    return out


async def nearest_military_site(
    lat: float,
    lon: float,
    fetch_sites: Callable[[float, float, float, float], Awaitable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Nearest mapped military site to a point, from a cached Overpass box.

    The box is SITE_HALF_SPAN_DEG either side of the whole-degree cell holding
    the point, under the 6 degree cap of /api/military-sites, so every point in
    the cell is searched at least 2 degrees of latitude out.
    """
    st = _STATE
    clat, clon = round(lat), round(lon)
    key = f"{clat},{clon}"
    now = time.monotonic()
    hit = st.sites.get(key)
    box = {
        "south": max(-90.0, clat - SITE_HALF_SPAN_DEG),
        "west": max(-180.0, clon - SITE_HALF_SPAN_DEG),
        "north": min(90.0, clat + SITE_HALF_SPAN_DEG),
        "east": min(180.0, clon + SITE_HALF_SPAN_DEG),
    }
    data: dict[str, Any] | None = None
    if hit is not None:
        ttl = SITE_ERROR_CACHE_S if hit[1].get("error") else SITE_CACHE_S
        if now - hit[0] < ttl:
            data = hit[1]
    if data is None:
        try:
            got = await fetch_sites(box["south"], box["west"], box["north"], box["east"])
            data = dict(got) if isinstance(got, Mapping) else {"sites": [], "error": "bad answer"}
        except Exception:
            data = {"sites": [], "error": "Overpass request failed"}
        if len(st.sites) > 1000:
            st.sites.clear()
        st.sites[key] = (time.monotonic(), data)
    sites = data.get("sites") or []
    near = nearest_site(sites if isinstance(sites, list) else [], lat, lon)
    return {
        "nearest": near,
        "sites_in_box": len(sites) if isinstance(sites, list) else 0,
        "box": box,
        "error": str(data.get("error") or ""),
        "source": SITE_SOURCE,
    }


def status() -> dict[str, Any]:
    st = _STATE
    keyed = api_key() is not None
    intervals = feed_intervals(st.bucket.per_day)
    if keyed:
        note = "adsb.lol key set (ADSBLOL_API_KEY)."
    else:
        note = (f"No adsb.lol key: the API allows {KEYLESS_DAILY_LIMIT} requests a day per IP, so "
                f"feeds refresh about every {round(intervals['mil'] / 60)} min. A free key "
                "(adsb.lol, set ADSBLOL_API_KEY and raise SDA_ADSBLOL_BUDGET) makes it faster.")
    return {
        "source": ADSBLOL_SOURCE,
        "keyed": keyed,
        "key_env": "ADSBLOL_API_KEY",
        "budget_env": "SDA_ADSBLOL_BUDGET",
        "budget": st.bucket.status(),
        "intervals_s": intervals,
        "note": note,
        "report_fresh_s": REPORT_FRESH_S,
        "dead_reckon_max_s": DEAD_RECKON_MAX_S,
        "gps_window_s": GPS_WINDOW_S,
        "aircraft_known": len(st.store),
        "emergency_codes": dict(EMERGENCY_SQUAWKS),
    }
