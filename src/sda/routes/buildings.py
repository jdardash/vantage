"""OpenStreetMap buildings behind the globe's 3D city view.

* GET /api/buildings?south=&west=&north=&east=   footprints with heights in a box

Each side of the box may span at most 0.02 degrees (about 2 km); a larger box
answers 400 "Zoom in to see buildings". A box whose east edge is west of its
west edge crosses the antimeridian and is asked for in two halves. When
Overpass fails the answer is still 200, with no buildings and ``error`` set to
a sentence for the user, as the other globe proxies do.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from sda import buildings

router = APIRouter(tags=["Buildings"])

_SLACK = 1e-9


@router.get(
    "/api/buildings",
    summary="OpenStreetMap building footprints and heights in a small box",
)
async def get_buildings(
    south: float = Query(..., ge=-90.0, le=90.0),
    west: float = Query(..., ge=-180.0, le=180.0),
    north: float = Query(..., ge=-90.0, le=90.0),
    east: float = Query(..., ge=-180.0, le=180.0),
) -> dict[str, Any]:
    """Building footprints from OpenStreetMap, each with a height to extrude.

    Height is the ``height`` tag in metres when it parses, else
    ``building:levels`` times 3.2 m, else 8 m; ``height_from`` says which.
    Answers are cached for 24 hours per box rounded outward to 0.001 degrees.
    """
    span_lon = buildings.lon_span(west, east)
    if north <= south or span_lon <= 0:
        raise HTTPException(status_code=400, detail="Empty bounding box")
    limit = buildings.MAX_SPAN_DEG + _SLACK
    if north - south > limit or span_lon > limit:
        raise HTTPException(status_code=400, detail="Zoom in to see buildings")
    return await buildings.lookup(south, west, north, east)
