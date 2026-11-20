"""FastAPI service for the SDA collision predictor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sda import __version__
from sda.demo_tles import DEMO_TLE_TEXT
from sda.models import TLERecord
from sda.omm import omm_to_tle_text, parse_omm_json
from sda.ratelimit import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    bind_host,
    bind_port,
    cors_origins,
)
from sda.routes.agent import router as agent_router
from sda.routes.analysis import router as analysis_router
from sda.routes.aviation import router as aviation_router
from sda.routes.buildings import router as buildings_router
from sda.routes.cameras import router as cameras_router
from sda.routes.conflict import router as conflict_router
from sda.routes.conjunctions import router as conjunctions_router
from sda.routes.eonet import router as eonet_router
from sda.routes.history import router as history_router
from sda.routes.infra import router as infra_router
from sda.routes.intel import router as intel_router
from sda.routes.maritime import router as maritime_router
from sda.routes.ops import router as ops_router
from sda.routes.orbitviz import router as orbitviz_router
from sda.routes.passes import router as passes_router
from sda.routes.pc import router as pc_router
from sda.routes.peaks import router as peaks_router
from sda.routes.sanctions import router as sanctions_router
from sda.routes.satcat import router as satcat_router
from sda.routes.satellites import router as satellites_router
from sda.routes.search import router as search_router
from sda.routes.space import router as space_router
from sda.routes.summit import router as summit_router
from sda.routes.system import router as system_router
from sda.routes.weather import router as weather_router
from sda.routes.worldview import router as worldview_router
from sda.tle_store import STALE_THRESHOLD_HOURS, TLEStore

# Module-level store singleton
store = TLEStore()

# Performance metrics
_metrics: dict[str, Any] = {
    "propagations": 0,
    "conjunctions_run": 0,
    "conjunctions_found": 0,
    "api_requests": 0,
    "last_analysis_duration_ms": 0.0,
    "startup_time": None,
}

# CelesTrak TLE sources - live groups covering LEO, MEO, GEO, and special orbits
CELESTRAK_GROUPS = [
    "stations",          # ISS, CSS, crewed vehicles
    "active",            # All active satellites (large set)
    "visual",            # Bright/visible objects
    "starlink",          # Starlink constellation
    "gps-ops",           # GPS operational
    "galileo",           # Galileo GNSS
    "beidou",            # BeiDou GNSS
    "geo",               # Geostationary
    "weather",           # Weather satellites
    "science",           # Science missions
    "resource",          # Earth resources
    # "last-30-days" is left out on purpose. The public five-digit catalog numbers ran
    # out in July 2026 and a TLE cannot carry the six-digit numbers issued since, so in
    # TLE format the group answered HTTP 404 "No GP data found" on every request. The
    # fetch below now asks for OMM JSON, which carries them, so the group could come
    # back once that has been checked against the live service.
]
# OMM JSON rather than TLE text: objects cataloged since July 2026 (six-digit numbers)
# are published only as OMM. A reply is converted to TLE lines (sda.omm, alpha-5 up to
# 339,999) before it is loaded, held for the globe or cached, so everything downstream,
# the cache files included, still reads TLE text. A TLE reply is read as it is.
CELESTRAK_BASE = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"

# Optional disk cache of the last good catalog, switched on by SDA_TLE_CACHE_DIR. CelesTrak
# answers 403 when a group is requested again before its data has updated (every 2 hours), so
# a restart soon after a start would otherwise lose most of the catalog. Unset or empty means
# no cache: nothing is created, read or written.
TLE_CACHE_ENV = "SDA_TLE_CACHE_DIR"
# Cached files older than this are not loaded: stale elements propagate to wrong positions
# and this catalog feeds conjunction screening.
TLE_CACHE_MAX_AGE_HOURS = 72.0
# CelesTrak asks for no more than one download of a group every two hours. The loop runs
# five minutes past that, so the pass after a startup fetch never finds the fetch too
# recent and skips it.
TLE_REFRESH_INTERVAL_S = 7500
# How long a group is left alone after CelesTrak answered for it, with element sets or
# without (a 403 inside its two-hour window, an error page, an empty body). Enough refused
# repeats get an address blocked, so a restart must not send them. Kept under the refresh
# interval so the running loop never finds its own last download too recent.
CELESTRAK_REPEAT_WINDOW_S = 7200.0

# Keyless fallbacks, asked in order only when CelesTrak has failed (see
# _celestrak_failing and CATALOG_FALLBACKS below). Each has its own two-hour limit and
# its own files in SDA_TLE_CACHE_DIR: <stem>.tle is the last good reply as TLE text,
# loaded at startup like any CelesTrak group, and <stem>.asked marks the last request.
#
# First, the SatNOGS DB TLE list. One request returns every object SatNOGS tracks,
# about 550 KB of JSON rows {tle0, tle1, tle2, tle_source, norad_cat_id, updated}; most
# rows come from Space-Track and are current. About 1,500 objects.
SATNOGS_TLE_URL = "https://db.satnogs.org/api/tle/?format=json"
# Not "satnogs", which is the name of a CelesTrak group.
SATNOGS_CACHE_NAME = "satnogs-db"
# The courtesy CelesTrak asks for, applied here too: one request every two hours at
# most, counted from the request whether or not anything came back.
SATNOGS_REPEAT_WINDOW_S = 7200.0
# Second, COMSPOC Spacebook's OMM catalog: about 30 MB of OMM JSON in one request,
# 32,951 objects on 2026-11-19 (1,573 numbered above 99,999), SGP4 mean elements in
# TEME and UTC fitted by COMSPOC. Spacebook's guide says no account is needed to
# download its data; attribute COMSPOC. The /api/entity base is the one ISHANTARE/ASTRA
# astra/spacebook.py (MIT) uses for the TLE variant; no code was copied from it.
SPACEBOOK_OMM_URL = "https://spacebook.com/api/entity/omm/json"
SPACEBOOK_CACHE_NAME = "spacebook"
SPACEBOOK_REPEAT_WINDOW_S = 7200.0
# Spacebook publishes some deep-space sets with epochs days ahead of the present (11 of
# 32,951 more than a day ahead on 2026-11-19, the VELA series among them). Elements
# dated in the future are not an observation, so sets further ahead than this are
# left out.
SPACEBOOK_MAX_EPOCH_AHEAD_HOURS = 24.0
# Both fallbacks keep the last element set of objects that have since decayed or gone
# unobserved, some decades old (218 of 1,778 SatNOGS rows and 135 of 32,951 Spacebook
# records on 2026-11-19 were older than 14 days). Sets past the store's EXPIRED line
# are left out: they propagate to wrong positions and this catalog feeds conjunction
# screening.
FALLBACK_MAX_ELEMENT_AGE_HOURS = STALE_THRESHOLD_HOURS
# SDA_CATALOG_FALLBACKS picks the fallbacks at startup, in the order asked:
# "satnogs,spacebook" when unset, one of the two, or "none" (or empty) for neither.
CATALOG_FALLBACKS_ENV = "SDA_CATALOG_FALLBACKS"

# Where each element set in the catalog came from: TLEStore.source_of(norad_id) and
# TLEStore.source_counts() report these, for the object card and for CDMs.
SOURCE_CELESTRAK = "celestrak"
SOURCE_SATNOGS = "satnogs"
SOURCE_SPACEBOOK = "spacebook"
SOURCE_BUNDLED = "bundled"

# Under uvicorn only the uvicorn loggers have handlers, so INFO lines from a module-named
# logger would never reach the console; uvicorn.error is configured at INFO.
logger = logging.getLogger("uvicorn.error")

# NOAA Space Weather - live F10.7 solar flux
NOAA_F107_URL = "https://services.swpc.noaa.gov/json/solar-cycle/predicted-solar-cycle.json"
NOAA_F107_OBSERVED_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"

# Live data state
_live_f107: float = 150.0  # updated from NOAA
_last_tle_refresh: datetime | None = None
_tle_refresh_task: asyncio.Task | None = None

# What this process remembers about its own CelesTrak requests. The cache directory
# holds the same facts across a restart; without SDA_TLE_CACHE_DIR this is the only
# record, and it is what keeps a second refresh, or the globe asking for one group,
# inside CelesTrak's rule.
_celestrak_lock = threading.Lock()
_celestrak_in_flight: set[str] = set()  # groups with a request on the wire
_celestrak_answered_at: dict[str, float] = {}  # group -> time.time() of the last reply
_celestrak_text: dict[str, tuple[float, str]] = {}  # group -> (time taken, last good text)
# Fallback key -> time.time() of this process's last request to it. <dir>/<stem>.asked
# carries the same fact across a restart when the cache directory is on.
_fallback_asked_at: dict[str, float] = {}
# Where the catalog's live elements came from; catalog_source() reports it.
_catalog_source: dict[str, Any] = {"source": "bundled"}

# Response caches for external APIs
class _CacheEntry(TypedDict):
    data: dict[str, Any]
    ts: float


_cache: dict[str, _CacheEntry] = {}


def _get_cached(key: str, ttl_sec: float) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if entry is not None and time.time() - entry["ts"] < ttl_sec:
        return entry["data"]
    return None


def _set_cached(key: str, data: dict[str, Any]) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


# --- TLE disk cache ---

def _tle_cache_dir() -> Path | None:
    """Cache directory from SDA_TLE_CACHE_DIR, or None when the cache is off."""
    raw = os.environ.get(TLE_CACHE_ENV, "").strip()
    return Path(raw) if raw else None


def _write_tle_cache(group: str, text: str) -> None:
    """Save a group's raw response as <dir>/<group>.tle. A failed write is logged, never raised."""
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return
    # Temp file in the same directory so os.replace is atomic. The .tmp suffix keeps a
    # half-written file out of the *.tle glob that startup uses.
    tmp_path = cache_dir / f"{group}.{uuid.uuid4().hex}.tmp"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp_path, cache_dir / f"{group}.tle")
    except Exception as exc:
        logger.warning("TLE cache: could not write %s.tle: %s", group, exc)
        with suppress(OSError):
            tmp_path.unlink()


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _apply_newer(records: Iterable[TLERecord], source: str | None) -> int:
    """Apply the records newer than the live store's, labelled *source*. Returns applied."""
    applied = 0
    for record in records:
        current = store.get(record.norad_id)
        if current is None or _utc(record.epoch) > _utc(current.epoch):
            store.upsert(record, source=source)
            applied += 1
    return applied


