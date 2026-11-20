"""Armed conflict routes behind the globe's conflict layer.

All reads, all under /api/conflict:

* GET /summary                     sources, theatres, history store state
* GET /frontline/ukraine           DeepStateMap areas and the line of contact
                                   (``?date=YYYY-MM-DD`` for an earlier map)
* GET /frontline/ukraine/dates     the days DeepStateMap has a map for
* GET /control/{theatre}           control of settlements from a Wikipedia module
* GET /events                      GeoConfirmed verified events, compact
* GET /events/{id}                 one verified event with its source links
* GET /acled                       ACLED events when an account is configured
* GET /acled/{id}                  one ACLED event
* GET /history/summary             what the UCDP store holds, or how to import it
* GET /history/years               events and deaths per year
* GET /history/density             UCDP events per grid cell for a year range
* GET /history/events              the deadliest UCDP events in a view
* GET /history/{id}                one UCDP event

Live sources answer 200 with ``error`` set when the upstream fails, as the
other globe proxies do, so the layer degrades instead of breaking. The history
endpoints answer 503 with the import command until ``python -m sda.conflict
import`` has run. Coordinates are geodetic degrees.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda import conflict as cf
from sda.routes.peaks import _parse_bbox

router = APIRouter(tags=["Conflict"])

FIRST_DEEPSTATE_DAY = date(2022, 2, 24)


# --------------------------------------------------------------------------
# Response models (the live map payloads stay plain JSON objects)
# --------------------------------------------------------------------------


class HistorySummary(BaseModel):
    available: bool
    events: int
    deaths: int = 0
    countries: int = 0
    year_min: int = 0
    year_max: int = 0
    datasets: list[str] = Field(default_factory=list)
    imported_at: str = ""
    attribution: str = ""
    types: dict[str, str] = Field(default_factory=dict)
    hint: str = ""


class YearRow(BaseModel):
    year: int
    events: int
    deaths: int


class DensityCell(BaseModel):
    lat: float = Field(description="Mean position of the cell's events")
    lon: float
    events: int
    deaths: int = Field(description="Sum of UCDP best estimates")


class DensityResponse(BaseModel):
    cell_deg: float
    cells: list[DensityCell]
    events: int
    deaths: int
    year_from: int
    year_to: int
    attribution: str


class HistoryEvent(BaseModel):
    id: int
    relid: str
    year: int
    date: str
    date_end: str
    type: int
    type_name: str
    conflict: str
    dyad: str
    side_a: str
    side_b: str
    country: str
    region: str
    place: str
    lat: float
    lon: float
    where_prec: int = Field(description="UCDP location precision, 1 exact to 7 country")
    best: int = Field(description="Best estimate of deaths")
    low: int
    high: int
    civilian_deaths: int
    source: str
    dataset: str
    status: str
    url: str = Field(description="UCDP Conflict Encyclopedia page for the conflict")


class HistoryEvents(BaseModel):
    events: list[HistoryEvent]
    count: int
    total: int
    total_capped: bool
    capped: bool
    attribution: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _store() -> cf.HistoryStore:
    store = cf.get_store()
    if not store.available:
        raise HTTPException(status_code=503, detail=cf.IMPORT_HINT)
    return store


def _parse_types(raw: str | None) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return ()
    try:
        types = tuple(int(v) for v in raw.split(",") if v.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail="types must be a list such as 1,3") from None
    if any(t not in cf.VIOLENCE_TYPES for t in types):
        raise HTTPException(status_code=422, detail="types are 1, 2 or 3")
    return types


def _parse_day(raw: str | None) -> date | None:
    if raw is None or not raw.strip() or raw.strip().lower() == "latest":
        return None
    try:
        day = date.fromisoformat(raw.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from None
    if day < FIRST_DEEPSTATE_DAY:
        raise HTTPException(status_code=422, detail="DeepStateMap begins on 2022-02-24")
    if day >= cf.utc_now().date():
        return None
    return day


def _years(year_from: int | None, year_to: int | None) -> tuple[int, int]:
    lo = year_from if year_from is not None else 1989
    hi = year_to if year_to is not None else 2100
    if lo > hi:
        raise HTTPException(status_code=422, detail="year_from is after year_to")
    return lo, hi


_BBOX = Query(None, description="west,south,east,north in degrees (default: whole Earth)")


# --------------------------------------------------------------------------
# Summary, front line and control
# --------------------------------------------------------------------------


@router.get("/api/conflict/summary", summary="Conflict layer sources and their state")
def conflict_summary() -> dict[str, Any]:
    return {
        "theatres": cf.theatre_list(),
        "history": cf.get_store().summary(),
        "acled_configured": cf.acled_credentials() is not None,
        "acled_hint": "" if cf.acled_credentials() else cf.ACLED_NEEDS,
        "attribution": {
            "frontline": cf.DS_ATTRIBUTION, "control": "Wikipedia contributors, CC BY-SA 4.0",
            "events": cf.GC_ATTRIBUTION, "history": cf.UCDP_ATTRIBUTION,
            "acled": cf.ACLED_ATTRIBUTION,
        },
        "types": {str(k): v for k, v in cf.VIOLENCE_TYPES.items()},
    }


@router.get("/api/conflict/frontline/ukraine/dates",
            summary="Days with a DeepStateMap update, oldest first")
async def frontline_dates() -> dict[str, Any]:
    history = await cf.deepstate_history()
    days = cf.daily_snapshots(history)
    return {
        "dates": [d["date"] for d in days],
        "first": days[0]["date"] if days else "",
        "last": days[-1]["date"] if days else "",
        "updates": len(history),
        "error": "" if days else "DeepStateMap update list unavailable",
    }


@router.get("/api/conflict/frontline/ukraine",
            summary="Russia-Ukraine areas of control and line of contact (DeepStateMap)")
async def frontline_ukraine(
    date_: str | None = Query(None, alias="date",
                              description="YYYY-MM-DD for the map as of that day; "
                                          "absent for the latest"),
) -> dict[str, Any]:
    return await cf.ukraine_frontline(_parse_day(date_))


@router.get("/api/conflict/control/{theatre}",
            summary="Control of settlements in one theatre (Wikipedia detailed map)")
async def control_theatre(theatre: str) -> dict[str, Any]:
    if theatre not in cf.THEATRE_BY_SLUG:
        raise HTTPException(status_code=404, detail="Unknown theatre")
    return await cf.control_map(theatre)


# --------------------------------------------------------------------------
# Verified events (GeoConfirmed) and ACLED
# --------------------------------------------------------------------------


@router.get("/api/conflict/events",
            summary="Events verified against photo or video (GeoConfirmed), newest first")
async def verified_events(
    days: int = Query(90, ge=1, le=cf.GC_WINDOW_DAYS),
    conflict: str | None = Query(None, max_length=40, description="GeoConfirmed conflict"),
) -> dict[str, Any]:
    data = await cf.geoconfirmed_events()
    since = (cf.utc_now().date() - timedelta(days=days)).isoformat()
    events = [e for e in data.get("events", []) if e["date"] >= since
              and (not conflict or e["conflict"] == conflict)]
    counts: dict[str, int] = {}
    for e in data.get("events", []):
        if e["date"] >= since:
            counts[e["conflict"]] = counts.get(e["conflict"], 0) + 1
    return {
        "events": cf.compact(events, cf.GC_COMPACT), "count": len(events),
        "conflicts": counts, "conflict_names": data.get("conflict_names", {}),
        "days": days, "fetched_at": data.get("fetched_at", ""),
        "stale": bool(data.get("stale")), "error": data.get("error", ""),
        "attribution": cf.GC_ATTRIBUTION, "source_url": cf.GC_SITE,
    }


@router.get("/api/conflict/events/{event_id}", summary="One verified event with its sources")
async def verified_event(event_id: str) -> dict[str, Any]:
    data = await cf.geoconfirmed_events()
    want = event_id.strip().lower()
    for e in data.get("events", []):
        if e["id"] == want:
            return {**e, "attribution": cf.GC_ATTRIBUTION}
    raise HTTPException(status_code=404, detail="Unknown event")


@router.get("/api/conflict/acled",
            summary="Recent ACLED events (needs ACLED_EMAIL and ACLED_PASSWORD on the server)")
async def acled(days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
    data = await cf.acled_events(days)
    events = data.get("events", [])
    return {**data, "events": cf.compact(events, cf.ACLED_COMPACT), "count": len(events)}


@router.get("/api/conflict/acled/{event_id}", summary="One ACLED event")
async def acled_event(event_id: str, days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
    data = await cf.acled_events(days)
    for e in data.get("events", []):
        if e["id"] == event_id:
            return {**e, "attribution": cf.ACLED_ATTRIBUTION}
    raise HTTPException(status_code=404, detail="Unknown event")


# --------------------------------------------------------------------------
# UCDP history (fixed paths before /{event_id})
# --------------------------------------------------------------------------


@router.get("/api/conflict/history/summary", response_model=HistorySummary,
            summary="What the UCDP history store holds")
def history_summary() -> HistorySummary:
    return HistorySummary(**cf.get_store().summary())


@router.get("/api/conflict/history/years", response_model=list[YearRow],
            summary="UCDP events and deaths per year")
def history_years() -> list[YearRow]:
    return [YearRow(**r) for r in _store().years()]


@router.get("/api/conflict/history/density", response_model=DensityResponse,
            summary="UCDP events per grid cell over a year range")
def history_density(
    bbox: str | None = _BBOX,
    cell: float = Query(2.0, gt=0.0, le=10.0, description="Cell size in degrees"),
    year_from: int | None = Query(None, ge=1946, le=2100),
    year_to: int | None = Query(None, ge=1946, le=2100),
    types: str | None = Query(None, description="Violence types: 1 state-based, "
                                                "2 non-state, 3 one-sided"),
) -> DensityResponse:
    west, south, east, north = _parse_bbox(bbox)
    lo, hi = _years(year_from, year_to)
    used, cells = _store().density(west, south, east, north, cell, lo, hi, _parse_types(types))
    return DensityResponse(
        cell_deg=used, cells=[DensityCell(**c) for c in cells],
        events=sum(c["events"] for c in cells), deaths=sum(c["deaths"] for c in cells),
        year_from=lo, year_to=hi, attribution=cf.UCDP_ATTRIBUTION,
    )


@router.get("/api/conflict/history/events", response_model=HistoryEvents,
            summary="The deadliest UCDP events inside a view")
def history_events(
    bbox: str | None = _BBOX,
    year_from: int | None = Query(None, ge=1946, le=2100),
    year_to: int | None = Query(None, ge=1946, le=2100),
    types: str | None = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> HistoryEvents:
    west, south, east, north = _parse_bbox(bbox)
    lo, hi = _years(year_from, year_to)
    rows, total = _store().events(west, south, east, north, lo, hi, _parse_types(types), limit)
    return HistoryEvents(
        events=[HistoryEvent(**r) for r in rows], count=len(rows), total=total,
        total_capped=total >= cf.COUNT_CAP, capped=total > len(rows),
        attribution=cf.UCDP_ATTRIBUTION,
    )


@router.get("/api/conflict/history/{event_id}", response_model=HistoryEvent,
            summary="One UCDP event")
def history_event(event_id: int) -> HistoryEvent:
    row = _store().get(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown event")
    return HistoryEvent(**row)
