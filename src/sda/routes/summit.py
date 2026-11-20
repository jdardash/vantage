"""Summit conditions routes: PeakWatch's screen for any mountain on Earth.

Endpoints:

* GET  /api/summit/conditions   one point, seven days: per day the verdict (GO, MARG, NO
  or ?), the reasons and the numbers behind it, which sources were read; the hourly
  meteogram series; every source with its fetch time, age and state
* POST /api/summit/verdicts      up to 50 points, one verdict each for today's summit hours
  (10:00 to 14:00 local), or tomorrow's once today's have passed, from one batched fetch

The engine is sda.summit. It is a screen, not a go/no-go: the answer carries that line in
``screen_note`` and the drawer shows it. Every upstream is keyless (Open-Meteo forecast,
ensemble, air quality and elevation everywhere; api.weather.gov, avalanche.org and NIFC
WFIGS where the NWS covers the point). Requests are batched per Open-Meteo call and cached
per 0.01 degree cell and model run, so a peaks layer polling 50 summits costs a handful of
upstream calls an hour. A source that fails falls back to its last copy (flagged stale, with
its age) and past its limit counts as not read, which holds a day at MARG, never GO.

A missing elev_m is filled from Open-Meteo's 90 m elevation model; the answer says which.
Coordinates are geodetic degrees.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from sda import summit

router = APIRouter(tags=["Summit"])

MAX_POINTS = 50


class SummitPoint(BaseModel):
    """One summit for the verdicts batch."""

    id: str = Field(min_length=1, max_length=80)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    elev_m: float | None = Field(default=None, ge=-500.0, le=9000.0)
    name: str | None = Field(default=None, max_length=120)


class VerdictsRequest(BaseModel):
    """The batch, as an object; a bare JSON array of points is accepted too."""

    points: list[SummitPoint] = Field(min_length=1, max_length=MAX_POINTS)


class Verdict(BaseModel):
    verdict: str = Field(description="GO, MARG, NO or ?")
    day: str | None = Field(description="local date scored, YYYY-MM-DD")
    when: str | None = Field(description="today, or tomorrow once today's hours have passed")
    top_reason: str


class SummitDay(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    label: str
    verdict: str
    top_reason: str
    reasons: list[dict[str, str]]
    numbers: dict[str, Any]
    read: dict[str, bool]


class SummitConditions(BaseModel):
    """The full answer; fields beyond these are passed through as the engine builds them."""

    model_config = ConfigDict(extra="allow")

    name: str
    lat: float
    lon: float
    elev_m: float | None
    elev_source: str
    timezone: str
    tz_exact: bool
    generated: str
    screen_note: str
    in_us: bool | None
    aqi_scale: str
    levels_hpa: list[int]
    days: list[SummitDay]
    sources: list[dict[str, Any]]
    hourly: dict[str, list[Any]]


@router.get("/api/summit/conditions", response_model=SummitConditions)
def summit_conditions(
    lat: Annotated[float, Query(ge=-90.0, le=90.0)],
    lon: Annotated[float, Query(ge=-180.0, le=180.0)],
    elev_m: Annotated[float | None, Query(ge=-500.0, le=9000.0)] = None,
    name: Annotated[str, Query(max_length=120)] = "",
    days: Annotated[int, Query(ge=1, le=7)] = 7,
) -> dict[str, Any]:
    """Seven days of summit-hours verdicts for one point, with the meteogram series."""
    try:
        return summit.conditions(lat, lon, elev_m, name, days)
    except summit.UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/summit/verdicts", response_model=dict[str, Verdict])
def summit_verdicts(
    payload: Annotated[list[SummitPoint] | VerdictsRequest, Body()],
) -> dict[str, dict[str, Any]]:
    """{id: {verdict, day, when, top_reason}} for up to 50 points."""
    points = payload.points if isinstance(payload, VerdictsRequest) else payload
    if not points:
        raise HTTPException(status_code=422, detail="no points")
    if len(points) > MAX_POINTS:
        raise HTTPException(status_code=422, detail=f"at most {MAX_POINTS} points per call")
    return summit.verdicts([p.model_dump() for p in points])
