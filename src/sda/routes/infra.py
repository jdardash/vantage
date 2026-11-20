"""Infrastructure routes: the catalog behind the globe's infrastructure layer.

Six endpoints, all reads:

* GET /api/infra/summary          counts per kind, sources, licences, attribution
* GET /api/infra/items            items inside a view for chosen kinds, limit shared
* GET /api/infra/search           items by name or code (ICAO, IATA, UN/LOCODE)
* GET /api/infra/item/{id}        one item with its fields, links and source
* GET /api/infra/cables           submarine cables as GeoJSON
* GET /api/infra/cables/{id}      one cable with the countries it lands in and how
                                  many other cables land in each

The catalog lives in sda.infra and is built by ``python -m sda.infra import``.
Until it is, the summary reports ``available: false`` with the command to run.
The cable map is a disk cache the server refreshes in the background once a
week (never under SDA_OFFLINE=1); while the first fetch runs, the cables answer
says ``loading: true`` with no features. Coordinates are geodetic degrees.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sda import infra

router = APIRouter(tags=["Infrastructure"])

MAX_LIST = 3000
DEFAULT_LIST = 1500
NOT_IMPORTED = "No infrastructure catalog imported. On the server run: python -m sda.infra import"
_UID = re.compile(r"^[a-z]+-[A-Za-z0-9._-]{1,140}$")
_CABLE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")


class InfraItem(BaseModel):
    id: str = Field(description="Stable id, '<kind>-<source key>', e.g. airport-KJFK")
    kind: str = Field(description="power, nuclear, airport, port, dam, datacenter, landing"
                                  " or radiation")
    sub: str = Field(description="Sub-kind: fuel, airport size, harbor size, network")
    name: str
    lat: float
    lon: float
    country: str = Field(description="ISO code or name, as the source gives it")
    brief: str = Field(description="One-line summary for a label or list")
    score: float = Field(description="Ranking within the kind (capacity, size, height...)")


class InfraList(BaseModel):
    items: list[InfraItem]
    count: int
    counts: dict[str, int] = Field(description="Items of each requested kind in the view")
    total: int
    capped: bool = Field(description="True when the view holds more items than returned")


class InfraLink(BaseModel):
    label: str
    url: str


class InfraSource(BaseModel):
    name: str
    licence: str
    url: str


class InfraDetail(InfraItem):
    label: str = Field(description="Kind label, e.g. 'Airports'")
    fields: dict[str, Any] = Field(description="The kind's own fields")
    links: list[InfraLink]
    source: InfraSource


class InfraResults(BaseModel):
    items: list[InfraItem]
    count: int


class CableCountry(BaseModel):
    country: str
    landings: int = Field(description="Landing points of this cable in the country")
    cables_total: int = Field(description="Cables landing in the country, this one included")
    other_cables: int = Field(description="Other cables landing there; 0 means sole link")


class CableLanding(BaseModel):
    id: str
    name: str
    country: str


class CableDetail(BaseModel):
    id: str
    name: str
    color: str
    length: str
    rfs_year: int | None
    is_planned: bool
    owners: str
    suppliers: str
    url: str
    map_url: str
    landing_points: list[CableLanding]
    countries: list[str]
    countries_detail: list[CableCountry]
    attribution: str = "TeleGeography submarinecablemap.com, CC BY-NC-SA 3.0"


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float]:
    """``west,south,east,north`` in degrees; the whole Earth when absent.

    West greater than east means the box crosses the antimeridian.
    """
    if raw is None or not raw.strip():
        return -180.0, -90.0, 180.0, 90.0
    try:
        west, south, east, north = (float(v) for v in raw.split(","))
    except ValueError:
        raise HTTPException(
            status_code=422, detail="bbox must be west,south,east,north in degrees"
        ) from None
    for v in (west, south, east, north):
        if v != v or v in (float("inf"), float("-inf")):
            raise HTTPException(status_code=422, detail="bbox values must be finite")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HTTPException(status_code=422, detail="bbox longitudes must be in [-180, 180]")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HTTPException(status_code=422, detail="bbox latitudes must be in [-90, 90]")
    return west, south, east, north


def _kinds(raw: str | None) -> list[str]:
    kinds = infra.parse_kinds(raw)
    if not kinds:
        raise HTTPException(status_code=422,
                            detail="kinds must name one or more of: " + ",".join(infra.KINDS))
    return kinds


def _require_data() -> None:
    if not infra.get_catalog().available and not infra.get_cable_store().status()["available"]:
        raise HTTPException(status_code=503, detail=NOT_IMPORTED)


_BBOX = Query(None, description="west,south,east,north in degrees (default: whole Earth)")
_KINDS = Query(None, description="Comma list of kinds (default: all): " + ",".join(infra.KINDS))


@router.get("/api/infra/summary", summary="What the infrastructure catalog holds")
def infra_summary() -> dict[str, Any]:
    return infra.summary()


@router.get("/api/infra/items", response_model=InfraList,
            summary="Infrastructure inside a view, the limit shared across kinds")
def infra_items(
    bbox: str | None = _BBOX,
    kinds: str | None = _KINDS,
    limit: int = Query(DEFAULT_LIST, ge=1, le=MAX_LIST),
) -> InfraList:
    west, south, east, north = _parse_bbox(bbox)
    wanted = _kinds(kinds)
    _require_data()
    got = infra.query_items(west, south, east, north, wanted, limit)
    return InfraList(items=[InfraItem(**i) for i in got["items"]], count=got["count"],
                     counts=got["counts"], total=got["total"], capped=got["capped"])


@router.get("/api/infra/search", response_model=InfraResults,
            summary="Infrastructure by name or code, highest ranked first")
def infra_search(
    q: str = Query(..., min_length=2, max_length=100),
    kinds: str | None = _KINDS,
    limit: int = Query(20, ge=1, le=100),
) -> InfraResults:
    wanted = _kinds(kinds)
    _require_data()
    rows = infra.get_catalog().search(q, [k for k in wanted if k != "landing"], limit)
    if "landing" in wanted and len(rows) < limit:
        needle = q.strip().lower()
        extra = [r for r in infra.get_cable_store().landing_rows() if needle in r.name.lower()]
        extra.sort(key=lambda r: -r.score)
        rows.extend({"id": r.uid, "kind": r.kind, "sub": r.sub, "name": r.name, "lat": r.lat,
                     "lon": r.lon, "country": r.country, "brief": r.brief, "score": r.score}
                    for r in extra[: limit - len(rows)])
    return InfraResults(items=[InfraItem(**r) for r in rows], count=len(rows))


@router.get("/api/infra/item/{uid}", response_model=InfraDetail,
            summary="One infrastructure item with its fields, links and source")
def infra_item(uid: str) -> InfraDetail:
    if not _UID.match(uid):
        raise HTTPException(status_code=404, detail="Unknown item")
    got = infra.item_detail(uid)
    if got is None:
        raise HTTPException(status_code=404, detail="Unknown item")
    return InfraDetail(**got)


@router.get("/api/infra/cables", summary="Submarine cables as GeoJSON (TeleGeography)")
def infra_cables() -> JSONResponse:
    store = infra.get_cable_store()
    store.ensure_fresh()
    data = store.geojson()
    cache = "public, max-age=3600" if data["available"] else "no-store"
    return JSONResponse(data, headers={"Cache-Control": cache})


@router.get("/api/infra/cables/{cable_id}", response_model=CableDetail,
            summary="One cable: landing countries and how many other cables land in each")
def infra_cable(cable_id: str) -> CableDetail:
    if not _CABLE_ID.match(cable_id):
        raise HTTPException(status_code=404, detail="Unknown cable")
    got = infra.get_cable_store().cable(cable_id)
    if got is None:
        raise HTTPException(status_code=404, detail="Unknown cable")
    return CableDetail(**got)
