"""SATCAT routes behind the globe's object dossier.

Four endpoints, all reads:

* GET /api/satcat/status       what is loaded, from where, how old
* GET /api/satcat/facets       counts by owner, type, launch decade and year, RCS size
* GET /api/satcat/ids          NORAD ids matching owner / type / launch years / RCS
* GET /api/satcat/{norad_id}   the card for one object: whose, what, how big

The store lives in sda.satcat. The first request loads the disk cache if nothing
is in memory yet (never the network); the download itself belongs to the app's
startup task. 404 for an id the catalog does not hold, 503 while the catalog is
empty. Every reply names its source and age and carries the attribution
"COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)". Nothing here feeds Pc or
risk classification.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda.satcat import (
    ATTRIBUTION,
    RCS_SIZES,
    TYPE_ALIASES,
    SatcatStore,
    decode_norad,
    get_store,
)

router = APIRouter(prefix="/api/satcat", tags=["SATCAT"])

_OWNER_CODE = re.compile(r"^[A-Z0-9/-]{1,16}$")
MAX_LIST_ITEMS = 50


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class Provenance(BaseModel):
    source: str = Field(description="spacebook or space-track; empty when nothing is loaded")
    fetched_at: str = Field(description="When the cached copy was downloaded (UTC, ISO 8601)")
    age_hours: float | None
    stale: bool = Field(description="True when a refresh failed and an older copy is in use")
    attribution: str = ATTRIBUTION


class SatcatDetails(BaseModel):
    mass_kg: float | None
    span_m: float | None
    size_source: str = Field(
        description="Where mass and span come from: COMSPOC (adopted), GCAT, both, or empty. "
                    "COMSPOC's 5 kg / 10 m no-data placeholder is never shown."
    )
    manufacturer: str = Field(description="GCAT organisation code")
    bus: str
    operator: str = Field(description="GCAT organisation code of the owner or operator")
    parent: str = Field(description="GCAT id of the object it came from (S + NORAD id or A + id)")
    parent_norad_id: int | None
    parent_name: str
    source: str
    fetched_at: str


class SatcatCard(Provenance):
    norad_id: int
    name: str
    intl_designator: str
    owner_code: str
    owner: str
    object_type: str = Field(description="PAYLOAD, ROCKET BODY, DEBRIS or UNKNOWN")
    object_type_label: str
    status: str = Field(description="On orbit or Decayed")
    on_orbit: bool
    launch_date: str
    launch_site_code: str
    launch_site: str
    decay_date: str
    rcs_size: str = Field(description="SMALL, MEDIUM, LARGE or UNKNOWN")
    rcs_label: str
    spacebook_id: str = Field(description="COMSPOC GUID, the key of per-object Spacebook files")
    summary: str
    notes: list[str]
    details: SatcatDetails | None
    details_status: str = Field(
        description="loaded, loading, stale, unavailable or not requested"
    )


class FacetCount(BaseModel):
    code: str
    name: str
    count: int


class DecadeCount(BaseModel):
    code: str
    year_from: int
    year_to: int
    count: int


class YearCount(BaseModel):
    year: int
    count: int


class SatcatFacets(Provenance):
    total: int
    on_orbit_only: bool
    owners: list[FacetCount]
    types: list[FacetCount]
    rcs: list[FacetCount]
    decades: list[DecadeCount]
    years: list[YearCount]
    year_min: int | None
    year_max: int | None
    no_launch_date: int


class IdFilters(BaseModel):
    owner: list[str]
    type: list[str]
    rcs: list[str]
    year_from: int | None
    year_to: int | None
    on_orbit: bool


class SatcatIds(Provenance):
    count: int
    ids: list[int]
    filters: IdFilters


class DetailsStatus(BaseModel):
    status: str
    count: int
    fetched_at: str
    load_seconds: float
    last_error: str
    network: bool


class SatcatStatus(Provenance):
    available: bool
    count: int
    on_orbit: int
    load_seconds: float
    last_error: str
    details: DetailsStatus
    hint: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _store() -> SatcatStore:
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
                "last_error": status["last_error"],
                "hint": status["hint"],
                "attribution": ATTRIBUTION,
            },
        )


def _split(raw: str | None) -> list[str]:
    if not raw:
        return []
    items = [part.strip().upper() for part in raw.split(",")]
    items = [item for item in items if item]
    if len(items) > MAX_LIST_ITEMS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_LIST_ITEMS} values per filter")
    return items


def _owners(raw: str | None) -> list[str]:
    owners = _split(raw)
    bad = [o for o in owners if not _OWNER_CODE.match(o)]
    if bad:
        raise HTTPException(status_code=422, detail=f"invalid owner code {bad[0]!r}")
    return sorted(set(owners))


def _types(raw: str | None) -> list[str]:
    out: set[str] = set()
    for item in _split(raw):
        code = TYPE_ALIASES.get(item)
        if code is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown type {item!r}; use PAYLOAD, ROCKET BODY, DEBRIS or UNKNOWN",
            )
        out.add(code)
    return sorted(out)


def _rcs(raw: str | None) -> list[str]:
    out: set[str] = set()
    for item in _split(raw):
        if item not in RCS_SIZES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown RCS size {item!r}; use SMALL, MEDIUM, LARGE or UNKNOWN",
            )
        out.add(item)
    return sorted(out)


# --------------------------------------------------------------------------
# Routes (fixed paths before the {norad_id} catch-all)
# --------------------------------------------------------------------------


@router.get("/status", response_model=SatcatStatus)
def satcat_status() -> dict[str, Any]:
    """What is loaded, its source and age, and the details file's state."""
    return _store().status()