def _load_newer_only(raw: str, source: str | None = None) -> tuple[int, int]:
    """Load TLE text through TLEStore.load_from_text without letting older elements win.

    TLEStore.upsert is last-write-wins per NORAD id whatever the epoch, so a plain load of
    older text would replace a newer element set. The text is parsed and validated into a
    scratch store, then only records newer than the live store's are applied, each
    labelled *source*. Returns (records parsed, records applied).
    """
    scratch = TLEStore()
    scratch.load_from_text(raw)
    return scratch.count(), _apply_newer(scratch.get_all(), source)


def _load_tle_cache() -> tuple[int, int]:
    """Load every recent *.tle file from the cache dir. Returns (files, objects) applied.

    Runs after the bundled demo catalog and before the network fetch. A file that is too
    old, unreadable or corrupt is skipped and never stops startup.
    """
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return 0, 0
    max_age_s = TLE_CACHE_MAX_AGE_HOURS * 3600.0
    now = time.time()
    ages_h: list[float] = []
    loaded: list[str] = []
    objects = 0
    try:
        paths = sorted(cache_dir.glob("*.tle"))
    except Exception as exc:
        logger.warning("TLE cache: could not list %s: %s", cache_dir, exc)
        return 0, 0
    for path in paths:
        try:
            age_s = now - path.stat().st_mtime
            if age_s > max_age_s:
                logger.info(
                    "TLE cache: skipping %s, %.1f h old (limit %.0f h)",
                    path.name, age_s / 3600.0, TLE_CACHE_MAX_AGE_HOURS,
                )
                continue
            parsed, applied = _load_newer_only(
                path.read_text(encoding="utf-8", errors="replace"),
                source=_cache_file_source(path.stem),
            )
        except Exception as exc:
            logger.warning("TLE cache: could not load %s: %s", path.name, exc)
            continue
        if parsed == 0:
            logger.warning("TLE cache: %s holds no valid TLE records, skipped", path.name)
            continue
        ages_h.append(age_s / 3600.0)
        loaded.append(path.name)
        objects += applied
    if ages_h:
        logger.info(
            "TLE cache: applied %d objects from %d file%s in %s (%.1f to %.1f h old)",
            objects, len(ages_h), "" if len(ages_h) == 1 else "s", cache_dir,
            min(ages_h), max(ages_h),
        )
        if objects:
            _set_catalog_source(
                "cache", now - min(ages_h) * 3600.0, files=loaded, objects=objects
            )
    return len(ages_h), objects


