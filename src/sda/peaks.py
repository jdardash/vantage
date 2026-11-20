"""Worldwide mountain catalog behind the globe's peak layer.

Every summit on Earth with at least 100 ft (30.48 m) of prominence, about 11.9
million of them, with elevation and prominence, a name, country and region where a
gazetteer has one, and topographic isolation where it can be matched. Held in a
local SQLite file with an R*Tree index so the globe can ask for the peaks inside
its view (most prominent first), a density grid for the whole Earth, the peaks
nearest a point, a name search, and one peak's record.

Where the peaks come from
-------------------------
* Summits, elevation and prominence: the 2023 global prominence run of Andrew
  Kirmse and Jonathan de Ferranti (https://www.andrewkirmse.com/prominence-update-2023,
  code at https://github.com/akirmse/mountains, MIT). File
  ``all-peaks-sorted-p100.txt`` inside ``all-peaks-p100-2023.zip``: one summit a
  line, ``lat,lon,elevation m,key saddle lat,key saddle lon,prominence m``, from
  the Copernicus GLO-30 DEM (the US outside Alaska from USGS 10 m data). The page
  states no licence for the results; they are used here with attribution, read at
  import and never redistributed. GLO-30 is (c) DLR e.V. 2010-2014 and (c) Airbus
  Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union
  and ESA. It is a surface model, so a forest canopy or a tall building can stand
  in for ground, and summit heights are those of a 30 m cell, not a survey.
* Names, country and region: GeoNames (https://www.geonames.org, CC BY 4.0),
  ``allCountries.zip`` and ``admin1CodesASCII.txt``. Summit features (feature
  class T, the codes in ``NAME_CODES``) name the Kirmse summit near them; see
  ``match_names``. A named summit with no Kirmse summit near it (a subsidiary top
  with under 100 ft of prominence, or a hill in flat country) is kept as its own
  row with prominence unknown. Ranges (MTS, PKS, HLLS) name no single summit and
  are left out.
* Isolation: Kirmse's isolation run (``alliso-sorted.txt`` inside
  ``AllPeaksIsolationSorted.zip``, 2017, older SRTM-era terrain, elevation in feet,
  isolation in km, no licence stated), matched to a summit within
  ``ISO_MATCH_KM`` and ``ISO_MATCH_DZ_M``.

Nothing is downloaded by the server. ``python -m sda.peaks import`` fetches the
files (about 1 GB, keyless) or reads local copies (``--from``) and writes
``peaks.sqlite`` under ``SDA_PEAK_DIR`` (default ``%LOCALAPPDATA%/sda/peaks`` on
Windows, ``~/.cache/sda/peaks`` elsewhere). The catalog is generated locally and
never committed.

Display order: ``rid`` is the rank, 1 the most prominent summit. Peaks whose
prominence is known come first by prominence, then the rest by elevation, so
every list here is "most prominent first" with an ``ORDER BY rid``.

Coordinates are geodetic degrees as the sources publish them. Nothing here
touches an inertial frame.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import warnings
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse

import numpy as np
import numpy.typing as npt

KIRMSE_URL = (
    "https://drive.usercontent.google.com/download"
    "?id=1HnMIZemDu3MgyCu7Fdy-VJOsiXTN5sYW&export=download&confirm=t"
)
ISOLATION_URL = (
    "https://drive.usercontent.google.com/download"
    "?id=0B3icWNhBosDXRm1pak56blp1RGc&export=download&confirm=t"
)
GEONAMES_URL = "https://download.geonames.org/export/dump/allCountries.zip"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
USER_AGENT = "sda-peaks-importer/1.0"

PEAK_DIR_ENV = "SDA_PEAK_DIR"
DB_NAME = "peaks.sqlite"
SCHEMA_VERSION = "1"
IMPORT_HINT = "Run: python -m sda.peaks import"

#: GeoNames summit codes in the order they claim a summit, with the radius (km)
#: inside which each looks for one. Peaks, volcanoes and mountains are placed
#: less precisely in GeoNames than the summit itself, so they search wider.
NAME_CODES: dict[str, float] = {
    "PK": 2.0, "VLC": 2.0, "MT": 2.0, "NTK": 1.0, "CONE": 1.0, "BUTE": 1.0, "HLL": 1.0,
}
#: A GeoNames height replaces the DEM height when the two agree this closely;
#: further apart, the survey value is more likely another summit's.
SURVEY_MAX_DZ_M = 150.0
#: Metres of height difference worth one kilometre of distance when choosing
#: which summit near a named point carries its name.
MATCH_M_PER_KM = 100.0
#: A surveyed feature never names a summit whose DEM height is further than this
#: from the survey. Without the gate a named shoulder whose own top is not in
#: the list (under 100 m of prominence) took the nearest free hill instead:
#: Pic Tyndall (4,241 m) named a 3,287 m summit 1.5 km below the Matterhorn.
NAME_MAX_DZ_M = 300.0
ISO_MATCH_KM = 0.25
ISO_MATCH_DZ_M = 60.0

#: elev_src values.
ELEV_DEM = 0  # GLO-30 summit cell, from the Kirmse run
ELEV_SURVEY = 1  # GeoNames elevation field
ELEV_GN_DEM = 2  # GeoNames SRTM3 / GTOPO30 cell average (reads low on a summit)
ELEV_SOURCES = {ELEV_DEM: "glo30-dem", ELEV_SURVEY: "geonames", ELEV_GN_DEM: "geonames-dem"}

FT_PER_M = 1.0 / 0.3048
KM_PER_DEG = 111.195  # mean Earth radius 6371 km
EARTH_MEAN_RADIUS_KM = 6371.0
MAX_NAME = 120
MAX_ALT_NAMES = 6
MAX_KEYS_PER_NAME = 4

#: Grid sizes, in degrees, of the density table written at import.
DENSITY_CELLS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
#: At most this many density cells are returned for one view; a finer cell is
#: swapped for the next coarser one past it.
MAX_DENSITY_CELLS = 20000
#: Counts of peaks in a view stop here.
COUNT_CAP = 100_000

_FloatArray = npt.NDArray[np.float64]
_IntArray = npt.NDArray[np.int64]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def peak_dir() -> Path:
    """Directory holding the catalog: SDA_PEAK_DIR, else the user cache dir."""
    configured = os.environ.get(PEAK_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "peaks"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "peaks"


def db_path() -> Path:
    return peak_dir() / DB_NAME


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text(value: str, limit: int = MAX_NAME) -> str:
    """Printable characters only, trimmed and cut to *limit*."""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def fold(text: str) -> str:
    """Lower case, accents removed, punctuation to single spaces: the search form."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return " ".join(_NON_WORD.sub(" ", bare).split())


def search_keys(*names: str) -> list[str]:
    """Search keys for a peak: each name folded, and its tail from every later word.

    "Mount Whitney" gives "mount whitney" and "whitney", so typing either finds it.
    """
    keys: list[str] = []
    for raw in names:
        folded = fold(raw)
        if len(folded) < 2:
            continue
        words = folded.split(" ")
        for i in range(min(len(words), MAX_KEYS_PER_NAME)):
            key = " ".join(words[i:])
            if len(key) >= 2 and key not in keys:
                keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Reading the source files
