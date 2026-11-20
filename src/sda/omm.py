"""OMM JSON ingest: CCSDS Orbit Mean-Elements Messages turned into TLE lines.

Objects cataloged since July 2026 carry six-digit catalog numbers, and CelesTrak
and Space-Track may publish those only as OMM (GP JSON). The catalog here stays
TLE shaped: each record is initialised with python-sgp4 (``sgp4.omm.initialize``,
WGS72, the same 'i' mode as ``twoline2rv``), written back out as a TLE by
``sgp4.exporter.export_tle`` (alpha-5 above 99,999), and the text then goes
through ``TLEStore.load_from_text`` so ``Satrec.twoline2rv()`` validates every
element set (CLAUDE.md rule 4). The browser reads TLE lines (satellite.js 5.0.0
keeps the catalog field as a string), so it needs no change.

Limits. Each skipped record is counted by reason and the counts are logged once
per batch; nothing is dropped silently.

* A TLE carries catalog numbers up to 339,999 (alpha-5 "Z9999"), and python-sgp4
  2.27 refuses anything larger. Such records are skipped.
* Only SGP4 mean elements are accepted: EPHEMERIS_TYPE 0 and, when the record
  names them, CENTER_NAME EARTH, REF_FRAME TEME, TIME_SYSTEM UTC and
  MEAN_ELEMENT_THEORY SGP4. SGP4-XP elements (type 4) are skipped.
* TLE fields are fixed width: ELEMENT_SET_NO wraps at 10,000 and REV_AT_EPOCH at
  100,000, as the TLE products of both publishers do. A value that still does not
  fit its column (a drag term out of range) skips the record.
* ``sgp4.omm.initialize`` reads EPOCH only with fractional seconds; the epoch is
  normalised first, so "2026-11-18T12:00:00" and a trailing "Z" are accepted.
* A caller may pass ``not_after`` to skip epochs later than a cutoff. COMSPOC
  Spacebook publishes a few deep-space sets (the VELA series among them) with
  epochs days ahead of the present; sda.api skips those more than a day ahead.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sgp4 import omm as sgp4_omm
from sgp4.api import WGS72, Satrec
from sgp4.exporter import export_tle

# Same logger as sda.api: under uvicorn only the uvicorn loggers have handlers.
logger = logging.getLogger("uvicorn.error")

#: Largest catalog number a TLE can carry (alpha-5 "Z9999"); python-sgp4 refuses more.
MAX_TLE_NORAD_ID = 339_999

#: Fields with no sensible default: a record without any of them is skipped.
REQUIRED_FIELDS = (
    "NORAD_CAT_ID",
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
)

#: Metadata that, when present, must say these are SGP4 mean elements.
_EXPECTED_METADATA = {
    "CENTER_NAME": "EARTH",
    "REF_FRAME": "TEME",
    "TIME_SYSTEM": "UTC",
    "MEAN_ELEMENT_THEORY": "SGP4",
}

# International designator as OMM writes it, "1998-067A"; anything else leaves the
# TLE field blank rather than spilling past its eight columns.
_OBJECT_ID = re.compile(r"^(\d{4})-(\d{3})([A-Z]{1,3})$")
# The cheap test for "might be JSON" before a parse is attempted.
_JSON_START = re.compile(r"[﻿\s]*[\[{]")

SKIP_NOT_A_RECORD = "not a JSON object"
SKIP_MISSING_FIELD = "missing a required field"
SKIP_ID_TOO_LARGE = "catalog number above 339999"
SKIP_NOT_SGP4 = "not SGP4 mean elements"
SKIP_BAD_EPOCH = "unreadable EPOCH"
SKIP_BAD_VALUE = "value a TLE cannot carry"
SKIP_FUTURE_EPOCH = "EPOCH too far in the future"


class OMMRecordSkipped(ValueError):
    """One OMM record that cannot become a TLE. The message is the counted reason."""


@dataclass
class OMMConversion:
    """TLE text built from a batch of OMM records, and what was left out."""

    text: str = ""
    converted: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def records(self) -> int:
        return self.converted + sum(self.skipped.values())


def parse_omm_json(raw: str | bytes) -> list[Any] | None:
    """The records in an OMM JSON body, or None when the body is not one.

    A JSON list is a catalog and a JSON object a single record. Anything else (TLE
    text, an HTML error page served with HTTP 200, a bare JSON string or number) is
    None, so a caller can go on to read the body as TLE lines.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not _JSON_START.match(raw):
        return None
    try:
        payload = json.loads(raw.lstrip().lstrip("﻿"))
    except ValueError:
        return None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return None


