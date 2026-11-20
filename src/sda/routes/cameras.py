"""Camera routes: the public camera catalog behind the globe's CCTV layer.

Eight endpoints, all reads:

* GET /api/cameras/summary        what the catalog holds, or how to import it
* GET /api/cameras/density        camera counts per grid cell for a view
* GET /api/cameras                cameras inside a view, capped and evenly sampled
* GET /api/cameras/nearest        cameras nearest a point, nearest first
* GET /api/cameras/random         one random camera
* GET /api/cameras/search         cameras by name or city
* GET /api/cameras/{rid}          one camera with its feed
* GET /api/cameras/{rid}/snapshot the current TxDOT snapshot for a TxDOT camera

The catalog lives in sda.cameras and is built by ``python -m sda.cameras import``.
Coordinates are geodetic degrees. Feeds are played by the browser straight from
their operators; the server fetches nothing except the TxDOT snapshot, whose URL
it reads from the catalog.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from sda.cameras import (
    DENSITY_CELLS,
    GROUPS,
    OTHER_GROUP,
    fetch_txdot_snapshot,
    get_catalog,
)

router = APIRouter(tags=["Cameras"])

MAX_LIST = 3000
DEFAULT_LIST = 1500


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class CameraItem(BaseModel):
    rid: int
    name: str
    lat: float
    lon: float
    group: str
    feed: str = Field(description="image, mjpeg, hls, mp4, youtube, txdot or page")
    playable: bool
    country: str
    city: str
    distance_km: float | None = None


class CameraDetail(BaseModel):
    rid: int
    id: str
    name: str
    type: str
    group: str
    city: str
    country: str
    lat: float
    lon: float
    feed: str
    feed_url: str = Field(description="Image, stream or page URL; the video id for youtube")
    page_url: str
    source: str
    playable: bool
    refresh_s: int


class CountryCount(BaseModel):
    code: str
    count: int


class CameraSummary(BaseModel):
    available: bool
    count: int
    playable: int = 0
    groups: dict[str, int]
    feeds: dict[str, int]
    countries: list[CountryCount]
    country_count: int = 0
    source_count: int = 0
    imported_at: str = ""
    generated: str = ""
    origin: str = ""
    origin_ref: str = ""
    attribution: str = ""
    hint: str = ""
    group_names: list[str] = Field(default_factory=lambda: [*GROUPS, OTHER_GROUP])


class DensityCell(BaseModel):
    lat: float
    lon: float
    count: int
    playable: int


class DensityResponse(BaseModel):
    cell_deg: float
    cells: list[DensityCell]
    total: int


class CameraList(BaseModel):
    cameras: list[CameraItem]
    count: int
    total: int = Field(description="Cameras in the view under the same filters")
    capped: bool


class NearestResponse(BaseModel):
    cameras: list[CameraItem]
    count: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _groups(raw: str | None) -> list[str] | None:
    if raw is None or not raw.strip():
        return None
    return [g.strip().lower() for g in raw.split(",") if g.strip()]


_BBOX = {
    "west": Query(-180.0, ge=-180.0, le=180.0),
    "south": Query(-90.0, ge=-90.0, le=90.0),
    "east": Query(180.0, ge=-180.0, le=180.0),
    "north": Query(90.0, ge=-90.0, le=90.0),
}


# --------------------------------------------------------------------------
# Endpoints (fixed paths before /{rid})
# --------------------------------------------------------------------------


@router.get("/api/cameras/summary", response_model=CameraSummary,
            summary="What the public camera catalog holds")
def cameras_summary() -> CameraSummary:
    return CameraSummary(**get_catalog().summary())


@router.get("/api/cameras/density", response_model=DensityResponse,
            summary="Camera counts per grid cell inside a view")
def cameras_density(
    west: float = _BBOX["west"],
    south: float = _BBOX["south"],
    east: float = _BBOX["east"],
    north: float = _BBOX["north"],
    cell: float = Query(4.0, gt=0.0, le=30.0, description="Cell size in degrees"),
    groups: str | None = Query(None, description="Comma-separated groups; empty for all"),
) -> DensityResponse:
    cat = get_catalog()
    cells = cat.density(west, south, east, north, cell, _groups(groups))
    used = next((c for c in DENSITY_CELLS if c >= cell - 1e-9), DENSITY_CELLS[-1])
    return DensityResponse(
        cell_deg=used,
        cells=[DensityCell(**c) for c in cells],
        total=sum(c["count"] for c in cells),
    )


@router.get("/api/cameras/nearest", response_model=NearestResponse,
            summary="Cameras nearest a point, nearest first")
def cameras_nearest(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
    n: int = Query(10, ge=1, le=100),
    radius_km: float = Query(50.0, gt=0.0, le=2000.0),
    groups: str | None = Query(None),
    playable_only: bool = Query(True),
) -> NearestResponse:
    rows = get_catalog().nearest(lat, lon, n, radius_km, _groups(groups), playable_only)
    return NearestResponse(cameras=[CameraItem(**r) for r in rows], count=len(rows))


@router.get("/api/cameras/random", response_model=CameraDetail,
            summary="One random camera")
def cameras_random(
    groups: str | None = Query(None),
    playable_only: bool = Query(True),
) -> CameraDetail:
    row = get_catalog().random(_groups(groups), playable_only)
    if row is None:
        raise HTTPException(status_code=404, detail="No camera matches")
    return CameraDetail(**row)


@router.get("/api/cameras/search", response_model=NearestResponse,
            summary="Cameras whose name or city contains the text")
def cameras_search(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(20, ge=1, le=100),
) -> NearestResponse:
    rows = get_catalog().search(q, limit)
    return NearestResponse(cameras=[CameraItem(**r) for r in rows], count=len(rows))


@router.get("/api/cameras", response_model=CameraList,
            summary="Public cameras inside a view (capped, evenly sampled)")
def cameras_in_view(
    west: float = _BBOX["west"],
    south: float = _BBOX["south"],
    east: float = _BBOX["east"],
    north: float = _BBOX["north"],
    limit: int = Query(DEFAULT_LIST, ge=1, le=MAX_LIST),
    groups: str | None = Query(None),
    playable_only: bool = Query(False),
) -> CameraList:
    rows, total = get_catalog().in_bbox(
        west, south, east, north, limit, _groups(groups), playable_only
    )
    return CameraList(
        cameras=[CameraItem(**r) for r in rows],
        count=len(rows),
        total=total,
        capped=total > len(rows),
    )


@router.get("/api/cameras/{rid}", response_model=CameraDetail,
            summary="One camera with its feed")
def camera_detail(rid: int) -> CameraDetail:
    row = get_catalog().get(rid)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    return CameraDetail(**row)


#: Last snapshot per camera, so several open viewers cost TxDOT one request a
#: minute per camera. Bounded; cleared when it grows past the cap.
_SNAPSHOT_TTL_S = 30.0
_SNAPSHOT_CACHE_MAX = 64
_snapshot_cache: dict[int, tuple[float, bytes]] = {}


@router.get(
    "/api/cameras/{rid}/snapshot",
    summary="Current snapshot of a TxDOT camera (JPEG)",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def camera_snapshot(rid: int) -> Response:
    row = get_catalog().get(rid)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    if row["feed"] != "txdot":
        raise HTTPException(status_code=400, detail="This camera's feed is played directly")
    now = time.monotonic()
    hit = _snapshot_cache.get(rid)
    if hit is not None and now - hit[0] < _SNAPSHOT_TTL_S:
        data = hit[1]
    else:
        fetched = await fetch_txdot_snapshot(row["feed_url"])
        if fetched is None:
            raise HTTPException(status_code=502, detail="TxDOT did not return a snapshot")
        if len(_snapshot_cache) >= _SNAPSHOT_CACHE_MAX:
            _snapshot_cache.clear()
        _snapshot_cache[rid] = (now, fetched)
        data = fetched
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