# ---------------------------------------------------------------------------


@contextmanager
def open_source(path: Path) -> Iterator[IO[bytes]]:
    """Binary stream of a text file, or of the largest member of a zip."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if not members:
                raise ValueError(f"{path.name} is an empty zip")
            info = max(members, key=lambda i: i.file_size)
            with zf.open(info) as fh:
                yield fh
    else:
        with path.open("rb") as fh:
            yield fh


def iter_blocks(fh: IO[bytes], size: int = 1 << 25) -> Iterator[bytes]:
    """Whole lines, about *size* bytes at a time."""
    rest = b""
    while True:
        chunk = fh.read(size)
        if not chunk:
            if rest.strip():
                yield rest + b"\n"
            return
        data = rest + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            rest = data
            continue
        rest = data[cut + 1:]
        yield data[: cut + 1]


def parse_numeric(block: bytes, ncols: int, usecols: tuple[int, ...]) -> _FloatArray:
    """Rows of a comma-separated numeric block; a line that does not parse is skipped."""
    if not block.strip():
        return np.empty((0, len(usecols)), dtype=np.float64)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            arr = np.loadtxt(io.BytesIO(block), delimiter=",", dtype=np.float64,
                             usecols=usecols, ndmin=2, comments=None)
        return arr
    except ValueError:
        rows: list[list[float]] = []
        for line in block.splitlines():
            parts = line.split(b",")
            if len(parts) != ncols:
                continue
            try:
                rows.append([float(parts[i]) for i in usecols])
            except ValueError:
                continue
        return np.array(rows, dtype=np.float64).reshape(-1, len(usecols))


@dataclass
class SummitArrays:
    """Summits as parallel arrays (the Kirmse run, then GeoNames-only rows)."""

    lat: _FloatArray
    lon: _FloatArray
    elev: _FloatArray
    prom: _FloatArray


def read_prominence(
    path: Path, min_prom_m: float = 0.0
) -> tuple[SummitArrays, int, int]:
    """Kirmse summits from *path*; returns (summits, lines read, lines dropped)."""
    parts: list[_FloatArray] = []
    read = 0
    with open_source(path) as fh:
        for block in iter_blocks(fh):
            read += block.count(b"\n")
            arr = parse_numeric(block, 6, (0, 1, 2, 5))
            if arr.size:
                parts.append(arr)
    data = np.concatenate(parts) if parts else np.empty((0, 4), dtype=np.float64)
    lat, lon, elev, prom = data[:, 0], data[:, 1], data[:, 2], data[:, 3]
    ok = (
        np.isfinite(lat) & np.isfinite(lon) & np.isfinite(elev) & np.isfinite(prom)
        & (np.abs(lat) <= 90.0) & (np.abs(lon) <= 180.0) & (prom >= max(0.0, min_prom_m))
    )
    kept = SummitArrays(lat[ok].copy(), lon[ok].copy(), elev[ok].copy(), prom[ok].copy())
    return kept, read, read - int(ok.sum())


@dataclass(frozen=True)
class NameRow:
    """One GeoNames summit feature."""

    gid: int
    name: str
    ascii: str
    alts: tuple[str, ...]
    lat: float
    lon: float
    code: str
    country: str
    admin1: str
    survey_m: float | None
    dem_m: float | None

    @property
    def height(self) -> float:
        if self.survey_m is not None:
            return self.survey_m
        return self.dem_m if self.dem_m is not None else -1e9


def _int_or_none(raw: bytes, low: float) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value > low else None


def _alt_names(raw: str, name: str, ascii_name: str) -> tuple[str, ...]:
    out: list[str] = []
    seen = {fold(name), fold(ascii_name)}
    for alt in raw.split(","):
        alt = clean_text(alt, 60)
        if len(alt) < 2 or "/" in alt or "http" in alt or alt.replace(" ", "").isdigit():
            continue
        key = fold(alt)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(alt)
        if len(out) >= MAX_ALT_NAMES:
            break
    return tuple(out)


def parse_geonames_line(line: bytes, codes: dict[str, float]) -> NameRow | None:
    """One allCountries.txt line, or None unless it is a summit feature."""
    if b"\tT\t" not in line:
        return None
    parts = line.rstrip(b"\r\n").split(b"\t")
    if len(parts) < 17 or parts[6] != b"T":
        return None
    code = parts[7].decode("ascii", "replace")
    if code not in codes:
        return None
    try:
        gid = int(parts[0])
        lat = float(parts[4])
        lon = float(parts[5])
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > 90.0 or abs(lon) > 180.0 or (lat == 0.0 and lon == 0.0):
        return None
    name = clean_text(parts[1].decode("utf-8", "replace"))
    ascii_name = clean_text(parts[2].decode("utf-8", "replace"))
    if not name and not ascii_name:
        return None
    return NameRow(
        gid=gid,
        name=name or ascii_name,
        ascii=ascii_name,
        alts=_alt_names(parts[3].decode("utf-8", "replace"), name, ascii_name),
        lat=lat,
        lon=lon,
        code=code,
        country=clean_text(parts[8].decode("ascii", "replace"), 3).upper(),
        admin1=clean_text(parts[10].decode("utf-8", "replace"), 20),
        survey_m=_int_or_none(parts[15], 0.0),
        dem_m=_int_or_none(parts[16], -1000.0),
    )


def read_geonames(path: Path, codes: dict[str, float] | None = None) -> tuple[list[NameRow], int]:
    """GeoNames summit features in *path*; returns (rows, lines read)."""
    wanted = codes if codes is not None else NAME_CODES
    rows: list[NameRow] = []
    read = 0
    with open_source(path) as fh:
        for block in iter_blocks(fh):
            lines = block.split(b"\n")
            read += len(lines) - 1
            for line in lines:
                row = parse_geonames_line(line, wanted)
                if row is not None:
                    rows.append(row)
    return rows, read


def read_admin1(path: Path) -> dict[str, str]:
    """GeoNames admin1CodesASCII.txt as {"US.CA": "California"}."""
    out: dict[str, str] = {}
    with open_source(path) as fh:
        for raw in fh.read().decode("utf-8", "replace").splitlines():
            parts = raw.split("\t")
            if len(parts) >= 2 and parts[0]:
                out[parts[0].strip()] = clean_text(parts[1], 80)
    return out


# ---------------------------------------------------------------------------
# Spatial matching
# ---------------------------------------------------------------------------


def flat_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Distance in km on a local flat projection (good to a few metres under 5 km)."""
    dy = (np.asarray(lat2) - lat1) * KM_PER_DEG
    dx = (np.asarray(lon2) - lon1) * KM_PER_DEG * np.cos(np.radians(lat1))
    return np.hypot(dx, dy)