def normalise_epoch(value: object) -> str:
    """EPOCH in the one layout sgp4.omm reads: ``YYYY-MM-DDTHH:MM:SS.ffffff``, UTC."""
    if not isinstance(value, str) or not value.strip():
        raise OMMRecordSkipped(SKIP_BAD_EPOCH)
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1]
    try:
        epoch = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OMMRecordSkipped(SKIP_BAD_EPOCH) from exc
    if epoch.tzinfo is not None:
        epoch = epoch.astimezone(UTC).replace(tzinfo=None)
    # A TLE writes a two-digit year read as 1957 to 2056.
    if not 1957 <= epoch.year <= 2056:
        raise OMMRecordSkipped(SKIP_BAD_EPOCH)
    return epoch.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"not finite: {value!r}")
    return number


def _integer(value: object) -> int:
    number = _number(value)
    if not number.is_integer():
        raise ValueError(f"not an integer: {value!r}")
    return int(number)


def _optional(record: Mapping[str, Any], key: str, default: Any) -> Any:
    value = record.get(key)
    return default if _blank(value) else value


def _intldesg(object_id: object) -> str:
    """OMM OBJECT_ID in the form sgp4.omm turns into the 8-column TLE designator."""
    match = _OBJECT_ID.match(str(object_id).strip().upper()) if object_id else None
    return match.group(0) if match else ""


def _naive_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is None else moment.astimezone(UTC).replace(tzinfo=None)