def _set_catalog_source(source: str, taken_at: float, **detail: Any) -> None:
    global _catalog_source
    _catalog_source = {
        "source": source,
        "taken_at": datetime.fromtimestamp(taken_at, UTC).isoformat(),
        **detail,
    }


def _cache_file_source(stem: str) -> str:
    """Provenance label for the sets in <dir>/<stem>.tle: a fallback's, else CelesTrak's."""
    for fallback in CATALOG_FALLBACKS:
        if stem == fallback.cache_name:
            return fallback.key
    return SOURCE_CELESTRAK


def catalog_source() -> dict[str, Any]:
    """Where the catalog's live elements came from, for /health and the status routes.

    "source" is "bundled" until something newer is loaded. Then "cache" (the files
    startup read), "CelesTrak" (groups and objects of the last refresh that brought
    any), or a fallback by name ("SatNOGS DB", "COMSPOC Spacebook"), each with
    "taken_at" in UTC ISO 8601. The last of those to deliver is the one named.
    "element_sets" counts the sets held now by where each came from ("celestrak",
    "satnogs", "spacebook", "bundled"); sets loaded through POST /tle carry no label
    and are not counted.
    """
    snapshot: dict[str, Any] = {
        key: list(value) if isinstance(value, list)
        else dict(value) if isinstance(value, dict)
        else value
        for key, value in _catalog_source.items()
    }
    snapshot["element_sets"] = store.source_counts()
    return snapshot


def _asked_age_s(group: str) -> float | None:
    """Seconds since CelesTrak last answered for this group, None when unknown.

    The newest of what this process saw and what the cache directory records. The
    directory is what survives a restart: <group>.tle is the last download that worked,
    <group>.refused the last reply that carried no element sets.
    """
    stamps: list[float] = []
    answered_at = _celestrak_answered_at.get(group)
    if answered_at is not None:
        stamps.append(answered_at)
    cache_dir = _tle_cache_dir()
    if cache_dir is not None:
        for suffix in (".tle", ".refused"):
            with suppress(OSError):
                stamps.append((cache_dir / f"{group}{suffix}").stat().st_mtime)
    return time.time() - max(stamps) if stamps else None


