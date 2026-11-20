"""One polite queue per shared free server.

Volunteer-run hosts such as Overpass and Nominatim ask clients to space their
requests. Every outbound call to one of them awaits ``wait_turn(host)`` first:
one asyncio lock and one last-call time per host, so calls from every route in
this process leave at least ``gap_s`` apart, while calls to different hosts do
not wait on each other.
"""

from __future__ import annotations

import asyncio
import math
import time

_last: dict[str, float] = {}
_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _lock(host: str) -> asyncio.Lock:
    """One lock per host per event loop (each TestClient runs its own loop)."""
    loop = asyncio.get_running_loop()
    held = _locks.get(host)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _locks[host] = held
    return held[1]


async def wait_turn(host: str, gap_s: float = 1.0) -> None:
    """Return once ``gap_s`` seconds have passed since the last call to ``host``."""
    async with _lock(host):
        wait = _last.get(host, -math.inf) + gap_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last[host] = time.monotonic()


def reset() -> None:
    """Forget every host's last call (tests)."""
    _last.clear()
    _locks.clear()
