"""Peak routes: the worldwide mountain catalog behind the globe's peak layer.

Six endpoints, all reads:

* GET /api/peaks/summary   what the catalog holds, or how to import it
* GET /api/peaks/density   peak counts per grid cell with the highest peak of each
* GET /api/peaks           peaks inside a view, most prominent first
* GET /api/peaks/search    named peaks by name prefix, most prominent first
* GET /api/peaks/nearest   peaks nearest a point, nearest first
* GET /api/peaks/{id}      one peak with its sources

The catalog lives in sda.peaks and is built by ``python -m sda.peaks import``.
Until it is, the summary reports ``available: false`` with the command to run,
and every other endpoint answers 503 with the same instruction. Coordinates are
geodetic degrees; heights are metres.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda.peaks import PeakCatalog, cell_for_zoom, get_catalog, snap_cell

router = APIRouter(tags=["Peaks"])

MAX_LIST = 1000
DEFAULT_LIST = 300
NOT_IMPORTED = "No peak catalog imported. On the server run: python -m sda.peaks import"


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class PeakItem(BaseModel):
    id: int = Field(description="Display rank: 1 is the most prominent summit")
    name: str = Field(description="Empty when no gazetteer names this summit")
    lat: float
    lon: float
    elev_m: float | None
    prom_m: float | None = Field(description="Prominence; null when unknown")
    iso_km: float | None = Field(description="Topographic isolation; null when unknown")
    country: str
    region: str
    distance_km: float | None = None


class PeakDetail(PeakItem):
    elev_source: str = Field(description="glo30-dem, geonames or geonames-dem")
    dem_m: float | None = Field(description="DEM summit height when elev_m is surveyed")
    geonames_id: int | None
    source: str = Field(description="kirmse+geonames, kirmse or geonames")


class PeakSummary(BaseModel):
    available: bool
    count: int
    named: int = 0
    with_prominence: int = 0
    with_isolation: int = 0
    geonames_only: int = 0
    country_count: int = 0
    min_prom_m: float = 0.0
    imported_at: str = ""
    sources: list[str] = Field(default_factory=list)
    attribution: str = ""
    hint: str = ""


class DensityCell(BaseModel):
    lat: float = Field(description="Mean position of the cell's peaks")
    lon: float
    count: int
    top: PeakItem = Field(description="The highest peak in the cell")


class DensityResponse(BaseModel):
    cell_deg: float
    cells: list[DensityCell]
    total: int


class PeakList(BaseModel):
    peaks: list[PeakItem]
    count: int
    total: int = Field(description="Peaks in the view under the same filter")
    total_capped: bool = Field(description="True when total stopped at its cap")
    capped: bool = Field(description="True when the view holds more peaks than returned")


class PeakResults(BaseModel):
    peaks: list[PeakItem]
    count: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


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


def _catalog() -> PeakCatalog:
    cat = get_catalog()
    if not cat.available:
        raise HTTPException(status_code=503, detail=NOT_IMPORTED)
    return cat


_BBOX = Query(None, description="west,south,east,north in degrees (default: whole Earth)")


# --------------------------------------------------------------------------
# Endpoints (fixed paths before /{peak_id})
# --------------------------------------------------------------------------


@router.get("/api/peaks/summary", response_model=PeakSummary,
            summary="What the worldwide peak catalog holds")
def peaks_summary() -> PeakSummary:
    return PeakSummary(**get_catalog().summary())


@router.get("/api/peaks/density", response_model=DensityResponse,
            summary="Peak counts per grid cell, with the highest peak in each")
def peaks_density(
    bbox: str | None = _BBOX,
    zoom: float = Query(2.0, ge=0.0, le=22.0, description="Web-map zoom level"),
    cell: float | None = Query(None, gt=0.0, le=30.0,
                               description="Cell size in degrees (overrides zoom)"),
) -> DensityResponse:
    west, south, east, north = _parse_bbox(bbox)
    want = snap_cell(cell) if cell is not None else cell_for_zoom(zoom)
    used, cells = _catalog().density(west, south, east, north, want)
    return DensityResponse(
        cell_deg=used,
        cells=[DensityCell(**c) for c in cells],
        total=sum(c["count"] for c in cells),
    )


@router.get("/api/peaks/search", response_model=PeakResults,
            summary="Named peaks whose name starts with the text, most prominent first")
def peaks_search(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(20, ge=1, le=100),
) -> PeakResults:
    rows = _catalog().search(q, limit)
    return PeakResults(peaks=[PeakItem(**r) for r in rows], count=len(rows))


@router.get("/api/peaks/nearest", response_model=PeakResults,
            summary="Peaks nearest a point, nearest first")
def peaks_nearest(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
    n: int = Query(10, ge=1, le=100),
    radius_km: float = Query(50.0, gt=0.0, le=500.0),
    min_prom: float = Query(0.0, ge=0.0, le=9000.0, description="Minimum prominence, m"),
) -> PeakResults:
    rows = _catalog().nearest(lat, lon, n, radius_km, min_prom)
    return PeakResults(peaks=[PeakItem(**r) for r in rows], count=len(rows))


@router.get("/api/peaks", response_model=PeakList,
            summary="Peaks inside a view, most prominent first")
def peaks_in_view(
    bbox: str | None = _BBOX,
    limit: int = Query(DEFAULT_LIST, ge=1, le=MAX_LIST),
    min_prom: float = Query(0.0, ge=0.0, le=9000.0, description="Minimum prominence, m"),
) -> PeakList:
    west, south, east, north = _parse_bbox(bbox)
    rows, total, total_capped = _catalog().in_bbox(west, south, east, north, limit, min_prom)
    return PeakList(
        peaks=[PeakItem(**r) for r in rows],
        count=len(rows),
        total=total,
        total_capped=total_capped,
        capped=total > len(rows),
    )


@router.get("/api/peaks/{peak_id}", response_model=PeakDetail,
            summary="One peak with its sources")
def peak_detail(peak_id: int) -> PeakDetail:
    row = _catalog().get(peak_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown peak")
    return PeakDetail(**row)
