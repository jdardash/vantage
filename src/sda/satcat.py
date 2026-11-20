"""SATCAT store behind the globe's object dossier: whose is it, what is it, how big.

One record per catalogued object (NORAD id, name, international designator, owner,
object type, launch date and site, decay date, RCS size) held in memory as a compact
slotted record keyed by NORAD id, plus optional physical details (mass, span,
manufacturer, bus, operator, parent) loaded on first use.

Where the data comes from
-------------------------
Primary: COMSPOC Spacebook, keyless.

* ``https://spacebook.com/api/entity/satcat`` (about 21 MB of JSON, 72k records):
  Space-Track SATCAT fields plus a COMSPOC GUID per object.
* ``https://spacebook.com/api/entity/satcat/details`` (about 70 MB, 71k records):
  ``discosMsg.comspocDerivedMessage`` (COMSPOC-adopted mass and span) and
  ``jMcDowellMsg`` (GCAT: manufacturer, bus, operator, parent). Lazy: fetched the
  first time a card is asked for, never at startup. ``SDA_SATCAT_DETAILS=0`` keeps
  it off the network (a cached copy is still used).

Spacebook's terms page could not be found, so it is treated as a free service of
one company: each file is downloaded at most once a day, kept on disk under
``SDA_SATCAT_DIR`` (default ``%LOCALAPPDATA%/sda/satcat`` on Windows,
``~/.cache/sda/satcat`` elsewhere, the convention of ``sda.cameras``), loaded from
there without a network call while it is under 24 hours old, and every response
names "COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)" as Spacebook's own
guide does. A failed download is retried after an hour, then two, up to a day.

Fallback when Spacebook fails and there is no cache: the Space-Track ``satcat``
class through ``sda.spacetrack`` when SDA_SPACETRACK_USER / SDA_SPACETRACK_PASS
are set (Space-Track asks for one satcat query a day, after 1700 UTC; the same
daily cache applies), else an empty store whose status says why. A stale cache is
always preferred to an empty store.

Reuse
-----
* ISHANTARE/ASTRA ``astra/spacebook.py`` (MIT, commit 74123de), taken: the
  Spacebook endpoint paths, the ``noradId`` / ``id`` (GUID) field names, cache age
  by file and "download failed, use the stale cache" fallback. The streaming
  parser, the compact records, the backoff and the Space-Track fallback are ours.
* PremaanshVyas/satlas ``apps/web/src/lib/satcat.ts`` (MIT, commit 410f923),
  taken: the fact-card field set, the 24 h cache rule, status derived from the
  decay date, a minimum record count before a reply replaces the cache, and the
  idea of mapping Space-Track owner and site codes to readable names. Its tables
  were corrected and extended against the codes this file actually carries.
* keeptrack.space (AGPL-3.0, commit db3b64e), ideas only, no code: the
  plain-words summary line of ``sat-info-box-object/object-summary.ts`` and the
  launch-site names of ``app/data/catalogs/countries.ts`` used to check ours.

Nothing here touches positions: this module is metadata only, and nothing it
returns feeds Pc, screening or risk classification.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import IO, Any

import httpx

logger = logging.getLogger(__name__)

SPACEBOOK_BASE = "https://spacebook.com/api"
SATCAT_URL = f"{SPACEBOOK_BASE}/entity/satcat"
DETAILS_URL = f"{SPACEBOOK_BASE}/entity/satcat/details"

SATCAT_DIR_ENV = "SDA_SATCAT_DIR"
DETAILS_ENV = "SDA_SATCAT_DETAILS"
OFFLINE_ENV = "SDA_OFFLINE"

ATTRIBUTION = "COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)"
USER_AGENT = "vantage/0.3 (satcat daily cache)"

SATCAT_FILE = "satcat.json"
DETAILS_FILE = "satcat-details.json"

#: A cached file younger than this is used without asking the network.
MAX_AGE_S = 24 * 3600
#: First wait after a failed download; doubles per failure up to MAX_AGE_S.
RETRY_AFTER_FAILURE_S = 3600
#: A reply with fewer records than this is not the catalog and never replaces it.
MIN_RECORDS = 1000
DOWNLOAD_TIMEOUT_S = 180.0
CHUNK_CHARS = 1 << 20

MAX_NORAD_ID = 339_999
MAX_NAME = 80

SOURCE_SPACEBOOK = "spacebook"
SOURCE_SPACETRACK = "space-track"

OBJECT_TYPES: tuple[str, ...] = ("PAYLOAD", "ROCKET BODY", "DEBRIS", "UNKNOWN")
TYPE_LABELS: dict[str, str] = {
    "PAYLOAD": "Payload",
    "ROCKET BODY": "Rocket body",
    "DEBRIS": "Debris",
    "UNKNOWN": "Unknown type",
}
TYPE_ALIASES: dict[str, str] = {
    "PAYLOAD": "PAYLOAD", "PAY": "PAYLOAD", "P": "PAYLOAD",
    "ROCKET BODY": "ROCKET BODY", "ROCKET_BODY": "ROCKET BODY", "ROCKETBODY": "ROCKET BODY",
    "R/B": "ROCKET BODY", "RB": "ROCKET BODY",
    "DEBRIS": "DEBRIS", "DEB": "DEBRIS",
    "UNKNOWN": "UNKNOWN", "UNK": "UNKNOWN", "TBA": "UNKNOWN", "OTHER": "UNKNOWN",
}

#: Space-Track RCS_SIZE bands (radar cross-section, square metres).
RCS_SIZES: tuple[str, ...] = ("SMALL", "MEDIUM", "LARGE", "UNKNOWN")
RCS_LABELS: dict[str, str] = {
    "SMALL": "Small (under 0.1 m2)",
    "MEDIUM": "Medium (0.1 to 1 m2)",
    "LARGE": "Large (over 1 m2)",
    "UNKNOWN": "Not published",
}

#: Space-Track owner (COUNTRY) codes as the SATCAT carries them. A code missing
#: here is shown as the code itself.
OWNER_NAMES: dict[str, str] = {
    "US": "United States", "CIS": "Russia / former USSR", "PRC": "China",
    "FR": "France", "JPN": "Japan", "IND": "India", "UK": "United Kingdom",
    "TBD": "Owner not yet assigned", "ESA": "European Space Agency", "GER": "Germany",
    "IT": "Italy", "ITSO": "Intelsat", "CA": "Canada", "SPN": "Spain",
    "SKOR": "South Korea", "ORB": "ORBCOMM", "CHBZ": "China / Brazil",
    "GLOB": "Globalstar", "SES": "SES", "ARGN": "Argentina", "AUS": "Australia",
    "EUTE": "Eutelsat", "ISRA": "Israel", "TURK": "Turkey", "TWN": "Taiwan",
    "ROC": "Taiwan", "FIN": "Finland", "BRAZ": "Brazil", "SEAL": "Sea Launch",
    "UAE": "United Arab Emirates", "IRAN": "Iran", "SING": "Singapore", "POL": "Poland",
    "NOR": "Norway", "NZ": "New Zealand", "LUXE": "Luxembourg", "GREC": "Greece",
    "INDO": "Indonesia", "NETH": "Netherlands", "SWTZ": "Switzerland", "IM": "Inmarsat",
    "O3B": "O3b Networks", "BEL": "Belgium", "DEN": "Denmark", "EUME": "EUMETSAT",
    "SWED": "Sweden", "AB": "Arabsat", "THAI": "Thailand", "SAUD": "Saudi Arabia",
    "POR": "Portugal", "LTU": "Lithuania", "EGYP": "Egypt", "MEX": "Mexico",
    "RWA": "Rwanda", "UKR": "Ukraine", "SAFR": "South Africa", "HUN": "Hungary",
    "BGR": "Bulgaria", "ESRO": "European Space Research Organisation",
    "PAKI": "Pakistan", "CZE": "Czech Republic", "MALA": "Malaysia", "NATO": "NATO",
    "RP": "Philippines", "AC": "AsiaSat", "CHLE": "Chile", "KAZ": "Kazakhstan",
    "NKOR": "North Korea", "ALG": "Algeria", "NIG": "Nigeria",
    "ABS": "Asia Broadcast Satellite", "VTNM": "Vietnam", "ASRA": "Austria",
    "CZCH": "Czechoslovakia", "PER": "Peru", "PERU": "Peru", "MA": "Morocco",
    "SVN": "Slovenia", "COL": "Colombia", "VENZ": "Venezuela", "AZER": "Azerbaijan",
    "FGER": "France / Germany", "STCT": "Singapore / Taiwan", "RASC": "RascomStar-QAF",
    "ROM": "Romania", "BELA": "Belarus", "ECU": "Ecuador", "EST": "Estonia",
    "QAT": "Qatar", "FRIT": "France / Italy", "SVK": "Slovakia", "BGD": "Bangladesh",
    "AGO": "Angola", "KEN": "Kenya", "KWT": "Kuwait", "DJI": "Djibouti",
    "NICO": "New ICO", "USBZ": "United States / Brazil", "BOL": "Bolivia",
    "URY": "Uruguay", "IRAQ": "Iraq", "TMMC": "Turkmenistan / Monaco", "LAOS": "Laos",
    "GHA": "Ghana", "MNG": "Mongolia", "CRI": "Costa Rica", "JOR": "Jordan",
    "LKA": "Sri Lanka", "NPL": "Nepal", "SDN": "Sudan", "PRY": "Paraguay",
    "TUN": "Tunisia", "MMR": "Myanmar", "MUS": "Mauritius", "PRI": "Puerto Rico",
    "MDA": "Moldova", "UGA": "Uganda", "ZWE": "Zimbabwe", "MCO": "Monaco",
    "HRV": "Croatia", "BWA": "Botswana", "BHR": "Bahrain", "SLB": "Solomon Islands",
    "ISS": "ISS partners",
}

#: Space-Track LAUNCH_SITE codes.
SITE_NAMES: dict[str, str] = {
    "AFETR": "Cape Canaveral, Florida, USA",
    "AFWTR": "Vandenberg, California, USA",
    "PKMTR": "Plesetsk Cosmodrome, Russia",
    "TTMTR": "Baikonur Cosmodrome, Kazakhstan",
    "TSC": "Taiyuan Satellite Launch Center, China",
    "FRGUI": "Guiana Space Centre, Kourou, French Guiana",
    "JSC": "Jiuquan Satellite Launch Center, China",
    "SRI": "Satish Dhawan Space Centre, Sriharikota, India",
    "XSC": "Xichang Satellite Launch Center, China",
    "TNSTA": "Tanegashima Space Center, Japan",
    "VOSTO": "Vostochny Cosmodrome, Russia",
    "WSC": "Wenchang Space Launch Site, China",
    "RLLC": "Rocket Lab Launch Complex 1, Mahia, New Zealand",
    "KYMTR": "Kapustin Yar, Russia",
    "WLPIS": "Wallops Island, Virginia, USA",
    "YSLA": "Yellow Sea launch area, China",
    "KSCUT": "Uchinoura Space Center, Japan",
    "OREN": "Yasny (Dombarovsky), Orenburg, Russia",
    "WRAS": "Western Range airspace (air launch), USA",
    "ERAS": "Eastern Range airspace (air launch), USA",
    "SEAL": "Sea Launch platform, Pacific Ocean",
    "BOCA": "Starbase, Boca Chica, Texas, USA",
    "HGSTR": "Hammaguir, Algeria",
    "NSC": "Naro Space Center, South Korea",
    "KODAK": "Pacific Spaceport Complex, Kodiak, Alaska, USA",
    "SCSLA": "South China Sea launch area, China",
    "YAVNE": "Palmachim (Yavne), Israel",
    "SVOB": "Svobodny Cosmodrome, Russia",
    "SEM": "Semnan, Iran",
    "SNMLP": "San Marco platform, Kenya",
    "SMTS": "Shahrud missile test site, Iran",
    "SADOL": "Submarine launch, Barents Sea, Russia",
    "KWAJL": "Kwajalein Atoll, Marshall Islands",
    "YUN": "Sohae (Yunsong), North Korea",
    "WOMRA": "Woomera, Australia",
    "CAS": "Canary Islands airspace (air launch)",
    "JJSLA": "Jeju Island sea launch area, South Korea",
}

#: Objects released from the ISS keep the station's international designator.
_ISS_LAUNCH = "1998-067"
# Zarya itself and the Proton stage that launched it; every other 1998-067 piece
# (565 on 2026-11-19, debris and deployed cubesats) came off the station later.
_ISS_OWN_PIECES = frozenset({"1998-067A", "1998-067B"})

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_INTL_ID = re.compile(r"^\d{4}-\d{3}[A-Z]{0,3}$")
_ALPHA5 = re.compile(r"^([A-HJ-NP-Z])(\d{4})$")
_ALPHA5_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_GCAT_SATCAT_ID = re.compile(r"^S(\d{1,6})$")
_WS = frozenset(" \t\r\n")


class SatcatFetchError(RuntimeError):
    """A download that failed or returned something that is not the catalog."""


# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------


def satcat_dir() -> Path:
    """Cache directory: SDA_SATCAT_DIR, else the user cache dir (as sda.cameras)."""
    configured = os.environ.get(SATCAT_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "satcat"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "satcat"


def _flag_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def network_allowed() -> bool:
    """False under SDA_OFFLINE (the static demo build), as api.py reads it."""
    return os.environ.get(OFFLINE_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Streaming JSON array reader
# ---------------------------------------------------------------------------


class _ChunkReader:
    """A text stream read in chunks, decoded one JSON value at a time."""

    def __init__(self, stream: IO[str], chunk_chars: int) -> None:
        self.stream = stream
        self.chunk = max(16, chunk_chars)
        self.buf = ""
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _refill(self) -> bool:
        if self.eof:
            return False
        data = self.stream.read(self.chunk)
        if not data:
            self.eof = True
            return False
        self.buf = self.buf[self.pos:] + data
        self.pos = 0
        return True

    def peek(self) -> str:
        """Next character that is not whitespace, or "" at the end of the stream."""
        while True:
            buf = self.buf
            pos = self.pos
            while pos < len(buf) and buf[pos] in _WS:
                pos += 1
            self.pos = pos
            if pos < len(buf):
                return buf[pos]
            if not self._refill():
                return ""

    def value(self) -> Any:
        # raw_decode does not skip whitespace, and pretty-printed JSON has some
        # after every comma.
        if not self.peek():
            raise ValueError("the JSON array ends early")
        while True:
            try:
                obj, end = self.decoder.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if not self._refill():
                    raise
                continue
            # A value that ends exactly at the buffer edge may be a cut number.
            if end >= len(self.buf) and self._refill():
                continue
            self.pos = end
            return obj


def _close_array(reader: _ChunkReader) -> None:
    """Step over the closing bracket; anything but whitespace after it is an error."""
    reader.pos += 1
    if reader.peek():
        raise ValueError("data after the end of the JSON array")


def iter_json_array(stream: IO[str], chunk_chars: int = CHUNK_CHARS) -> Iterator[Any]:
    """Yield the items of a top-level JSON array without holding the whole text.

    Peak memory is one chunk plus one item, so the 70 MB details file never sits
    in memory as text or as a list of dicts. Raises ValueError (JSONDecodeError
    is one) on anything that is not a well-formed array.
    """
    reader = _ChunkReader(stream, chunk_chars)
    if reader.peek() == "\ufeff":
        reader.pos += 1
    if reader.peek() != "[":
        raise ValueError("expected a JSON array")
    reader.pos += 1
    if reader.peek() == "]":
        _close_array(reader)
        return
    while True:
        yield reader.value()
        ch = reader.peek()
        if ch == ",":
            reader.pos += 1
            continue
        if ch == "]":
            _close_array(reader)
            return
        raise ValueError(f"expected ',' or ']' in the JSON array, found {ch!r}")


def iter_json_file(path: Path, chunk_chars: int = CHUNK_CHARS) -> Iterator[Any]:
    with path.open("r", encoding="utf-8-sig") as fh:
        yield from iter_json_array(fh, chunk_chars)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SatcatEntry:
    """One catalogued object. Empty strings mean "not published". Read-only by use.

    Not frozen: a frozen dataclass sets each field through object.__setattr__,
    which made building 72k of them a measurable part of the load.
    """

    norad_id: int
    name: str
    intl_id: str
    owner: str
    object_type: str
    launch_date: str
    launch_year: int
    launch_site: str
    decay_date: str
    rcs_size: str
    guid: str

    @property
    def on_orbit(self) -> bool:
        return not self.decay_date


@dataclass(slots=True)
class DetailsEntry:
    """Physical details. Mass and span are COMSPOC-adopted, the rest is GCAT."""

    mass_kg: float | None
    span_m: float | None
    size_source: str  # COMSPOC, GCAT, "COMSPOC and GCAT" or "" (see details_from_row)
    manufacturer: str
    bus: str
    operator: str
    parent: str


# Shared values (owner codes, sites, launch dates, debris names) are normalized
# once per distinct raw string (the caches below) and interned, so 3,000 copies of
# "FENGYUN 1C DEB" cost one string and one cleaning pass. The caches are cleared
# after each parse so they do not hold the catalog twice.
_YEARS: dict[int, int] = {}


def _year(value: int) -> int:
    return _YEARS.setdefault(value, value)


def _clean(text: str, limit: int) -> str:
    # The per-character pass only runs for the rare value holding a control character.
    cleaned = text if text.isprintable() else "".join(ch for ch in text if ch.isprintable())
    return cleaned.strip()[:limit]


@lru_cache(maxsize=1 << 17)
def _shared_text(text: str, limit: int) -> str:
    cleaned = _clean(text, limit)
    return sys.intern(cleaned) if cleaned else ""


@lru_cache(maxsize=4096)
def _shared_code(text: str, limit: int) -> str:
    cleaned = _clean(text, limit).upper()
    return "" if cleaned in {"", "-", "?"} else sys.intern(cleaned)


@lru_cache(maxsize=1 << 16)
def _shared_date(text: str) -> str:
    text = text.strip()
    return sys.intern(text[:10]) if _DATE.match(text) else ""


def _clear_caches() -> None:
    _shared_text.cache_clear()
    _shared_code.cache_clear()
    _shared_date.cache_clear()


def decode_norad(value: Any) -> int | None:
    """NORAD id from an int, a digit string or an alpha-5 string ("A0001" is 100001)."""
    if type(value) is int:  # the common case; also keeps bool out
        nid = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, float) and value.is_integer():
        nid = int(value)
    elif isinstance(value, str):
        text = value.strip().upper()
        if text.isdigit() and len(text) <= 6:
            nid = int(text)
        else:
            match = _ALPHA5.match(text)
            if match is None:
                return None
            nid = (_ALPHA5_LETTERS.index(match.group(1)) + 10) * 10_000 + int(match.group(2))
    else:
        return None
    return nid if 0 < nid <= MAX_NORAD_ID else None


def _text(value: Any, limit: int = MAX_NAME) -> str:
    """A repeated free-text value (a name, a bus), cleaned and interned."""
    return _shared_text(value, limit) if isinstance(value, str) else ""


def _unique(value: Any, limit: int) -> str:
    """A value unique per object (designator, GUID): cleaned, never cached."""
    return _clean(value, limit) if isinstance(value, str) else ""


def _code(value: Any, limit: int = 16) -> str:
    return _shared_code(value, limit) if isinstance(value, str) else ""


def _date(value: Any) -> str:
    """YYYY-MM-DD from "1998-11-20T00:00:00" or "1998-11-20", else ""."""
    return _shared_date(value) if isinstance(value, str) else ""


@lru_cache(maxsize=256)
def _type_of(text: str) -> str:
    return TYPE_ALIASES.get(text.strip().upper(), "UNKNOWN")


@lru_cache(maxsize=256)
def _rcs_of(text: str) -> str:
    text = text.strip().upper()
    return text if text in ("SMALL", "MEDIUM", "LARGE") else "UNKNOWN"


def normalize_type(value: Any) -> str:
    """PAYLOAD, ROCKET BODY, DEBRIS or UNKNOWN; anything unrecognised is UNKNOWN."""
    return _type_of(value) if isinstance(value, str) else "UNKNOWN"


def normalize_rcs(value: Any) -> str:
    """SMALL, MEDIUM, LARGE or UNKNOWN."""
    return _rcs_of(value) if isinstance(value, str) else "UNKNOWN"


def entry_from_row(row: Any) -> SatcatEntry | None:
    """A compact record from one Spacebook SATCAT row, or None for junk."""
    if not isinstance(row, dict):
        return None
    get = row.get
    nid = decode_norad(get("noradId"))
    if nid is None:
        return None
    launch = _date(get("launchDate"))
    intl = _unique(get("intlId"), 16).upper()
    return SatcatEntry(
        nid,
        _text(get("name")) or f"NORAD {nid}",
        intl if _INTL_ID.match(intl) else "",
        _code(get("owner")),
        normalize_type(get("objectType")),
        launch,
        _year(int(launch[:4])) if launch else 0,
        _code(get("launchSite")),
        _date(get("decayDate")),
        normalize_rcs(get("rcsSize")),
        _unique(get("id"), 64),
    )


#: COMSPOC's adopted (mass kg, span m) for an object it has no physical data on:
#: 32,312 of the 71,304 Spacebook details rows on 2026-11-19, 31,885 of them debris.
PLACEHOLDER_SIZE = (5.0, 10.0)
_SIZE_SOURCES: dict[tuple[bool, bool], str] = {
    (True, False): "COMSPOC", (False, True): "GCAT",
    (True, True): "COMSPOC and GCAT", (False, False): "",
}


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if 0 < number < 1e9 else None


def details_from_row(row: Any) -> tuple[int, DetailsEntry] | None:
    """(NORAD id, details) from one Spacebook SATCAT details row, or None."""
    if not isinstance(row, dict):
        return None
    nid = decode_norad(row.get("noradId"))
    if nid is None:
        return None
    discos = row.get("discosMsg")
    derived = discos.get("comspocDerivedMessage") if isinstance(discos, dict) else None
    derived = derived if isinstance(derived, dict) else {}
    gcat = row.get("jMcDowellMsg")
    gcat = gcat if isinstance(gcat, dict) else {}
    # COMSPOC's adopted values first: for the ISS they describe the whole station
    # where GCAT describes the Zarya module, and GCAT's total mass counts stages
    # still attached (Sputnik 2: 8,298 kg against 504 kg). Its 5 kg / 10 m pair is
    # a placeholder for "no data", not a size, so GCAT fills in behind it.
    mass_c = _positive(derived.get("adoptedMass"))
    span_c = _positive(derived.get("adoptedSpan"))
    if (mass_c, span_c) == PLACEHOLDER_SIZE:
        mass_c = span_c = None
    mass_g = _positive(gcat.get("totalMass")) or _positive(gcat.get("mass"))
    span_g = _positive(gcat.get("span"))
    mass = mass_c if mass_c is not None else mass_g
    span = span_c if span_c is not None else span_g
    from_comspoc = mass_c is not None or span_c is not None
    from_gcat = (mass_c is None and mass_g is not None) or (span_c is None and span_g is not None)
    entry = DetailsEntry(
        mass,
        span,
        _SIZE_SOURCES[(from_comspoc, from_gcat)],
        _code(gcat.get("manufacturer"), 24),
        _text(gcat.get("bus"), 40),
        _code(gcat.get("owner"), 24),
        _code(gcat.get("parent"), 16),
    )
    return nid, entry


def row_from_spacetrack(row: Any) -> dict[str, Any] | None:
    """A Space-Track satcat row in Spacebook's shape, so one parser reads both."""
    if not isinstance(row, dict):
        return None
    nid = decode_norad(row.get("NORAD_CAT_ID"))
    if nid is None:
        return None
    return {
        "id": "",
        "noradId": nid,
        "name": row.get("SATNAME") or row.get("OBJECT_NAME") or "",
        "intlId": row.get("INTLDES") or row.get("OBJECT_ID") or "",
        "objectType": row.get("OBJECT_TYPE") or "",
        "owner": row.get("COUNTRY") or "",
        "launchSite": row.get("SITE") or "",
        "launchDate": row.get("LAUNCH") or "",
        "decayDate": row.get("DECAY") or "",
        "rcsSize": row.get("RCS_SIZE") or "",
    }


