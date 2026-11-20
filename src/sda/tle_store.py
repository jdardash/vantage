"""In-memory TLE catalog with staleness tracking."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sgp4.api import WGS72, Satrec

from sda.models import TLEFreshness, TLERecord
from sda.omm import omm_to_tle_text, parse_omm_json
from sda.propagator import jd_to_datetime

# Staleness thresholds (hours)
FRESH_THRESHOLD_HOURS = 48.0      # < 2 days
AGING_THRESHOLD_HOURS = 168.0     # < 7 days
STALE_THRESHOLD_HOURS = 336.0     # < 14 days
# > 14 days = EXPIRED

# TLE columns 3-7: up to five digits, or alpha-5 (one letter then four digits) for
# catalog numbers 100,000 to 339,999. The letters skip I and O, so A is 10 and Z is 33.
_ALPHA5_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_CATALOG_FIELD = re.compile(r"^(?:([0-9]{1,5})|([A-HJ-NP-Z])([0-9]{4}))$")


def decode_catalog_field(field: str) -> int:
    """Catalog number from TLE columns 3-7: "25544" is 25544, "A0005" is 100005.

    Raises ValueError for any other shape. Satrec.twoline2rv decodes letters it
    should not ("XXXXX" comes back as 310000), so the field is checked here too.
    """
    match = _CATALOG_FIELD.match(field.strip())
    if match is None:
        raise ValueError(f"invalid TLE catalog number {field!r}")
    digits, letter, rest = match.groups()
    if digits is not None:
        return int(digits)
    return (_ALPHA5_LETTERS.index(letter) + 10) * 10_000 + int(rest)


class TLEStore:
    def __init__(self) -> None:
        self._catalog: dict[int, TLERecord] = {}
        # Where each element set came from ("celestrak", "satnogs", "spacebook",
        # "bundled"), keyed like _catalog. A set loaded without a source (a user
        # upload, a test) has no entry. Read only through source_of/source_counts,
        # which look at _catalog too, so a set removed some other way never keeps
        # a stale label.
        self._sources: dict[int, str] = {}

    def upsert(self, tle: TLERecord, source: str | None = None) -> None:
        """Insert or replace the element set for tle.norad_id.

        *source* labels where this set came from. A set without one replaces any
        label the old set had, since the label describes the elements, not the object.
        """
        self._catalog[tle.norad_id] = tle
        if source is None:
            self._sources.pop(tle.norad_id, None)
        else:
            self._sources[tle.norad_id] = source

    def get(self, norad_id: int) -> TLERecord | None:
        return self._catalog.get(norad_id)

    def get_all(self) -> list[TLERecord]:
        return list(self._catalog.values())

    def remove(self, norad_id: int) -> bool:
        self._sources.pop(norad_id, None)
        return self._catalog.pop(norad_id, None) is not None

    def source_of(self, norad_id: int) -> str | None:
        """Where the held element set for this object came from, None when unlabelled."""
        return self._sources.get(norad_id) if norad_id in self._catalog else None

    def source_counts(self) -> dict[str, int]:
        """How many held element sets came from each source; unlabelled sets are left out."""
        counts: dict[str, int] = {}
        for norad_id, source in self._sources.items():
            if norad_id in self._catalog:
                counts[source] = counts.get(source, 0) + 1
        return dict(sorted(counts.items()))

    def count(self) -> int:
        return len(self._catalog)

    def get_freshness(self, norad_id: int) -> TLEFreshness | None:
        """Check TLE freshness for a satellite."""
        tle = self._catalog.get(norad_id)
        if tle is None:
            return None
        return compute_freshness(tle)

    def get_all_freshness(self) -> list[TLEFreshness]:
        """Get freshness status for all tracked satellites."""
        return [compute_freshness(tle) for tle in self._catalog.values()]

    def get_stale_satellites(
        self, threshold_hours: float = STALE_THRESHOLD_HOURS
    ) -> list[TLEFreshness]:
        """Get satellites with TLEs older than threshold."""
        results = []
        now = datetime.now(UTC)
        for tle in self._catalog.values():
            epoch = tle.epoch.replace(tzinfo=UTC) if tle.epoch.tzinfo is None else tle.epoch
            age_hours = (now - epoch).total_seconds() / 3600.0
            if age_hours > threshold_hours:
                results.append(compute_freshness(tle))
        return results

    def load_from_text(self, raw: str, source: str | None = None) -> int:
        """Parse 2-line or 3-line TLE sets from raw text. Returns count ingested.

        Catalog numbers above 99,999 in alpha-5 form ("A0001" is 100001) are read
        as integers. OMM JSON (one GP record or a list of them, as CelesTrak and
        Space-Track publish) is accepted too: load_from_omm converts it to TLE
        lines and hands them back here, so twoline2rv still validates every set.
        *source*, when given, labels every set ingested (see upsert).
        """
        records = parse_omm_json(raw)
        if records is not None:
            return self.load_from_omm(records, source=source)
        lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
        ingested = 0
        i = 0
        while i < len(lines):
            # Detect if current line is a TLE line 1
            name: str | None
            if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                line1, line2 = lines[i], lines[i + 1]
                name = None  # named from the decoded catalog number below
                i += 2
            elif (
                i + 2 < len(lines)
                and lines[i + 1].startswith("1 ")
                and lines[i + 2].startswith("2 ")
            ):
                name = lines[i]
                line1, line2 = lines[i + 1], lines[i + 2]
                i += 3
            else:
                i += 1
                continue

            try:
                sat = Satrec.twoline2rv(line1, line2, WGS72)
                # Alpha-5 ("A0005" is 100005) is read as an integer. int() of the raw
                # field raised on the letter and the record was dropped without a word.
                norad_id = decode_catalog_field(line1[2:7])
                if norad_id != sat.satnum:
                    continue  # python-sgp4 read the field differently: malformed
                # Convert JD epoch to datetime
                epoch = jd_to_datetime(sat.jdsatepoch, sat.jdsatepochF)

                record = TLERecord(
                    norad_id=norad_id,
                    name=name if name is not None else f"OBJECT {norad_id}",
                    line1=line1,
                    line2=line2,
                    epoch=epoch,
                )
                self.upsert(record, source=source)
                ingested += 1
            except Exception:
                continue

        return ingested

    def load_from_omm(
        self,
        raw: str | bytes | Mapping[str, Any] | Iterable[Any],
        source: str | None = None,
    ) -> int:
        """Ingest OMM JSON: a body, one GP record, or a list of them. Returns count ingested.

        sda.omm converts each record to TLE lines (alpha-5 above 99,999) and the
        lines go through load_from_text, so Satrec.twoline2rv() validates every
        element set (CLAUDE.md rule 4). Records a TLE cannot carry (catalog numbers
        above 339,999, non-SGP4 elements) are skipped, counted and logged there.
        """
        conversion = omm_to_tle_text(raw)
        return self.load_from_text(conversion.text, source=source) if conversion.text else 0


def compute_freshness(tle: TLERecord) -> TLEFreshness:
    """Compute TLE freshness status."""
    now = datetime.now(UTC)
    epoch = tle.epoch.replace(tzinfo=UTC) if tle.epoch.tzinfo is None else tle.epoch
    age_seconds = (now - epoch).total_seconds()
    age_hours = age_seconds / 3600.0
    age_days = age_hours / 24.0

    if age_hours < FRESH_THRESHOLD_HOURS:
        freshness = "FRESH"
        warning = None
    elif age_hours < AGING_THRESHOLD_HOURS:
        freshness = "AGING"
        warning = "TLE is aging; propagation accuracy may be reduced"
    elif age_hours < STALE_THRESHOLD_HOURS:
        freshness = "STALE"
        warning = "TLE is stale; propagation accuracy significantly degraded"
    else:
        freshness = "EXPIRED"
        warning = "TLE is expired; results are unreliable — update immediately"

    return TLEFreshness(
        norad_id=tle.norad_id,
        name=tle.name,
        epoch=tle.epoch,
        age_hours=round(age_hours, 1),
        age_days=round(age_days, 1),
        freshness=freshness,
        accuracy_warning=warning,
    )