class Grid:
    """Points bucketed into lat/lon cells, for neighbour lookups in numpy."""

    def __init__(self, lat: _FloatArray, lon: _FloatArray, cell_deg: float) -> None:
        self.cell = cell_deg
        self.width = int(math.ceil(360.0 / cell_deg)) + 2
        keys = self._gy(lat) * self.width + self._gx(lon)
        self.order: _IntArray = np.argsort(keys, kind="stable").astype(np.int64)
        self.keys: _IntArray = keys[self.order]

    def _gy(self, lat: Any) -> _IntArray:
        return np.floor((np.asarray(lat) + 90.0) / self.cell).astype(np.int64)

    def _gx(self, lon: Any) -> _IntArray:
        return np.floor((np.asarray(lon) + 180.0) / self.cell).astype(np.int64)

    def ranges(
        self, lat: _FloatArray, lon: _FloatArray, radius_km: float
    ) -> list[tuple[_IntArray, _IntArray]]:
        """Per row of cells, [lo, hi) slices of ``order`` covering *radius_km* of each query."""
        dlat = radius_km / KM_PER_DEG
        rows = int(math.ceil(dlat / self.cell))
        coslat = np.cos(np.radians(np.clip(lat, -89.0, 89.0)))
        dlon = np.minimum(180.0, dlat / np.maximum(coslat, 1e-3))
        gx0 = self._gx(np.maximum(-180.0, lon - dlon))
        gx1 = self._gx(np.minimum(180.0, lon + dlon))
        gy = self._gy(lat)
        out = []
        for dy in range(-rows, rows + 1):
            base = (gy + dy) * self.width
            lo = np.searchsorted(self.keys, base + gx0, side="left").astype(np.int64)
            hi = np.searchsorted(self.keys, base + gx1, side="right").astype(np.int64)
            out.append((lo, hi))
        return out


@dataclass
class NameMatch:
    """Result of naming the summits."""

    #: summit index -> the GeoNames feature that names it
    named: dict[int, NameRow] = field(default_factory=dict)
    #: features kept as their own row (no unclaimed summit near them)
    extra: list[NameRow] = field(default_factory=list)
    duplicates: int = 0
    #: extra rows whose survey fit none of the free summits in range (NAME_MAX_DZ_M)
    height_misses: int = 0


def match_names(
    summits: SummitArrays, rows: list[NameRow], codes: dict[str, float] | None = None
) -> NameMatch:
    """Give each GeoNames summit feature to one Kirmse summit near it.

    Features are taken highest first, whatever their code (peaks and volcanoes
    before mountains, then the smaller codes, only to break a tie), so a big
    mountain claims its summit before a named shoulder or subpeak beside it:
    with the code first, Chogri Peak (PK) took K2's summit from K2 (MT), and
    Monte Bianco di Courmayeur took Mont Blanc's. Each feature looks within its
    code's radius (NAME_CODES) at the summits nobody has claimed:

    * with a surveyed height, it takes the one that best fits it, scored as
      metres of height difference plus MATCH_M_PER_KM per km of distance,
      among those within NAME_MAX_DZ_M of the survey;
    * without one, it takes the highest.

    A feature is a duplicate (GeoNames often lists one mountain twice, as a
    peak and as a mountain, say) when a summit in range already carries the
    same name. A feature with no free summit in range is kept as its own row.
    """
    wanted = codes if codes is not None else NAME_CODES
    priority = {code: i for i, code in enumerate(wanted)}
    ordered = sorted(
        (r for r in rows if r.code in wanted),
        key=lambda r: (-r.height, priority[r.code], r.gid),
    )
    result = NameMatch()
    if not ordered:
        return result
    radius = max(wanted.values())
    qlat = np.array([r.lat for r in ordered], dtype=np.float64)
    qlon = np.array([r.lon for r in ordered], dtype=np.float64)
    claimed: dict[int, str] = {}
    extra_keys: set[tuple[str, float, float]] = set()
    if summits.lat.size:
        grid = Grid(summits.lat, summits.lon, 0.02)
        spans = grid.ranges(qlat, qlon, radius)
    else:
        grid = None
        spans = []
    for i, row in enumerate(ordered):
        cand: _IntArray = np.empty(0, dtype=np.int64)
        if grid is not None:
            pieces = [grid.order[lo[i]:hi[i]] for lo, hi in spans if hi[i] > lo[i]]
            if pieces:
                cand = np.concatenate(pieces)
        key = fold(row.name)
        pick = -1
        missed = False
        if cand.size:
            dist = flat_km(row.lat, row.lon, summits.lat[cand], summits.lon[cand])
            inside = dist <= wanted[row.code]
            cand = cand[inside]
            dist = dist[inside]
            near = cand.tolist()
            if any(claimed.get(c) == key for c in near):
                result.duplicates += 1
                continue
            free = np.array([c not in claimed for c in near], dtype=bool)
            if free.any():
                fc = cand[free]
                if row.survey_m is not None:
                    dz = np.abs(summits.elev[fc] - row.survey_m)
                    fit = dz <= NAME_MAX_DZ_M
                    if fit.any():
                        score = dz[fit] + MATCH_M_PER_KM * dist[free][fit]
                        pick = int(fc[fit][int(np.argmin(score))])
                    else:
                        missed = True
                else:
                    pick = int(fc[int(np.argmax(summits.elev[fc]))])
        if pick >= 0:
            claimed[pick] = key
            result.named[pick] = row
            continue
        spot = (key, round(row.lat, 2), round(row.lon, 2))
        if spot in extra_keys:
            result.duplicates += 1
            continue
        extra_keys.add(spot)
        result.extra.append(row)
        result.height_misses += missed
    return result


def match_isolation(
    summits: SummitArrays,
    grid: Grid,
    lat: _FloatArray,
    lon: _FloatArray,
    elev_m: _FloatArray,
    iso_km: _FloatArray,
    out: _FloatArray,
    slots: int = 4,
) -> int:
    """Write each isolation row's value onto the summit it describes; returns matches.

    A row matches the nearest summit within ISO_MATCH_KM whose height is within
    ISO_MATCH_DZ_M of the row's. A summit matched twice keeps the larger value.
    """
    n = lat.size
    if n == 0:
        return 0
    best_d = np.full(n, np.inf)
    best_i = np.full(n, -1, dtype=np.int64)
    for lo, hi in grid.ranges(lat, lon, ISO_MATCH_KM):
        for k in range(slots):
            idx = lo + k
            has = idx < hi
            if not has.any():
                break
            q = np.nonzero(has)[0]
            cand = grid.order[idx[q]]
            d = flat_km(lat[q], lon[q], summits.lat[cand], summits.lon[cand])
            dz = np.abs(summits.elev[cand] - elev_m[q])
            good = (d <= ISO_MATCH_KM) & (dz <= ISO_MATCH_DZ_M) & (d < best_d[q])
            best_d[q[good]] = d[good]
            best_i[q[good]] = cand[good]
    hit = best_i >= 0
    np.fmax.at(out, best_i[hit], iso_km[hit])
    return int(hit.sum())


