"""AutoScout24.ch adapter — implemented directly against their JSON API.

No third-party scraper package: the request shape below is implemented here so
there's one less dependency between you and a site that can change.

  POST https://api.autoscout24.ch/v1/listings/search
  {
    "query":      {"vehicleCategories": ["car"], ...filters},
    "sort":       [{"type": "PRICE", "order": "ASC"}],
    "pagination": {"page": 0, "size": 20}
  }
  -> {"content": [...], "totalPages": N, "totalElements": M}

WHY SORT EXPLICITLY BY PRICE
----------------------------
With no sort specified the API injects a rotating boosted ("top-list") listing
at position 0 on every request. That shifts the page window underneath you, so
listings get skipped or duplicated across pages. A stable sort makes pagination
deterministic.

WHY THIS SOURCE IS WORTH MORE THAN THE OTHERS
---------------------------------------------
tutti gives you ad text you have to read signals out of. This gives you the
signals themselves, already structured:

  inspected            bool    -- MFK status, as a field
  lastInspectionDate   date    -- when it was last through MFK
  hadAccident          bool    -- accident history, declared
  previousPrice        float   -- the old price, so a drop is visible on sight
  createdDate          ISO     -- true listing age from the first run
  seller.type          str     -- "private" | "professional"
  seller.zipCode/city  str     -- location, for radius filtering
  mileage, firstRegistrationYear, make.key, model.key

BEFORE YOU ENABLE THIS
----------------------
robots.txt is per-host, and this is api.autoscout24.ch, not www. Check
https://api.autoscout24.ch/robots.txt and read AutoScout24's terms of use before
turning `enabled` on in config.yaml. It ships disabled deliberately.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .base import Source, register
from ..models import Listing

API_BASE = "https://api.autoscout24.ch/v1"
PAGE_SIZE = 20

IMG_BASE = "https://listing-images.autoscout24.ch/"


def _iso_date(value) -> str:
    """'2026-07-02T10:52:42.486Z' or '2025-01-28' -> 'YYYY-MM-DD'."""
    if not value or not isinstance(value, str):
        return ""
    return value[:10]


def _months_since(datestr: str) -> float | None:
    if not datestr:
        return None
    try:
        d = datetime.strptime(datestr[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).days / 30.44


@register
class AutoScout24(Source):
    name = "autoscout24"

    # ---- request ---------------------------------------------------------
    def _build_query(self) -> dict:
        s = self.search
        q: dict = {"vehicleCategories": [s.get("vehicle_category", "car")]}

        # Optional make/model pinning. Omit both to search the whole category —
        # worth verifying against the live API on your first run, since the
        # official frontend always sends at least a category.
        if s.get("make") and s.get("model"):
            q["makeModelVersions"] = [
                {"makeKey": s["make"], "modelKey": s["model"]}]

        for key, field in (
            ("price_from", "priceFrom"), ("price_to", "priceTo"),
            ("km_from", "mileageFrom"), ("km_to", "mileageTo"),
            ("year_from", "firstRegistrationYearFrom"),
            ("year_to", "firstRegistrationYearTo"),
        ):
            if s.get(key) is not None:
                q[field] = s[key]
        return q

    def fetch(self) -> Iterable[dict]:
        query = self._build_query()
        sort = [{"type": "PRICE", "order": "ASC"}]   # see module docstring

        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "de-CH,de;q=0.9,fr-CH;q=0.8",
        })

        seen: set = set()
        out: list[dict] = []
        page, total_pages = 0, 1
        max_pages = int(self.search.get("pages", 5))

        while page < total_pages and page < max_pages:
            body = {"query": query, "sort": sort,
                    "pagination": {"page": page, "size": PAGE_SIZE}}
            r = self.session.post(
                f"{API_BASE}/listings/search", json=body, timeout=30)
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError:
                enc = r.headers.get("content-encoding", "none")
                raise RuntimeError(
                    f"HTTP {r.status_code} but the body isn't JSON.\n"
                    f"  content-type: {r.headers.get('content-type')}\n"
                    f"  content-encoding: {enc}\n"
                    f"  first bytes: {r.content[:60]!r}\n"
                    + ("  -> The server used an encoding we can't decode. "
                       "Check Accept-Encoding in radar/sources/base.py."
                       if enc not in ("gzip", "deflate", "none", "")
                       else "  -> Unexpected body; the endpoint may have moved.")
                ) from None

            total_pages = data.get("totalPages", 1)
            if page == 0:
                pages_to_do = min(total_pages, max_pages)
                eta = pages_to_do * (self.min_delay + self.max_delay) / 2
                print(f"  [{self.name}] {data.get('totalElements', '?')} matches, "
                      f"{total_pages} pages available, fetching {pages_to_do} "
                      f"(~{eta/60:.0f} min)")

            for item in data.get("content", []):
                if item.get("id") not in seen:
                    seen.add(item["id"])
                    out.append(item)

            page += 1
            # Long runs need to look alive. One line per 10 pages is enough to
            # tell a slow fetch apart from a hung one.
            if page % 10 == 0 or page >= min(total_pages, max_pages):
                print(f"  [{self.name}] page {page}/"
                      f"{min(total_pages, max_pages)} · "
                      f"{len(out)} listings", flush=True)
            if page < total_pages and page < max_pages:
                self.sleep()

        return out

    def iter_listings(self):
        """Page-by-page generator. An index run is minutes long; buffering it
        all means an SSH drop or a network blip loses every page fetched."""
        prefixes = self.search.get("zip_prefixes") or []
        for item in self.fetch():
            li = self.parse(item)
            if li is None:
                continue
            if prefixes and not any(
                    li.region.strip().startswith(str(p)) for p in prefixes):
                continue
            yield li

    # ---- mapping ---------------------------------------------------------
    def parse(self, item: dict) -> Listing | None:
        lid = item.get("id")
        if not lid:
            return None

        seller = item.get("seller") or {}
        make = item.get("make") or {}
        model = item.get("model") or {}

        title = " ".join(str(p) for p in (
            make.get("name"), model.get("name"), item.get("versionFullName")
        ) if p).strip()

        # The teaser is a headline; description is the real body text.
        body = " ".join(str(p) for p in (
            item.get("teaser"), item.get("description")) if p).strip()

        city = seller.get("city") or ""
        zipc = seller.get("zipCode") or ""
        region = f"{zipc} {city}".strip()

        return Listing(
            source=self.name,
            external_id=str(lid),
            url=item.get("url") or f"https://www.autoscout24.ch/de/d/{lid}",
            title=title,
            body=body,
            price_chf=item.get("price"),
            km=item.get("mileage"),
            year=item.get("firstRegistrationYear"),
            make=make.get("key") or make.get("name") or "",
            model=model.get("key") or model.get("name") or "",
            fuel=item.get("fuelType") or "",
            gearbox=item.get("transmissionTypeGroup")
                    or item.get("transmissionType") or "",
            seller_type=(seller.get("type") or "unknown").lower(),
            region=region,
            photo_count=len(item.get("images") or []),
            published_at=_iso_date(item.get("createdDate")),
            mfk_date=_iso_date(item.get("lastInspectionDate")),
            inspected=item.get("inspected"),
            had_accident=item.get("hadAccident"),
            previous_price=item.get("previousPrice"),
            raw=item,
        )

    # ---- detail enrichment -----------------------------------------------
    def enrich(self, li) -> None:
        """Fetch one listing's full record.

        The search response omits the fields this whole tool exists for:
        lastInspectionDate, inspected, hadAccident, previousPrice. They are
        only on GET /v1/listings/{id}. One extra request per listing.
        """
        try:
            r = self.session.get(f"{API_BASE}/listings/{li.external_id}",
                                 timeout=30)
            if r.status_code != 200:
                return
            item = r.json()
        except Exception as exc:
            print(f"  [{self.name}] enrich failed for {li.external_id}: {exc}")
            return

        li.mfk_date = _iso_date(item.get("lastInspectionDate"))
        li.inspected = item.get("inspected")
        li.had_accident = item.get("hadAccident")
        li.previous_price = item.get("previousPrice")
        desc = item.get("description")
        if desc and len(desc) > len(li.body):
            li.body = desc

    # ---- client-side location filter -------------------------------------
    def collect(self) -> list[Listing]:
        """AutoScout24's API has no location constraint we can rely on, so the
        region filter is applied here against seller.zipCode."""
        listings = super().collect()
        prefixes = self.search.get("zip_prefixes") or []
        if not prefixes:
            return listings
        keep = [li for li in listings
                if any(li.region.strip().startswith(str(p)) for p in prefixes)]
        print(f"  [{self.name}] {len(keep)}/{len(listings)} within "
              f"zip prefixes {prefixes}")
        return keep