@router.get("/facets", response_model=SatcatFacets)
def satcat_facets(
    on_orbit: bool = Query(True, description="Count only objects without a decay date"),
) -> dict[str, Any]:
    """Counts for the dossier's filter controls."""
    store = _store()
    _require(store)
    return store.facets(on_orbit=on_orbit)


@router.get("/ids", response_model=SatcatIds)
def satcat_ids(
    owner: str | None = Query(None, description="Owner codes, comma separated (US,PRC)"),
    object_type: str | None = Query(
        None, alias="type",
        description="PAYLOAD, ROCKET BODY, DEBRIS, UNKNOWN (or PAY, R/B, DEB, UNK)",
    ),
    year_from: int | None = Query(None, ge=1957, le=2100, description="First launch year"),
    year_to: int | None = Query(None, ge=1957, le=2100, description="Last launch year"),
    rcs: str | None = Query(None, description="SMALL, MEDIUM, LARGE, UNKNOWN"),
    on_orbit: bool = Query(True, description="Only objects without a decay date"),
) -> dict[str, Any]:
    """NORAD ids matching every filter given, ascending, for the globe to filter on."""
    owners = _owners(owner)
    types = _types(object_type)
    sizes = _rcs(rcs)
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(status_code=422, detail="year_from is after year_to")
    store = _store()
    _require(store)
    ids = store.match_ids(
        owners=owners or None, types=types or None, rcs=sizes or None,
        year_from=year_from, year_to=year_to, on_orbit=on_orbit,
    )
    return {
        "count": len(ids),
        "ids": ids,
        "filters": {
            "owner": owners, "type": types, "rcs": sizes,
            "year_from": year_from, "year_to": year_to, "on_orbit": on_orbit,
        },
        **store.provenance(),
    }


@router.get("/{norad_id}", response_model=SatcatCard)
def satcat_card(norad_id: str) -> dict[str, Any]:
    """The dossier card: owner, type, status, launch, decay, RCS, and physical details.

    The first card asked for starts loading the physical details (cached file,
    else one download a day) in the background; until they arrive the card says
    ``details_status: loading`` and ``details`` is null.
    """
    nid = decode_norad(norad_id)
    if nid is None:
        raise HTTPException(status_code=422, detail=f"invalid NORAD id {norad_id!r}")
    store = _store()
    _require(store)
    if store.get(nid) is None:
        raise HTTPException(status_code=404, detail=f"NORAD {nid} is not in the SATCAT")
    store.request_details()
    card = store.card(nid)
    if card is None:  # replaced between the two reads; vanishingly rare
        raise HTTPException(status_code=404, detail=f"NORAD {nid} is not in the SATCAT")
    return card