def parse_satcat(rows: Iterable[Any]) -> dict[int, SatcatEntry]:
    out: dict[int, SatcatEntry] = {}
    try:
        for row in rows:
            entry = entry_from_row(row)
            if entry is not None:
                out[entry.norad_id] = entry
    finally:
        _clear_caches()
    return out


def parse_details(rows: Iterable[Any]) -> dict[int, DetailsEntry]:
    out: dict[int, DetailsEntry] = {}
    try:
        for row in rows:
            parsed = details_from_row(row)
            if parsed is not None:
                out[parsed[0]] = parsed[1]
    finally:
        _clear_caches()
    return out


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def owner_name(code: str) -> str:
    if not code:
        return "Not published"
    return OWNER_NAMES.get(code, code)


def site_name(code: str) -> str:
    if not code:
        return ""
    return SITE_NAMES.get(code, code)


def status_label(entry: SatcatEntry) -> str:
    return "On orbit" if entry.on_orbit else "Decayed"


def summary(entry: SatcatEntry) -> str:
    """One plain-words line: what it is, whose, since when."""
    parts = [TYPE_LABELS.get(entry.object_type, "Object")]
    if entry.owner and entry.owner != "TBD":
        parts.append(f"owned by {owner_name(entry.owner)}")
    if entry.launch_year:
        parts.append(f"launched {entry.launch_year}")
    parts.append("on orbit" if entry.on_orbit else f"decayed {entry.decay_date}")
    return ", ".join(parts) + "."


