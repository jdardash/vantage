"""Sanctions lists for the globe: listed vessels and aircraft, matched by identifier.

Two keyless public lists, refreshed at most once a day:

* OFAC SDN (US Treasury), the legacy CSV pair ``sdn.csv`` and ``sdn_comments.csv``
  from treasury.gov. Vessels carry their IMO number and often an MMSI in the
  remarks ("Vessel Registration Identification IMO 9187629; MMSI 572469210");
  aircraft carry the tail number, manufacturer serial (MSN), construction number
  and sometimes the Mode S transponder code in the remarks. A remark too long
  for the CSV column continues in ``sdn_comments.csv`` under the same entry
  number. Public domain (US Government work).
* UK Sanctions List (FCDO), ``UK-Sanctions-List.csv``. Only the rows whose
  designation type is Ship are kept: about 700 ships, nearly all with an IMO
  number, most under the Russia regime. Open Government Licence v3.0. The file
  is about 50 MB, so it is streamed to disk, parsed and deleted; set
  ``SDA_SANCTIONS_UK=0`` to skip it.

The OFSI Consolidated List this module would once have read closed in January
2026 in favour of the UK Sanctions List. The EU consolidated list needs a token
in its URL, so it is not used.

Parsed records are kept in ``sanctions.json`` under ``SDA_SANCTIONS_DIR``
(default ``%LOCALAPPDATA%/sda/sanctions`` on Windows, ``~/.cache/sda/sanctions``
elsewhere). The cache is reused while it is under a day old; a download is tried
at most once an hour (``sanctions.asked`` holds the time of the last try), and a
source that fails keeps its previous records. ``SDA_OFFLINE=1`` stops every
download.

A match is a match on an identifier: IMO number or MMSI for a vessel, tail
number, Mode S code or serial for an aircraft. A vessel name alone is never
reported as listed; see ``SanctionsStore.vessel_name_match`` for the separate,
weaker signal the maritime layer shows as unconfirmed.

Command line::

    python -m sda.sanctions status
    python -m sda.sanctions refresh
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SANCTIONS_DIR_ENV = "SDA_SANCTIONS_DIR"
UK_ENV = "SDA_SANCTIONS_UK"
OFFLINE_ENV = "SDA_OFFLINE"

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
OFAC_COMMENTS_URL = "https://www.treasury.gov/ofac/downloads/sdn_comments.csv"
UK_LIST_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"
OFAC_DETAIL_URL = "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={uid}"
UK_LIST_PAGE = "https://www.gov.uk/government/publications/the-uk-sanctions-list"

OFAC = "ofac"
UK = "uk"
SOURCE_NAMES = {OFAC: "OFAC SDN", UK: "UK Sanctions List"}
SOURCE_LICENCES = {
    OFAC: "US Government work, public domain",
    UK: "Open Government Licence v3.0",
}
ATTRIBUTION = (
    "Sanctions: OFAC Specially Designated Nationals list (US Treasury) and the UK "
    "Sanctions List (FCDO, Open Government Licence v3.0)."
)

CACHE_FILE = "sanctions.json"
ASKED_FILE = "sanctions.asked"
CACHE_VERSION = 1
MAX_AGE_S = 24 * 3600.0
RETRY_S = 3600.0
DOWNLOAD_TIMEOUT_S = 180.0
USER_AGENT = "vantage/0.2 (sanctions list)"

#: The download hook: fetch *url* into the file *dest*. Tests substitute it.
Fetcher = Callable[[str, Path], None]


# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------


def sanctions_dir() -> Path:
    """Cache directory: SDA_SANCTIONS_DIR, else the user cache dir (as sda.satcat)."""
    configured = os.environ.get(SANCTIONS_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "sda" / "sanctions"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "sda" / "sanctions"


def network_allowed() -> bool:
    """False under SDA_OFFLINE, as api.py reads it."""
    return os.environ.get(OFFLINE_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def uk_enabled() -> bool:
    return os.environ.get(UK_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SPACES = re.compile(r"\s+")
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def clean_text(value: object) -> str:
    """One line of plain text: every run of whitespace (NBSP included) to a space."""
    text = str(value or "")
    return _SPACES.sub(" ", text.replace("\u202f", " ").replace("\xa0", " ")).strip()


def norm_reg(value: object) -> str:
    """Registration or serial key: upper case letters and digits only (EP-GOL -> EPGOL)."""
    return _NOT_ALNUM.sub("", str(value or "").upper())


def norm_imo(value: object) -> str:
    """Seven-digit IMO number from 'IMO 9187629', '9187629' or 'IMO9187629'; else ''."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 7 else ""


