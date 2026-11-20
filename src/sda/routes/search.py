"""Universal search for the globe: one query, every local catalog.

* GET /api/search?q=&limit=&kinds=&places=&live=

One box finds a coordinate typed as decimal degrees, satellites by name or
NORAD id (alpha-5 too) from the TLE store, aircraft by callsign, registration
or ICAO hex from the shared adsb.lol cache in ``sda.aviation``, public cameras
by name or city (``sda.cameras``), summits by name (``sda.peaks``) and places
through the existing geocoder route (``routes/worldview.proxy_geocode``).

Every source is local or cached, so a keystroke costs nothing upstream, with
two exceptions that the caller asks for explicitly:

* ``places=1`` asks OpenStreetMap Nominatim, whose usage policy forbids
  search-as-you-type. The globe sends it only when Enter is pressed; answers
  are cached for a day and calls are spaced at least 1.1 s apart.
* ``live=1`` lets an aircraft-shaped query that has no exact match in the
  cache ask adsb.lol once, through ``sda.aviation.fetch_v2``, which holds the
  shared daily request budget. Again the globe sends it only on Enter.

Results are merged on one match scale (exact, prefix, word prefix, substring),
then by kind, and each kind keeps a share of the list so one large catalog does
not crowd out the rest.

Ethics: nothing here searches people. The aircraft cache withholds callsign and
registration for privately operated aircraft (sda.aviation), so a private
registration finds nothing, and no source indexes named individuals.

Frames: positions are geodetic degrees for display. Satellite points come from
SGP4 (WGS72) in TEME, turned into latitude, longitude and height by the same
display helper the globe's position feed uses.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from sda import cameras as camera_catalog
from sda import peaks as peak_catalog
from sda.propagator import build_satrec, datetime_to_jd, propagate_at
from sda.tle_store import TLEStore, decode_catalog_field

router = APIRouter(tags=["Search"])

KINDS: tuple[str, ...] = ("coordinate", "satellite", "aircraft", "camera", "peak", "place")
DEFAULT_LIMIT = 12
MAX_LIMIT = 50
MAX_QUERY = 120

# One match scale for every kind, best first.
TIER_EXACT = 0
TIER_PREFIX = 1
TIER_WORD = 2
TIER_SUBSTRING = 3
TIER_OTHER = 4

PLACE_TTL_S = 24 * 3600.0
PLACE_GAP_S = 1.1
PLACE_CACHE_MAX = 512

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_COORD = re.compile(
    r"^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$"
)
_ALPHA5 = re.compile(r"^[A-HJ-NP-Z]\d{4}$")
_HEX = re.compile(r"^[0-9a-f]{6}$")
_CALLSIGN = re.compile(r"^[A-Z]{2,3}\d[0-9A-Z]{0,4}$")
_REGISTRATION = re.compile(r"^(?:[A-Z0-9]{1,2}-[A-Z0-9]{2,5}|N\d[0-9A-Z]{0,4})$")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=65536)
def fold(text: str) -> str:
    """Lower case, accents dropped, every run of punctuation one space."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD.sub(" ", plain.lower()).strip()


def name_tier(name: str, query: str) -> int:
    """Tier of a folded *name* against a folded *query*, or -1 for no match."""
    if not name or not query:
        return -1
    if name == query:
        return TIER_EXACT
    if name.startswith(query):
        return TIER_PREFIX
    if (" " + query) in name:
        return TIER_WORD
    if query in name:
        return TIER_SUBSTRING
    return -1


# Generic summit words. "Mount Everest" is the exact answer to "everest": the
# word says what kind of thing it is, not which one.
_SUMMIT_WORDS = frozenset({
    "mount", "mt", "mont", "monte", "peak", "pic", "pico", "piz", "cerro", "nevado",
    "volcan", "gunung", "gora", "jebel", "djebel", "jabal",
})