def read_isolation(
    path: Path, summits: SummitArrays, log: Callable[[str], None] | None = None
) -> tuple[_FloatArray, int, int]:
    """Isolation (km) per summit from Kirmse's alliso file; returns (values, read, matched)."""
    out = np.full(summits.lat.size, np.nan)
    if not summits.lat.size:
        return out, 0, 0
    grid = Grid(summits.lat, summits.lon, 0.005)
    read = 0
    matched = 0
    with open_source(path) as fh:
        for block in iter_blocks(fh):
            arr = parse_numeric(block, 6, (0, 1, 2, 5))
            if not arr.size:
                continue
            ok = np.isfinite(arr).all(axis=1) & (np.abs(arr[:, 0]) <= 90) & (arr[:, 3] > 0)
            arr = arr[ok]
            read += int(arr.shape[0])
            matched += match_isolation(
                summits, grid, arr[:, 0], arr[:, 1], arr[:, 2] * 0.3048, arr[:, 3], out
            )
            if log is not None and read and read % 5_000_000 < arr.shape[0]:
                log(f"  isolation: {read:,} rows read, {matched:,} matched")
    return out, read, matched


# ---------------------------------------------------------------------------
# Building the database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE peaks (
    rid INTEGER PRIMARY KEY,  -- rank: 1 is the most prominent summit
    name TEXT NOT NULL,
    lat_e5 INTEGER NOT NULL,  -- degrees * 1e5
    lon_e5 INTEGER NOT NULL,
    elev_dm INTEGER,          -- decimetres; NULL when unknown
    elev_src INTEGER NOT NULL,
    dem_dm INTEGER,           -- GLO-30 height when elev_dm is a surveyed one
    prom_dm INTEGER,          -- NULL when unknown
    iso_m INTEGER,            -- NULL when unknown
    country TEXT NOT NULL,
    region TEXT NOT NULL,
    gid INTEGER               -- GeoNames id of the name; NULL when unnamed
);
CREATE VIRTUAL TABLE peak_index USING rtree(rid, min_lon, max_lon, min_lat, max_lat);
CREATE TABLE names (key TEXT NOT NULL, rid INTEGER NOT NULL, PRIMARY KEY (key, rid))
    WITHOUT ROWID;