def _asked_recently(group: str) -> bool:
    age_s = _asked_age_s(group)
    return age_s is not None and 0.0 <= age_s < CELESTRAK_REPEAT_WINDOW_S


def _resting(group: str) -> bool:
    """Inside the repeat window, or being asked for right now."""
    return group in _celestrak_in_flight or _asked_recently(group)


def _claim_group(group: str) -> bool:
    """True when the caller may ask CelesTrak for this group now.

    Two refreshes that overlap (the startup fetch and a POST /tle/refresh, say) would
    otherwise both ask before either had an answer to remember. The caller that gets
    True takes the group out of _celestrak_in_flight when its request ends.
    """
    with _celestrak_lock:
        if _resting(group):
            return False
        _celestrak_in_flight.add(group)
        return True


def _forget_celestrak() -> None:
    """Drop what this process remembers about its catalog requests.

    CelesTrak's and the fallbacks' alike: what a restart does. The tests call it
    between cases so one case's download does not rest the next one's sources.
    """
    global _catalog_source
    with _celestrak_lock:
        _celestrak_in_flight.clear()
        _celestrak_answered_at.clear()
        _celestrak_text.clear()
        _fallback_asked_at.clear()
        _catalog_source = {"source": "bundled"}


def last_group_download(group: str) -> tuple[float, str] | None:
    """(time taken, raw text) of the last good download of a group, None when there is none.

    From this process when it made one, else from the cache directory if that copy is
    young enough to be loaded at startup. GET /api/tles?group= serves from here, so the
    globe never has to ask CelesTrak for a group the refresh already holds.
    """
    held = _celestrak_text.get(group)
    if held is not None:
        return held
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return None
    path = cache_dir / f"{group}.tle"
    try:
        taken_at = path.stat().st_mtime
        if time.time() - taken_at > TLE_CACHE_MAX_AGE_HOURS * 3600.0:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _celestrak_text[group] = (taken_at, text)
    return taken_at, text


def _cached_download_at(name: str) -> float | None:
    """mtime of <dir>/<name>.tle when startup would load it, else None."""
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return None
    try:
        taken_at = (cache_dir / f"{name}.tle").stat().st_mtime
    except OSError:
        return None
    return taken_at if taken_at >= time.time() - TLE_CACHE_MAX_AGE_HOURS * 3600.0 else None


def _celestrak_download_at() -> float | None:
    """time.time() of the newest good CelesTrak download still young enough to load."""
    stamps = [taken_at for taken_at, _ in _celestrak_text.values()]
    for group in CELESTRAK_GROUPS:
        taken_at = _cached_download_at(group)
        if taken_at is not None:
            stamps.append(taken_at)
    return max(stamps) if stamps else None


def _last_download_at() -> datetime | None:
    """When the newest good download this process is running on was taken."""
    candidates = [_celestrak_download_at()]
    candidates += [_cached_download_at(fallback.cache_name) for fallback in CATALOG_FALLBACKS]
    stamps = [stamp for stamp in candidates if stamp is not None]
    return datetime.fromtimestamp(max(stamps), UTC) if stamps else None


def _celestrak_failing() -> bool:
    """True when CelesTrak has delivered no group for a whole repeat window.

    The keyless fallbacks are for that case only. A group still on the wire may yet
    deliver, and a download younger than the window means CelesTrak is serving this
    address and the catalog holds its elements, so neither counts as failing.
    """
    if _celestrak_in_flight:
        return False
    newest = _celestrak_download_at()
    return newest is None or time.time() - newest >= CELESTRAK_REPEAT_WINDOW_S


def _mark_refused(group: str) -> None:
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{group}.refused").touch()
    except OSError as exc:
        logger.warning("TLE cache: could not record the refusal of %s: %s", group, exc)


def _first_refresh_delay_s() -> float:
    """Seconds until the first background refresh.

    Normally the full interval. After a restart that skipped groups still inside
    CelesTrak's window, it is the time until the last of them may be asked for again.
    """
    waits = [
        CELESTRAK_REPEAT_WINDOW_S - age_s
        for age_s in (_asked_age_s(group) for group in CELESTRAK_GROUPS)
        if age_s is not None and 0.0 <= age_s < CELESTRAK_REPEAT_WINDOW_S
    ]
    return max(waits) + 60.0 if waits else TLE_REFRESH_INTERVAL_S


def _as_tle_text(raw: str) -> str:
    """A catalog reply as TLE text: OMM JSON is converted, TLE text passes through.

    What comes out is what gets loaded, held for the globe and written to the cache,
    so a cache file is TLE text whichever format the reply came in.
    """
    records = parse_omm_json(raw)
    return raw if records is None else omm_to_tle_text(records).text


class _FallbackRead(NamedTuple):
    """A fallback reply once read: the sets to apply and what was left out, by reason."""

    sets: list[TLERecord]
    left_out: Counter[str]