#: Places whose name matches exactly but which Nominatim rates below this
#: (0 to 1) rank one tier lower: a dry cleaner called ISS, a hamlet called Everest.
PLACE_MINOR_IMPORTANCE = 0.5

#: Catalog names that are not what people type for the best-known objects.
SATELLITE_ALIASES: dict[str, int] = {
    "iss": 25544, "international space station": 25544,
    "hubble": 20580, "hst": 20580, "hubble space telescope": 20580,
    "tiangong": 48274, "css": 48274, "chinese space station": 48274,
}


def summit_tier(name: str, query: str) -> int:
    """name_tier, with generic summit words dropped from the name as well."""
    tier = name_tier(name, query)
    words = name.split()
    core = " ".join(w for w in words if w not in _SUMMIT_WORDS)
    if core and core != name:
        alt = name_tier(core, query)
        if alt >= 0 and (tier < 0 or alt < tier):
            tier = alt
    return tier


def canonical_id(text: str) -> str:
    """Upper-case letters and digits only: "G-EUPT", "geupt" and "GEUPT" meet."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def query_shape(raw: str) -> dict[str, bool]:
    """What a query could be. Drives source order and the live lookups."""
    text = raw.strip()
    upper = text.upper()
    return {
        "digits": text.isdigit(),
        "alpha5": bool(_ALPHA5.match(upper)),
        "hex": bool(_HEX.match(text.lower())) and any(c.isdigit() for c in text),
        "callsign": bool(_CALLSIGN.match(upper.replace(" ", ""))),
        "registration": bool(_REGISTRATION.match(upper)),
    }


def parse_coordinate(raw: str) -> tuple[float, float] | None:
    """Decimal "lat, lon" (comma, semicolon or space between), or None."""
    m = _COORD.match(raw)
    if m is None:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _result(
    kind: str, ident: str, name: str, detail: str, tier: int, **extra: Any
) -> dict[str, Any]:
    lat = _num(extra.pop("lat", None))
    lon = _num(extra.pop("lon", None))
    alt_m = _num(extra.pop("alt_m", None))
    return {
        "kind": kind, "id": ident, "name": name, "detail": detail, "tier": tier,
        "lat": lat, "lon": lon, "alt_m": alt_m,
        "extra": {k: v for k, v in extra.items() if v is not None},
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _store() -> TLEStore:
    """The app's TLE store, imported late as the other routers do."""
    from sda.api import store

    return store


def _sub_point(tle: Any, when: datetime) -> tuple[float, float, float] | None:
    """Latitude, longitude (deg) and height (km) under a satellite, for display."""
    from sda.routes.worldview import _teme_to_geographic

    try:
        state = propagate_at(build_satrec(tle), when)
        jd, fr = datetime_to_jd(when)
        x, y, z = state.position_km
        lat, lon, alt_km = _teme_to_geographic(x, y, z, jd, fr)
    except Exception:
        return None
    if not all(math.isfinite(v) for v in (lat, lon, alt_km)):
        return None
    return lat, lon, alt_km


def search_satellites(
    store: TLEStore, raw: str, limit: int, when: datetime | None = None
) -> list[dict[str, Any]]:
    """Satellites by NORAD id (exact, then prefix) or by name, positions for the hits."""
    text = raw.strip()
    q = fold(text)
    if not q:
        return []
    digits = text.isdigit()
    id_exact: int | None = int(text) if digits else None
    if id_exact is None and _ALPHA5.match(text.upper()):
        try:
            id_exact = decode_catalog_field(text.upper())
        except ValueError:
            id_exact = None
    id_prefix = text.lstrip("0") or "0"
    alias = SATELLITE_ALIASES.get(q)
    hits: list[tuple[int, str, int, Any]] = []
    for tle in store.get_all():
        tier = -1
        if tle.norad_id in (id_exact, alias):
            tier = TIER_EXACT
        elif digits and str(tle.norad_id).startswith(id_prefix):
            tier = TIER_PREFIX
        if tier < 0:
            tier = name_tier(fold(tle.name or ""), q)
        if tier >= 0:
            hits.append((tier, tle.name or "", tle.norad_id, tle))
    # Inside a tier the lowest catalog number first: the oldest object of a
    # family, which is usually the one meant (ISS (ZARYA) before its modules).
    hits.sort(key=lambda h: (h[0], h[2]))
    at = when or datetime.now(UTC)
    out = []
    for tier, name, norad, tle in hits[:limit]:
        point = _sub_point(tle, at)
        parts = [f"NORAD {norad}"]
        if point is not None:
            parts.append(f"{point[2]:,.0f} km")
        out.append(_result(
            "satellite", str(norad), name or f"NORAD {norad}", " | ".join(parts), tier,
            lat=point[0] if point else None, lon=point[1] if point else None,
            alt_m=point[2] * 1000.0 if point else None, norad_id=norad,
        ))
    return out


