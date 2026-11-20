"""Public camera catalog behind the globe's CCTV layer.

About 225,000 public cameras (road authority traffic cameras, city and landmark
webcams, beaches, ski areas, airports, ports, observatories) from 170+ countries,
held in a local SQLite file with an R*Tree index so the globe can ask for the
cameras inside its view, a density grid for the whole Earth, the cameras nearest
a point (a selected satellite's ground point, a tracked aircraft), or one camera's
feed.

Where the cameras come from
---------------------------
The catalog is imported from the camera export of Argus
(https://github.com/GoSlowPoke168/Argus, MIT license, Copyright (c) 2026
Jeremy Hou), file ``public/cameras.geojson``, pinned at ``ARGUS_COMMIT``. Argus
builds it from about ten government and commercial camera APIs plus the
opencctv.org index. Taken from it: the data file, its field names and the
meaning of ``directEligible`` (the feed embeds straight from its origin) and of
the ``txdot://`` snapshot API (``scripts/scrapers/txdot_resolver.py``). The
SQLite schema, the R*Tree queries, the feed classification, the type groups and
the snapshot proxy are ours. The MIT notice is in THIRD_PARTY.md.

Nothing is downloaded by the server. ``python -m sda.cameras import`` fetches the
file once (about 100 MB, keyless, from raw.githubusercontent.com) or reads a
local copy (``--from``), and writes ``cameras.sqlite`` under ``SDA_CAMERA_DIR``
(default: ``%LOCALAPPDATA%/sda/cameras`` on Windows, ``~/.cache/sda/cameras``
elsewhere). Until that has run the endpoints answer with an empty catalog and
say how to import it.

Coordinates are geodetic degrees as the camera operators publish them. Nothing
here touches an inertial frame.

Feeds are never fetched by the server except one case: TxDOT snapshots, whose
API sends no CORS header and returns base64 JSON, are fetched from the fixed host
``its.txdot.gov`` by ``fetch_txdot_snapshot`` for the camera id the client names.
The URL comes from this catalog, never from the request, so the proxy cannot be
pointed anywhere else.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ARGUS_REPO = "GoSlowPoke168/Argus"
ARGUS_COMMIT = "7a56f8d9676240c9c6c28bc2e278df88ea803914"
ARGUS_DATA_PATH = "public/cameras.geojson"
ARGUS_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

CAMERA_DIR_ENV = "SDA_CAMERA_DIR"
DB_NAME = "cameras.sqlite"
SCHEMA_VERSION = "1"

#: Camera types as Argus labels them, grouped into the chips the globe shows.
TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "traffic": ("traffic",),
    "city": ("landmark", "tourism"),
    "beach": ("beach", "water"),
    "mountain": ("ski", "volcano"),
    "weather": ("weather",),
    "aviation": ("aviation", "airport"),
    "nature": ("nature", "wildlife"),
    "transport": ("port", "border", "railway"),
    "science": ("science", "observatory", "satellite", "space"),
}
_GROUP_OF_TYPE = {t: g for g, types in TYPE_GROUPS.items() for t in types}
GROUPS: tuple[str, ...] = tuple(TYPE_GROUPS)
OTHER_GROUP = "other"

#: How the client plays a feed.
#:   image    a still image, re-requested every ``refresh_s`` seconds
#:   mjpeg    a motion-JPEG stream, shown in an <img>
#:   hls      an HLS playlist (.m3u8)
#:   mp4      a short video clip
#:   youtube  a YouTube embed id, played through youtube-nocookie.com
#:   txdot    a TxDOT snapshot, fetched through /api/cameras/{rid}/snapshot
#:   page     a web page that hosts the feed; the client links to it and never
#:            frames it
FEED_KINDS: tuple[str, ...] = ("image", "mjpeg", "hls", "mp4", "youtube", "txdot", "page")

#: Sources left out of the import because they list devices their owners never
#: published: cameras found by scanning the internet for open devices, the
#: insecam pattern. Opentopia says on its own site that its cameras come from such
#: a scan (599 rows in the Argus export at ARGUS_COMMIT). A name added here drops
#: every camera from that source.
EXCLUDED_SOURCES: frozenset[str] = frozenset({"opencctv_opentopia"})

#: Hosts whose cameras are dropped wherever they appear: scanned-device
#: directories, and the dynamic-DNS services home routers and DVRs use, which
#: mark a private camera rather than an operator's public one.
PRIVATE_HOST_SUFFIXES: tuple[str, ...] = (
    "opentopia.com", "insecam.org", "insecam.com",
    "myfritz.net", "ddns.net", "dyndns.org", "dyndns.com", "dyndns.info", "dynalias.com",
    "duckdns.org", "no-ip.org", "no-ip.biz", "no-ip.info", "noip.me", "hopto.org",
    "zapto.org", "sytes.net", "ddns.me", "freeddns.org", "dvrdns.org", "camddns.com",
    "dlinkddns.com", "tplinkdns.com", "asuscomm.com", "synology.me", "myqnapcloud.com",
)
#: Host labels that mark a dynamic-DNS name on any domain (ddns.example.com).
_PRIVATE_HOST_LABELS = frozenset({"ddns", "dyndns", "myfritz"})
#: Query keys that carry a password. A URL holding one is never stored.
_SECRET_QUERY_KEYS = frozenset({"pwd", "pass", "password", "passwd", "pw"})

#: Sources whose terms forbid reuse or framing of their images. Their cameras
#: are kept as links to the operator (feed kind "page"), never played in the app.
LINK_ONLY_SOURCES: frozenset[str] = frozenset({
    "opencctv_houstontranstar", "opencctv_autostrade", "opencctv_weatherstem",
    "opencctv_itraffic", "opencctv_vedetta",
})

TXDOT_HOST = "its.txdot.gov"
TXDOT_PATH = "/its/DistrictIts/GetCctvSnapshotByIcdId"

_YOUTUBE_EMBED_HOSTS = frozenset({
    "www.youtube.com", "youtube.com", "www.youtube-nocookie.com", "youtube-nocookie.com",
})
_YOUTUBE_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

MAX_NAME = 160
MAX_TEXT = 80
MAX_URL = 2000
MIN_REFRESH_S = 5
DEFAULT_REFRESH_S = 60
MAX_REFRESH_S = 3600

EARTH_MEAN_RADIUS_KM = 6371.0


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def camera_dir() -> Path:
    """Directory holding the catalog: SDA_CAMERA_DIR, else the user cache dir."""
    configured = os.environ.get(CAMERA_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "cameras"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "cameras"


def db_path() -> Path:
    return camera_dir() / DB_NAME


# ---------------------------------------------------------------------------
# Normalizing one Argus feature
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraRow:
    """One camera as stored. ``rid`` is assigned at insert time."""

    id: str
    name: str
    type: str
    group: str
    city: str
    country: str
    lat: float
    lon: float
    feed_kind: str
    feed_url: str
    page_url: str
    source: str
    playable: bool
    refresh_s: int
    rank: int


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit]


def is_private_host(host: str) -> bool:
    """True for a scanned-device directory or a home dynamic-DNS name."""
    host = host.lower().rstrip(".")
    if any(host == s or host.endswith("." + s) for s in PRIVATE_HOST_SUFFIXES):
        return True
    return any(label in _PRIVATE_HOST_LABELS for label in host.split("."))


def _has_secret(parsed: Any) -> bool:
    """True when the URL carries a login: user:pass@ or a password query key."""
    try:
        if parsed.username or parsed.password:
            return True
    except ValueError:
        return True
    for part in parsed.query.split("&"):
        key = part.split("=", 1)[0].strip().lower()
        if key.startswith("amp;"):
            key = key[4:]
        if key in _SECRET_QUERY_KEYS:
            return True
    return False


def _http_url(value: Any) -> str:
    """The value when it is a public absolute http(s) URL of sane length, else ''.

    Refused as well: a URL holding a login (user:pass@ or a password query key)
    and one on a scanned-device or home dynamic-DNS host (is_private_host).
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        return ""
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not host:
        return ""
    if any(ch.isspace() for ch in value):
        return ""
    if _has_secret(parsed) or is_private_host(host):
        return ""
    return value


