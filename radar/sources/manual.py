"""Manual intake — listings you found by hand.

Facebook Marketplace, a garage owner's tip-off, a card in a Migros window, your
cousin's neighbour's Golf. Anything that never passes through a scraper.

Why this exists: the scoring and the price baseline are worth more than the
fetching is. A car you found on Facebook still deserves a market-median check,
still belongs in your comparables, and still gets a price-drop tracked if you
re-run after the seller lowers it. This gets those cars into the same database
as everything else, without automating any site that doesn't want to be.

Usage: keep a manual.yaml next to config.yaml, then

    python run.py --source manual --mode buy

Re-run it whenever you edit the file. Entries are matched on `id`, so changing
a `price` on an existing entry records a price drop exactly like a scraped one.

Format (every field except id/title is optional):

    - id: fb-leon-delemont            # any stable string you choose
      title: "Seat Leon 1.8 TFSI 2007"
      body: "121000km, expertise 10.07.2026, 2 jeux de pneus, vitre cassée"
      price: 3500
      km: 121000
      year: 2007
      make: seat
      model: leon
      fuel: essence
      gearbox: manuelle
      region: "Delémont, JU"
      url: "https://www.facebook.com/marketplace/item/1234567890/"
      photos: 6
      published: "2026-07-15"        # when the SELLER posted it, if you know
      platform: "Facebook Marketplace"
      seller_type: private           # private | dealer | unknown
"""
from __future__ import annotations

import os

import yaml

from .base import Source, register
from ..models import Listing

DEFAULT_PATH = "manual.yaml"


@register
class Manual(Source):
    name = "manual"

    def fetch(self):
        path = (self.cfg.get("sources", {})
                        .get("manual", {})
                        .get("path", DEFAULT_PATH))
        if not os.path.exists(path):
            print(f"  [manual] no {path} yet — see the notes at the top of "
                  f"radar/sources/manual.py for the format")
            return []

        with open(path, encoding="utf-8") as fh:
            entries = yaml.safe_load(fh) or []

        if not isinstance(entries, list):
            raise RuntimeError(f"{path} must be a list of listings")
        return entries

    def parse(self, e: dict) -> Listing | None:
        if not isinstance(e, dict):
            return None
        ext_id = e.get("id")
        title = e.get("title")
        if not ext_id or not title:
            print(f"  [manual] skipping entry without id+title: {e!r:.60}")
            return None

        return Listing(
            source=self.name,
            external_id=str(ext_id),
            url=e.get("url", ""),
            title=str(title),
            body=str(e.get("body", "")),
            price_chf=e.get("price"),
            km=e.get("km"),
            year=e.get("year"),
            make=str(e.get("make", "")),
            model=str(e.get("model", "")),
            fuel=str(e.get("fuel", "")),
            gearbox=str(e.get("gearbox", "")),
            seller_type=str(e.get("seller_type", "private")),
            region=str(e.get("region", "")),
            photo_count=int(e.get("photos", 0) or 0),
            published_at=str(e.get("published", "")),
            external_source=str(e.get("platform", "")),
            raw=e,
        )

    def sleep(self):
        """Local file. Nothing to be polite to."""
        return None