def search_cameras(raw: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Public cameras by name or city from the local catalog."""
    cat = camera_catalog.get_catalog()
    if not cat.available:
        return [], "unavailable"
    q = fold(raw)
    out = []
    for row in cat.search(raw, limit):
        tier = name_tier(fold(row.get("name") or ""), q)
        if tier < 0:
            city = name_tier(fold(row.get("city") or ""), q)
            tier = max(TIER_WORD, city) if city >= 0 else TIER_OTHER
        where = ", ".join(p for p in (row.get("city"), row.get("country")) if p)
        detail = " | ".join(p for p in (where, row.get("group"),
                                        "plays here" if row.get("playable") else "link") if p)
        out.append(_result(
            "camera", str(row["rid"]), row.get("name") or "Camera", detail, tier,
            lat=row.get("lat"), lon=row.get("lon"), rid=row["rid"],
            group=row.get("group"), playable=bool(row.get("playable")),
        ))
    return out, "ok"


def search_peaks(raw: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Named summits from the local peak catalog, most prominent first."""
    cat = peak_catalog.get_catalog()
    if not cat.available:
        return [], "unavailable"
    q = fold(raw)
    out = []
    for row in cat.search(raw, limit):
        tier = summit_tier(fold(row.get("name") or ""), q)
        if tier < 0:
            tier = TIER_WORD  # matched one of its other names
        parts = []
        if row.get("elev_m") is not None:
            parts.append(f"{row['elev_m']:,.0f} m")
        if row.get("prom_m") is not None:
            parts.append(f"prom {row['prom_m']:,.0f} m")
        where = ", ".join(p for p in (row.get("region"), row.get("country")) if p)
        if where:
            parts.append(where)
        out.append(_result(
            "peak", str(row["id"]), row.get("name") or "Unnamed peak", " | ".join(parts), tier,
            lat=row.get("lat"), lon=row.get("lon"), peak_id=row["id"],
            elev_m=row.get("elev_m"), prom_m=row.get("prom_m"),
        ))
    return out, "ok"


# Aircraft sources: callables returning dicts with icao24, callsign,
# registration, type, lat, lon, alt_m, military, seen_at. sda.aviation's
# aircraft_snapshot is read without registering when the module is present.
_AIRCRAFT_SOURCES: list[Callable[[], Iterable[dict[str, Any]]]] = []


def register_aircraft_source(fn: Callable[[], Iterable[dict[str, Any]]]) -> None:
    """Add an in-memory aircraft list for search to read. Never fetches upstream."""
    if fn not in _AIRCRAFT_SOURCES:
        _AIRCRAFT_SOURCES.append(fn)


def clear_aircraft_sources() -> None:
    """Forget registered aircraft sources. Used by the tests."""
    _AIRCRAFT_SOURCES.clear()


def _aviation() -> Any | None:
    """sda.aviation when it is installed, else None."""
    try:
        return importlib.import_module("sda.aviation")
    except Exception:
        return None


def _aircraft_records() -> tuple[list[dict[str, Any]], bool]:
    """Every cached aircraft, newest fix per ICAO hex, and whether any source exists."""
    sources: list[Callable[[], Iterable[dict[str, Any]]]] = list(_AIRCRAFT_SOURCES)
    av = _aviation()
    snap = getattr(av, "aircraft_snapshot", None) if av is not None else None
    if callable(snap) and snap not in sources:
        sources.append(snap)
    best: dict[str, dict[str, Any]] = {}
    for src in sources:
        try:
            rows = list(src())
        except Exception:
            continue
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            hexid = str(rec.get("icao24") or "").strip().lower()
            if not hexid:
                continue
            seen = _num(rec.get("seen_at")) or 0.0
            old = best.get(hexid)
            if old is None or seen >= (_num(old.get("seen_at")) or 0.0):
                best[hexid] = rec
    return list(best.values()), bool(sources)


def aircraft_tier(rec: dict[str, Any], raw: str) -> int:
    """Tier of one aircraft: hex, callsign, then registration, exact before partial.

    The order (hex exact, callsign exact, registration exact, then prefixes,
    then substrings, callsign ahead of registration at equal strength) follows
    rankContactMatch in gods-eye-view src/data/contactMatch.js (MIT, Bilawal
    Sidhu); rewritten here on this module's tier scale.
    """
    q = canonical_id(raw)
    if not q:
        return -1
    hexid = str(rec.get("icao24") or "").strip().lower()
    if hexid and hexid == raw.strip().lower():
        return TIER_EXACT
    call = canonical_id(str(rec.get("callsign") or ""))
    reg = canonical_id(str(rec.get("registration") or ""))
    for tier, test in ((TIER_EXACT, lambda v: v == q), (TIER_PREFIX, lambda v: v.startswith(q)),
                       (TIER_SUBSTRING, lambda v: len(q) >= 3 and q in v)):
        if (call and test(call)) or (reg and test(reg)):
            return tier
    return -1


def _aircraft_result(rec: dict[str, Any], tier: int) -> dict[str, Any]:
    call = str(rec.get("callsign") or "").strip()
    reg = str(rec.get("registration") or "").strip()
    hexid = str(rec.get("icao24") or "").strip().lower()
    alt = _num(rec.get("alt_m"))
    parts = [p for p in (reg if reg and reg != call else "", str(rec.get("type") or ""),
                         f"FL{round(alt / 0.3048 / 100):03d}" if alt and alt > 0 else "",
                         "military" if rec.get("military") else "", f"hex {hexid}") if p]
    return _result(
        "aircraft", hexid, call or reg or hexid.upper(), " | ".join(parts), tier,
        lat=rec.get("lat"), lon=rec.get("lon"), alt_m=alt, icao24=hexid,
        callsign=call or None, registration=reg or None,
        military=bool(rec.get("military")),
    )


def match_aircraft(
    records: Iterable[dict[str, Any]], raw: str, limit: int
) -> list[dict[str, Any]]:
    """Aircraft with a position whose hex, callsign or registration matches."""
    hits = []
    for rec in records:
        if _num(rec.get("lat")) is None or _num(rec.get("lon")) is None:
            continue
        tier = aircraft_tier(rec, raw)
        if tier >= 0:
            hits.append((tier, str(rec.get("callsign") or rec.get("registration") or ""), rec))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [_aircraft_result(rec, tier) for tier, _name, rec in hits[:limit]]


def live_lookup_path(raw: str) -> str | None:
    """The one adsb.lol /v2 path worth asking for an aircraft-shaped query."""
    shape = query_shape(raw)
    text = raw.strip()
    if shape["hex"]:
        return "/v2/hex/" + text.lower()
    if shape["registration"]:
        return "/v2/reg/" + text.upper()
    if shape["callsign"]:
        return "/v2/callsign/" + text.upper().replace(" ", "")
    return None


async def search_aircraft(raw: str, limit: int, live: bool) -> tuple[list[dict[str, Any]], str]:
    records, any_source = _aircraft_records()
    found = match_aircraft(records, raw, limit)
    if live and not any(r["tier"] == TIER_EXACT for r in found):
        av = _aviation()
        fetch = getattr(av, "fetch_v2", None) if av is not None else None
        path = live_lookup_path(raw)
        if callable(fetch) and path is not None:
            try:
                await fetch(path, "lookup")
            except Exception:
                return found, "error"
            records, any_source = _aircraft_records()
            found = match_aircraft(records, raw, limit)
            return found, "ok"
    return found, ("ok" if any_source else "unavailable")


# Places: the existing geocoder route, called by import, cached and spaced.
_place_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_place_last = 0.0
_place_lock: tuple[Any, asyncio.Lock] | None = None


def _lock_for_loop() -> asyncio.Lock:
    global _place_lock
    loop = asyncio.get_running_loop()
    if _place_lock is None or _place_lock[0] is not loop:
        _place_lock = (loop, asyncio.Lock())
    return _place_lock[1]


def clear_place_cache() -> None:
    """Forget cached place answers. Used by the tests."""
    global _place_last
    _place_cache.clear()
    _place_last = 0.0


async def search_places(raw: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    from sda.routes.worldview import proxy_geocode

    global _place_last
    key = fold(raw)
    hit = _place_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < PLACE_TTL_S:
        rows, status = hit[1], "ok"
    else:
        async with _lock_for_loop():
            hit = _place_cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < PLACE_TTL_S:
                rows, status = hit[1], "ok"
            else:
                wait = PLACE_GAP_S - (time.monotonic() - _place_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                _place_last = time.monotonic()
                try:
                    data = await proxy_geocode(raw)
                except Exception:
                    return [], "error"
                rows = [r for r in (data.get("results") or []) if isinstance(r, dict)]
                if data.get("error"):
                    return [], "error"
                if len(_place_cache) >= PLACE_CACHE_MAX:
                    _place_cache.pop(next(iter(_place_cache)))
                _place_cache[key] = (time.monotonic(), rows)
                status = "ok"
    out = []
    for i, row in enumerate(rows[:limit]):
        full = str(row.get("name") or "")
        short = full.split(",")[0].strip() or full
        tier = summit_tier(fold(short), key)
        if tier < 0:
            tier = TIER_OTHER
        importance = _num(row.get("importance"))
        if tier == TIER_EXACT and importance is not None and importance < PLACE_MINOR_IMPORTANCE:
            tier = TIER_PREFIX
        out.append(_result(
            "place", f"place-{i}", short or "Place", full, tier,
            lat=row.get("lat"), lon=row.get("lon"), type=row.get("type"), rank=i,
            importance=importance,
        ))
    # Inside a tier, Nominatim's importance first, then its own order.
    out.sort(key=lambda r: (r["tier"], -(r["extra"].get("importance") or 0.0),
                            r["extra"]["rank"]))
    return out, status


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def kind_order(raw: str) -> list[str]:
    """Kinds in display order for this query: ids first when it looks like one."""
    shape = query_shape(raw)
    if shape["digits"] or shape["alpha5"]:
        return ["coordinate", "satellite", "aircraft", "camera", "peak", "place"]
    if shape["callsign"] or shape["registration"] or shape["hex"]:
        return ["coordinate", "aircraft", "satellite", "camera", "peak", "place"]
    return ["coordinate", "place", "peak", "camera", "satellite", "aircraft"]


def merge(groups: dict[str, list[dict[str, Any]]], raw: str, limit: int) -> list[dict[str, Any]]:
    """One list: best tier first, then kind order; every kind with hits keeps a share."""
    order = kind_order(raw)
    rank = {k: i for i, k in enumerate(order)}
    pool = []
    for kind, rows in groups.items():
        for i, row in enumerate(rows):
            pool.append((row["tier"], rank.get(kind, len(order)), i, row))
    pool.sort(key=lambda t: (t[0], t[1], t[2]))
    live = [k for k in groups if groups[k]]
    share = max(2, limit // max(1, len(live)))
    picked: list[tuple[int, int, int, dict[str, Any]]] = []
    taken: dict[str, int] = {}
    rest = []
    for item in pool:
        kind = item[3]["kind"]
        if taken.get(kind, 0) < share and len(picked) < limit:
            picked.append(item)
            taken[kind] = taken.get(kind, 0) + 1
        else:
            rest.append(item)
    for item in rest:
        if len(picked) >= limit:
            break
        picked.append(item)
    picked.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in picked]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    kind: str = Field(description="coordinate, satellite, aircraft, camera, peak or place")
    id: str
    name: str
    detail: str = ""
    tier: int = Field(description="0 exact, 1 prefix, 2 word prefix, 3 substring, 4 other")
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    count: int
    sources: dict[str, str] = Field(
        description="Per kind: ok, unavailable (catalog not imported or module absent), "
                    "skipped (not asked for) or error")


def _parse_kinds(raw: str | None) -> list[str]:
    if not raw:
        return list(KINDS)
    wanted = [k.strip().lower() for k in raw.split(",")]
    return [k for k in KINDS if k in wanted]


@router.get("/api/search", response_model=SearchResponse,
            summary="Universal search: coordinates, satellites, aircraft, cameras, peaks, places")
async def universal_search(
    q: str = Query(..., min_length=1, max_length=MAX_QUERY, description="What to find"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    kinds: str | None = Query(None, description="Comma list to search only some kinds"),
    places: bool = Query(True, description="Ask the place geocoder (send false while typing)"),
    live: bool = Query(False, description="Allow one adsb.lol lookup for an aircraft id"),
) -> SearchResponse:
    raw = q.strip()
    wanted = _parse_kinds(kinds)
    sources = {k: "skipped" for k in KINDS}
    groups: dict[str, list[dict[str, Any]]] = {}
    if not raw:
        return SearchResponse(query=raw, results=[], count=0, sources=sources)
    long_enough = len(fold(raw)) >= 2 or raw.isdigit()

    if "coordinate" in wanted:
        point = parse_coordinate(raw)
        sources["coordinate"] = "ok"
        if point is not None:
            groups["coordinate"] = [_result(
                "coordinate", f"{point[0]:.6f},{point[1]:.6f}",
                f"{point[0]:.5f}, {point[1]:.5f}", "Decimal degrees", TIER_EXACT,
                lat=point[0], lon=point[1])]

    async def run_sync(kind: str, fn: Callable[[], tuple[list[dict[str, Any]], str]]) -> None:
        try:
            rows, status = await asyncio.to_thread(fn)
        except Exception:
            rows, status = [], "error"
        groups[kind] = rows
        sources[kind] = status

    tasks = []
    if "satellite" in wanted and long_enough:
        def sats() -> tuple[list[dict[str, Any]], str]:
            return search_satellites(_store(), raw, limit), "ok"
        tasks.append(run_sync("satellite", sats))
    if "camera" in wanted and len(raw) >= 2:
        tasks.append(run_sync("camera", lambda: search_cameras(raw, limit)))
    if "peak" in wanted and len(fold(raw)) >= 2:
        tasks.append(run_sync("peak", lambda: search_peaks(raw, limit)))

    async def run_async(kind: str, coro: Any) -> None:
        try:
            rows, status = await coro
        except Exception:
            rows, status = [], "error"
        groups[kind] = rows
        sources[kind] = status

    if "aircraft" in wanted and len(canonical_id(raw)) >= 2:
        tasks.append(run_async("aircraft", search_aircraft(raw, limit, live)))
    if "place" in wanted and places and len(fold(raw)) >= 2 and "coordinate" not in groups:
        tasks.append(run_async("place", search_places(raw, min(limit, 8))))

    if tasks:
        await asyncio.gather(*tasks)
    results = merge(groups, raw, limit)
    return SearchResponse(
        query=raw, results=[SearchResult(**r) for r in results], count=len(results),
        sources=sources,
    )