_EXPIRED_REASON = f"older than {FALLBACK_MAX_ELEMENT_AGE_HOURS / 24.0:.0f} days"


def _current_sets(sets: Iterable[TLERecord], left_out: Counter[str]) -> _FallbackRead:
    """One set per catalog number, the newest, less those past the store's EXPIRED line.

    Where sets share a catalog number the newest epoch is kept: SatNOGS has an ISS row
    four years old beside the current one, and a plain load keeps whichever comes last.
    """
    newest: dict[int, TLERecord] = {}
    for record in sets:
        held = newest.get(record.norad_id)
        if held is None or _utc(record.epoch) > _utc(held.epoch):
            newest[record.norad_id] = record
    cutoff = datetime.now(UTC) - timedelta(hours=FALLBACK_MAX_ELEMENT_AGE_HOURS)
    current = [record for record in newest.values() if _utc(record.epoch) >= cutoff]
    if len(current) < len(newest):
        left_out[_EXPIRED_REASON] += len(newest) - len(current)
    return _FallbackRead(current, left_out)


def _read_satnogs(raw: str) -> _FallbackRead:
    """SatNOGS DB rows {tle0, tle1, tle2, ...} read as one current set per object.

    Each row is validated on its own by TLEStore.load_from_text. tle0 is the name line,
    with the "0 " that the three-line format puts in front of it taken off.
    """
    try:
        rows = json.loads(raw)
    except ValueError:
        return _FallbackRead([], Counter())
    if not isinstance(rows, list):
        return _FallbackRead([], Counter())
    sets: list[TLERecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        line1 = str(row.get("tle1") or "").strip()
        line2 = str(row.get("tle2") or "").strip()
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            continue
        name = " ".join(str(row.get("tle0") or "").split())
        if name == "0" or name.startswith("0 "):
            name = name[1:].strip()
        one = TLEStore()
        if one.load_from_text(f"{name}\n{line1}\n{line2}" if name else f"{line1}\n{line2}") != 1:
            continue
        sets.append(one.get_all()[0])
    return _current_sets(sets, Counter())


def _read_spacebook(raw: str) -> _FallbackRead:
    """Spacebook's OMM JSON read as one current set per object.

    Every record goes through sda.omm (python-sgp4's OMM reader, then export_tle,
    alpha-5 above 99,999) and the TLE text through TLEStore.load_from_text. Epochs
    more than SPACEBOOK_MAX_EPOCH_AHEAD_HOURS ahead of now are left out, as are the
    records sda.omm cannot turn into a TLE, each counted by reason. Several seconds of
    CPU for the full catalog, so _fetch_fallback runs it off the event loop.
    """
    not_after = datetime.now(UTC) + timedelta(hours=SPACEBOOK_MAX_EPOCH_AHEAD_HOURS)
    conversion = omm_to_tle_text(raw, not_after=not_after, log_skips=False)
    scratch = TLEStore()
    if conversion.text:
        scratch.load_from_text(conversion.text)
    return _current_sets(scratch.get_all(), Counter(conversion.skipped))


@dataclass(frozen=True)
class CatalogFallback:
    """A keyless catalog source asked while CelesTrak is failing."""

    key: str  # SDA_CATALOG_FALLBACKS name, and the provenance label of every set it brings
    name: str  # catalog_source()["source"] and the log prefix: the attribution
    url: str
    cache_name: str  # stem of <dir>/<stem>.tle (last good reply) and <dir>/<stem>.asked
    window_s: float  # at most one request per window, counted from the request
    read: Callable[[str], _FallbackRead]
    timeout_s: float = 30.0


SATNOGS = CatalogFallback(
    key=SOURCE_SATNOGS,
    name="SatNOGS DB",
    url=SATNOGS_TLE_URL,
    cache_name=SATNOGS_CACHE_NAME,
    window_s=SATNOGS_REPEAT_WINDOW_S,
    read=_read_satnogs,
)
SPACEBOOK = CatalogFallback(
    key=SOURCE_SPACEBOOK,
    name="COMSPOC Spacebook",
    url=SPACEBOOK_OMM_URL,
    cache_name=SPACEBOOK_CACHE_NAME,
    window_s=SPACEBOOK_REPEAT_WINDOW_S,
    read=_read_spacebook,
    timeout_s=60.0,
)
# In the order asked. SatNOGS is small and mostly Space-Track elements; Spacebook fills
# in the rest of the catalog. Each applies only sets newer than the catalog's, so the
# order decides nothing about which elements win.
CATALOG_FALLBACKS = (SATNOGS, SPACEBOOK)


def _catalog_fallbacks() -> list[CatalogFallback]:
    """The fallbacks SDA_CATALOG_FALLBACKS names, in its order; all of them when unset."""
    raw = os.environ.get(CATALOG_FALLBACKS_ENV)
    if raw is None:
        return list(CATALOG_FALLBACKS)
    known = {fallback.key: fallback for fallback in CATALOG_FALLBACKS}
    chosen: list[CatalogFallback] = []
    for word in raw.split(","):
        key = word.strip().lower()
        if not key or key == "none":
            continue
        fallback = known.get(key)
        if fallback is None:
            logger.warning(
                "%s: %r is not a catalog fallback (known: %s), ignored",
                CATALOG_FALLBACKS_ENV, key, ", ".join(known),
            )
        elif fallback not in chosen:
            chosen.append(fallback)
    return chosen


def _fallback_asked_age_s(fallback: CatalogFallback) -> float | None:
    """Seconds since the fallback was last asked, from this process or the cache directory."""
    asked_at = _fallback_asked_at.get(fallback.key)
    stamps = [] if asked_at is None else [asked_at]
    cache_dir = _tle_cache_dir()
    if cache_dir is not None:
        for suffix in (".asked", ".tle"):
            with suppress(OSError):
                stamps.append((cache_dir / f"{fallback.cache_name}{suffix}").stat().st_mtime)
    return time.time() - max(stamps) if stamps else None


def _claim_fallback(fallback: CatalogFallback) -> bool:
    """True when the fallback may be asked now. The request is counted from this moment.

    Recorded before the request goes out, so two refreshes that overlap send one
    request between them, and a request that never comes back still counts.
    """
    with _celestrak_lock:
        age_s = _fallback_asked_age_s(fallback)
        if age_s is not None and 0.0 <= age_s < fallback.window_s:
            return False
        _fallback_asked_at[fallback.key] = time.time()
    cache_dir = _tle_cache_dir()
    if cache_dir is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"{fallback.cache_name}.asked").touch()
        except OSError as exc:
            logger.warning("TLE cache: could not record the %s request: %s", fallback.name, exc)
    return True


