"""Orbit visuals for the globe: SATCAT colour attributes, catalog history, debris
clouds and the "visible tonight" list.

Four endpoints behind static/js/orbitviz.js:

* GET  /api/orbitviz/attributes  type, owner, radar size and launch year of every
                                 object on orbit, packed for 30,000+ dots
* GET  /api/orbitviz/history     objects in orbit at the end of each year since 1957,
                                 by object type, from SATCAT launch and decay dates
* GET  /api/orbitviz/debris      the large fragmentation events and the launches with
                                 the most debris still on orbit, by international
                                 designator, with the NORAD ids of the pieces
* POST /api/orbitviz/visible     the brightest sunlit passes over an observer in the
                                 next hours, Heavens-Above style

The first three read the SATCAT store in sda.satcat (disk cache only, never the
network; 503 while it is empty) and carry its provenance and attribution. The
last one reads the app TLE catalog and the CelesTrak "visual" and "stations"
groups the background refresh already holds, screens them in one vectorized SGP4
pass, and refines every candidate window with sda.passes.predict_passes. Nothing
here feeds Pc or risk classification.

Ideas, not code: keeptrack.space (AGPL-3.0, commit db3b64e) colours by object
type, country, RCS and element age (engine/rendering/color-schemes), has a time
machine that replays launches by year (plugins/time-machine) and a breakup view;
its standard-magnitude estimator falls back from a catalogue value to presets to a
size model. Everything below is written for this repo.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sgp4.api import SatrecArray

from sda.constants import EARTH_RADIUS_KM
from sda.models import TLERecord
from sda.passes import (
    CIVIL_TWILIGHT_DEG,
    Observer,
    SatellitePass,
    compass_point,
    gmst_rad,
    predict_passes,
    solar_position_eci_km,
)
from sda.propagator import build_satrec, datetime_to_jd
from sda.satcat import ATTRIBUTION, OBJECT_TYPES, SatcatEntry, SatcatStore, get_store, owner_name
from sda.tle_store import TLEStore

router = APIRouter(prefix="/api/orbitviz", tags=["Orbit visuals"])

FIRST_YEAR = 1957

TYPE_CODES: dict[str, str] = {
    "PAYLOAD": "P", "ROCKET BODY": "R", "DEBRIS": "D", "UNKNOWN": "U",
}
RCS_CODES: dict[str, str] = {"SMALL": "S", "MEDIUM": "M", "LARGE": "L", "UNKNOWN": "U"}

#: One printable character per launch year in /attributes: no quote or backslash,
#: so each costs one byte of JSON. Position 0 is "no launch date"; position k is
#: the year YEAR_BASE + k, which covers 1957 to 2045.
YEAR_ALPHABET = "".join(chr(c) for c in range(35, 127) if chr(c) != "\\")
YEAR_BASE = FIRST_YEAR - 1


def encode_year(year: int) -> str:
    """The /attributes character for a launch year (0 or out of range: no date)."""
    offset = year - YEAR_BASE if year else 0
    if offset <= 0 or offset >= len(YEAR_ALPHABET):
        offset = 0
    return YEAR_ALPHABET[offset]


# ---------------------------------------------------------------------------
# SATCAT snapshot, cached per loaded catalog
# ---------------------------------------------------------------------------


def _satcat() -> SatcatStore:
    """The process SATCAT store with its disk cache loaded (never the network)."""
    store = get_store()
    store.ensure_cache_loaded()
    return store


def _require(store: SatcatStore) -> None:
    if len(store) == 0:
        status = store.status()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "SATCAT not loaded",
                "hint": status["hint"],
                "attribution": ATTRIBUTION,
            },
        )


def satcat_entries(store: SatcatStore) -> list[SatcatEntry]:
    """Every entry in the store, ascending NORAD id, through its public API."""
    out: list[SatcatEntry] = []
    for nid in store.match_ids(on_orbit=False):
        entry = store.get(nid)
        if entry is not None:
            out.append(entry)
    return out


_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[Any, ...], Any]] = {}


def _cached(name: str, store: SatcatStore, extra: tuple[Any, ...],
            build: Callable[[], Any]) -> Any:
    """Build once per loaded catalog (store, download time, size) and argument set."""
    key = (id(store), store.provenance()["fetched_at"], len(store), *extra)
    with _cache_lock:
        held = _cache.get(name)
        if held is not None and held[0] == key:
            return held[1]
    value = build()
    with _cache_lock:
        _cache[name] = (key, value)
    return value


def clear_cache() -> None:
    """Forget every derived table (tests, or after a store swap)."""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Attributes for colour modes
# ---------------------------------------------------------------------------


def build_attributes(entries: Iterable[SatcatEntry], on_orbit: bool = True) -> dict[str, Any]:
    """Columnar attributes, one position per object, ascending NORAD id.

    ``id_deltas`` holds each id minus the previous one (the first minus zero), so a
    catalog of 36,000 ids costs about two characters each. ``types`` and ``rcs`` are
    strings with one code letter per object (``type_codes`` and ``rcs_codes`` decode
    them); ``owner_index`` points into ``owners``, which is sorted by count.
    ``years`` has one character per object: its position in ``year_alphabet`` is the
    launch year minus ``year_base``, and position 0 means no launch date.
    """
    rows = sorted((e for e in entries if not on_orbit or e.on_orbit),
                  key=lambda e: e.norad_id)
    counts: Counter[str] = Counter(e.owner for e in rows)
    order = [code for code, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    index = {code: i for i, code in enumerate(order)}
    deltas: list[int] = []
    previous = 0
    for entry in rows:
        deltas.append(entry.norad_id - previous)
        previous = entry.norad_id
    return {
        "count": len(rows),
        "on_orbit_only": on_orbit,
        "id_deltas": deltas,
        "types": "".join(TYPE_CODES.get(e.object_type, "U") for e in rows),
        "rcs": "".join(RCS_CODES.get(e.rcs_size, "U") for e in rows),
        "owner_index": [index[e.owner] for e in rows],
        "owners": [
            {"code": code, "name": owner_name(code), "count": counts[code]} for code in order
        ],
        "years": "".join(encode_year(e.launch_year) for e in rows),
        "year_base": YEAR_BASE,
        "year_alphabet": YEAR_ALPHABET,
        "type_codes": {v: k for k, v in TYPE_CODES.items()},
        "rcs_codes": {v: k for k, v in RCS_CODES.items()},
    }


# ---------------------------------------------------------------------------
# Catalog history
# ---------------------------------------------------------------------------


def _decay_year(entry: SatcatEntry) -> int | None:
    text = entry.decay_date
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def build_history(entries: Iterable[SatcatEntry], last_year: int) -> dict[str, Any]:
    """Objects in orbit at the end of each year, by object type.

    An object counts for year Y when it was launched in Y or earlier and had not
    decayed by the end of Y (no decay date, or a decay year after Y). Objects with
    no launch date are left out and counted in ``no_launch_date``; a decay year
    earlier than the launch year is read as the launch year.
    """
    last_year = max(last_year, FIRST_YEAR)
    span = last_year - FIRST_YEAR + 1
    add = {t: [0] * (span + 1) for t in OBJECT_TYPES}
    sub = {t: [0] * (span + 1) for t in OBJECT_TYPES}
    launched = [0] * span
    decayed = [0] * span
    no_date = 0
    for entry in entries:
        year = entry.launch_year
        if not year:
            no_date += 1
            continue
        kind = entry.object_type if entry.object_type in add else "UNKNOWN"
        at = min(max(year, FIRST_YEAR), last_year) - FIRST_YEAR
        add[kind][at] += 1
        launched[at] += 1
        end = _decay_year(entry)
        if end is not None:
            end = max(end, year)
            if end <= last_year:
                gone = max(end, FIRST_YEAR) - FIRST_YEAR
                sub[kind][gone] += 1
                decayed[gone] += 1
    in_orbit: dict[str, list[int]] = {}
    for kind in OBJECT_TYPES:
        running = 0
        series = []
        for i in range(span):
            running += add[kind][i] - sub[kind][i]
            series.append(running)
        in_orbit[kind] = series
    total = [sum(in_orbit[k][i] for k in OBJECT_TYPES) for i in range(span)]
    return {
        "first_year": FIRST_YEAR,
        "last_year": last_year,
        "years": list(range(FIRST_YEAR, last_year + 1)),
        "in_orbit": in_orbit,
        "total": total,
        "launched": launched,
        "decayed": decayed,
        "no_launch_date": no_date,
        "definition": "Objects in orbit at the end of each year: launched that year or "
                      "earlier and not decayed by the year's end. Counts include every "
                      "catalogued object, so later years include debris created then.",
    }


# ---------------------------------------------------------------------------
# Debris clouds
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DebrisEvent:
    key: str
    name: str
    designators: tuple[str, ...]
    event_year: int
    kind: str


#: Large fragmentation events. Pieces are the SATCAT DEBRIS objects whose
#: international designator starts with one of the launch designators. Event
#: years are those of the fragmentation, not of the launch.
DEBRIS_EVENTS: tuple[DebrisEvent, ...] = (
    DebrisEvent("fengyun-1c", "Fengyun-1C", ("1999-025",), 2007, "ASAT test"),
    DebrisEvent("cosmos-2251-iridium-33", "Cosmos 2251 and Iridium 33",
                ("1993-036", "1997-051"), 2009, "Collision"),
    DebrisEvent("cosmos-1408", "Cosmos 1408", ("1982-092",), 2021, "ASAT test"),
    DebrisEvent("cz-6a-2022", "CZ-6A upper stage (Yunhai 3 launch)", ("2022-151",), 2022,
                "Upper stage breakup"),
    DebrisEvent("cz-6a-2024", "CZ-6A upper stage (Qianfan launch)", ("2024-140",), 2024,
                "Upper stage breakup"),
    DebrisEvent("cosmos-1275", "Cosmos 1275", ("1981-053",), 1981, "Breakup"),
    DebrisEvent("noaa-16", "NOAA 16", ("2000-055",), 2015, "Breakup"),
    DebrisEvent("noaa-17", "NOAA 17", ("2002-032",), 2021, "Breakup"),
    DebrisEvent("dmsp-f13", "DMSP F13", ("1995-015",), 2015, "Breakup"),
    DebrisEvent("pegasus-haps", "Pegasus HAPS (STEP 2 launch)", ("1994-029",), 1996,
                "Upper stage breakup"),
    DebrisEvent("ariane-1-spot-1", "Ariane 1 third stage (SPOT 1 launch)", ("1986-019",), 1986,
                "Upper stage breakup"),
    DebrisEvent("centaur-aehf-4", "Atlas 5 Centaur (AEHF 4 launch)", ("2018-079",), 2019,
                "Upper stage breakup"),
    DebrisEvent("intelsat-33e", "Intelsat 33e", ("2016-053",), 2024, "Breakup"),
    DebrisEvent("microsat-r", "Microsat-R", ("2019-006",), 2019, "ASAT test"),
)


def launch_designator(intl_id: str) -> str:
    """'1999-025ABC' -> '1999-025'; '' when the designator is not of that form."""
    if len(intl_id) >= 8 and intl_id[4] == "-" and intl_id[:4].isdigit() \
            and intl_id[5:8].isdigit():
        return intl_id[:8]
    return ""


def _cloud_name(names: Counter[str], fallback: str) -> str:
    if not names:
        return fallback
    name = names.most_common(1)[0][0]
    for suffix in (" DEB", " DEBRIS"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip() or fallback


def build_debris(entries: Iterable[SatcatEntry], top: int = 12, min_pieces: int = 50,
                 include_ids: bool = True) -> dict[str, Any]:
    """Curated fragmentation events plus the launches with most debris on orbit."""
    pieces: dict[str, list[SatcatEntry]] = {}
    for entry in entries:
        if entry.object_type != "DEBRIS":
            continue
        launch = launch_designator(entry.intl_id)
        if launch:
            pieces.setdefault(launch, []).append(entry)

    def describe(key: str, name: str, designators: Sequence[str], year: int | None,
                 kind: str, curated: bool) -> dict[str, Any]:
        group = [e for d in designators for e in pieces.get(d, [])]
        on = sorted(e.norad_id for e in group if e.on_orbit)
        return {
            "key": key,
            "name": name,
            "designators": list(designators),
            "event_year": year,
            "kind": kind,
            "curated": curated,
            "catalogued": len(group),
            "on_orbit": len(on),
            "decayed": len(group) - len(on),
            "ids": on if include_ids else [],
        }

    events = [describe(ev.key, ev.name, ev.designators, ev.event_year, ev.kind, True)
              for ev in DEBRIS_EVENTS]
    taken = {d for ev in DEBRIS_EVENTS for d in ev.designators}
    ranked = sorted(
        ((sum(1 for e in group if e.on_orbit), launch) for launch, group in pieces.items()
         if launch not in taken),
        key=lambda item: (-item[0], item[1]),
    )
    found = []
    for count, launch in ranked[: max(0, top)]:
        if count < min_pieces:
            break
        names: Counter[str] = Counter(e.name for e in pieces[launch])
        found.append(describe("launch-" + launch, _cloud_name(names, launch), (launch,),
                              None, "Fragmentation", False))
    return {"events": events, "found": found, "min_pieces": min_pieces}


# ---------------------------------------------------------------------------
# Visible tonight
# ---------------------------------------------------------------------------


#: Standard magnitudes (1000 km range, 90 deg phase angle) used before the size
#: estimate below. Rounded working values, not a maintained list: a McCants-style
#: qs.mag table would replace them.
STD_MAGNITUDES: dict[int, float] = {
    25544: -1.3,   # ISS (ZARYA)
    48274: 0.5,    # CSS (TIANHE)
    20580: 2.2,    # HST
}

#: Size-class estimate from the SATCAT type and radar cross-section band.
_RCS_STD_MAG: dict[str, float] = {"LARGE": 4.5, "MEDIUM": 6.5, "SMALL": 8.0}
_LARGE_ROCKET_BODY_STD_MAG = 3.5
_DEFAULT_STD_MAG = 5.5

VISIBLE_GROUPS: tuple[str, ...] = ("visual", "stations")
MAX_CANDIDATES = 300
#: Naked-eye objects are in low orbits: anything slower than this (a period over
#: four hours) is skipped. Geostationary objects are magnitude 10 and fainter.
MIN_MEAN_MOTION_REV_DAY = 6.0
SCREEN_STEP_S = 60.0
SCREEN_BATCH = 64
WINDOW_MARGIN_S = 10 * 60.0
REFINE_COARSE_STEP_S = 20.0
REFINE_SAMPLE_STEP_S = 15.0


def standard_magnitude(norad_id: int, satcat: SatcatStore | None) -> tuple[float, str]:
    """(standard magnitude, where it came from) for one object."""
    if norad_id in STD_MAGNITUDES:
        return STD_MAGNITUDES[norad_id], "table"
    entry = satcat.get(norad_id) if satcat is not None else None
    if entry is not None and entry.rcs_size in _RCS_STD_MAG:
        if entry.object_type == "ROCKET BODY" and entry.rcs_size == "LARGE":
            return _LARGE_ROCKET_BODY_STD_MAG, "estimate from SATCAT type and radar size"
        return _RCS_STD_MAG[entry.rcs_size], "estimate from SATCAT radar size"
    return _DEFAULT_STD_MAG, "default"


def phase_factor(phase_rad: float) -> float:
    """Diffuse-sphere phase law normalized to 1 at 90 deg: sin(p) + (pi - p) cos(p)."""
    return math.sin(phase_rad) + (math.pi - phase_rad) * math.cos(phase_rad)


def apparent_magnitudes(std_mag: float, sat_km: np.ndarray, obs_km: np.ndarray,
                        sun_km: np.ndarray) -> np.ndarray:
    """Visual magnitudes from a standard magnitude, range and phase angle.

    m = std + 5 log10(range / 1000 km) - 2.5 log10(F(phase)), F from
    :func:`phase_factor`. Rows of (n, 3) arrays in one inertial frame, km.
    """
    to_obs = obs_km - sat_km
    to_sun = sun_km - sat_km
    rng = np.maximum(np.linalg.norm(to_obs, axis=-1), 1e-9)
    sun_dist = np.maximum(np.linalg.norm(to_sun, axis=-1), 1e-9)
    cos_phase = np.clip(np.sum(to_obs * to_sun, axis=-1) / (rng * sun_dist), -1.0, 1.0)
    phase = np.arccos(cos_phase)
    factor = np.maximum(np.sin(phase) + (np.pi - phase) * np.cos(phase), 1e-6)
    result: np.ndarray = std_mag + 5.0 * np.log10(rng / 1000.0) - 2.5 * np.log10(factor)
    return result


def apparent_magnitude(std_mag: float, sat_km: Sequence[float], obs_km: Sequence[float],
                       sun_km: Sequence[float]) -> float:
    """One-row form of :func:`apparent_magnitudes`."""
    return float(apparent_magnitudes(
        std_mag, np.asarray([sat_km], dtype=float), np.asarray([obs_km], dtype=float),
        np.asarray([sun_km], dtype=float))[0])


def mean_motion_rev_day(tle: TLERecord) -> float:
    """Mean motion from TLE line 2 columns 53-63, 0.0 when unreadable."""
    try:
        return float(tle.line2[52:63])
    except ValueError:
        return 0.0


def _tle_store() -> TLEStore:
    """The app-level TLE catalog (late import, as in routes/passes.py)."""
    from sda.api import store

    return store


def _group_text(group: str) -> str | None:
    """Raw text of the last good download of a CelesTrak group, never a new request."""
    from sda.api import last_group_download

    held = last_group_download(group)
    return held[1] if held is not None else None


def candidate_tles(
    store: TLEStore,
    group_texts: dict[str, str | None],
    requested: Sequence[int],
    limit: int = MAX_CANDIDATES,
) -> tuple[list[TLERecord], dict[str, int]]:
    """Objects worth screening: requested ids, the magnitude table, then the groups.

    The app catalog's copy of an object wins over a group's copy. Group text goes
    through TLEStore.load_from_text, so every set is validated by twoline2rv.
    Objects in orbits slower than MIN_MEAN_MOTION_REV_DAY are left out.
    """
    chosen: dict[int, TLERecord] = {}
    sources: dict[str, int] = {}

    def take(tle: TLERecord | None, source: str) -> None:
        if tle is None or tle.norad_id in chosen or len(chosen) >= limit:
            return
        if mean_motion_rev_day(tle) < MIN_MEAN_MOTION_REV_DAY:
            return
        chosen[tle.norad_id] = tle
        sources[source] = sources.get(source, 0) + 1

    for nid in requested:
        take(store.get(nid), "requested")
    for nid in STD_MAGNITUDES:
        take(store.get(nid), "table")
    for group, text in group_texts.items():
        if not text:
            continue
        scratch = TLEStore()
        try:
            scratch.load_from_text(text)
        except ValueError:
            continue
        for tle in scratch.get_all():
            take(store.get(tle.norad_id) or tle, group)
    return list(chosen.values()), sources


@dataclass(frozen=True, slots=True)
class _Site:
    """Observer and Sun geometry on a time grid, inertial frame, km."""

    jd: np.ndarray
    fr: np.ndarray
    obs: np.ndarray      # (T, 3) observer position
    up: np.ndarray       # (T, 3) geodetic zenith unit vector
    sun: np.ndarray      # (T, 3) Sun position
    sun_hat: np.ndarray  # (T, 3) unit vector to the Sun
    dark: np.ndarray     # (T,) Sun below civil twilight


def _site(times: Sequence[datetime], observer: Observer) -> _Site:
    """Observer position and zenith rotated by GMST (sda.passes.gmst_rad), and the Sun."""
    count = len(times)
    jd = np.empty(count)
    fr = np.empty(count)
    for k, moment in enumerate(times):
        jd[k], fr[k] = datetime_to_jd(moment)
    gmst = np.array([gmst_rad(moment) for moment in times])
    cos_g, sin_g = np.cos(gmst), np.sin(gmst)
    ox, oy, oz = (float(v) for v in observer.earth_fixed_position_km())
    obs = np.stack([ox * cos_g - oy * sin_g, ox * sin_g + oy * cos_g, np.full(count, oz)], axis=1)
    lat = math.radians(observer.latitude_deg)
    lon = math.radians(observer.longitude_deg)
    ux, uy, uz = math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)
    up = np.stack([ux * cos_g - uy * sin_g, ux * sin_g + uy * cos_g, np.full(count, uz)], axis=1)
    sun = np.array([solar_position_eci_km(moment) for moment in times]).reshape(count, 3)
    sun_rel = sun - obs
    sun_el = np.degrees(np.arcsin(np.clip(
        np.einsum("tk,tk->t", sun_rel, up) / np.linalg.norm(sun_rel, axis=1), -1.0, 1.0)))
    sun_hat = sun / np.linalg.norm(sun, axis=1)[:, None]
    return _Site(jd, fr, obs, up, sun, sun_hat, sun_el < CIVIL_TWILIGHT_DEG)


def sunlit_mask(pos: np.ndarray, sun_hat: np.ndarray) -> np.ndarray:
    """:func:`sda.passes.is_sunlit` (cylindrical shadow) for (..., T, 3) positions."""
    along = np.einsum("...tk,tk->...t", pos, sun_hat)
    perp = np.linalg.norm(pos - along[..., None] * sun_hat, axis=-1)
    result: np.ndarray = (along >= 0.0) | (perp > EARTH_RADIUS_KM)
    return result


def _elevation(pos: np.ndarray, site: _Site) -> np.ndarray:
    rel = pos - site.obs
    rng = np.maximum(np.linalg.norm(rel, axis=-1), 1e-9)
    result: np.ndarray = np.degrees(np.arcsin(np.clip(
        np.einsum("...tk,tk->...t", rel, site.up) / rng, -1.0, 1.0)))
    return result


def _times(start: datetime, hours: float, step_s: float) -> list[datetime]:
    count = int(hours * 3600.0 // step_s) + 1
    return [start + timedelta(seconds=k * step_s) for k in range(count)]


def screen_visible(
    tles: Sequence[TLERecord],
    observer: Observer,
    start: datetime,
    hours: float,
    min_elevation_deg: float,
    step_s: float = SCREEN_STEP_S,
) -> tuple[dict[int, list[tuple[datetime, datetime]]], int]:
    """Coarse windows in which each object is above the mask, sunlit, under a dark sky.

    One SGP4 call per batch of objects over the whole time grid (SatrecArray).
    Returns the windows per NORAD id (each padded by WINDOW_MARGIN_S and merged) and
    the number of dark minutes on the grid.
    """
    times = _times(start, hours, step_s)
    site = _site(times, observer)
    dark_minutes = int(round(float(site.dark.sum()) * step_s / 60.0))
    if not site.dark.any():
        return {}, 0

    satrecs = []
    ids = []
    for tle in tles:
        try:
            satrecs.append(build_satrec(tle))
        except (RuntimeError, ValueError):
            continue
        ids.append(tle.norad_id)

    end = start + timedelta(hours=hours)
    margin = timedelta(seconds=WINDOW_MARGIN_S)
    windows: dict[int, list[tuple[datetime, datetime]]] = {}
    for lo in range(0, len(satrecs), SCREEN_BATCH):
        err, pos, _ = SatrecArray(satrecs[lo: lo + SCREEN_BATCH]).sgp4(site.jd, site.fr)
        ok = ((err == 0) & (_elevation(pos, site) >= min_elevation_deg)
              & sunlit_mask(pos, site.sun_hat) & site.dark[None, :])
        for row in np.flatnonzero(ok.any(axis=1)):
            hits = np.flatnonzero(ok[row])
            breaks = np.flatnonzero(np.diff(hits) > 1)
            starts = np.concatenate(([hits[0]], hits[breaks + 1]))
            stops = np.concatenate((hits[breaks], [hits[-1]]))
            merged: list[tuple[datetime, datetime]] = []
            for a, b in zip(starts, stops, strict=True):
                w0 = max(start, times[int(a)] - margin)
                w1 = min(end, times[int(b)] + margin)
                if merged and w0 <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], w1))
                else:
                    merged.append((w0, w1))
            windows[ids[lo + int(row)]] = merged
    return windows, dark_minutes


def _point(moment: datetime, azimuth: float, elevation: float) -> dict[str, Any]:
    return {
        "time": moment.isoformat(),
        "azimuth_deg": round(azimuth, 1),
        "elevation_deg": round(elevation, 1),
        "compass": compass_point(azimuth),
    }


def visible_part(
    item: SatellitePass,
    tle: TLERecord,
    observer: Observer,
    std_mag: float,
    min_elevation_deg: float,
) -> dict[str, Any] | None:
    """The naked-eye part of one pass, or None when no sample of it is visible.

    A sample is visible when it is above the mask, the object is sunlit (the
    cylindrical shadow of sda.passes.is_sunlit) and the Sun is below civil twilight
    at the observer. The magnitude is :func:`apparent_magnitudes` at each sample.
    """
    samples = [s for s in item.samples if s.elevation_deg >= min_elevation_deg]
    if not samples:
        return None
    site = _site([s.time for s in samples], observer)
    satrec = build_satrec(tle)
    err, pos, _ = satrec.sgp4_array(site.jd, site.fr)
    ok = (np.asarray(err) == 0) & sunlit_mask(np.asarray(pos), site.sun_hat) & site.dark
    rows = np.flatnonzero(ok)
    if rows.size == 0:
        return None
    mags = apparent_magnitudes(std_mag, np.asarray(pos)[rows], site.obs[rows], site.sun[rows])
    seen = [samples[int(i)] for i in rows]
    first, last = seen[0], seen[-1]
    peak = max(seen, key=lambda s: s.elevation_deg)
    best = int(np.argmin(mags))
    bright = seen[best]
    return {
        "magnitude": round(float(mags[best]), 1),
        "brightest_time": bright.time.isoformat(),
        "visible_start": _point(first.time, first.azimuth_deg, first.elevation_deg),
        "visible_peak": _point(peak.time, peak.azimuth_deg, peak.elevation_deg),
        "visible_end": _point(last.time, last.azimuth_deg, last.elevation_deg),
        "visible_seconds": round((last.time - first.time).total_seconds(), 0),
    }


def find_visible_passes(
    tles: Sequence[TLERecord],
    observer: Observer,
    start: datetime,
    hours: float,
    min_elevation_deg: float,
    max_magnitude: float,
    limit: int,
    satcat: SatcatStore | None,
) -> tuple[list[dict[str, Any]], int]:
    """Brightest visible passes, brightest first, and the dark minutes in the window."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    windows, dark_minutes = screen_visible(tles, observer, start, hours, min_elevation_deg)
    by_id = {tle.norad_id: tle for tle in tles}
    found: list[dict[str, Any]] = []
    for nid, spans in windows.items():
        tle = by_id[nid]
        std_mag, std_source = standard_magnitude(nid, satcat)
        seen_minutes: set[int] = set()
        for w0, w1 in spans:
            try:
                passes = predict_passes(
                    tle, observer, w0, hours=(w1 - w0).total_seconds() / 3600.0,
                    min_elevation_deg=min_elevation_deg,
                    coarse_step_s=REFINE_COARSE_STEP_S,
                    sample_step_s=REFINE_SAMPLE_STEP_S,
                    include_samples=True,
                )
            except (RuntimeError, ValueError):
                continue
            for item in passes:
                minute = int(item.culmination_time.timestamp() // 60)
                if minute in seen_minutes:
                    continue
                seen_minutes.add(minute)
                part = visible_part(item, tle, observer, std_mag, min_elevation_deg)
                if part is None or part["magnitude"] > max_magnitude:
                    continue
                found.append({
                    "norad_id": nid,
                    "name": tle.name,
                    "std_magnitude": std_mag,
                    "std_magnitude_source": std_source,
                    **part,
                    "rise_time": item.rise_time.isoformat(),
                    "culmination_time": item.culmination_time.isoformat(),
                    "set_time": item.set_time.isoformat(),
                    "max_elevation_deg": round(item.max_elevation_deg, 1),
                    "partial": item.partial,
                })
    found.sort(key=lambda p: (p["magnitude"], p["brightest_time"]))
    return found[:limit], dark_minutes


class VisibleRequest(BaseModel):
    """Bright passes over one observer."""

    observer: Observer
    start: datetime | None = None
    hours: float = Field(default=24.0, gt=0.0, le=48.0)
    min_elevation_deg: float = Field(default=10.0, ge=0.0, le=80.0)
    max_magnitude: float = Field(default=4.5, ge=-5.0, le=8.0,
                                 description="Faintest magnitude listed")
    limit: int = Field(default=20, ge=1, le=100)
    norad_ids: list[int] = Field(default_factory=list, max_length=50,
                                 description="Extra objects to consider (a watchlist)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/attributes", summary="SATCAT attributes of every object on orbit, packed")
def orbitviz_attributes(
    on_orbit: bool = Query(True, description="Only objects without a decay date"),
) -> dict[str, Any]:
    """Type, owner, radar size and launch year per object for the globe's colour modes."""
    store = _satcat()
    _require(store)
    body = _cached("attributes", store, (on_orbit,),
                   lambda: build_attributes(satcat_entries(store), on_orbit=on_orbit))
    return {**body, **store.provenance()}


@router.get("/history", summary="Objects in orbit by year, from SATCAT launch and decay dates")
def orbitviz_history() -> dict[str, Any]:
    """Population in orbit at the end of each year since 1957, by object type."""
    store = _satcat()
    _require(store)
    year = datetime.now(UTC).year
    body = _cached("history", store, (year,),
                   lambda: build_history(satcat_entries(store), year))
    return {**body, **store.provenance()}


@router.get("/debris", summary="Fragmentation events and their pieces still on orbit")
def orbitviz_debris(
    top: int = Query(12, ge=0, le=40, description="Launches found by debris count"),
    min_pieces: int = Query(50, ge=1, le=5000, description="Smallest cloud listed as found"),
    ids: bool = Query(True, description="Include the NORAD ids of pieces on orbit"),
) -> dict[str, Any]:
    """Curated events (Fengyun-1C, Cosmos 2251 and Iridium 33, Cosmos 1408, ...) and more."""
    store = _satcat()
    _require(store)
    body = _cached("debris", store, (top, min_pieces, ids),
                   lambda: build_debris(satcat_entries(store), top, min_pieces, ids))
    return {**body, **store.provenance()}


@router.post("/visible", summary="Brightest visible passes over an observer")
def orbitviz_visible(request: VisibleRequest) -> dict[str, Any]:
    """Heavens-Above style list: sunlit passes under a dark sky, brightest first.

    Candidates are the requested ids, the standard-magnitude table and the
    CelesTrak "visual" and "stations" groups as last downloaded (no new request).
    Magnitudes are estimates: a standard magnitude (table, else from the SATCAT
    radar size) scaled by range and a diffuse-sphere phase law.
    """
    start = request.start or datetime.now(UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    groups: dict[str, str | None] = {}
    for group in VISIBLE_GROUPS:
        try:
            groups[group] = _group_text(group)
        except Exception:  # a missing group must not fail the list
            groups[group] = None
    tles, sources = candidate_tles(_tle_store(), groups, request.norad_ids)
    if not tles:
        raise HTTPException(status_code=503, detail="No candidate objects in the catalog yet")
    try:
        satcat: SatcatStore | None = _satcat()
    except Exception:
        satcat = None
    passes, dark_minutes = find_visible_passes(
        tles, request.observer, start, request.hours, request.min_elevation_deg,
        request.max_magnitude, request.limit, satcat,
    )
    return {
        "observer": request.observer.model_dump(mode="json"),
        "start": start.isoformat(),
        "hours": request.hours,
        "min_elevation_deg": request.min_elevation_deg,
        "max_magnitude": request.max_magnitude,
        "candidates": len(tles),
        "candidate_sources": sources,
        "dark_minutes": dark_minutes,
        "count": len(passes),
        "passes": passes,
        "method": "Sunlit (cylindrical shadow) above the elevation mask with the Sun below "
                  "-6 deg; magnitude = standard magnitude + 5 log10(range / 1000 km) - "
                  "2.5 log10(diffuse-sphere phase factor). Magnitudes are estimates.",
    }