def notes_for(entry: SatcatEntry) -> list[str]:
    notes: list[str] = []
    if entry.intl_id.startswith(_ISS_LAUNCH) and entry.intl_id not in _ISS_OWN_PIECES:
        notes.append(
            "Released from the ISS: the catalog gives it the station's 1998 designator "
            "and launch date, not its own."
        )
    if entry.owner == "TBD":
        notes.append("Owner not yet assigned by Space-Track (newly catalogued object).")
    return notes


def decade_label(year: int) -> str:
    return f"{year - year % 10}s"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

Downloader = Callable[[str, Path, float], None]
SpaceTrackRows = Callable[[], list[dict[str, Any]]]


def http_download(url: str, dest: Path, timeout: float) -> None:
    """Stream ``url`` into ``dest``; raises SatcatFetchError on any failure."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        with httpx.stream(
            "GET", url, headers=headers, timeout=timeout, follow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                raise SatcatFetchError(f"HTTP {resp.status_code} from {url}")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise SatcatFetchError(f"{type(exc).__name__} fetching {url}: {exc}") from exc


def spacetrack_rows() -> list[dict[str, Any]]:
    """The Space-Track SATCAT, or SatcatFetchError. Needs the account variables."""
    from sda import spacetrack

    if not spacetrack.credentials_available():
        raise SatcatFetchError("Space-Track credentials are not set")
    try:
        with spacetrack.SpaceTrackClient() as client:
            return client.satcat()
    except spacetrack.SpaceTrackError as exc:
        raise SatcatFetchError(f"Space-Track: {exc}") from exc


def _spacetrack_configured() -> bool:
    from sda import spacetrack

    return spacetrack.credentials_available()


def cache_is_fresh(fetched_at: float | None, now: float, max_age_s: float = MAX_AGE_S) -> bool:
    """True when a copy fetched at ``fetched_at`` (epoch s) may be used without a fetch.

    A time more than an hour in the future is a broken clock or a broken file,
    and counts as stale so it cannot block refreshes forever.
    """
    if fetched_at is None:
        return False
    age = now - fetched_at
    return -3600 < age < max_age_s


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class _FileState:
    """What is known about one cached file (catalog or details)."""

    source: str = ""
    fetched_at: float | None = None
    stale: bool = False
    load_seconds: float = 0.0
    last_error: str = ""
    last_attempt: float | None = None
    failures: int = 0
    cache_tried: bool = False


class SatcatStore:
    """SATCAT records in memory, refreshed from Spacebook at most once a day."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        download: Downloader = http_download,
        spacetrack: SpaceTrackRows | None = spacetrack_rows,
        spacetrack_configured: Callable[[], bool] = _spacetrack_configured,
        clock: Callable[[], float] = time.time,
        min_records: int = MIN_RECORDS,
        max_age_s: float = MAX_AGE_S,
        details_network: bool | None = None,
    ) -> None:
        self.directory = directory if directory is not None else satcat_dir()
        self._download = download
        self._spacetrack = spacetrack
        self._spacetrack_configured = spacetrack_configured
        self._clock = clock
        self.min_records = min_records
        self.max_age_s = max_age_s
        self._details_network = details_network
        self._entries: dict[int, SatcatEntry] = {}
        self._details: dict[int, DetailsEntry] = {}
        self._cat = _FileState()
        self._det = _FileState()
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._details_lock = threading.Lock()
        self._details_thread: threading.Thread | None = None
        self._facets: dict[bool, dict[str, Any]] = {}

    # --- paths -------------------------------------------------------------

    @property
    def satcat_path(self) -> Path:
        return self.directory / SATCAT_FILE

    @property
    def details_path(self) -> Path:
        return self.directory / DETAILS_FILE

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_name(path.stem + ".meta.json")

    # --- generic file handling ---------------------------------------------

    def _read_meta(self, path: Path) -> tuple[str, float | None]:
        """(source, fetched_at) from the sidecar, else the file's mtime."""
        source = SOURCE_SPACEBOOK
        fetched: float | None = None
        with suppress(OSError, ValueError):
            meta = json.loads(self._meta_path(path).read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                if meta.get("source") in (SOURCE_SPACEBOOK, SOURCE_SPACETRACK):
                    source = str(meta["source"])
                fetched = _epoch(meta.get("fetched_at"))
        if fetched is None:
            with suppress(OSError):
                fetched = path.stat().st_mtime
        return source, fetched

    def _write_meta(self, path: Path, source: str, url: str, records: int, at: float) -> None:
        meta = {"source": source, "url": url, "fetched_at": _iso(at), "records": records}
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, self._meta_path(path))
        except OSError as exc:
            logger.warning("satcat: could not write %s: %s", self._meta_path(path).name, exc)
            with suppress(OSError):
                tmp.unlink()

    def _may_attempt(self, state: _FileState, now: float) -> bool:
        if state.last_attempt is None or state.failures == 0:
            return True
        doubling = 1 << min(state.failures - 1, 16)
        wait = min(float(RETRY_AFTER_FAILURE_S * doubling), self.max_age_s)
        return now - state.last_attempt >= wait

    def _fetch_to_cache(
        self, url: str, path: Path, parse: Callable[[Iterable[Any]], dict[int, Any]]
    ) -> dict[int, Any]:
        """Download into a temp file, parse it, and only then replace the cache."""
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
        try:
            self._download(url, tmp, DOWNLOAD_TIMEOUT_S)
            try:
                parsed = parse(iter_json_file(tmp))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise SatcatFetchError(f"{url} did not return a JSON array: {exc}") from exc
            if len(parsed) < self.min_records:
                raise SatcatFetchError(
                    f"{url} returned {len(parsed)} records, fewer than {self.min_records}"
                )
            os.replace(tmp, path)
            return parsed
        finally:
            with suppress(OSError):
                tmp.unlink()

    # --- catalog -----------------------------------------------------------

    def _install(self, entries: dict[int, SatcatEntry], source: str, fetched: float | None,
                 load_s: float, stale: bool) -> None:
        with self._lock:
            self._entries = entries
            self._cat.source = source
            self._cat.fetched_at = fetched
            self._cat.load_seconds = load_s
            self._cat.stale = stale
            self._facets = {}

    def load_cache(self) -> bool:
        """Load the cached catalog whatever its age. No network. True when loaded."""
        path = self.satcat_path
        self._cat.cache_tried = True
        if not path.is_file():
            return False
        started = time.perf_counter()
        try:
            entries = parse_satcat(iter_json_file(path))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._cat.last_error = f"cached {SATCAT_FILE} unreadable: {exc}"
            logger.warning("satcat: %s", self._cat.last_error)
            return False
        if not entries:
            self._cat.last_error = f"cached {SATCAT_FILE} holds no records"
            return False
        source, fetched = self._read_meta(path)
        stale = not cache_is_fresh(fetched, self._clock(), self.max_age_s)
        self._install(entries, source, fetched, time.perf_counter() - started, stale)
        logger.info("satcat: %d records from cache (%s)", len(entries), source)
        return True

    def ensure_cache_loaded(self) -> None:
        """Load the disk cache once if nothing is in memory. Never touches the network."""
        if self._entries or self._cat.cache_tried:
            return
        with self._refresh_lock:
            if not self._entries and not self._cat.cache_tried:
                self.load_cache()

    def ensure_loaded(self, allow_network: bool | None = None) -> str:
        """Make the catalog current and say what happened.

        'fresh' (nothing to do), 'offline', 'backoff', 'downloaded',
        'space-track', 'stale' (refresh failed, older copy kept) or 'empty'.
        """
        net = network_allowed() if allow_network is None else allow_network
        with self._refresh_lock:
            if not self._entries and not self._cat.cache_tried:
                self.load_cache()
            now = self._clock()
            if self._entries and cache_is_fresh(self._cat.fetched_at, now, self.max_age_s):
                self._cat.stale = False
                return "fresh"
            if not net:
                return "offline"
            if not self._may_attempt(self._cat, now):
                return "backoff"
            self._cat.last_attempt = now
            started = time.perf_counter()
            try:
                entries = self._fetch_to_cache(SATCAT_URL, self.satcat_path, parse_satcat)
            except (SatcatFetchError, OSError) as exc:
                self._cat.failures += 1
                self._cat.last_error = f"Spacebook: {exc}"
                logger.warning("satcat: %s", self._cat.last_error)
            else:
                fetched = self._clock()
                self._write_meta(self.satcat_path, SOURCE_SPACEBOOK, SATCAT_URL,
                                 len(entries), fetched)
                self._install(entries, SOURCE_SPACEBOOK, fetched,
                              time.perf_counter() - started, False)
                self._cat.failures = 0
                self._cat.last_error = ""
                return "downloaded"
            if not self._entries and self._spacetrack is not None:
                if self._spacetrack_configured():
                    return self._load_from_spacetrack()
                self._cat.last_error += (
                    "; no cached copy, and Space-Track is not configured "
                    "(set SDA_SPACETRACK_USER and SDA_SPACETRACK_PASS)"
                )
            if self._entries:
                self._cat.stale = True
                return "stale"
            return "empty"

    def _load_from_spacetrack(self) -> str:
        assert self._spacetrack is not None
        started = time.perf_counter()
        try:
            raw = self._spacetrack()
        except (SatcatFetchError, OSError) as exc:
            self._cat.last_error += f"; {exc}"
            return "empty"
        rows = [r for r in (row_from_spacetrack(x) for x in raw) if r is not None]
        entries = parse_satcat(rows)
        if len(entries) < self.min_records:
            self._cat.last_error += (
                f"; Space-Track returned {len(entries)} records, fewer than {self.min_records}"
            )
            return "empty"
        fetched = self._clock()
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.satcat_path.with_name(f"satcat.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.satcat_path)
            self._write_meta(self.satcat_path, SOURCE_SPACETRACK, "space-track satcat",
                             len(entries), fetched)
        except OSError as exc:
            logger.warning("satcat: could not cache the Space-Track copy: %s", exc)
            with suppress(OSError):
                tmp.unlink()
        self._install(entries, SOURCE_SPACETRACK, fetched, time.perf_counter() - started, False)
        self._cat.failures = 0
        return "space-track"

    # --- details -----------------------------------------------------------

    def _details_net(self, allow_network: bool | None) -> bool:
        if allow_network is not None:
            return allow_network
        if self._details_network is not None:
            return self._details_network
        return network_allowed() and not _flag_off(DETAILS_ENV)

    def ensure_details(self, allow_network: bool | None = None) -> str:
        """Load the details (cache, then a download if the cache is old or missing)."""
        net = self._details_net(allow_network)
        with self._details_lock:
            path = self.details_path
            if not self._details and not self._det.cache_tried:
                self._det.cache_tried = True
                if path.is_file():
                    started = time.perf_counter()
                    try:
                        details = parse_details(iter_json_file(path))
                    except (OSError, ValueError, UnicodeDecodeError) as exc:
                        self._det.last_error = f"cached {DETAILS_FILE} unreadable: {exc}"
                    else:
                        source, fetched = self._read_meta(path)
                        with self._lock:
                            self._details = details
                            self._det.source = source
                            self._det.fetched_at = fetched
                            self._det.load_seconds = time.perf_counter() - started
            now = self._clock()
            if self._details and cache_is_fresh(self._det.fetched_at, now, self.max_age_s):
                self._det.stale = False
                return "fresh"
            if not net:
                self._det.stale = bool(self._details)
                return "offline"
            if not self._may_attempt(self._det, now):
                return "backoff"
            self._det.last_attempt = now
            started = time.perf_counter()
            try:
                details = self._fetch_to_cache(DETAILS_URL, path, parse_details)
            except (SatcatFetchError, OSError) as exc:
                self._det.failures += 1
                self._det.last_error = f"Spacebook details: {exc}"
                self._det.stale = bool(self._details)
                logger.warning("satcat: %s", self._det.last_error)
                return "stale" if self._details else "empty"
            fetched = self._clock()
            self._write_meta(path, SOURCE_SPACEBOOK, DETAILS_URL, len(details), fetched)
            with self._lock:
                self._details = details
                self._det.source = SOURCE_SPACEBOOK
                self._det.fetched_at = fetched
                self._det.load_seconds = time.perf_counter() - started
                self._det.stale = False
            self._det.failures = 0
            self._det.last_error = ""
            return "downloaded"

    def details_status(self) -> str:
        """loaded, loading, stale, unavailable or not requested."""
        thread = self._details_thread
        if thread is not None and thread.is_alive():
            return "loading"
        if self._details:
            return "stale" if self._det.stale else "loaded"
        if self._det.cache_tried:
            return "unavailable"
        return "not requested"

    def request_details(self) -> str:
        """Start loading the details in the background if they are missing or old."""
        thread = self._details_thread
        if thread is not None and thread.is_alive():
            return "loading"
        now = self._clock()
        current = bool(self._details) and cache_is_fresh(self._det.fetched_at, now,
                                                         self.max_age_s)
        if current:
            return "loaded"
        net = self._details_net(None)
        if self._det.cache_tried and (not net or not self._may_attempt(self._det, now)):
            return self.details_status()
        thread = threading.Thread(target=self._details_worker, name="satcat-details",
                                  daemon=True)
        self._details_thread = thread
        thread.start()
        return "loading"

    def _details_worker(self) -> None:
        try:
            self.ensure_details()
        except Exception as exc:  # a background thread must log, never die silently
            self._det.last_error = f"details load failed: {exc}"
            logger.exception("satcat: details load failed")

    def join_details(self, timeout: float | None = None) -> None:
        thread = self._details_thread
        if thread is not None:
            thread.join(timeout)

    # --- queries -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, norad_id: int) -> SatcatEntry | None:
        return self._entries.get(norad_id)

    def get_details(self, norad_id: int) -> DetailsEntry | None:
        return self._details.get(norad_id)

    def age_hours(self) -> float | None:
        fetched = self._cat.fetched_at
        if fetched is None:
            return None
        return round(max(0.0, self._clock() - fetched) / 3600.0, 2)

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self._cat.source,
            "fetched_at": _iso(self._cat.fetched_at),
            "age_hours": self.age_hours(),
            "stale": self._cat.stale,
            "attribution": ATTRIBUTION,
        }

    def _parent(self, parent: str) -> tuple[int | None, str]:
        match = _GCAT_SATCAT_ID.match(parent)
        if match is None:
            return None, ""
        nid = int(match.group(1))
        entry = self._entries.get(nid)
        return nid, entry.name if entry is not None else ""

    def card(self, norad_id: int) -> dict[str, Any] | None:
        """Everything the dossier card shows for one object, or None if unknown."""
        entry = self._entries.get(norad_id)
        if entry is None:
            return None
        detail = self._details.get(norad_id)
        details: dict[str, Any] | None = None
        if detail is not None:
            parent_id, parent_name = self._parent(detail.parent)
            details = {
                "mass_kg": detail.mass_kg,
                "span_m": detail.span_m,
                "size_source": detail.size_source,
                "manufacturer": detail.manufacturer,
                "bus": detail.bus,
                "operator": detail.operator,
                "parent": detail.parent,
                "parent_norad_id": parent_id,
                "parent_name": parent_name,
                "source": "Mass and span COMSPOC-adopted, else GCAT; manufacturer, bus, "
                          "operator and parent from GCAT (J. McDowell); via Spacebook",
                "fetched_at": _iso(self._det.fetched_at),
            }
        return {
            "norad_id": entry.norad_id,
            "name": entry.name,
            "intl_designator": entry.intl_id,
            "owner_code": entry.owner,
            "owner": owner_name(entry.owner),
            "object_type": entry.object_type,
            "object_type_label": TYPE_LABELS[entry.object_type],
            "status": status_label(entry),
            "on_orbit": entry.on_orbit,
            "launch_date": entry.launch_date,
            "launch_site_code": entry.launch_site,
            "launch_site": site_name(entry.launch_site),
            "decay_date": entry.decay_date,
            "rcs_size": entry.rcs_size,
            "rcs_label": RCS_LABELS[entry.rcs_size],
            "spacebook_id": entry.guid,
            "summary": summary(entry),
            "notes": notes_for(entry),
            "details": details,
            "details_status": self.details_status(),
            **self.provenance(),
        }

    def facets(self, on_orbit: bool = True) -> dict[str, Any]:
        """Counts by owner, type, launch decade and year, and RCS size."""
        with self._lock:
            cached = self._facets.get(on_orbit)
            entries = self._entries
        if cached is not None:
            return cached
        owners: Counter[str] = Counter()
        types: Counter[str] = Counter()
        rcs: Counter[str] = Counter()
        years: Counter[int] = Counter()
        total = 0
        for entry in entries.values():
            if on_orbit and not entry.on_orbit:
                continue
            total += 1
            owners[entry.owner] += 1
            types[entry.object_type] += 1
            rcs[entry.rcs_size] += 1
            years[entry.launch_year] += 1
        no_date = years.pop(0, 0)
        decades: Counter[int] = Counter()
        for year, count in years.items():
            decades[year - year % 10] += count
        result: dict[str, Any] = {
            "total": total,
            "on_orbit_only": on_orbit,
            "owners": [
                {"code": code, "name": owner_name(code), "count": count}
                for code, count in sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "types": [
                {"code": code, "name": TYPE_LABELS[code], "count": types.get(code, 0)}
                for code in OBJECT_TYPES
            ],
            "rcs": [
                {"code": code, "name": RCS_LABELS[code], "count": rcs.get(code, 0)}
                for code in RCS_SIZES
            ],
            "decades": [
                {"code": f"{d}s", "year_from": d, "year_to": d + 9, "count": decades[d]}
                for d in sorted(decades)
            ],
            "years": [{"year": y, "count": years[y]} for y in sorted(years)],
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None,
            "no_launch_date": no_date,
            **self.provenance(),
        }
        with self._lock:
            if self._entries is entries:
                self._facets[on_orbit] = result
        return result

    def match_ids(
        self,
        owners: Iterable[str] | None = None,
        types: Iterable[str] | None = None,
        rcs: Iterable[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        on_orbit: bool = True,
    ) -> list[int]:
        """NORAD ids matching every filter given, ascending. Codes must be normalized."""
        owner_set = {o for o in owners} if owners else None
        type_set = set(types) if types else None
        rcs_set = set(rcs) if rcs else None
        years = year_from is not None or year_to is not None
        lo = year_from if year_from is not None else 0
        hi = year_to if year_to is not None else 9999
        out: list[int] = []
        for nid, entry in self._entries.items():
            if on_orbit and entry.decay_date:
                continue
            if owner_set is not None and entry.owner not in owner_set:
                continue
            if type_set is not None and entry.object_type not in type_set:
                continue
            if rcs_set is not None and entry.rcs_size not in rcs_set:
                continue
            if years and not (entry.launch_year and lo <= entry.launch_year <= hi):
                continue
            out.append(nid)
        out.sort()
        return out

    def status(self) -> dict[str, Any]:
        """Store state for GET /api/satcat/status. Holds no local paths."""
        on_orbit = sum(1 for e in self._entries.values() if e.on_orbit)
        available = bool(self._entries)
        hint = ""
        if not available:
            hint = (
                "No SATCAT loaded. The server downloads it from Spacebook at startup into "
                f"{SATCAT_DIR_ENV} (default: the user cache folder, sda/satcat). Offline, run "
                "`python -m sda.satcat refresh` once with network, or set "
                "SDA_SPACETRACK_USER and SDA_SPACETRACK_PASS for the Space-Track fallback."
            )
        return {
            "available": available,
            "count": len(self._entries),
            "on_orbit": on_orbit,
            **self.provenance(),
            "load_seconds": round(self._cat.load_seconds, 3),
            "last_error": self._cat.last_error,
            "details": {
                "status": self.details_status(),
                "count": len(self._details),
                "fetched_at": _iso(self._det.fetched_at),
                "load_seconds": round(self._det.load_seconds, 3),
                "last_error": self._det.last_error,
                "network": self._details_net(None),
            },
            "hint": hint,
        }


# ---------------------------------------------------------------------------
# Process-wide store
# ---------------------------------------------------------------------------

_store: SatcatStore | None = None
_store_pinned = False
_store_lock = threading.Lock()


def get_store() -> SatcatStore:
    """The store for the current SDA_SATCAT_DIR (a new one if the variable changed).

    A store installed with set_store() is returned as is until set_store(None).
    """
    global _store
    with _store_lock:
        if _store is None or (not _store_pinned and _store.directory != satcat_dir()):
            _store = SatcatStore()
        return _store


def set_store(store: SatcatStore | None) -> None:
    """Install a store (tests), or forget the current one with None."""
    global _store, _store_pinned
    with _store_lock:
        _store = store
        _store_pinned = store is not None


async def refresh_forever(
    allow_network: bool | None = None, interval_s: float = 3600.0
) -> None:
    """Startup and hourly check for the app lifespan.

    Loads the cache (no network while it is under a day old), downloads when it
    is older, and checks again every ``interval_s``. The work runs in a thread so
    the event loop keeps serving. Cancel the task on shutdown.
    """
    while True:
        store = get_store()
        try:
            outcome = await asyncio.to_thread(store.ensure_loaded, allow_network)
            logger.info("satcat: %s (%d records)", outcome, len(store))
        except Exception:
            logger.exception("satcat: refresh failed")
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _print_status(store: SatcatStore) -> None:
    print(f"cache: {store.directory}")
    print(json.dumps(store.status(), indent=2, ensure_ascii=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sda.satcat",
                                     description="SATCAT cache for the object dossier")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="load the cache (no network) and print its status")
    refresh = sub.add_parser("refresh", help="download if the cache is over a day old")
    refresh.add_argument("--details", action="store_true",
                         help="also refresh the 70 MB physical details file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = SatcatStore()
    if args.command == "status":
        store.load_cache()
        _print_status(store)
        return 0 if len(store) else 1
    outcome = store.ensure_loaded(allow_network=True)
    print(f"catalog: {outcome}")
    if args.details:
        print(f"details: {store.ensure_details(allow_network=True)}")
    _print_status(store)
    return 0 if len(store) else 1


if __name__ == "__main__":
    raise SystemExit(main())