def omm_record_to_tle(
    record: object, not_after: datetime | None = None
) -> tuple[str, str, str]:
    """(name, line 1, line 2) for one OMM JSON record. Raises OMMRecordSkipped.

    *not_after* (UTC; a naive value is read as UTC) skips a record whose EPOCH is
    later than it.
    """
    if not isinstance(record, Mapping):
        raise OMMRecordSkipped(SKIP_NOT_A_RECORD)
    if any(_blank(record.get(key)) for key in REQUIRED_FIELDS):
        raise OMMRecordSkipped(SKIP_MISSING_FIELD)
    for key, expected in _EXPECTED_METADATA.items():
        value = record.get(key)
        if not _blank(value) and str(value).strip().upper() != expected:
            raise OMMRecordSkipped(SKIP_NOT_SGP4)

    epoch = normalise_epoch(record["EPOCH"])
    if not_after is not None and datetime.fromisoformat(epoch) > _naive_utc(not_after):
        raise OMMRecordSkipped(SKIP_FUTURE_EPOCH)
    try:
        norad_id = _integer(record["NORAD_CAT_ID"])
        ephemeris_type = _integer(_optional(record, "EPHEMERIS_TYPE", 0))
        element_set_no = _integer(_optional(record, "ELEMENT_SET_NO", 999))
        rev_at_epoch = _integer(_optional(record, "REV_AT_EPOCH", 0))
        mean_motion = _number(record["MEAN_MOTION"])
        eccentricity = _number(record["ECCENTRICITY"])
        inclination = _number(record["INCLINATION"])
        angles = [
            _number(record[key])
            for key in ("RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY")
        ]
        # Drag terms default to zero, as satvis does (customSatellites.ts:71-81, MIT).
        drag = {
            key: _number(_optional(record, key, 0.0))
            for key in ("BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT")
        }
    except (TypeError, ValueError) as exc:
        raise OMMRecordSkipped(SKIP_BAD_VALUE) from exc

    if norad_id > MAX_TLE_NORAD_ID:
        raise OMMRecordSkipped(SKIP_ID_TOO_LARGE)
    if ephemeris_type != 0:
        raise OMMRecordSkipped(SKIP_NOT_SGP4)
    classification = str(_optional(record, "CLASSIFICATION_TYPE", "U")).strip().upper()
    if (
        norad_id < 0
        or element_set_no < 0
        or rev_at_epoch < 0
        or mean_motion <= 0.0
        or not 0.0 <= eccentricity < 1.0
        or not 0.0 <= inclination <= 180.0
        or not all(0.0 <= angle <= 360.0 for angle in angles)
        or len(classification) != 1
        or not classification.isalpha()
    ):
        raise OMMRecordSkipped(SKIP_BAD_VALUE)

    fields: dict[str, Any] = {
        "OBJECT_ID": _intldesg(record.get("OBJECT_ID")),
        "CLASSIFICATION_TYPE": classification,
        "EPHEMERIS_TYPE": 0,
        "ELEMENT_SET_NO": element_set_no % 10_000,
        "REV_AT_EPOCH": rev_at_epoch % 100_000,
        "EPOCH": epoch,
        "NORAD_CAT_ID": norad_id,
        "MEAN_MOTION": mean_motion,
        "ECCENTRICITY": eccentricity,
        "INCLINATION": inclination,
        "RA_OF_ASC_NODE": angles[0],
        "ARG_OF_PERICENTER": angles[1],
        "MEAN_ANOMALY": angles[2],
        **drag,
    }
    sat = Satrec()
    try:
        sgp4_omm.initialize(sat, fields, gravconst=WGS72)
        if sat.error:
            raise ValueError(f"sgp4init error {sat.error}")
        line1, line2 = export_tle(sat)
    except (TypeError, ValueError) as exc:
        raise OMMRecordSkipped(SKIP_BAD_VALUE) from exc
    # A value too wide for its column pushes the line past 69 characters, and
    # twoline2rv reads fixed columns, so it would be misread rather than refused.
    if len(line1) != 69 or len(line2) != 69:
        raise OMMRecordSkipped(SKIP_BAD_VALUE)

    name = " ".join(str(record.get("OBJECT_NAME") or "").split())
    return name or f"OBJECT {norad_id}", line1, line2


def omm_to_tle_text(
    raw: str | bytes | Mapping[str, Any] | Iterable[Any],
    not_after: datetime | None = None,
    log_skips: bool = True,
) -> OMMConversion:
    """Convert OMM JSON (a body, one record or a list of records) to 3-line TLE text.

    The text is meant for ``TLEStore.load_from_text``, which validates every
    element set with ``Satrec.twoline2rv()``. A body that is not OMM JSON converts
    to nothing. Records with an EPOCH later than *not_after* are skipped. Skips are
    logged as one warning unless *log_skips* is False, for a caller that reports
    result.skipped in its own log line.
    """
    records: Iterable[Any]
    if isinstance(raw, (str, bytes)):
        parsed = parse_omm_json(raw)
        records = parsed if parsed is not None else []
    elif isinstance(raw, Mapping):
        records = [raw]
    else:
        records = raw

    result = OMMConversion()
    blocks: list[str] = []
    for record in records:
        try:
            name, line1, line2 = omm_record_to_tle(record, not_after)
        except OMMRecordSkipped as exc:
            result.skipped[str(exc)] += 1
            continue
        blocks.append(f"{name}\n{line1}\n{line2}\n")
        result.converted += 1
    result.text = "".join(blocks)
    if result.skipped and log_skips:
        logger.warning(
            "OMM: %d of %d records converted to TLE lines; skipped %s",
            result.converted,
            result.records,
            ", ".join(f"{count} {reason}" for reason, count in result.skipped.most_common()),
        )
    return result