async def _fetch_fallback(fallback: CatalogFallback) -> int:
    """Ask one keyless fallback for its catalog because CelesTrak failed. Returns sets read.

    Never under SDA_OFFLINE, and at most once per fallback.window_s, across a restart
    too when the cache directory is on. Every element set goes through
    TLEStore.load_from_text, only sets newer than the catalog's are applied, and each
    applied set is labelled fallback.key. A good reply is cached as
    <dir>/<cache_name>.tle in TLE text, which startup loads while it is younger than
    TLE_CACHE_MAX_AGE_HOURS. One log line either way.
    """
    if _offline():
        return 0
    if not _claim_fallback(fallback):
        age_s = _fallback_asked_age_s(fallback) or 0.0
        logger.info(
            "%s: asked %.0f minutes ago, not asked again for %.0f minutes",
            fallback.name, age_s / 60.0, max(0.0, fallback.window_s - age_s) / 60.0,
        )
        return 0
    try:
        async with httpx.AsyncClient(
            timeout=fallback.timeout_s, follow_redirects=True
        ) as client:
            resp = await client.get(fallback.url)
    except Exception as exc:
        logger.info("%s: did not answer (%s)", fallback.name, type(exc).__name__)
        return 0
    body = resp.text
    read = (
        await asyncio.to_thread(fallback.read, body)
        if resp.status_code == 200
        else _FallbackRead([], Counter())
    )
    left_out = ", ".join(
        f"{count} {reason}" for reason, count in read.left_out.most_common()
    ) or "nothing"
    if not read.sets:
        logger.info(
            "%s: gave no element sets (HTTP %d, %d characters; left out %s)",
            fallback.name, resp.status_code, len(body), left_out,
        )
        return 0
    applied = _apply_newer(read.sets, fallback.key)
    _write_tle_cache(
        fallback.cache_name, "".join(f"{r.name}\n{r.line1}\n{r.line2}\n" for r in read.sets)
    )
    _set_catalog_source(
        fallback.name, time.time(),
        objects=len(read.sets), applied=applied, left_out=dict(read.left_out),
        url=fallback.url,
    )
    logger.info(
        "%s: %d objects (%d newer than the catalog held), left out %s; CelesTrak fallback",
        fallback.name, len(read.sets), applied, left_out,
    )
    return len(read.sets)


# --- Background tasks ---

async def _fetch_celestrak_group(
    client: httpx.AsyncClient, group: str, into: TLEStore | None = None
) -> int:
    """Fetch a single CelesTrak group. Returns count ingested.

    The elements go into the process catalog, or into *into* when the caller wants the
    group on its own (the globe's single-group view). It is the same request either
    way, under the same repeat window.
    """
    if not _claim_group(group):
        return 0
    try:
        resp = await client.get(CELESTRAK_BASE.format(group=group))
    except Exception as exc:
        # No reply reached us, so nothing was counted against this address and the
        # next call may try again.
        logger.info("CelesTrak: %s did not answer (%s)", group, type(exc).__name__)
        return 0
    finally:
        _celestrak_in_flight.discard(group)
    answered_at = time.time()
    _celestrak_answered_at[group] = answered_at

    count = 0
    text = resp.text
    if resp.status_code == 200 and text.strip():
        try:
            text = _as_tle_text(text)
            count = (store if into is None else into).load_from_text(
                text, source=SOURCE_CELESTRAK
            )
        except Exception:
            count = 0
    if count > 0:
        _celestrak_text[group] = (answered_at, text)
        _write_tle_cache(group, text)
        return count

    # CelesTrak answered and there was nothing to load: a 403 inside its window, an
    # error page, an empty or unreadable body. Asking again before the window ends
    # gets the same answer, and a restart used to do exactly that for every reply
    # except the 403.
    _mark_refused(group)
    logger.info(
        "CelesTrak: %s gave no element sets (HTTP %d, %d characters)",
        group, resp.status_code, len(resp.text),
    )
    return 0


