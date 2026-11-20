"""Sanctions routes: is this vessel or aircraft on the OFAC SDN or UK list?

Four reads:

* GET /api/sanctions/summary    what the cached lists hold and when they were fetched
* GET /api/sanctions/vessel     by IMO number (or MMSI)
* GET /api/sanctions/aircraft   by tail number (or Mode S hex code, or serial)
* GET /api/sanctions/index      every match key of one kind, for a layer to test
                                thousands of tracks without a request each

A hit answers ``listed: true`` with the programme, the listing name, the remark
text and every matching listing. A miss answers 200 with ``listed: false``.
``list_loaded`` is false until the first download has finished (it starts in
the background on first use), so a miss then means "not known yet".
``possible_matches`` holds leads that are not identifier matches, such as an
aircraft that once carried the tail number asked about.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from sda import sanctions

router = APIRouter(tags=["Sanctions"])


@router.get("/api/sanctions/summary", summary="What the cached sanctions lists hold")
def sanctions_summary() -> dict:
    store = sanctions.get_store()
    store.ensure_loaded()
    return store.summary()


@router.get("/api/sanctions/vessel", summary="Is a vessel listed? By IMO number or MMSI")
def sanctions_vessel(
    imo: str | None = Query(None, max_length=20, description="IMO number, 7 digits"),
    mmsi: str | None = Query(None, max_length=20, description="MMSI, 9 digits"),
) -> dict:
    imo_key = sanctions.norm_imo(imo)
    mmsi_key = sanctions.norm_mmsi(mmsi)
    if imo and not imo_key:
        raise HTTPException(status_code=422, detail="imo must be a 7-digit IMO number")
    if mmsi and not mmsi_key:
        raise HTTPException(status_code=422, detail="mmsi must be a 9-digit MMSI")
    if not (imo_key or mmsi_key):
        raise HTTPException(status_code=422, detail="Give imo or mmsi")
    return sanctions.lookup_vessel(imo=imo_key, mmsi=mmsi_key)


@router.get("/api/sanctions/aircraft",
            summary="Is an aircraft listed? By tail number, Mode S code or serial")
def sanctions_aircraft(
    reg: str | None = Query(None, max_length=20, description="Tail number, e.g. EP-GOL"),
    hex: str | None = Query(None, max_length=10, description="Mode S code, 6 hex digits"),
    serial: str | None = Query(None, max_length=30, description="Manufacturer serial (MSN)"),
) -> dict:
    reg_key = sanctions.norm_reg(reg)
    hex_key = sanctions.norm_hex(hex)
    serial_key = sanctions.norm_reg(serial)
    if reg and not (2 <= len(reg_key) <= 10):
        raise HTTPException(status_code=422, detail="reg must be a tail number")
    if hex and not hex_key:
        raise HTTPException(status_code=422, detail="hex must be 6 hex digits")
    if not (reg_key or hex_key or serial_key):
        raise HTTPException(status_code=422, detail="Give reg, hex or serial")
    return sanctions.lookup_aircraft(reg=reg_key, hex_code=hex_key, serial=serial_key)


@router.get("/api/sanctions/index", summary="Every match key of one kind")
def sanctions_index(
    kind: Literal["vessel", "aircraft"] = Query("aircraft"),
) -> dict:
    """Aircraft: normalised tail numbers (letters and digits, upper case) and Mode S
    codes. Vessels: IMO numbers and MMSIs. A key here is worth a call to the
    lookup endpoint for the programme and remark."""
    store = sanctions.get_store()
    store.ensure_loaded()
    out = store.index(kind)
    out["list_loaded"] = bool(store.records)
    out["updated"] = store.summary()["updated"]
    return out
