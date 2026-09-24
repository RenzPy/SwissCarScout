"""Price-band partitioning for the market index.

THE PROBLEM THIS SOLVES
-----------------------
The search API sorts PRICE ASC and every run starts at page 0, so an index
that fetches 120 pages always reads the same 2'400 cheapest cars. Measured on
a real run: 2'123 listings seen, 224 new. Roughly 90% of the work was
re-reading what was already known, and 91% of the market was never visited.

Splitting the price range into bands and rotating through them means every run
covers ground the last one didn't, and each band is small enough to page
through completely rather than being truncated by the page cap.

A band is also the right unit for sold-detection: only a band that has just
been re-read in full can tell you what has disappeared from it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db

CURSOR_KEY = "band_cursor"
SWEPT_PREFIX = "band_swept."


def compute_bands(lo: int, hi: int, width: int) -> list[tuple[int, int]]:
    """Contiguous [from, to) slices covering lo..hi."""
    bands = []
    at = lo
    while at < hi:
        top = min(at + width, hi)
        bands.append((at, top))
        at = top
    return bands


def band_key(lo: int, hi: int) -> str:
    return f"{lo}-{hi}"


def bands_from_cfg(cfg: dict) -> list[tuple[int, int]]:
    ix = cfg.get("index", {})
    return compute_bands(int(ix.get("price_from", 1500)),
                         int(ix.get("price_to", 30000)),
                         int(ix.get("band_width", 1500)))


def next_bands(conn, cfg: dict) -> list[tuple[int, int]]:
    """The bands this run should sweep, oldest-swept first.

    Ordering by staleness rather than strict round-robin means a band that
    failed or was interrupted gets picked up next time instead of waiting a
    full cycle.
    """
    bands = bands_from_cfg(cfg)
    per_run = int(cfg.get("index", {}).get("bands_per_run", 4))

    def swept_at(b) -> str:
        return db.get_state(conn, SWEPT_PREFIX + band_key(*b), "") or ""

    return sorted(bands, key=swept_at)[:per_run]


def mark_swept(conn, band: tuple[int, int]) -> None:
    db.set_state(conn, SWEPT_PREFIX + band_key(*band),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))


def coverage(conn, cfg: dict) -> list[tuple[str, str]]:
    """(band, when last swept) for every band, for reporting."""
    return [(band_key(*b),
             db.get_state(conn, SWEPT_PREFIX + band_key(*b), "never"))
            for b in bands_from_cfg(cfg)]