async def _seed_catalog() -> int:
    """Seed catalog from live CelesTrak data. Falls back to bundled TLEs if all groups fail.

    When CelesTrak has delivered no group for a whole repeat window (_celestrak_failing),
    the keyless fallbacks are asked in turn (SatNOGS DB, then COMSPOC Spacebook, or as
    SDA_CATALOG_FALLBACKS says), each under its own two-hour limit; their elements are
    applied only where they are newer than what the catalog holds.

    Returns how many groups CelesTrak was asked for, which is zero when every group is
    still inside the repeat window.
    """
    global _last_tle_refresh
    live_count = 0
    served = 0
    resting = [group for group in CELESTRAK_GROUPS if _resting(group)]
    if resting:
        logger.info(
            "CelesTrak: %d of %d groups were asked for in the last %.0f minutes, not asked again",
            len(resting), len(CELESTRAK_GROUPS), CELESTRAK_REPEAT_WINDOW_S / 60.0,
        )
    asked = len(CELESTRAK_GROUPS) - len(resting)
    if asked:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                import asyncio as _aio
                tasks = [_fetch_celestrak_group(client, g) for g in CELESTRAK_GROUPS]
                results = await _aio.gather(*tasks, return_exceptions=True)
                live_count = sum(r for r in results if isinstance(r, int))
                served = sum(1 for r in results if isinstance(r, int) and r > 0)
        except Exception:
            pass
        logger.info(
            "CelesTrak: %d objects from %d of %d groups asked", live_count, served, asked
        )
    if served:
        _set_catalog_source("CelesTrak", time.time(), groups=served, objects=live_count)

    fallback = 0
    if served == 0 and _celestrak_failing():
        for source in _catalog_fallbacks():
            fallback += await _fetch_fallback(source)

    if live_count == 0:
        # Fills in what is missing and nothing else. A plain load is last-write-wins,
        # and the bundled elements are older than anything a refresh or the cache put
        # in the catalog.
        _load_newer_only(DEMO_TLE_TEXT, source=SOURCE_BUNDLED)

    if served or fallback:
        _last_tle_refresh = datetime.now(UTC)
    elif _last_tle_refresh is None:
        # Nothing new arrived: a restart inside the window asked nobody, or nobody
        # answered with elements. The catalog is as new as the last download it was
        # loaded from, and that is the time to report.
        _last_tle_refresh = _last_download_at()
    return asked


async def _fetch_live_f107() -> None:
    """Fetch current F10.7 solar flux from NOAA Space Weather Prediction Center."""
    global _live_f107
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(NOAA_F107_OBSERVED_URL)
            if resp.status_code == 200:
                data = resp.json()
                rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
                if rows:
                    # SWPC lists this product newest first; pick by time_tag and
                    # fall back to the last row only when no row carries one.
                    tagged = [r for r in rows if r.get("time_tag")]
                    latest = max(tagged, key=lambda r: str(r["time_tag"])) if tagged else rows[-1]
                    flux = float(latest.get("flux", 150.0))
                    if 50.0 < flux < 400.0:
                        _live_f107 = flux
                        return
            resp = await client.get(NOAA_F107_URL)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list):
                    now_str = datetime.now(UTC).strftime("%Y-%m")
                    for entry in data:
                        if entry.get("time-tag", "").startswith(now_str):
                            flux = float(entry.get("predicted_ssn", 150.0))
                            _live_f107 = max(70.0, 67.0 + 0.572 * flux)
                            return
    except Exception:
        pass


async def _background_refresh_loop() -> None:
    """Background task: refresh TLEs every TLE_REFRESH_INTERVAL_S, F10.7 every third pass."""
    f107_counter = 0
    delay_s = _first_refresh_delay_s()
    while True:
        await asyncio.sleep(delay_s)
        delay_s = TLE_REFRESH_INTERVAL_S
        await _seed_catalog()
        f107_counter += 1
        if f107_counter % 3 == 0:
            await _fetch_live_f107()


# --- App lifecycle ---

async def _initial_live_fetch() -> None:
    """Fetch live CelesTrak + F10.7 data (runs in background after instant startup)."""
    await _seed_catalog()
    await _fetch_live_f107()