CREATE TABLE density (
    cell REAL NOT NULL,
    gy INTEGER NOT NULL,
    gx INTEGER NOT NULL,
    n INTEGER NOT NULL,
    slat REAL NOT NULL,
    slon REAL NOT NULL,
    top INTEGER NOT NULL,     -- rid of the highest summit in the cell
    PRIMARY KEY (cell, gy, gx)
) WITHOUT ROWID;
"""


@dataclass
class BuildReport:
    summits_read: int = 0
    summits_dropped: int = 0
    names_read: int = 0
    name_features: int = 0
    named: int = 0
    names_added: int = 0
    name_duplicates: int = 0
    name_height_misses: int = 0
    isolation_read: int = 0
    isolation_matched: int = 0
    kept: int = 0
    with_prominence: int = 0
    countries: int = 0
    seconds: dict[str, float] = field(default_factory=dict)


def _morton(lat: _FloatArray, lon: _FloatArray) -> npt.NDArray[np.uint64]:
    """Z-order key, so the R*Tree is filled in spatially coherent order."""
    def spread(v: npt.NDArray[np.uint64]) -> npt.NDArray[np.uint64]:
        v = v & np.uint64(0xFFFF)
        v = (v | (v << np.uint64(8))) & np.uint64(0x00FF00FF)
        v = (v | (v << np.uint64(4))) & np.uint64(0x0F0F0F0F)
        v = (v | (v << np.uint64(2))) & np.uint64(0x33333333)
        v = (v | (v << np.uint64(1))) & np.uint64(0x55555555)
        return v

    qx = np.clip((lon + 180.0) / 360.0 * 65535.0, 0, 65535).astype(np.uint64)
    qy = np.clip((lat + 90.0) / 180.0 * 65535.0, 0, 65535).astype(np.uint64)
    return spread(qx) | (spread(qy) << np.uint64(1))


def _to_int_or_none(values: _FloatArray, scale: float) -> list[int | None]:
    scaled = np.round(values * scale)
    return [None if math.isnan(v) else int(v) for v in scaled.tolist()]


def build_catalog(
    prominence: Path,
    dest: Path,
    *,
    names: Path | None = None,
    admin1: Path | None = None,
    isolation: Path | None = None,
    min_prom_m: float = 0.0,
    codes: dict[str, float] | None = None,
    meta: dict[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> BuildReport:
    """Write a fresh catalog to *dest* from the Kirmse run and, optionally, GeoNames.

    The file is written beside *dest* and renamed over it at the end. On Windows
    a running server holds *dest* open and the rename fails; the new file is
    then left as ``<dest>.new`` and the server swaps it in on its next query.
    """
    say = log or (lambda _msg: None)
    report = BuildReport()
    clock = time.monotonic()

    def lap(step: str) -> None:
        nonlocal clock
        now = time.monotonic()
        report.seconds[step] = round(now - clock, 1)
        clock = now

    summits, report.summits_read, report.summits_dropped = read_prominence(prominence, min_prom_m)
    n_k = int(summits.lat.size)
    lap("summits")
    say(f"  {n_k:,} summits kept of {report.summits_read:,} lines")

    iso = np.full(n_k, np.nan)
    if isolation is not None:
        iso, report.isolation_read, report.isolation_matched = read_isolation(
            isolation, summits, say)
        lap("isolation")
        say(f"  isolation: {report.isolation_matched:,} of {report.isolation_read:,} rows "
            "matched a summit")

    match = NameMatch()
    regions: dict[str, str] = {}
    if names is not None:
        rows, report.names_read = read_geonames(names, codes)
        report.name_features = len(rows)
        if admin1 is not None:
            regions = read_admin1(admin1)
        match = match_names(summits, rows, codes)
        del rows
        report.named = len(match.named)
        report.names_added = len(match.extra)
        report.name_duplicates = match.duplicates
        report.name_height_misses = match.height_misses
        lap("names")
        say(f"  names: {report.name_features:,} GeoNames summit features, "
            f"{report.named:,} name a summit, {report.names_added:,} kept as their own "
            f"row ({report.name_height_misses:,} of them for want of a summit within "
            f"{NAME_MAX_DZ_M:.0f} m of their survey), {report.name_duplicates:,} duplicates")

    # Every row as arrays: the Kirmse summits, then the GeoNames-only rows.
    extra = match.extra
    n = n_k + len(extra)
    lat = np.concatenate([summits.lat, np.array([r.lat for r in extra], dtype=np.float64)])
    lon = np.concatenate([summits.lon, np.array([r.lon for r in extra], dtype=np.float64)])
    prom = np.concatenate([summits.prom, np.full(len(extra), np.nan)])
    iso_all = np.concatenate([iso, np.full(len(extra), np.nan)])
    elev = np.concatenate([summits.elev, np.full(len(extra), np.nan)])
    elev_src = np.zeros(n, dtype=np.int64)
    dem = np.full(n, np.nan)
    info: dict[int, NameRow] = dict(match.named)
    for idx, row in match.named.items():
        if row.survey_m is not None and abs(row.survey_m - elev[idx]) <= SURVEY_MAX_DZ_M:
            dem[idx] = elev[idx]
            elev[idx] = row.survey_m
            elev_src[idx] = ELEV_SURVEY
    for j, row in enumerate(extra):
        idx = n_k + j
        info[idx] = row
        if row.survey_m is not None:
            elev[idx] = row.survey_m
            elev_src[idx] = ELEV_SURVEY
        elif row.dem_m is not None:
            elev[idx] = row.dem_m
            elev_src[idx] = ELEV_GN_DEM
    del summits, iso, match

    # Rank: known prominence first, highest first; then the rest by elevation.
    # Every array is then put in rank order, so the writes below read them front
    # to back: random gathers over a dozen 100 MB arrays page heavily when memory
    # is short.
    prom_key = np.where(np.isnan(prom), -1e12, prom)
    elev_key = np.where(np.isnan(elev), -1e12, elev)
    order = np.lexsort((-elev_key, -prom_key)).astype(np.int64)
    del prom_key
    # one array at a time, so only one extra copy is alive at any moment
    lat = lat[order]
    lon = lon[order]
    elev = elev[order]
    prom = prom[order]
    iso_all = iso_all[order]
    dem = dem[order]
    elev_src = elev_src[order]
    elev_key = elev_key[order]
    position = np.empty(n, dtype=np.int64)
    position[order] = np.arange(n, dtype=np.int64)
    named_at = sorted(((int(position[idx]), row) for idx, row in info.items()),
                      key=lambda item: item[0])
    del order, position, info
    report.kept = n
    report.with_prominence = int(np.count_nonzero(~np.isnan(prom)))
    lap("rank")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-262144")
        conn.executescript(_SCHEMA)
        countries: set[str] = set()
        batch = 200_000
        nxt = 0  # next entry of named_at
        for start in range(0, n, batch):
            end = min(n, start + batch)
            lats = np.round(lat[start:end] * 1e5).astype(np.int64).tolist()
            lons = np.round(lon[start:end] * 1e5).astype(np.int64).tolist()
            elevs = _to_int_or_none(elev[start:end], 10.0)
            dems = _to_int_or_none(dem[start:end], 10.0)
            proms = _to_int_or_none(prom[start:end], 10.0)
            isos = _to_int_or_none(iso_all[start:end], 1000.0)
            srcs = elev_src[start:end].tolist()
            rows_out = []
            for k in range(end - start):
                pos = start + k
                if nxt < len(named_at) and named_at[nxt][0] == pos:
                    feature = named_at[nxt][1]
                    nxt += 1
                    name = feature.name
                    country = feature.country
                    region = (regions.get(f"{feature.country}.{feature.admin1}", "")
                              if feature.admin1 else "")
                    gid: int | None = feature.gid
                    if country:
                        countries.add(country)
                else:
                    name = country = region = ""
                    gid = None
                rows_out.append((pos + 1, name, lats[k], lons[k], elevs[k], srcs[k], dems[k],
                                 proms[k], isos[k], country, region, gid))
            conn.executemany(
                "INSERT INTO peaks(rid, name, lat_e5, lon_e5, elev_dm, elev_src, dem_dm,"
                " prom_dm, iso_m, country, region, gid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows_out,
            )
        del rows_out
        report.countries = len(countries)
        lap("write peaks")
        say(f"  wrote {n:,} peaks")

        zorder = np.argsort(_morton(lat, lon), kind="stable")
        for start in range(0, n, batch):
            sel = zorder[start:start + batch]
            la = lat[sel].tolist()
            lo = lon[sel].tolist()
            conn.executemany(
                "INSERT INTO peak_index(rid, min_lon, max_lon, min_lat, max_lat)"
                " VALUES (?,?,?,?,?)",
                zip((sel + 1).tolist(), lo, lo, la, la, strict=True),
            )
        del zorder
        lap("index")
        say("  spatial index written")

        # In slices, so a few million name keys never sit in memory at once.
        name_rows: list[tuple[str, int]] = []
        for pos, row in named_at:
            for key in search_keys(row.name, row.ascii, *row.alts):
                name_rows.append((key, pos + 1))
            if len(name_rows) >= batch:
                name_rows.sort()
                conn.executemany("INSERT OR IGNORE INTO names(key, rid) VALUES (?, ?)",
                                 name_rows)
                name_rows = []
        name_rows.sort()
        conn.executemany("INSERT OR IGNORE INTO names(key, rid) VALUES (?, ?)", name_rows)
        del name_rows
        lap("names index")

        _fill_density(conn, lat, lon, elev_key, np.arange(1, n + 1, dtype=np.int64))
        lap("density")

        all_meta = {
            "schema": SCHEMA_VERSION,
            "imported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": str(n),
            "named": str(len(named_at)),
            "with_prominence": str(report.with_prominence),
            "with_isolation": str(int(np.count_nonzero(~np.isnan(iso_all)))),
            "geonames_only": str(len(extra)),
            "country_count": str(report.countries),
            "min_prom_m": f"{max(0.0, min_prom_m):g}",
        }
        all_meta.update(meta or {})
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", list(all_meta.items()))
        conn.commit()
    finally:
        conn.close()

    try:
        os.replace(tmp, dest)
    except PermissionError:
        os.replace(tmp, dest.with_name(dest.name + ".new"))
    return report


def _fill_density(
    conn: sqlite3.Connection,
    lat: _FloatArray,
    lon: _FloatArray,
    elev_key: _FloatArray,
    rank: _IntArray,
) -> None:
    """Counts, mean position and highest summit per grid cell, for each DENSITY_CELLS size."""
    if lat.size == 0:
        return
    for cell in DENSITY_CELLS:
        width = int(math.ceil(360.0 / cell)) + 2
        gy = np.floor((lat + 90.0) / cell).astype(np.int64)
        gx = np.floor((lon + 180.0) / cell).astype(np.int64)
        key = gy * width + gx
        o = np.lexsort((rank, -elev_key, key))
        ks = key[o]
        starts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1]])
        counts = np.diff(np.r_[starts, ks.size])
        slat = np.add.reduceat(lat[o], starts)
        slon = np.add.reduceat(lon[o], starts)
        first = o[starts]
        conn.executemany(
            "INSERT INTO density(cell, gy, gx, n, slat, slon, top) VALUES (?,?,?,?,?,?,?)",
            zip([cell] * starts.size, gy[first].tolist(), gx[first].tolist(), counts.tolist(),
                slat.tolist(), slon.tolist(), rank[first].tolist(), strict=True),
        )


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

_LIST_COLUMNS = (
    "p.rid, p.name, p.lat_e5, p.lon_e5, p.elev_dm, p.prom_dm, p.iso_m, p.country, p.region"
)
_DETAIL_COLUMNS = _LIST_COLUMNS + ", p.elev_src, p.dem_dm, p.gid"


def _dm(value: int | None) -> float | None:
    return None if value is None else round(value / 10.0, 1)


def _list_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "name": row[1],
        "lat": row[2] / 1e5,
        "lon": row[3] / 1e5,
        "elev_m": _dm(row[4]),
        "prom_m": _dm(row[5]),
        "iso_km": None if row[6] is None else round(row[6] / 1000.0, 2),
        "country": row[7],
        "region": row[8],
    }


def _detail_row(row: tuple[Any, ...]) -> dict[str, Any]:
    out = _list_row(row)
    prom_known = row[5] is not None
    named = row[11] is not None
    if prom_known and named:
        source = "kirmse+geonames"
    elif prom_known:
        source = "kirmse"
    else:
        source = "geonames"
    out.update({
        "elev_source": ELEV_SOURCES.get(int(row[9]), "unknown"),
        "dem_m": _dm(row[10]),
        "geonames_id": None if row[11] is None else int(row[11]),
        "source": source,
    })
    return out


def _lon_spans(west: float, east: float) -> list[tuple[float, float]]:
    """Longitude intervals for a box, split in two when it crosses the antimeridian."""
    west = max(-180.0, min(180.0, west))
    east = max(-180.0, min(180.0, east))
    if west <= east:
        return [(west, east)]
    return [(west, 180.0), (-180.0, east)]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def cell_for_zoom(zoom: float) -> float:
    """Density cell (degrees) for a web-map zoom level: 16 deg at zoom 0, halving per level."""
    want = 16.0 / (2.0 ** max(0.0, zoom))
    return next((c for c in DENSITY_CELLS if c >= want - 1e-9), DENSITY_CELLS[-1])


def snap_cell(cell_deg: float) -> float:
    return next((c for c in DENSITY_CELLS if c >= cell_deg - 1e-9), DENSITY_CELLS[-1])


class PeakCatalog:
    """Read-only access to one catalog file, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._meta: dict[str, str] = {}
        self._cutoffs: dict[int, int] = {}
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
        self._meta = {}
        self._cutoffs.clear()
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
                self._meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                conn.execute("SELECT 1 FROM peaks LIMIT 1")
            except sqlite3.Error:
                return None
            self._conn = conn
        return self._conn

    @property
    def available(self) -> bool:
        with self._lock:
            return self._connection() is not None

    def _meta_int(self, key: str) -> int:
        try:
            return int(self._meta.get(key, "0"))
        except ValueError:
            return 0

    # -- queries ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return {"available": False, "count": 0, "hint": IMPORT_HINT}
            meta = self._meta
            return {
                "available": True,
                "count": self._meta_int("count"),
                "named": self._meta_int("named"),
                "with_prominence": self._meta_int("with_prominence"),
                "with_isolation": self._meta_int("with_isolation"),
                "geonames_only": self._meta_int("geonames_only"),
                "country_count": self._meta_int("country_count"),
                "min_prom_m": float(meta.get("min_prom_m", "0") or 0),
                "imported_at": meta.get("imported_at", ""),
                "sources": [s for s in meta.get("sources", "").split("|") if s],
                "attribution": ATTRIBUTION,
                "hint": "",
            }

    def _cutoff(self, conn: sqlite3.Connection, min_prom_m: float) -> int | None:
        """Largest rid whose prominence is at least *min_prom_m* (rows are ranked)."""
        if min_prom_m <= 0:
            return None
        want = int(math.ceil(min_prom_m * 10.0 - 1e-6))
        hit = self._cutoffs.get(want)
        if hit is not None:
            return hit
        lo, hi = 0, self._meta_int("with_prominence")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            row = conn.execute("SELECT prom_dm FROM peaks WHERE rid = ?", (mid,)).fetchone()
            if row is not None and row[0] is not None and row[0] >= want:
                lo = mid
            else:
                hi = mid - 1
        if len(self._cutoffs) > 64:
            self._cutoffs.clear()
        self._cutoffs[want] = lo
        return lo

    def _estimate(
        self, conn: sqlite3.Connection, spans: list[tuple[float, float]], south: float,
        north: float,
    ) -> tuple[int, int]:
        """Bounds on the peaks in the box from the 1 degree density cells.

        Returns (every cell the box touches, so at least the true count; only
        the cells wholly inside it, so at most the true count).
        """
        gy0 = int((south + 90.0) // 1.0)
        gy1 = int((north + 90.0) // 1.0)
        iy0 = int(math.ceil(south + 90.0))
        iy1 = int(math.floor(north + 90.0)) - 1
        upper = lower = 0
        sql = ("SELECT coalesce(sum(n), 0) FROM density WHERE cell = 1.0"
               " AND gy BETWEEN ? AND ? AND gx BETWEEN ? AND ?")
        for w, e in spans:
            gx0 = int((w + 180.0) // 1.0)
            gx1 = int((e + 180.0) // 1.0)
            upper += conn.execute(sql, (gy0, gy1, gx0, gx1)).fetchone()[0]
            ix0 = int(math.ceil(w + 180.0))
            ix1 = int(math.floor(e + 180.0)) - 1
            if iy1 >= iy0 and ix1 >= ix0:
                lower += conn.execute(sql, (iy0, iy1, ix0, ix1)).fetchone()[0]
        return int(upper), int(lower)

    def in_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        limit: int = 300,
        min_prom_m: float = 0.0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Peaks inside the box, most prominent first.

        Returns (peaks, total in the box under the same filter, whether that
        total stopped at COUNT_CAP).
        """
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        spans = _lon_spans(west, east)
        limit = max(1, limit)
        rows: list[tuple[Any, ...]] = []
        with self._lock:
            conn = self._connection()
            if conn is None:
                return [], 0, False
            cutoff = self._cutoff(conn, min_prom_m)
            if cutoff == 0:
                return [], 0, False
            ranked = cutoff if cutoff is not None else self._meta_int("count")
            est, inside = self._estimate(conn, spans, south, north)
            s5, n5 = int(math.floor(south * 1e5)), int(math.ceil(north * 1e5))
            cond = " OR ".join("(p.lon_e5 BETWEEN ? AND ?)" for _ in spans)
            box_args: list[Any] = [s5, n5]
            for w, e in spans:
                box_args += [int(math.floor(w * 1e5)), int(math.ceil(e * 1e5))]
            if cutoff is not None:
                box_args.append(cutoff)
            box_sql = ("p.lat_e5 BETWEEN ? AND ? AND (" + cond + ")"
                       + (" AND p.rid <= ?" if cutoff is not None else ""))
            if est * est > limit * max(1, ranked):
                # Many peaks in view: walk the ranked table until the box has
                # filled the limit (about limit * ranked / est rows read).
                rows = conn.execute(
                    "SELECT " + _LIST_COLUMNS + " FROM peaks p WHERE " + box_sql
                    + " ORDER BY p.rid LIMIT ?",
                    [*box_args, limit],
                ).fetchall()
            else:
                rid_sql = " AND r.rid <= ?" if cutoff is not None else ""
                for w, e in spans:
                    args = [w, e, south, north] + ([cutoff] if cutoff is not None else [])
                    rows.extend(conn.execute(
                        "SELECT " + _LIST_COLUMNS
                        + " FROM peak_index r CROSS JOIN peaks p ON p.rid = r.rid"
                        " WHERE r.max_lon >= ? AND r.min_lon <= ? AND r.max_lat >= ?"
                        " AND r.min_lat <= ?" + rid_sql + " ORDER BY p.rid LIMIT ?",
                        [*args, limit],
                    ).fetchall())
                rows.sort(key=lambda r: r[0])
                rows = rows[:limit]
            # The total: whichever of three ways reads the fewest rows.
            total = 0
            if cutoff is None and inside >= COUNT_CAP:
                # the cells wholly inside the box already hold the cap
                total = COUNT_CAP
            elif cutoff is not None and cutoff < est:
                # few ranked rows against a crowded box (the Himalaya at 1000 m):
                # read the ranked rows rather than every index entry in the box
                total = conn.execute(
                    "SELECT count(*) FROM (SELECT 1 FROM peaks p WHERE " + box_sql
                    + " LIMIT ?)",
                    [*box_args, COUNT_CAP],
                ).fetchone()[0]
            else:
                rid_sql = " AND rid <= ?" if cutoff is not None else ""
                for w, e in spans:
                    args = [w, e, south, north] + ([cutoff] if cutoff is not None else [])
                    total += conn.execute(
                        "SELECT count(*) FROM (SELECT 1 FROM peak_index WHERE max_lon >= ?"
                        " AND min_lon <= ? AND max_lat >= ? AND min_lat <= ?" + rid_sql
                        + " LIMIT ?)",
                        [*args, COUNT_CAP],
                    ).fetchone()[0]
        capped = total >= COUNT_CAP
        return [_list_row(r) for r in rows], min(int(total), COUNT_CAP), capped

    def density(
        self, west: float, south: float, east: float, north: float, cell_deg: float
    ) -> tuple[float, list[dict[str, Any]]]:
        """Peak counts per grid cell with the highest summit of each.

        Read from the table written at import, using the smallest stored cell
        that is at least *cell_deg* and keeps the view under MAX_DENSITY_CELLS.
        Returns (cell used, cells).
        """
        south, north = sorted((max(-90.0, min(90.0, south)), max(-90.0, min(90.0, north))))
        spans = _lon_spans(west, east)
        cell = snap_cell(cell_deg)
        for c in DENSITY_CELLS:
            if c < cell:
                continue
            ny = int((north + 90.0) // c) - int((south + 90.0) // c) + 1
            nx = sum(int((e + 180.0) // c) - int((w + 180.0) // c) + 1 for w, e in spans)
            cell = c
            if ny * nx <= MAX_DENSITY_CELLS:
                break
        key = (round(west, 2), round(south, 2), round(east, 2), round(north, 2), cell)
        gy0 = int((south + 90.0) // cell)
        gy1 = int((north + 90.0) // cell)
        with self._lock:
            conn = self._connection()
            if conn is None:
                return cell, []
            cached = self._density_cache.get(key)
            if cached is not None:
                return cell, cached
            cells: list[dict[str, Any]] = []
            for w, e in spans:
                gx0 = int((w + 180.0) // cell)
                gx1 = int((e + 180.0) // cell)
                for row in conn.execute(
                    "SELECT d.n, d.slat, d.slon, " + _LIST_COLUMNS
                    + " FROM density d JOIN peaks p ON p.rid = d.top"
                    " WHERE d.cell = ? AND d.gy BETWEEN ? AND ? AND d.gx BETWEEN ? AND ?",
                    (cell, gy0, gy1, gx0, gx1),
                ):
                    n = int(row[0])
                    cells.append({
                        "lat": round(row[1] / n, 4), "lon": round(row[2] / n, 4),
                        "count": n, "top": _list_row(row[3:]),
                    })
            if len(self._density_cache) > 256:
                self._density_cache.clear()
            self._density_cache[key] = cells
            return cell, cells

    def nearest(
        self, lat: float, lon: float, n: int = 10, radius_km: float = 50.0,
        min_prom_m: float = 0.0,
    ) -> list[dict[str, Any]]:
        """The *n* peaks closest to (lat, lon) within *radius_km*, nearest first."""
        radius_km = max(0.1, min(500.0, radius_km))
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
        found: list[tuple[float, tuple[Any, ...]]] = []
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            cutoff = self._cutoff(conn, min_prom_m)
            if cutoff == 0:
                return []
            rid_sql = " AND r.rid <= ?" if cutoff is not None else ""
            for w, e in _lon_spans(west, east):
                args = [w, e, lat - dlat, lat + dlat] + ([cutoff] if cutoff is not None else [])
                for row in conn.execute(
                    "SELECT " + _LIST_COLUMNS
                    + " FROM peak_index r CROSS JOIN peaks p ON p.rid = r.rid"
                    " WHERE r.max_lon >= ? AND r.min_lon <= ? AND r.max_lat >= ?"
                    " AND r.min_lat <= ?" + rid_sql,
                    args,
                ):
                    d = haversine_km(lat, lon, row[2] / 1e5, row[3] / 1e5)
                    if d <= radius_km:
                        found.append((d, row))
        found.sort(key=lambda t: (t[0], t[1][0]))
        out = []
        for d, row in found[: max(1, min(100, n))]:
            item = _list_row(row)
            item["distance_km"] = round(d, 2)
            out.append(item)
        return out

    def get(self, rid: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connection()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT " + _DETAIL_COLUMNS + " FROM peaks p WHERE p.rid = ?", (rid,)
            ).fetchone()
        return _detail_row(row) if row else None

    def search(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Named peaks whose name, or a later word of it, starts with *text*.

        Accents and case are ignored; most prominent first.
        """
        needle = fold(text)
        if len(needle) < 2:
            return []
        with self._lock:
            conn = self._connection()
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT " + _LIST_COLUMNS + " FROM peaks p WHERE p.rid IN"
                " (SELECT rid FROM names WHERE key >= ? AND key < ?)"
                " ORDER BY p.rid LIMIT ?",
                (needle, needle + "\U0010ffff", max(1, min(100, limit))),
            ).fetchall()
        return [_list_row(r) for r in rows]


ATTRIBUTION = (
    "Summits, elevation and prominence: Andrew Kirmse and Jonathan de Ferranti, global"
    " prominence 2023 (andrewkirmse.com), from Copernicus GLO-30 (c) DLR e.V. 2010-2014"
    " and (c) Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the"
    " European Union and ESA. Names: GeoNames (geonames.org, CC BY 4.0). Isolation:"
    " Kirmse isolation run 2017."
)

_catalog: PeakCatalog | None = None
_catalog_lock = threading.Lock()


def get_catalog() -> PeakCatalog:
    """The process-wide catalog for the current SDA_PEAK_DIR."""
    global _catalog
    path = db_path()
    with _catalog_lock:
        if _catalog is None or _catalog.path != path:
            if _catalog is not None:
                _catalog.close()
            _catalog = PeakCatalog(path)
        return _catalog


def reset_catalog() -> None:
    """Forget the cached catalog (tests change SDA_PEAK_DIR between cases)."""
    global _catalog
    with _catalog_lock:
        if _catalog is not None:
            _catalog.close()
        _catalog = None


# ---------------------------------------------------------------------------
# Import command
# ---------------------------------------------------------------------------


def is_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def download(url: str, dest: Path) -> None:
    """Stream *url* to *dest*, refusing an HTML page in place of a data file."""
    import httpx

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    last = 0.0
    with httpx.stream("GET", url, timeout=300.0, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise RuntimeError(f"{url} answered with a web page, not a data file")
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 5.0:
                    last = time.monotonic()
                    print(f"  downloaded {done / 1e6:.1f} MB", flush=True)
    os.replace(part, dest)
    print(f"  downloaded {done / 1e6:.1f} MB to {dest.name}", flush=True)


def _first(folder: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        hits = sorted(p for p in folder.glob(pattern) if p.is_file())
        if hits:
            return hits[0]
    return None


@dataclass
class Sources:
    prominence: Path
    names: Path | None
    admin1: Path | None
    isolation: Path | None
    fetched: list[Path] = field(default_factory=list)


def resolve_sources(args: argparse.Namespace, work: Path) -> Sources:
    """Local paths for every input: --from a file, a folder or a URL, or the defaults."""
    fetched: list[Path] = []

    def obtain(spec: str, name: str) -> Path:
        if is_url(spec):
            target = work / name
            print(f"Fetching {name} ...", flush=True)
            download(spec, target)
            fetched.append(target)
            return target
        path = Path(spec)
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {spec}")
        return path

    names: Path | None = None
    admin1: Path | None = None
    isolation: Path | None = None
    if args.source and not is_url(args.source) and Path(args.source).is_dir():
        folder = Path(args.source)
        found = _first(folder, ("all-peaks*.zip", "all-peaks*.txt", "*p100*.zip", "*p100*.txt"))
        if found is None:
            raise FileNotFoundError(f"no all-peaks*.zip or *.txt in {folder}")
        prominence = found
        names = _first(folder, ("allCountries.zip", "allCountries.txt"))
        admin1 = _first(folder, ("admin1CodesASCII.txt",))
        isolation = _first(folder, ("AllPeaksIsolationSorted.zip", "alliso*.txt", "alliso*.zip"))
    elif args.source:
        prominence = obtain(args.source, "all-peaks-p100.zip")
    else:
        prominence = obtain(KIRMSE_URL, "all-peaks-p100-2023.zip")
        names = obtain(GEONAMES_URL, "allCountries.zip") if not args.no_names else None
        admin1 = obtain(ADMIN1_URL, "admin1CodesASCII.txt") if not args.no_names else None
        if args.isolation is None and not args.no_isolation:
            isolation = obtain(ISOLATION_URL, "AllPeaksIsolationSorted.zip")
    if args.names:
        names = obtain(args.names, "allCountries.zip")
    if args.admin1:
        admin1 = obtain(args.admin1, "admin1CodesASCII.txt")
    if args.isolation:
        isolation = obtain(args.isolation, "AllPeaksIsolationSorted.zip")
    if args.no_names:
        names = admin1 = None
    if args.no_isolation:
        isolation = None
    return Sources(prominence, names, admin1, isolation, fetched)


def _describe(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%d")
    except OSError:
        stamp = "?"
    return f"{path.name} ({stamp})"


def _cmd_import(args: argparse.Namespace) -> int:
    dest = Path(args.db) if args.db else db_path()
    work = dest.parent / "download"
    started = time.monotonic()
    src = resolve_sources(args, work)
    print(f"Importing summits from {src.prominence.name}"
          + (f", names from {src.names.name}" if src.names else "")
          + (f", isolation from {src.isolation.name}" if src.isolation else "") + " ...",
          flush=True)
    report = build_catalog(
        src.prominence,
        dest,
        names=src.names,
        admin1=src.admin1,
        isolation=src.isolation,
        min_prom_m=args.min_prom,
        meta={"sources": "|".join(
            d for d in (_describe(src.prominence), _describe(src.names),
                        _describe(src.admin1), _describe(src.isolation)) if d)},
        log=lambda msg: print(msg, flush=True),
    )
    if not args.keep:
        for path in src.fetched:
            path.unlink(missing_ok=True)
    final = dest if dest.exists() else dest.with_name(dest.name + ".new")
    size_mb = final.stat().st_size / 1e6 if final.exists() else 0.0
    steps = ", ".join(f"{k} {v:.0f} s" for k, v in report.seconds.items())
    print(
        f"Done in {time.monotonic() - started:.0f} s: {report.kept:,} peaks"
        f" ({report.with_prominence:,} with prominence, {report.named:,} named from"
        f" GeoNames, {report.names_added:,} GeoNames-only, {report.isolation_matched:,}"
        f" isolation rows matched), {report.countries} countries. {size_mb:,.0f} MB at {final}."
        f" Steps: {steps}.",
        flush=True,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.db) if args.db else db_path()
    cat = PeakCatalog(path)
    try:
        s = cat.summary()
    finally:
        cat.close()
    if not s.get("available"):
        print(f"No peak catalog at {path}. {IMPORT_HINT}")
        return 1
    size_mb = path.stat().st_size / 1e6
    print(f"Catalog: {path} ({size_mb:,.0f} MB)")
    print(f"Peaks: {s['count']:,} ({s['with_prominence']:,} with prominence, "
          f"{s['named']:,} named, {s['with_isolation']:,} with isolation), "
          f"{s['country_count']} countries")
    print(f"Imported {s['imported_at']} from {', '.join(s['sources'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sda.peaks", description=__doc__.split(
        "\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="build the peak catalog")
    imp.add_argument("--from", dest="source",
                     help="the Kirmse all-peaks file (zip or txt), its URL, or a folder"
                          " holding it with allCountries.zip, admin1CodesASCII.txt and"
                          " AllPeaksIsolationSorted.zip (default: download all four)")
    imp.add_argument("--names", help="GeoNames allCountries.zip or .txt (file or URL)")
    imp.add_argument("--admin1", help="GeoNames admin1CodesASCII.txt (file or URL)")
    imp.add_argument("--isolation", help="Kirmse AllPeaksIsolationSorted.zip (file or URL)")
    imp.add_argument("--no-names", action="store_true", help="leave every peak unnamed")
    imp.add_argument("--no-isolation", action="store_true", help="skip the isolation join")
    imp.add_argument("--min-prom", type=float, default=0.0,
                     help="drop summits under this prominence in metres (default: keep all)")
    imp.add_argument("--db", help=f"catalog file (default: {db_path()})")
    imp.add_argument("--keep", action="store_true", help="keep downloaded files")
    imp.set_defaults(func=_cmd_import)
    st = sub.add_parser("status", help="show what the catalog holds")
    st.add_argument("--db", help=f"catalog file (default: {db_path()})")
    st.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