def norm_mmsi(value: object) -> str:
    """Nine-digit MMSI; else ''."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 9 else ""


def norm_hex(value: object) -> str:
    """ICAO 24-bit address as six upper-case hex digits; else ''."""
    text = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return text if len(text) == 6 else ""


def norm_name(value: object) -> str:
    """Vessel name key: letters and digits only, upper case."""
    return _NOT_ALNUM.sub("", clean_text(value).upper())


def imo_checksum_ok(imo: str) -> bool:
    """IMO check digit: the weighted sum of the first six digits (7..2), mod 10."""
    if len(imo) != 7 or not imo.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(imo[:6], range(7, 1, -1), strict=True))
    return total % 10 == int(imo[6])


# ---------------------------------------------------------------------------
# OFAC SDN parsing
# ---------------------------------------------------------------------------

_OFAC_NULL = "-0-"
_RE_IMO = re.compile(r"\bIMO\s*(\d{7})\b")
_RE_MMSI = re.compile(r"\bMMSI\s*(\d{9})\b")
_RE_TAIL = re.compile(r"\b(Previous )?Aircraft Tail Number\s+([A-Za-z0-9-]+)")
_RE_MSN = re.compile(r"Aircraft Manufacturer's Serial Number \(MSN\)\s+([A-Za-z0-9/-]+)")
_RE_CONSTRUCTION = re.compile(
    r"Aircraft Construction Number \(also called L/N or S/N or F/N\)\s+([A-Za-z0-9/-]+)")
_RE_SERIAL_ID = re.compile(r"Aircraft Serial Identification\s+([A-Za-z0-9/-]+)")
_RE_MODE_S = re.compile(r"Aircraft Mode S Transponder Code\s+([0-9A-Fa-f]{6})\b")
_RE_FIELD = {
    "model": re.compile(r"Aircraft Model\s+([^;]+)"),
    "operator": re.compile(r"Aircraft Operator\s+([^;]+)"),
    "manufactured": re.compile(r"Aircraft Manufacture Date\s+([^;]+)"),
}
_RE_ALIAS = re.compile(r"\b[afn]\.k\.a\. '([^']+)'")
_RE_REG_NAME = re.compile(r"^[A-Z0-9]{1,3}-?[A-Z0-9]{2,6}$")


def _ofac_value(value: str) -> str:
    text = clean_text(value)
    return "" if text == _OFAC_NULL else text


def _strip_period(value: str) -> str:
    return value.strip().rstrip(".").strip()


def _ofac_rows(text: str) -> Iterable[list[str]]:
    # The files end with a DOS end-of-file byte (0x1A) after the last record.
    for row in csv.reader(io.StringIO(text.replace("\x1a", ""))):
        if row and row[0].strip().isdigit():
            yield row


def parse_ofac(sdn_text: str, comments_text: str = "") -> list[dict]:
    """Vessel and aircraft records from the SDN CSV pair; other entries are skipped.

    Columns: ent_num, name, type, programme, title, call sign, vessel type,
    tonnage, GRT, vessel flag, vessel owner, remarks. ``-0-`` means empty.
    """
    overflow: dict[str, str] = {}
    for row in _ofac_rows(comments_text):
        if len(row) >= 2:
            overflow[row[0].strip()] = overflow.get(row[0].strip(), "") + row[1]

    records: list[dict] = []
    for row in _ofac_rows(sdn_text):
        if len(row) < 12:
            continue
        kind = clean_text(row[2]).lower()
        if kind not in ("vessel", "aircraft"):
            continue
        uid = row[0].strip()
        remark_raw = ("" if clean_text(row[11]) == _OFAC_NULL else row[11]) + overflow.get(uid, "")
        remarks = clean_text(remark_raw)
        programmes = [p.strip() for p in _ofac_value(row[3]).split("] [") if p.strip()]
        name = _ofac_value(row[1])
        rec: dict[str, Any] = {
            "source": OFAC, "uid": uid, "kind": kind, "name": name,
            "aliases": sorted({clean_text(a) for a in _RE_ALIAS.findall(remarks)}),
            "programmes": programmes, "remarks": remarks,
            "imo": "", "mmsi": "", "regs": [], "previous_regs": [], "serials": [],
            "mode_s": "", "details": {},
        }
        if kind == "vessel":
            imo = _RE_IMO.search(remarks)
            mmsi = _RE_MMSI.search(remarks)
            rec["imo"] = imo.group(1) if imo else ""
            rec["mmsi"] = mmsi.group(1) if mmsi else ""
            details = {
                "call_sign": _ofac_value(row[5]), "vessel_type": _ofac_value(row[6]),
                "tonnage": _ofac_value(row[7]), "grt": _ofac_value(row[8]),
                "flag": _ofac_value(row[9]), "owner": _ofac_value(row[10]),
            }
        else:
            regs: list[str] = []
            previous: list[str] = []
            for prev, tail in _RE_TAIL.findall(remarks):
                (previous if prev else regs).append(tail.upper())
            if name and " " not in name and _RE_REG_NAME.match(name.upper()) \
                    and not name.upper().startswith("MSN"):
                regs.insert(0, name.upper())
            serials = [_strip_period(s) for s in _RE_MSN.findall(remarks)]
            serials += [_strip_period(s) for s in _RE_CONSTRUCTION.findall(remarks)]
            serials += [_strip_period(s) for s in _RE_SERIAL_ID.findall(remarks)]
            if name.upper().startswith("MSN "):
                serials.insert(0, name[4:].strip())
            mode_s = _RE_MODE_S.search(remarks)
            rec["regs"] = list(dict.fromkeys(r for r in regs if r))
            rec["previous_regs"] = list(dict.fromkeys(r for r in previous if r))
            rec["serials"] = list(dict.fromkeys(s for s in serials if s))
            rec["mode_s"] = mode_s.group(1).upper() if mode_s else ""
            details = {k: _strip_period(m.group(1)) for k, rx in _RE_FIELD.items()
                       if (m := rx.search(remarks))}
        rec["details"] = {k: v for k, v in details.items() if v}
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# UK Sanctions List parsing
# ---------------------------------------------------------------------------

_RE_UK_REGIME = re.compile(r"^The (.+?) \(Sanctions\)")


def uk_programme(regime: str) -> str:
    """'The Russia (Sanctions) (EU Exit) Regulations 2019' -> 'UK RUSSIA'."""
    regime = clean_text(regime)
    m = _RE_UK_REGIME.match(regime)
    short = m.group(1) if m else regime
    return ("UK " + short).upper() if short else "UK"


def _uk_header_index(lines: Iterable[str]) -> Iterable[str]:
    """Skip the 'Report Date: ...' preamble so the header row comes first."""
    started = False
    for line in lines:
        if not started:
            if line.lstrip("\ufeff").startswith("Last Updated,"):
                started = True
            else:
                continue
        yield line


def parse_uk(lines: Iterable[str]) -> list[dict]:
    """Ship records from the UK Sanctions List CSV, one per Unique ID.

    *lines* is any iterable of text lines with their line endings kept (an open
    file with ``newline=''`` is one), so the 50 MB file is never held whole.
    """
    reader = csv.DictReader(_uk_header_index(lines))
    ships: dict[str, dict] = {}
    for row in reader:
        if clean_text(row.get("Designation Type")) != "Ship":
            continue
        uid = clean_text(row.get("Unique ID"))
        if not uid:
            continue
        name = clean_text(row.get("Name 6")) or " ".join(
            clean_text(row.get(f"Name {i}")) for i in range(1, 6)).strip()
        primary = clean_text(row.get("Name type")).lower() == "primary name"
        rec = ships.get(uid)
        if rec is None:
            imposed = [clean_text(p) for p in str(row.get("Sanctions Imposed") or "").split("|")]
            reasons = clean_text(row.get("UK Statement of Reasons"))
            other = clean_text(row.get("Other Information"))
            remarks = " ".join(t for t in (reasons, other) if t)
            rec = {
                "source": UK, "uid": uid, "kind": "vessel", "name": "", "aliases": [],
                "programmes": [uk_programme(str(row.get("Regime Name") or ""))],
                "remarks": remarks, "imo": norm_imo(row.get("IMO number")), "mmsi": "",
                "regs": [], "previous_regs": [], "serials": [], "mode_s": "",
                "details": {k: v for k, v in {
                    "regime": clean_text(row.get("Regime Name")),
                    "sanctions_imposed": "; ".join(dict.fromkeys(p for p in imposed if p)),
                    "flag": clean_text(row.get("Current believed flag of ship")),
                    "previous_flags": clean_text(row.get("Previous flags")),
                    "vessel_type": clean_text(row.get("Type of ship")),
                    "tonnage": clean_text(row.get("Tonnage of ship")),
                    "year_built": clean_text(row.get("Year Built")),
                    "owner": clean_text(row.get("Current owner/operator (s)")),
                    "designated": clean_text(row.get("Date Designated")),
                    "designation_source": clean_text(row.get("Designation source")),
                }.items() if v},
            }
            ships[uid] = rec
        if primary and not rec["name"]:
            rec["name"] = name
        elif name and name not in rec["aliases"]:
            rec["aliases"].append(name)
    out = []
    for rec in ships.values():
        if not rec["name"] and rec["aliases"]:
            rec["name"] = rec["aliases"].pop(0)
        rec["aliases"] = [a for a in rec["aliases"] if a != rec["name"]]
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


def http_fetch(url: str, dest: Path) -> None:
    """Stream *url* into *dest*; raises on any HTTP or network failure."""
    import httpx

    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client, \
            client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from {url}")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
    tmp.replace(dest)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass
class _Index:
    by_imo: dict[str, list[dict]] = field(default_factory=dict)
    by_mmsi: dict[str, list[dict]] = field(default_factory=dict)
    by_vessel_name: dict[str, list[dict]] = field(default_factory=dict)
    by_reg: dict[str, list[dict]] = field(default_factory=dict)
    by_previous_reg: dict[str, list[dict]] = field(default_factory=dict)
    by_hex: dict[str, list[dict]] = field(default_factory=dict)
    by_serial: dict[str, list[dict]] = field(default_factory=dict)


def _add(table: dict[str, list[dict]], key: str, rec: dict) -> None:
    if key:
        bucket = table.setdefault(key, [])
        if rec not in bucket:
            bucket.append(rec)


def build_index(records: list[dict]) -> _Index:
    idx = _Index()
    for rec in records:
        if rec.get("kind") == "vessel":
            _add(idx.by_imo, norm_imo(rec.get("imo")), rec)
            _add(idx.by_mmsi, norm_mmsi(rec.get("mmsi")), rec)
            key = norm_name(rec.get("name"))
            if len(key) >= 4:
                _add(idx.by_vessel_name, key, rec)
        elif rec.get("kind") == "aircraft":
            for reg in rec.get("regs") or []:
                _add(idx.by_reg, norm_reg(reg), rec)
            for reg in rec.get("previous_regs") or []:
                _add(idx.by_previous_reg, norm_reg(reg), rec)
            _add(idx.by_hex, norm_hex(rec.get("mode_s")), rec)
            for serial in rec.get("serials") or []:
                _add(idx.by_serial, norm_reg(serial), rec)
    return idx


def public_record(rec: dict, matched_on: str) -> dict:
    """A record as an API answer: names, programme, remark and a link to verify it."""
    source = str(rec.get("source") or "")
    programmes = list(rec.get("programmes") or [])
    return {
        "source": SOURCE_NAMES.get(source, source),
        "source_id": source,
        "uid": rec.get("uid", ""),
        "kind": rec.get("kind", ""),
        "name": rec.get("name", ""),
        "aliases": list(rec.get("aliases") or []),
        "programme": "; ".join(programmes),
        "programmes": programmes,
        "remarks": rec.get("remarks", ""),
        "imo": rec.get("imo", ""),
        "mmsi": rec.get("mmsi", ""),
        "regs": list(rec.get("regs") or []),
        "previous_regs": list(rec.get("previous_regs") or []),
        "serials": list(rec.get("serials") or []),
        "mode_s": rec.get("mode_s", ""),
        "details": dict(rec.get("details") or {}),
        "matched_on": matched_on,
        "url": OFAC_DETAIL_URL.format(uid=rec.get("uid", "")) if source == OFAC else UK_LIST_PAGE,
    }


class SanctionsStore:
    """Parsed listings, their indexes and the daily refresh."""

    def __init__(self, directory: Path | None = None, fetch: Fetcher | None = None,
                 allow_network: bool | None = None, include_uk: bool | None = None) -> None:
        self.directory = directory if directory is not None else sanctions_dir()
        self._fetch = fetch or http_fetch
        self._allow_network = allow_network
        self._include_uk = include_uk
        self.records: dict[str, list[dict]] = {}
        self.source_meta: dict[str, dict] = {}
        self.fetched_at = 0.0
        self.error: str | None = None
        self.loaded = False
        self._index = _Index()
        self._refresh_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- state ---------------------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return self.directory / CACHE_FILE

    @property
    def asked_path(self) -> Path:
        return self.directory / ASKED_FILE

    def network_ok(self) -> bool:
        return network_allowed() if self._allow_network is None else self._allow_network

    def uk_ok(self) -> bool:
        return uk_enabled() if self._include_uk is None else self._include_uk

    def refreshing(self) -> bool:
        return self._refresh_lock.locked()

    def count(self, kind: str | None = None, source: str | None = None) -> int:
        return sum(1 for src, recs in self.records.items() if source in (None, src)
                   for r in recs if kind in (None, r.get("kind")))

    def _install(self, records: dict[str, list[dict]], meta: dict[str, dict],
                 fetched_at: float) -> None:
        flat = [r for recs in records.values() for r in recs]
        index = build_index(flat)
        self.records = records
        self.source_meta = meta
        self.fetched_at = fetched_at
        self._index = index
        self.loaded = True

    # -- disk ----------------------------------------------------------------

    def load_cache(self) -> bool:
        """Load sanctions.json if present and readable. Never touches the network."""
        self.loaded = True
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return False
        records = data.get("records")
        if not isinstance(records, dict):
            return False
        clean = {str(k): [r for r in v if isinstance(r, dict)]
                 for k, v in records.items() if isinstance(v, list)}
        meta = data.get("sources") if isinstance(data.get("sources"), dict) else {}
        self._install(clean, meta or {}, float(data.get("fetched_at") or 0.0))
        return True

    def _write_cache(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "fetched_at": self.fetched_at,
                   "sources": self.source_meta, "records": self.records}
        tmp = self.cache_path.with_suffix(".json.part")
        tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _last_asked(self) -> float:
        try:
            return float(self.asked_path.read_text(encoding="ascii").strip() or 0.0)
        except (OSError, ValueError):
            return 0.0

    def _mark_asked(self, now: float) -> None:
        with contextlib.suppress(OSError):
            self.directory.mkdir(parents=True, exist_ok=True)
            self.asked_path.write_text(f"{now:.0f}", encoding="ascii")

    # -- refresh -------------------------------------------------------------

    def needs_refresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.fetched_at and now - self.fetched_at < MAX_AGE_S:
            return False
        return now - self._last_asked() >= RETRY_S

    def _download_ofac(self) -> list[dict]:
        self.directory.mkdir(parents=True, exist_ok=True)
        sdn = self.directory / "sdn.csv"
        comments = self.directory / "sdn_comments.csv"
        try:
            self._fetch(OFAC_SDN_URL, sdn)
            try:
                self._fetch(OFAC_COMMENTS_URL, comments)
                comments_text = _read_text(comments)
            except Exception:  # the overflow file is a nice-to-have
                comments_text = ""
            records = parse_ofac(_read_text(sdn), comments_text)
        finally:
            for p in (sdn, comments):
                with contextlib.suppress(OSError):
                    p.unlink()
        if not records:
            raise RuntimeError("the SDN file held no vessels or aircraft")
        return records

    def _download_uk(self) -> list[dict]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "uk-sanctions-list.csv"
        try:
            self._fetch(UK_LIST_URL, path)
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                records = parse_uk(fh)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if not records:
            raise RuntimeError("the UK list held no ships")
        return records

    def refresh(self, force: bool = False) -> str:
        """Download whatever is due; returns 'fresh', 'downloaded', 'partial', 'failed',
        'offline', 'busy' or 'waiting' (a try within the last hour)."""
        if not self.loaded:
            self.load_cache()
        if not force and not self.needs_refresh():
            return "fresh" if self.fetched_at else "waiting"
        if not self.network_ok():
            return "offline"
        if not self._refresh_lock.acquire(blocking=False):
            return "busy"
        try:
            now = time.time()
            self._mark_asked(now)
            records = dict(self.records)
            meta = dict(self.source_meta)
            jobs: list[tuple[str, str, Callable[[], list[dict]]]] = [
                (OFAC, OFAC_SDN_URL, self._download_ofac)]
            if self.uk_ok():
                jobs.append((UK, UK_LIST_URL, self._download_uk))
            errors: list[str] = []
            for source, url, job in jobs:
                try:
                    got = job()
                except Exception as exc:
                    errors.append(f"{SOURCE_NAMES[source]}: {type(exc).__name__}: {exc}")
                    logger.warning("sanctions: %s download failed: %s", source, exc)
                    continue
                records[source] = got
                meta[source] = {"fetched_at": _iso(now), "count": len(got), "url": url}
            self.error = "; ".join(errors) or None
            if len(errors) == len(jobs):
                return "failed"
            self._install(records, meta, now)
            try:
                self._write_cache()
            except OSError as exc:
                self.error = f"cache not written: {exc}"
            return "partial" if errors else "downloaded"
        finally:
            self._refresh_lock.release()

    def ensure_loaded(self) -> None:
        """Load the disk cache once, then start a background download if one is due.

        Never blocks on the network: lookups answer from what is held while the
        thread runs, and say so with ``list_loaded``.
        """
        if not self.loaded:
            self.load_cache()
        if self.refreshing() or not self.network_ok() or not self.needs_refresh():
            return
        thread = threading.Thread(target=self._refresh_quietly, name="sanctions-refresh",
                                  daemon=True)
        self._thread = thread
        thread.start()

    def _refresh_quietly(self) -> None:
        try:
            outcome = self.refresh()
            logger.info("sanctions: %s (%d records)", outcome, self.count())
        except Exception:
            logger.exception("sanctions: refresh failed")

    def wait(self, timeout_s: float = 30.0) -> None:
        """Join the background download (tests and the command line)."""
        if self._thread is not None:
            self._thread.join(timeout_s)

    # -- lookups ---------------------------------------------------------------

    def _collect(self, hits: list[tuple[str, list[dict]]]) -> list[dict]:
        seen: set[int] = set()
        out: list[dict] = []
        for matched_on, recs in hits:
            for rec in recs:
                if id(rec) in seen:
                    continue
                seen.add(id(rec))
                out.append(public_record(rec, matched_on))
        return out

    def vessel(self, imo: str = "", mmsi: str = "") -> list[dict]:
        """Listings whose IMO number or MMSI equals the one given."""
        idx = self._index
        hits: list[tuple[str, list[dict]]] = []
        if norm_imo(imo):
            hits.append(("imo", idx.by_imo.get(norm_imo(imo), [])))
        if norm_mmsi(mmsi):
            hits.append(("mmsi", idx.by_mmsi.get(norm_mmsi(mmsi), [])))
        return self._collect(hits)

    def vessel_name_match(self, name: str) -> list[dict]:
        """Listed vessels whose current name equals *name* (letters and digits only).

        A name is not an identifier: many ships share one, and a listed ship is
        often renamed. Callers must present this as unconfirmed.
        """
        key = norm_name(name)
        if len(key) < 4:
            return []
        return self._collect([("name", self._index.by_vessel_name.get(key, []))])

    def aircraft(self, reg: str = "", hex_code: str = "", serial: str = "") -> list[dict]:
        """Listings by current tail number, Mode S code or serial."""
        idx = self._index
        hits: list[tuple[str, list[dict]]] = []
        if norm_reg(reg):
            hits.append(("reg", idx.by_reg.get(norm_reg(reg), [])))
        if norm_hex(hex_code):
            hits.append(("hex", idx.by_hex.get(norm_hex(hex_code), [])))
        if norm_reg(serial):
            hits.append(("serial", idx.by_serial.get(norm_reg(serial), [])))
        return self._collect(hits)

    def aircraft_previous_reg(self, reg: str) -> list[dict]:
        """Listings that once carried tail number *reg*. A registration can be
        reissued to another aircraft, so this is a lead, not a match."""
        key = norm_reg(reg)
        return self._collect([("previous_reg", self._index.by_previous_reg.get(key, []))]
                             if key else [])

    def mmsi_set(self) -> frozenset[str]:
        return frozenset(self._index.by_mmsi)

    def index(self, kind: str) -> dict:
        """Compact match keys, so a client can test a whole layer without a call each."""
        idx = self._index
        if kind == "aircraft":
            return {"kind": kind, "regs": sorted(idx.by_reg), "hex": sorted(idx.by_hex)}
        return {"kind": "vessel", "imo": sorted(idx.by_imo), "mmsi": sorted(idx.by_mmsi)}

    def summary(self) -> dict:
        now = time.time()
        sources = []
        for source in (OFAC, UK):
            if source == UK and not self.uk_ok() and source not in self.records:
                continue
            meta = self.source_meta.get(source, {})
            sources.append({
                "id": source, "name": SOURCE_NAMES[source], "licence": SOURCE_LICENCES[source],
                "url": OFAC_SDN_URL if source == OFAC else UK_LIST_URL,
                "vessels": self.count("vessel", source),
                "aircraft": self.count("aircraft", source),
                "fetched_at": meta.get("fetched_at", ""),
            })
        age_h = round((now - self.fetched_at) / 3600.0, 2) if self.fetched_at else None
        return {
            "list_loaded": bool(self.records),
            "vessels": self.count("vessel"),
            "aircraft": self.count("aircraft"),
            "vessel_imos": len(self._index.by_imo),
            "vessel_mmsis": len(self._index.by_mmsi),
            "aircraft_regs": len(self._index.by_reg),
            "sources": sources,
            "updated": _iso(self.fetched_at),
            "age_hours": age_h,
            "refresh_every_hours": MAX_AGE_S / 3600.0,
            "refreshing": self.refreshing(),
            "network": self.network_ok(),
            "error": self.error,
            "attribution": ATTRIBUTION,
        }


# ---------------------------------------------------------------------------
# Answers shared by the routes and in-process callers (the aviation layer)
# ---------------------------------------------------------------------------


def _answer(store: SanctionsStore, query: dict, matches: list[dict],
            possible: list[dict] | None = None) -> dict:
    uids = {(m["source_id"], m["uid"]) for m in matches}
    out: dict[str, Any] = {
        "listed": bool(matches),
        "query": query,
        "matches": matches,
        "possible_matches": [p for p in possible or [] if (p["source_id"], p["uid"]) not in uids],
        "list_loaded": bool(store.records),
        "updated": _iso(store.fetched_at),
    }
    if matches:
        first = matches[0]
        out.update(programme=first["programme"], name=first["name"],
                   remarks=first["remarks"], source=first["source"])
    if not store.records:
        out["note"] = ("The sanctions list is not loaded yet; it downloads in the background "
                       "on first use." if store.network_ok() else
                       "The sanctions list is not loaded and downloads are off on this server.")
    return out


def lookup_vessel(imo: str = "", mmsi: str = "", store: SanctionsStore | None = None) -> dict:
    store = store or get_store()
    store.ensure_loaded()
    query = {k: v for k, v in (("imo", norm_imo(imo)), ("mmsi", norm_mmsi(mmsi))) if v}
    return _answer(store, query, store.vessel(imo, mmsi))


def lookup_aircraft(reg: str = "", hex_code: str = "", serial: str = "",
                    store: SanctionsStore | None = None) -> dict:
    """In-process aircraft check: the same answer as GET /api/sanctions/aircraft."""
    store = store or get_store()
    store.ensure_loaded()
    query = {k: v for k, v in (("reg", norm_reg(reg)), ("hex", norm_hex(hex_code)),
                               ("serial", norm_reg(serial))) if v}
    return _answer(store, query, store.aircraft(reg, hex_code, serial),
                   store.aircraft_previous_reg(reg))


# ---------------------------------------------------------------------------
# Process-wide store
# ---------------------------------------------------------------------------

_store: SanctionsStore | None = None
_store_pinned = False
_store_lock = threading.Lock()


def get_store() -> SanctionsStore:
    """The store for the current SDA_SANCTIONS_DIR (a new one if the variable changed).

    A store installed with set_store() is returned as is until set_store(None).
    """
    global _store
    with _store_lock:
        if _store is None or (not _store_pinned and _store.directory != sanctions_dir()):
            _store = SanctionsStore()
        return _store


def set_store(store: SanctionsStore | None) -> None:
    """Install a store (tests), or forget the current one with None."""
    global _store, _store_pinned
    with _store_lock:
        _store = store
        _store_pinned = store is not None


async def refresh_forever(interval_s: float = 3600.0) -> None:
    """Optional lifespan task: load at startup and re-check every hour.

    The routes load lazily without it; with it the first lookup after a restart
    already has the list.
    """
    import asyncio

    while True:
        store = get_store()
        try:
            outcome = await asyncio.to_thread(store.refresh)
            logger.info("sanctions: %s (%d records)", outcome, store.count())
        except Exception:
            logger.exception("sanctions: refresh failed")
        await asyncio.sleep(interval_s)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sda.sanctions",
                                     description="OFAC SDN and UK sanctions list cache")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="load the cache (no network) and print its summary")
    refresh = sub.add_parser("refresh", help="download if the cache is over a day old")
    refresh.add_argument("--force", action="store_true", help="download even if fresh")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = SanctionsStore()
    store.load_cache()
    if args.command == "refresh":
        print(f"refresh: {store.refresh(force=args.force)}")
    print(f"cache: {store.directory}")
    print(json.dumps(store.summary(), indent=2, ensure_ascii=True))
    return 0 if store.records else 1


if __name__ == "__main__":
    raise SystemExit(main())