def youtube_id(url: str) -> str:
    """The video id of a youtube.com/embed/<id> URL, or '' for anything else."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in _YOUTUBE_EMBED_HOSTS:
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2 or parts[0] != "embed":
        return ""
    vid = parts[1]
    if len(vid) != 11 or not set(vid) <= _YOUTUBE_ID_CHARS:
        return ""
    return vid


def is_txdot_snapshot_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == TXDOT_HOST
        and parsed.path == TXDOT_PATH
    )


def classify_feed(feed_type: str, feed_url: str, stream_url: str) -> tuple[str, str]:
    """Map an Argus feedType and its URLs to (feed kind, URL the client uses)."""
    ft = (feed_type or "").lower().strip()
    if ft in ("m3u8", "mp4"):
        url = _http_url(stream_url) or _http_url(feed_url)
        return ("hls" if ft == "m3u8" else "mp4"), url
    url = _http_url(feed_url)
    if not url:
        return "", ""
    if ft == "txdot-json":
        return ("txdot", url) if is_txdot_snapshot_url(url) else ("", "")
    if ft == "mjpeg":
        return "mjpeg", url
    if ft == "iframe":
        vid = youtube_id(url)
        return ("youtube", vid) if vid else ("page", url)
    if ft.startswith("image") or ft == "":
        return "image", url
    return "", ""


def group_of(cam_type: str) -> str:
    return _GROUP_OF_TYPE.get(cam_type, OTHER_GROUP)


def stable_rank(cam_id: str) -> int:
    """A fixed pseudo-random rank so a capped query samples the whole view evenly."""
    return zlib.crc32(cam_id.encode("utf-8")) & 0x7FFFFFFF


def normalize_feature(feature: Any) -> CameraRow | None:
    """One GeoJSON feature from the Argus export, or None when it is unusable.

    Dropped: non-point geometry, coordinates outside the valid range or exactly
    (0, 0), feeds whose URL is not plain http(s) (Argus keeps placeholder schemes
    such as ``faa-weathercam://`` that only its own resolvers understand), feeds
    that carry a login or sit on a private host (see _http_url), and every
    camera from a source in EXCLUDED_SOURCES. A camera from LINK_ONLY_SOURCES is
    kept as a link to its operator.
    """
    if not isinstance(feature, dict):
        return None
    geom = feature.get("geometry")
    props = feature.get("properties")
    if not isinstance(geom, dict) or not isinstance(props, dict):
        return None
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, list | tuple) or len(coords) < 2:
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
        return None

    cam_id = _text(props.get("id"), 300)
    source = _text(props.get("source"), MAX_TEXT)
    if not cam_id or source in EXCLUDED_SOURCES:
        return None

    kind, url = classify_feed(
        str(props.get("feedType") or ""),
        str(props.get("feedUrl") or ""),
        str(props.get("streamUrl") or ""),
    )
    if not kind:
        return None

    cam_type = _text(props.get("type"), 32).lower() or "other"
    direct = bool(props.get("directEligible", True))
    if source in LINK_ONLY_SOURCES and kind != "page":
        kind = "page"
        url = _http_url(props.get("playerUrl")) or _http_url(url)
        if not url:
            return None
    if kind in ("youtube", "txdot"):
        playable = True
    elif kind == "page":
        playable = False
    else:
        playable = direct

    rate_ms = props.get("updateRate")
    refresh_s = DEFAULT_REFRESH_S
    if isinstance(rate_ms, int | float) and math.isfinite(rate_ms) and rate_ms > 0:
        refresh_s = int(round(rate_ms / 1000.0))
    refresh_s = max(MIN_REFRESH_S, min(MAX_REFRESH_S, refresh_s))

    page_url = _http_url(props.get("playerUrl"))
    if kind == "page" and not page_url:
        page_url = url

    return CameraRow(
        id=cam_id,
        name=_text(props.get("name"), MAX_NAME) or cam_id[:MAX_NAME],
        type=cam_type,
        group=group_of(cam_type),
        city=_text(props.get("city"), MAX_TEXT),
        country=_text(props.get("country"), 8).upper(),
        lat=round(lat, 6),
        lon=round(lon, 6),
        feed_kind=kind,
        feed_url=url,
        page_url=page_url,
        source=source,
        playable=playable,
        refresh_s=refresh_s,
        rank=stable_rank(cam_id),
    )


# ---------------------------------------------------------------------------
# Building the database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE cameras (
    rid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    grp TEXT NOT NULL,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    feed_kind TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    page_url TEXT NOT NULL,
    source TEXT NOT NULL,
    playable INTEGER NOT NULL,
    refresh_s INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    dkey TEXT NOT NULL
);
CREATE VIRTUAL TABLE cam_index USING rtree(rid, min_lon, max_lon, min_lat, max_lat);
CREATE TABLE density (
    cell REAL NOT NULL,
    gy INTEGER NOT NULL,
    gx INTEGER NOT NULL,
    grp TEXT NOT NULL,
    n INTEGER NOT NULL,
    playable INTEGER NOT NULL,
    slat REAL NOT NULL,
    slon REAL NOT NULL,
    PRIMARY KEY (cell, gy, gx, grp)
) WITHOUT ROWID;
"""

_INDEXES = """
CREATE INDEX cameras_grp ON cameras(grp, playable);
CREATE INDEX cameras_country ON cameras(country);
"""

#: Grid sizes, in degrees, of the density table written at import.
DENSITY_CELLS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

_WINDY_ID = re.compile(r"windy\.com/(?:[^/?#]+/)*?(\d{8,12})(?:/|\.jpg)")


def dedupe_key(source: str, feed_url: str) -> str:
    """A key shared by copies of one webcam listed under two sources, else ''.

    Windy webcams appear both from Windy itself and through the opencctv index,
    under different ids but with the same numeric webcam id in the image URL.
    """
    m = _WINDY_ID.search(feed_url)
    return "windy:" + m.group(1) if m else ""


#: Duplicate passes run after the insert, each keeping the playable copy first
#: and then the earliest. Argus lists one webcam several times when two of its
#: sources index it (about 22,000 copies in the 2026-07-25 export).
_DEDUPE_PASSES = (
    "feed_url",
    "dkey",
    "round(lat, 4), round(lon, 4), lower(name)",
)


@dataclass
class BuildReport:
    read: int = 0
    kept: int = 0
    dropped: int = 0
    duplicates: int = 0
    copies: int = 0


def build_database(
    features: Iterable[Any], dest: Path, meta: dict[str, str] | None = None
) -> BuildReport:
    """Write a fresh catalog to *dest* from Argus GeoJSON features.

    The file is written beside *dest* and renamed over it at the end. On Windows
    a running server holds *dest* open and the rename fails; the new file is
    then left as ``<dest>.new`` and the server swaps it in on its next query.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    report = BuildReport()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(_SCHEMA)
        seen: set[str] = set()
        batch: list[CameraRow] = []
        for feature in features:
            report.read += 1
            row = normalize_feature(feature)
            if row is None:
                report.dropped += 1
                continue
            if row.id in seen:
                report.duplicates += 1
                continue
            seen.add(row.id)
            batch.append(row)
            if len(batch) >= 5000:
                _insert(conn, batch)
                batch.clear()
        if batch:
            _insert(conn, batch)
        report.copies = _dedupe(conn)
        report.kept = conn.execute("SELECT count(*) FROM cameras").fetchone()[0]
        conn.executescript(_INDEXES)
        _fill_density(conn)
        all_meta = {
            "schema": SCHEMA_VERSION,
            "imported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": str(report.kept),
        }
        all_meta.update(meta or {})
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", list(all_meta.items()))
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    try:
        os.replace(tmp, dest)
    except PermissionError:
        os.replace(tmp, dest.with_name(dest.name + ".new"))
    return report


def _dedupe(conn: sqlite3.Connection) -> int:
    """Delete repeated listings of one camera; returns how many went."""
    removed = 0
    for key in _DEDUPE_PASSES:
        where = " WHERE dkey != ''" if key == "dkey" else ""
        cur = conn.execute(
            "DELETE FROM cameras WHERE rid IN (SELECT rid FROM (SELECT rid, row_number()"
            f" OVER (PARTITION BY {key} ORDER BY playable DESC, rid) AS rn FROM cameras"
            f"{where}) WHERE rn > 1)"
        )
        removed += cur.rowcount
    conn.execute("DELETE FROM cam_index WHERE rid NOT IN (SELECT rid FROM cameras)")
    return removed


def _fill_density(conn: sqlite3.Connection) -> None:
    for cell in DENSITY_CELLS:
        conn.execute(
            "INSERT INTO density(cell, gy, gx, grp, n, playable, slat, slon)"
            " SELECT ?, CAST((lat + 90.0) / ? AS INTEGER) AS gy,"
            " CAST((lon + 180.0) / ? AS INTEGER) AS gx, grp, count(*), sum(playable),"
            " sum(lat), sum(lon) FROM cameras GROUP BY gy, gx, grp",
            (cell, cell, cell),
        )


def _insert(conn: sqlite3.Connection, rows: list[CameraRow]) -> None:
    cur = conn.cursor()
    for r in rows:
        cur.execute(
            "INSERT INTO cameras(id, name, type, grp, city, country, lat, lon, feed_kind,"
            " feed_url, page_url, source, playable, refresh_s, rank, dkey)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r.id, r.name, r.type, r.group, r.city, r.country, r.lat, r.lon, r.feed_kind,
             r.feed_url, r.page_url, r.source, int(r.playable), r.refresh_s, r.rank,
             dedupe_key(r.source, r.feed_url)),
        )
        rid = cur.lastrowid
        cur.execute(
            "INSERT INTO cam_index(rid, min_lon, max_lon, min_lat, max_lat)"
            " VALUES (?, ?, ?, ?, ?)",
            (rid, r.lon, r.lon, r.lat, r.lat),
        )


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

_LIST_COLUMNS = "c.rid, c.name, c.lat, c.lon, c.grp, c.feed_kind, c.playable, c.country, c.city"
_DETAIL_COLUMNS = (
    "rid, id, name, type, grp, city, country, lat, lon, feed_kind, feed_url, page_url,"
    " source, playable, refresh_s"
)


def _list_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "rid": row[0], "name": row[1], "lat": row[2], "lon": row[3], "group": row[4],
        "feed": row[5], "playable": bool(row[6]), "country": row[7], "city": row[8],
    }


def _detail_row(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = ("rid", "id", "name", "type", "group", "city", "country", "lat", "lon", "feed",
            "feed_url", "page_url", "source", "playable", "refresh_s")
    out = dict(zip(keys, row, strict=True))
    out["playable"] = bool(out["playable"])
    return out


def _lon_spans(west: float, east: float) -> list[tuple[float, float]]:
    """Longitude intervals for a box, split in two when it crosses the antimeridian."""
    west = max(-180.0, min(180.0, west))
    east = max(-180.0, min(180.0, east))
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def _group_filter(groups: Iterable[str] | None) -> tuple[str, list[str]]:
    if groups is None:
        return "", []
    wanted = [g for g in groups if g in GROUPS or g == OTHER_GROUP]
    if not wanted:
        return " AND 0", []
    return " AND c.grp IN (" + ",".join("?" * len(wanted)) + ")", wanted


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class CameraCatalog:
    """Read-only access to one catalog file, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._summary: dict[str, Any] | None = None
        self._density_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._checked = 0.0

    # -- connection ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._summary = None
        self._density_cache.clear()

    def _swap_pending_locked(self) -> None:
        pending = self.path.with_name(self.path.name + ".new")
        if not pending.exists():
            return
        self._close_locked()
        try:
            os.replace(pending, self.path)
        except OSError:
            return

    def _connection(self) -> sqlite3.Connection | None:
        """Open the file on first use; pick up a re-import at most every 10 s."""
        now = time.monotonic()
        if now - self._checked > 10.0 or self._conn is None:
            self._checked = now
            self._swap_pending_locked()
        if self._conn is None:
            if not self.path.exists():
                return None
            uri = self.path.resolve().as_uri() + "?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.execute("SELECT 1 FROM cameras LIMIT 1")
            except sqlite3.Error:
                return None
            self._conn = conn
        return self._conn

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connection() is not None

    # -- queries ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return {
                    "available": False,
                    "count": 0,
                    "hint": "Run: python -m sda.cameras import",
                    "groups": {},
                    "feeds": {},
                    "countries": [],
                }
            if self._summary is not None:
                return self._summary
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            total, playable = conn.execute(
                "SELECT count(*), coalesce(sum(playable), 0) FROM cameras"
            ).fetchone()
            groups = dict(conn.execute(
                "SELECT grp, count(*) FROM cameras GROUP BY grp ORDER BY 2 DESC"
            ).fetchall())
            feeds = dict(conn.execute(
                "SELECT feed_kind, count(*) FROM cameras GROUP BY feed_kind ORDER BY 2 DESC"
            ).fetchall())
            countries = conn.execute(
                "SELECT country, count(*) FROM cameras WHERE country != ''"
                " GROUP BY country ORDER BY 2 DESC"
            ).fetchall()
            sources = conn.execute("SELECT count(DISTINCT source) FROM cameras").fetchone()[0]
            self._summary = {
                "available": True,
                "count": int(total),
                "playable": int(playable),
                "groups": groups,
                "feeds": feeds,
                "countries": [{"code": c, "count": n} for c, n in countries],
                "country_count": len(countries),
                "source_count": int(sources),
                "imported_at": meta.get("imported_at", ""),
                "generated": meta.get("generated", ""),
                "origin": meta.get("origin", ""),
                "origin_ref": meta.get("origin_ref", ""),
                "attribution": "Camera catalog from Argus (GoSlowPoke168/Argus, MIT); "
                               "each feed belongs to its operator.",
            }
            return self._summary

    def in_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        limit: int = 1500,
        groups: Iterable[str] | None = None,
        playable_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Cameras inside the box, playable first, then an even sample by rank.

        Returns (cameras, total in the box under the same filters).
        """
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        gsql, gargs = _group_filter(groups)
        psql = " AND c.playable = 1" if playable_only else ""
        rows: list[tuple[Any, ...]] = []
        total = 0
        with self._lock:
            conn = self._connection()
            if conn is None:
                return [], 0
            for w, e in _lon_spans(west, east):
                where = (
                    " FROM cam_index r JOIN cameras c ON c.rid = r.rid"
                    " WHERE r.max_lon >= ? AND r.min_lon <= ? AND r.max_lat >= ?"
                    " AND r.min_lat <= ?" + gsql + psql
                )
                args: list[Any] = [w, e, south, north, *gargs]
                total += conn.execute("SELECT count(*)" + where, args).fetchone()[0]
                rows.extend(conn.execute(
                    "SELECT " + _LIST_COLUMNS + ", c.rank" + where
                    + " ORDER BY c.playable DESC, c.rank LIMIT ?",
                    [*args, limit],
                ).fetchall())
        if len(rows) > limit:
            rows.sort(key=lambda r: (-r[6], r[9]))
            rows = rows[:limit]
        return [_list_row(r) for r in rows], int(total)

    def density(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        cell_deg: float,
        groups: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Camera counts per grid cell, each placed at the mean position of its cameras.

        Read from the table written at import, using the smallest stored cell that
        is at least *cell_deg*, so a whole-Earth view costs a few milliseconds.
        """
        cell = next((c for c in DENSITY_CELLS if c >= cell_deg - 1e-9), DENSITY_CELLS[-1])
        group_list = None if groups is None else sorted(set(groups))
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        key = (round(west, 2), round(south, 2), round(east, 2), round(north, 2), cell,
               tuple(group_list) if group_list is not None else None)
        gsql, gargs = _group_filter(group_list)
        gsql = gsql.replace("c.grp", "grp")
        gy0 = int((south + 90.0) // cell)
        gy1 = int((north + 90.0) // cell)
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            cached = self._density_cache.get(key)
            if cached is not None:
                return cached
            cells: list[dict[str, Any]] = []
            for w, e in _lon_spans(west, east):
                gx0 = int((w + 180.0) // cell)
                gx1 = int((e + 180.0) // cell)
                for n, play, slat, slon in conn.execute(
                    "SELECT sum(n), sum(playable), sum(slat), sum(slon) FROM density"
                    " WHERE cell = ? AND gy BETWEEN ? AND ? AND gx BETWEEN ? AND ?"
                    + gsql + " GROUP BY gy, gx",
                    [cell, gy0, gy1, gx0, gx1, *gargs],
                ):
                    cells.append({
                        "lat": round(slat / n, 4), "lon": round(slon / n, 4),
                        "count": int(n), "playable": int(play or 0),
                    })
            if len(self._density_cache) > 256:
                self._density_cache.clear()
            self._density_cache[key] = cells
            return cells

    def nearest(
        self,
        lat: float,
        lon: float,
        n: int = 10,
        radius_km: float = 50.0,
        groups: Iterable[str] | None = None,
        playable_only: bool = True,
    ) -> list[dict[str, Any]]:
        """The *n* cameras closest to (lat, lon) within *radius_km*, nearest first."""
        radius_km = max(0.1, min(2000.0, radius_km))
        dlat = math.degrees(radius_km / EARTH_MEAN_RADIUS_KM)
        coslat = math.cos(math.radians(max(-89.0, min(89.0, lat))))
        dlon = min(180.0, dlat / max(coslat, 1e-3))
        west = lon - dlon
        east = lon + dlon
        if west < -180.0:
            west += 360.0
        if east > 180.0:
            east -= 360.0
        if dlon >= 180.0:
            west, east = -180.0, 180.0
        gsql, gargs = _group_filter(groups)
        psql = " AND c.playable = 1" if playable_only else ""
        found: list[tuple[float, tuple[Any, ...]]] = []
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            for w, e in _lon_spans(west, east):
                for row in conn.execute(
                    "SELECT " + _LIST_COLUMNS
                    + " FROM cam_index r JOIN cameras c ON c.rid = r.rid"
                    " WHERE r.max_lon >= ? AND r.min_lon <= ? AND r.max_lat >= ?"
                    " AND r.min_lat <= ?" + gsql + psql,
                    [w, e, lat - dlat, lat + dlat, *gargs],
                ):
                    d = haversine_km(lat, lon, row[2], row[3])
                    if d <= radius_km:
                        found.append((d, row))
        found.sort(key=lambda t: t[0])
        out = []
        for d, row in found[: max(1, min(100, n))]:
            item = _list_row(row)
            item["distance_km"] = round(d, 2)
            out.append(item)
        return out

    def random(
        self, groups: Iterable[str] | None = None, playable_only: bool = True
    ) -> dict[str, Any] | None:
        gsql, gargs = _group_filter(groups)
        psql = " AND c.playable = 1" if playable_only else ""
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT " + _DETAIL_COLUMNS
                + " FROM cameras c WHERE 1" + gsql + psql + " ORDER BY random() LIMIT 1",
                gargs,
            ).fetchone()
        return _detail_row(row) if row else None

    def get(self, rid: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT " + _DETAIL_COLUMNS + " FROM cameras WHERE rid = ?", (rid,)
            ).fetchone()
        return _detail_row(row) if row else None

    def search(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Cameras whose name or city contains *text* (case-insensitive), playable first."""
        needle = text.strip()
        if len(needle) < 2:
            return []
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT " + _LIST_COLUMNS + " FROM cameras c"
                " WHERE c.name LIKE ? ESCAPE '\\' OR c.city LIKE ? ESCAPE '\\'"
                " ORDER BY c.playable DESC, c.rank LIMIT ?",
                (pattern, pattern, max(1, min(100, limit))),
            ).fetchall()
        return [_list_row(r) for r in rows]


_catalog: CameraCatalog | None = None
_catalog_lock = threading.Lock()


def get_catalog() -> CameraCatalog:
    """The process-wide catalog for the current SDA_CAMERA_DIR."""
    global _catalog
    path = db_path()
    with _catalog_lock:
        if _catalog is None or _catalog.path != path:
            if _catalog is not None:
                _catalog.close()
            _catalog = CameraCatalog(path)
        return _catalog


def reset_catalog() -> None:
    """Forget the cached catalog (tests change SDA_CAMERA_DIR between cases)."""
    global _catalog
    with _catalog_lock:
        if _catalog is not None:
            _catalog.close()
        _catalog = None


# ---------------------------------------------------------------------------
# TxDOT snapshot proxy
# ---------------------------------------------------------------------------

_JPEG_MAGIC = b"\xff\xd8\xff"


def decode_txdot_snapshot(payload: Any) -> bytes | None:
    """JPEG bytes from a TxDOT GetCctvSnapshotByIcdId reply, or None."""
    if not isinstance(payload, dict):
        return None
    snippet = payload.get("snippet")
    if not isinstance(snippet, str) or not snippet:
        return None
    if snippet.startswith("data:"):
        snippet = snippet.split(",", 1)[-1]
    try:
        data = base64.b64decode(snippet, validate=False)
    except (ValueError, TypeError):
        return None
    return data if data.startswith(_JPEG_MAGIC) else None


async def fetch_txdot_snapshot(url: str, client: Any = None) -> bytes | None:
    """Fetch one TxDOT snapshot. *url* must come from the catalog, never a request."""
    if not is_txdot_snapshot_url(url):
        return None
    import httpx

    owned = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await http.get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None
        return decode_txdot_snapshot(resp.json())
    except Exception:
        return None
    finally:
        if owned:
            await http.aclose()


# ---------------------------------------------------------------------------
# Import command
# ---------------------------------------------------------------------------


def iter_features(path: Path) -> Iterator[Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError(f"{path} is not a GeoJSON FeatureCollection")
    yield from features


def read_generated(path: Path) -> str:
    """The export's own 'generated' stamp, read from the head of the file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    marker = '"generated"'
    i = head.find(marker)
    if i < 0:
        return ""
    rest = head[i + len(marker):].lstrip(" :")
    if not rest.startswith('"'):
        return ""
    end = rest.find('"', 1)
    return rest[1:end] if end > 0 else ""


def download(ref: str, dest: Path) -> None:
    """Stream the Argus export at *ref* to *dest* (keyless, about 100 MB)."""
    import httpx

    url = ARGUS_URL.format(repo=ARGUS_REPO, ref=ref, path=ARGUS_DATA_PATH)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    last = 0.0
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 2.0:
                    last = time.monotonic()
                    print(f"  downloaded {done / 1e6:.1f} MB", flush=True)
    os.replace(part, dest)
    print(f"  downloaded {done / 1e6:.1f} MB from {url}", flush=True)


def _cmd_import(args: argparse.Namespace) -> int:
    dest = Path(args.db) if args.db else db_path()
    if args.source:
        src = Path(args.source)
        origin = str(src)
        keep = True
    else:
        src = dest.with_name("argus-cameras.geojson")
        origin = f"https://github.com/{ARGUS_REPO}"
        print(f"Fetching the Argus camera export at {args.ref} ...", flush=True)
        download(args.ref, src)
        keep = args.keep
    print(f"Importing {src} ...", flush=True)
    started = time.monotonic()
    report = build_database(
        iter_features(src),
        dest,
        {"origin": origin, "origin_ref": args.ref if not args.source else "",
         "generated": read_generated(src)},
    )
    if not keep:
        src.unlink(missing_ok=True)
    print(
        f"Done in {time.monotonic() - started:.1f} s: {report.kept} cameras kept, "
        f"{report.dropped} unusable, {report.duplicates} duplicate ids, "
        f"{report.copies} copies of one camera under two sources, from "
        f"{report.read} features. Catalog: {dest}",
        flush=True,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else db_path()
    cat = CameraCatalog(path)
    try:
        s = cat.summary()
    finally:
        cat.close()
    if not s.get("available"):
        print(f"No camera catalog at {path}. Run: python -m sda.cameras import")
        return 1
    print(f"Catalog: {path}")
    print(f"Cameras: {s['count']} ({s['playable']} play in the browser), "
          f"{s['country_count']} countries, {s['source_count']} sources")
    print("Groups: " + ", ".join(f"{k} {v}" for k, v in s["groups"].items()))
    print("Feeds: " + ", ".join(f"{k} {v}" for k, v in s["feeds"].items()))
    print(f"Imported {s['imported_at']} from {s['origin']} {s['origin_ref']}".rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sda.cameras", description=__doc__.split(
        "\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="build the camera catalog")
    imp.add_argument("--from", dest="source", help="a local Argus cameras.geojson")
    imp.add_argument("--ref", default=ARGUS_COMMIT,
                     help="Argus commit or branch to fetch (default: the pinned commit;"
                          " 'master' for the newest export)")
    imp.add_argument("--db", help=f"catalog file (default: {db_path()})")
    imp.add_argument("--keep", action="store_true", help="keep the downloaded GeoJSON")
    imp.set_defaults(func=_cmd_import)
    st = sub.add_parser("status", help="show what the catalog holds")
    st.add_argument("--db", help=f"catalog file (default: {db_path()})")
    st.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
