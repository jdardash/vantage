"""Aviation intelligence routes behind the globe's aviation overlay.

Six reads, all built on sda.aviation (adsb.lol through one shared daily
request budget):

* GET /api/aviation/status            budget, refresh intervals, key state
* GET /api/aviation/military          military aircraft (adsb.lol /v2/mil)
* GET /api/aviation/emergencies       squawk 7500, 7600 and 7700
* GET /api/aviation/gps-interference  inferred GPS interference hexes, last hour
* GET /api/aviation/aircraft/{hex}    the aircraft card: identity, route, LiveATC
* GET /api/aviation/nearest-site      nearest mapped military site to a point

Every answer is 200 with an ``error`` string when upstream is down, so the
globe draws what it has. Coordinates are geodetic degrees, altitudes metres.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from sda import aviation
from sda.routes import worldview as wv

router = APIRouter(tags=["Aviation"])


@router.get(
    "/api/aviation/status",
    summary="adsb.lol request budget and refresh intervals for the aviation overlay",
)
def aviation_status() -> dict[str, Any]:
    return aviation.status()


@router.get(
    "/api/aviation/military",
    summary="Military aircraft from adsb.lol /v2/mil, cached to fit the request budget",
)
async def aviation_military() -> dict[str, Any]:
    return await aviation.military()


@router.get(
    "/api/aviation/emergencies",
    summary="Aircraft squawking 7500, 7600 or 7700 (adsb.lol)",
)
async def aviation_emergencies() -> dict[str, Any]:
    return await aviation.emergencies()


@router.get(
    "/api/aviation/gps-interference",
    summary="GPS interference inferred from aircraft NIC and NACp per hex, last hour",
)
async def aviation_gps_interference(
    lat: float | None = Query(None, ge=-90.0, le=90.0, description="View centre latitude"),
    lon: float | None = Query(None, ge=-180.0, le=180.0, description="View centre longitude"),
    size: float = Query(
        aviation.HEX_SIZE_DEG, ge=0.5, le=4.0,
        description="Hex circumradius in Mercator degrees",
    ),
) -> dict[str, Any]:
    """Hexes coloured by the share of aircraft reporting low navigation integrity.

    With lat and lon, the area around that point is sampled from adsb.lol when it
    has not been in the last half hour and the budget allows.
    """
    return await aviation.gps_interference(lat, lon, size)


@router.get(
    "/api/aviation/aircraft/{hex_id}",
    summary="Aircraft card: registration, type, operator, route and LiveATC links",
)
async def aviation_aircraft(
    hex_id: str = Path(..., min_length=6, max_length=7, description="24-bit address, hex"),
    callsign: str | None = Query(None, max_length=10),
    lat: float | None = Query(None, ge=-90.0, le=90.0),
    lon: float | None = Query(None, ge=-180.0, le=180.0),
) -> dict[str, Any]:
    """Everything known about one aircraft.

    The callsign and position hints let a card for an aircraft from another feed
    (OpenSky) find its route even when adsb.lol has nothing on it.
    """
    norm = aviation.normalize_hex(hex_id)
    if norm is None:
        raise HTTPException(status_code=422, detail="hex must be six hexadecimal digits")
    return await aviation.aircraft_card(norm, callsign, lat, lon)


@router.get(
    "/api/aviation/nearest-site",
    summary="Nearest mapped military site to a point (OpenStreetMap via Overpass)",
)
async def aviation_nearest_site(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
) -> dict[str, Any]:
    async def fetch_sites(south: float, west: float, north: float, east: float) -> dict:
        return await wv.proxy_military_sites(south=south, west=west, north=north, east=east)

    return await aviation.nearest_military_site(lat, lon, fetch_sites)