def _offline() -> bool:
    """True when SDA_OFFLINE asks for a start with no background fetch.

    The app then runs on the bundled catalog (and the disk cache, when one is
    configured) and asks CelesTrak and NOAA for nothing until told to. The static
    demo build bakes under it, so every build bakes the same catalog.
    """
    return os.environ.get("SDA_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tle_refresh_task
    _metrics["startup_time"] = datetime.now(UTC).isoformat()
    # Instant startup: load bundled demo TLEs synchronously
    store.load_from_text(DEMO_TLE_TEXT, source=SOURCE_BUNDLED)
    # Then the last good catalog from disk (no-op unless SDA_TLE_CACHE_DIR is set)
    _load_tle_cache()
    background: list[asyncio.Task] = []
    _tle_refresh_task = None
    if not _offline():
        # Fetch live data in background - app is already serving
        _tle_refresh_task = asyncio.create_task(_initial_live_fetch())
        background = [_tle_refresh_task, asyncio.create_task(_background_refresh_loop())]
    # Public SATCAT for the object dossier: loads its disk cache, downloads only
    # when the cache is a day old and the app is not offline.
    from sda import satcat

    background.append(asyncio.create_task(satcat.refresh_forever(allow_network=not _offline())))
    yield
    for task in background:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    # The maritime sampler stops itself when idle; this ends it with the app.
    from sda import maritime

    maritime.stop_sampler()


# --- App setup ---

app = FastAPI(
    title="Vantage: open space-domain awareness",
    description=(
        "Orbital mechanics engine for satellite conjunction analysis. "
        "Uses SGP4 propagation to predict close approaches between tracked objects, "
        "classify collision risk, compute collision probability (Pc), and render "
        "interactive 3D visualizations.\n\n"
        "## Features\n"
        "- **SGP4 Propagation**: WGS72 gravity model, ECI frame, vectorized NumPy\n"
        "- **Two-Phase Conjunction Detection**: Coarse (60s) -> Fine (1s) refinement\n"
        "- **Collision Probability**: 2D Pc via Chan/Alfano B-plane projection\n"
        "- **Risk Classification**: CRITICAL / HIGH / MODERATE / LOW / NEGLIGIBLE\n"
        "- **CCSDS CDM Generation**: Standard conjunction data messages\n"
        "- **Monte Carlo Analysis**: Gaussian position-noise miss distance distributions\n"
        "- **Maneuver Planning**: Along-track / cross-track / radial delta-V\n"
        "- **Orbital Decay Estimation**: Atmospheric drag lifetime prediction\n"
        "- **TLE Freshness Monitoring**: Staleness warnings and accuracy alerts\n"
        "- **WebSocket Live Tracking**: Real-time satellite position updates\n"
        "- **3D Visualization**: Plotly orbits, screening volumes, conjunction markers\n"
        "- **CesiumJS WorldView**: Interactive globe with CRT/NVG/FLIR modes\n"
    ),
    version=__version__,
    lifespan=lifespan,
)

# --- Network posture ---
#
# Middleware added later wraps middleware added earlier, so the order below puts the rate
# limiter innermost: a 429 it returns still passes back out through the security headers
# and the CORS layer, so browsers and operators see a well formed response.
#
# Per-IP token bucket over /api/ and /agent/ (configure with SDA_RATE_LIMIT).
app.add_middleware(RateLimitMiddleware)

# X-Content-Type-Options, Referrer-Policy, X-Frame-Options. Deliberately no
# Content-Security-Policy: the worldview page loads CesiumJS and the visualization page loads
# plotly.js from public CDNs and both use inline scripts and styles, so any CSP strict enough
# to be worth setting would break the globe. Revisit if those assets are ever vendored.
app.add_middleware(SecurityHeadersMiddleware)

# CORS allowlist from SDA_CORS_ORIGINS (comma separated). Defaults to the localhost and
# 127.0.0.1 origins for the bound port. Credentials are off: the API has no cookie session,
# so no browser origin needs them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def count_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    _metrics["api_requests"] += 1
    return await call_next(request)


# Include routers
app.include_router(worldview_router)
app.include_router(system_router)
app.include_router(satellites_router)
app.include_router(conjunctions_router)
app.include_router(analysis_router)

# --- feature routers registered by the integrator ---
app.include_router(ops_router)
app.include_router(passes_router)
app.include_router(agent_router)
app.include_router(history_router)
app.include_router(pc_router)
app.include_router(cameras_router)
app.include_router(space_router)
app.include_router(eonet_router)
app.include_router(summit_router)
app.include_router(peaks_router)
app.include_router(satcat_router)
app.include_router(weather_router)
app.include_router(sanctions_router)
app.include_router(maritime_router)
app.include_router(intel_router)
app.include_router(aviation_router)
app.include_router(infra_router)
app.include_router(orbitviz_router)
app.include_router(search_router)
app.include_router(conflict_router)
app.include_router(buildings_router)


def run() -> None:
    """Start the server. Entry point for `sda-server` and `python -m sda.api`.

    Binds SDA_HOST (default 127.0.0.1, loopback only) and SDA_PORT (default 8000). The
    container images set SDA_HOST=0.0.0.0 so published ports keep working.
    """
    import uvicorn

    uvicorn.run("sda.api:app", host=bind_host(), port=bind_port())


if __name__ == "__main__":
    run()
