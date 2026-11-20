"""Intel routes: country cards, instability, outbreaks, cyber, outages, convergence.

Eleven reads, all keyless upstream (see sda.intel for sources and caching):

* GET /api/intel/summary        sources, attribution, the instability formula
* GET /api/intel/countries      every country with its label point and box
* GET /api/intel/borders        simplified country polygons for the choropleth
* GET /api/intel/country-at     the country under a point (a globe click)
* GET /api/intel/country/{code} the country card (ISO 3166 alpha-2 or alpha-3)
* GET /api/intel/instability    the instability heuristic for every country
* GET /api/intel/outbreaks      WHO Disease Outbreak News, placed at countries
* GET /api/intel/cyber          Feodo Tracker botnet C2 servers per country
* GET /api/intel/outages        IODA internet outage alerts per country
* GET /api/intel/convergence    cells where three or more event kinds meet in 24 h
* GET /api/intel/exchanges      which major exchanges are in session now

Everything that places something on a country needs the Natural Earth borders,
which the server downloads once on first use and keeps on disk. Until they are
in, those endpoints answer 503 with the reason. Coordinates are geodetic
degrees.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from sda import intel

router = APIRouter(tags=["Intel"])


def _no_borders() -> HTTPException:
    reason = intel.borders_status()["error"] or "Country borders are still loading"
    return HTTPException(status_code=503, detail=reason)


async def _index() -> intel.CountryIndex:
    index = await intel.get_index()
    if index is None:
        raise _no_borders()
    return index


async def _snapshot() -> dict[str, Any]:
    snap = await intel.snapshot()
    if snap is None:
        raise _no_borders()
    return snap


@router.get("/api/intel/summary", summary="Country intel sources, attribution and formula")
async def intel_summary() -> dict[str, Any]:
    """Cheap: reports what is loaded without fetching anything."""
    snap = intel._state.snapshot
    return {
        "borders": intel.borders_status(),
        "gdelt": intel.gdelt_status(),
        "sources": snap["sources"] if snap else {},
        "generated": snap["generated"] if snap else "",
        "formula": list(intel.INSTABILITY_FORMULA),
        "levels": [{"from": f, "level": name} for f, name in intel.INSTABILITY_LEVELS],
        "note": intel.INSTABILITY_NOTE,
        "convergence_method": intel.CONVERGENCE_METHOD,
        "exchanges_note": intel.EXCHANGE_NOTE,
        "attribution": [
            intel.BORDERS_ATTRIBUTION,
            "Indicators: World Bank Open Data (CC BY 4.0)",
            "Outbreaks: WHO Disease Outbreak News",
            "C2 servers: abuse.ch Feodo Tracker (CC0)",
            "Internet outages: IODA, Georgia Tech",
            "News events: The GDELT Project",
        ],
    }


@router.get("/api/intel/countries", summary="Every country with its label point and box")
async def intel_countries() -> dict[str, Any]:
    index = await _index()
    rows = [{**c.brief(), "bbox": list(c.main_bbox)} for c in index.countries]
    return {"countries": rows, "count": len(rows)}


@router.get("/api/intel/borders", summary="Simplified country polygons for the choropleth")
async def intel_borders(
    tol: float = Query(0.2, ge=0.02, le=1.0, description="Point spacing, degrees"),
) -> dict[str, Any]:
    payload = await intel.get_borders(tol)
    if payload is None:
        raise _no_borders()
    return payload


@router.get("/api/intel/country-at", summary="The country under a point")
async def intel_country_at(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
) -> dict[str, Any]:
    index = await _index()
    c = index.locate(lat, lon)
    if c is None:
        raise HTTPException(status_code=404, detail="No country at this point")
    return c.brief()


@router.get("/api/intel/country/{code}", summary="The country card")
async def intel_country(
    code: str = Path(..., min_length=2, max_length=3, pattern=r"^[A-Za-z]{2,3}$"),
) -> dict[str, Any]:
    await _index()
    card = await intel.country_card(code)
    if card is None:
        raise HTTPException(status_code=404, detail="Unknown country code")
    return card


@router.get("/api/intel/instability",
            summary="This application's instability heuristic for every country")
async def intel_instability() -> dict[str, Any]:
    snap = await _snapshot()
    index = await _index()
    rows = []
    for iso3, s in snap["scores"].items():
        c = index.by_iso3.get(iso3)
        if c is None:
            continue
        rows.append({**c.brief(), **s, "inputs": snap["inputs"].get(iso3, {})})
    rows.sort(key=lambda r: -r["score"])
    return {
        "countries": rows, "count": len(rows), "generated": snap["generated"],
        "formula": list(intel.INSTABILITY_FORMULA), "note": intel.INSTABILITY_NOTE,
        "levels": [{"from": f, "level": name} for f, name in intel.INSTABILITY_LEVELS],
        "sources": snap["sources"],
    }


@router.get("/api/intel/outbreaks", summary="WHO Disease Outbreak News at affected countries")
async def intel_outbreaks(
    days: int = Query(90, ge=1, le=365, description="Items published within this many days"),
) -> dict[str, Any]:
    await _index()
    items, error = await intel.outbreaks()
    cutoff = intel._iso(intel._utcnow() - timedelta(days=days))
    rows = [o for o in items if o["published"] >= cutoff]
    return {"outbreaks": rows, "count": len(rows), "days": days, "error": error,
            "source": "WHO Disease Outbreak News"}


@router.get("/api/intel/cyber", summary="Botnet C2 servers per country (abuse.ch Feodo Tracker)")
async def intel_cyber() -> dict[str, Any]:
    index = await _index()
    per_iso2, error = await intel.cyber()
    rows = []
    for iso2, agg in per_iso2.items():
        c = index.by_iso2.get(iso2)
        if c is not None:
            rows.append({**c.brief(), **agg})
    rows.sort(key=lambda r: -r["servers"])
    return {"countries": rows, "count": len(rows),
            "servers": sum(r["servers"] for r in rows),
            "online": sum(r["online"] for r in rows), "error": error,
            "source": "abuse.ch Feodo Tracker (CC0)"}


@router.get("/api/intel/outages", summary="IODA internet outage alerts per country, 24 h")
async def intel_outages() -> dict[str, Any]:
    snap = await _snapshot()
    rows = sorted(snap["outages"].values(),
                  key=lambda r: (-r["critical"], -r["warning"], r["name"]))
    return {"countries": rows, "count": len(rows), "generated": snap["generated"],
            "error": snap["sources"]["ioda"]["note"],
            "source": "IODA, Georgia Tech Internet Outage Detection and Analysis"}


@router.get("/api/intel/convergence",
            summary="Cells where three or more event kinds meet within 24 hours")
async def intel_convergence(
    cell_deg: float = Query(2.0, ge=0.5, le=10.0, description="Grid cell size, degrees"),
    min_kinds: int = Query(intel.CONVERGENCE_MIN_KINDS, ge=2, le=len(intel.CONVERGENCE_KINDS)),
) -> dict[str, Any]:
    snap = await _snapshot()
    cells = intel.convergence_cells(
        snap["points"], intel.country_wide_kinds(snap), cell_deg, min_kinds)
    return {"cells": cells, "count": len(cells), "cell_deg": cell_deg,
            "min_kinds": min_kinds, "window_h": intel.WINDOW_H,
            "method": intel.CONVERGENCE_METHOD, "generated": snap["generated"],
            "sources": snap["sources"]}


@router.get("/api/intel/exchanges", summary="Which major stock exchanges are in session now")
async def intel_exchanges(
    country: str | None = Query(None, min_length=2, max_length=2,
                                description="ISO 3166 alpha-2 code; all when absent"),
) -> dict[str, Any]:
    now = intel._utcnow()
    rows = [intel.exchange_status(ex, now) for ex in intel.EXCHANGES
            if country is None or ex.iso2 == country.upper()]
    return {"exchanges": rows, "count": len(rows), "open": sum(1 for r in rows if r["open"]),
            "at": intel._iso(now), "note": intel.EXCHANGE_NOTE}
