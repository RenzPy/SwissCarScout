"""tutti.ch adapter — reads the search page, not the API.

WHY THIS WAY
------------
tutti.ch server-renders every listing into a <script id="__NEXT_DATA__"> blob on
the ordinary search results page. Everything we need is already in the HTML any
browser downloads, so there is no reason to call their internal GraphQL API:

  * /fr/q/... search pages are allowed by tutti's robots.txt. /api/ is not.
  * No DevTools step, no reverse-engineered headers, nothing to rediscover when
    they rotate an endpoint.
  * The blob is richer than a listing card: real publish timestamp, seller
    subscription badge, image count, canton, per-language slugs.

SETUP (about 30 seconds)
------------------------
  1. Go to tutti.ch, search Autos/Voitures, set your region, price ceiling and
     "Privat / Particulier" in the sidebar.
  2. Copy the URL out of the address bar. That is it — the gibberish at the end
     is tutti's own encoding of every filter you just set.
  3. Paste it into config.yaml under searches: -> url.

Example:
  https://www.tutti.ch/fr/q/voitures-moutier/Ak8CkY2Fyc5SRkqljb21wYW55QWSncHJpdmF0ZcDA...
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from .base import Source, register
from ..models import Listing

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

# The React Query cache key tutti uses for the search results themselves.
_SEARCH_KEY = "SearchListingsByConstraints"

# Structured car facts tutti carries on the individual listing page, sourced from
# its AutoScout integration. These are worth far more than regex over the body:
# "Reichweite 350 km" in an EV ad reads as mileage to a regex and doesn't here.
PROPERTY_MAP = {
    "cars_carAutoScoutRegistrationYear": "year",
    "cars_carAutoScoutMileage":          "km",
    "cars_carAutoScoutBrand":            "make",
    "cars_carAutoScoutModel":            "model",
    "cars_carAutoScoutFuelType":         "fuel",
    "cars_carAutoScoutTransmissionType": "gearbox",
}


def find_listing_node(obj, want=("listingID", "properties")):
    """Depth-first hunt for the listing record inside a detail page's blob.

    Searched by shape rather than by path: tutti wraps it under a query key we
    would otherwise have to guess, and that wrapper has changed before.
    """
    if isinstance(obj, dict):
        if all(k in obj for k in want):
            return obj
        for v in obj.values():
            hit = find_listing_node(v, want)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_listing_node(v, want)
            if hit is not None:
                return hit
    return None


def properties_to_fields(node: dict) -> dict:
    """Map tutti's `properties` list onto our Listing field names."""
    out: dict = {}
    for p in node.get("properties") or []:
        if not isinstance(p, dict):
            continue
        name = PROPERTY_MAP.get(p.get("listingPropertyID"))
        text = p.get("text")
        if name and text:
            out[name] = text
    # numericPrice is the real number behind the display-only "3 700.-"
    num = (node.get("seoInformation") or {}).get("numericPrice")
    if isinstance(num, (int, float)):
        out["price_chf"] = int(num)
    return out


def describe_refusal(r, url: str) -> str:
    """Say who refused and why, because the right response differs.

    A wrong URL is a bug to fix. A Cloudflare bot block on a datacenter IP is
    the site declining traffic from cloud hosts — that is an answer, not an
    obstacle to route around.
    """
    bits = [f"HTTP {r.status_code} for {url}"]
    server = r.headers.get("server", "")
    if server:
        bits.append(f"server: {server}")
    if r.headers.get("cf-ray"):
        bits.append(f"cf-ray: {r.headers['cf-ray']}  (Cloudflare is deciding)")
    if r.headers.get("retry-after"):
        bits.append(f"retry-after: {r.headers['retry-after']}")

    body = re.sub(r"\s+", " ", r.text[:400]).strip()
    if body:
        bits.append(f"body: {body[:300]}")

    low = (r.text[:4000] + server).lower()
    if r.status_code in (401, 403) and (
            "cloudflare" in low or "cf-ray" in low
            or "attention required" in low or "are you a human" in low):
        bits.append(
            "\n  -> This is bot management refusing, most likely because the "
            "request comes from a datacenter IP.\n"
            "     Do not try to defeat it. Either run this from a normal "
            "residential connection,\n"
            "     or drop tutti and rely on AutoScout24 + Telegram intake.")
    elif r.status_code == 404:
        bits.append("\n  -> Check the search URL is correct and complete.")
    elif r.status_code == 429:
        bits.append("\n  -> Rate limited. Raise politeness.min_delay_s and "
                    "poll_minutes in config.yaml.")
    return "\n  ".join(bits)


def find_search_payload(next_data: dict):
    """Locate the search-results query inside the dehydrated React Query cache.

    Found by key, not by array index: the position of that entry shifts with
    whatever else the page happened to prefetch (profile, categories, footer).
    """
    queries = (next_data.get("props", {})
                        .get("pageProps", {})
                        .get("dehydratedState", {})
                        .get("queries", []))
    for q in queries:
        key = q.get("queryKey") or []
        if key and key[0] == _SEARCH_KEY:
            return (q.get("state", {}).get("data") or {}).get("listings")
    return None


def seller_tier(node: dict) -> str:
    """tutti subscription badge. 'pro' = paying professional/semi-pro seller,
    'plus' = private power seller.

    These appear even inside a "private sellers only" search, which makes the
    PRO badge the single best available hint that a supposedly private ad is
    actually a trader.
    """
    info = node.get("sellerInfo") or {}
    sub = info.get("subscriptionInfo") or {}
    src = ((sub.get("subscriptionBadge") or {}).get("src") or "").lower()
    if "pro.svg" in src:
        return "pro"
    if "plus.svg" in src:
        return "plus"
    return ""


@register
class Tutti(Source):
    name = "tutti"

    def fetch(self) -> Iterable[dict]:
        url = self.search.get("url")
        if not url:
            raise RuntimeError(
                "This search has no 'url'. Copy your tutti.ch search URL out of "
                "the browser address bar into config.yaml — see the notes at the "
                "top of radar/sources/tutti.py")

        pages = int(self.search.get("pages", 1))
        records: list[dict] = []

        for page in range(1, pages + 1):
            page_url = url if page == 1 else f"{url}?page={page}"
            r = self.session.get(page_url, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(describe_refusal(r, page_url))

            m = _NEXT_DATA.search(r.text)
            if not m:
                raise RuntimeError(
                    f"No __NEXT_DATA__ block at {page_url}. Either the page "
                    "layout changed, or that URL is not a search results page.")

            listings = find_search_payload(json.loads(m.group(1)))
            if listings is None:
                raise RuntimeError(
                    f"No '{_SEARCH_KEY}' entry in the page data for {page_url}. "
                    "Is this definitely a search results URL?")

            if page == 1 and listings.get("totalCount") is not None:
                print(f"  [{self.name}] {listings['totalCount']} listings match")

            edges = listings.get("edges") or []
            if not edges:
                break
            records.extend(e["node"] for e in edges if e.get("node"))
            if page < pages:
                self.sleep()

        return records

    def enrich(self, li: Listing) -> None:
        """Fetch the listing's own page for the structured car facts the search
        results leave out: km, year, make, model, fuel, gearbox.

        Same approach as the search page — read the server-rendered blob out of
        HTML tutti serves to any browser. One extra request per listing, so it
        is off by default; turn it on with sources.tutti.detail in config.yaml.
        """
        if not li.url:
            return
        try:
            r = self.session.get(li.url, timeout=30)
            r.raise_for_status()
            m = _NEXT_DATA.search(r.text)
            if not m:
                return
            node = find_listing_node(json.loads(m.group(1)))
            if not node:
                return
            fields = properties_to_fields(node)
        except Exception as exc:                  # one bad page must not stop a run
            print(f"  [{self.name}] enrich failed for {li.external_id}: {exc}")
            return

        for name, value in fields.items():
            setattr(li, name, value)
        li.__post_init__()      # re-normalise: "300000" -> int, "TESLA" -> "tesla"

    def parse(self, node: dict) -> Listing | None:
        ext_id = node.get("listingID")
        if not ext_id:
            return None

        loc = node.get("localization") or {}
        pc = node.get("postcodeInformation") or {}
        canton = (pc.get("canton") or {}).get("shortName", "")
        seo = node.get("seoInformation") or {}

        lang = (node.get("language") or "de").lower()
        base = self.cfg["sources"]["tutti"].get(
            "url_prefix", "https://www.tutti.ch")
        slug = seo.get(f"{lang}Slug") or seo.get("deSlug") or ""
        path = f"/{lang}/vi/{slug}/{ext_id}" if slug else f"/{lang}/vi/{ext_id}"

        # "Biel/Bienne, BE" — matches the bilingual-region test in scoring.py
        loc_name = pc.get("locationName") or ""
        region = f"{loc_name}, {canton}".strip(", ")

        images = node.get("images") or []
        tier = seller_tier(node)

        return Listing(
            source=self.name,
            external_id=str(ext_id),
            url=base.rstrip("/") + path,
            title=loc.get("title") or "",
            body=loc.get("body") or "",
            price_chf=node.get("formattedPrice"),   # "2 000.-" -> parse_int
            km=None,                                 # not in the summary payload
            year=None,
            make="", model="",
            seller_type="dealer" if tier == "pro" else "private",
            region=region,
            photo_count=len(images) if isinstance(images, list) else 0,
            published_at=node.get("timestamp") or "",
            # Non-null when the ad lives on a partner platform. tutti and Ricardo
            # are both SMG, so Ricardo ads surface here natively -- this is how
            # you get Ricardo coverage without touching Ricardo's own frontend.
            external_source=node.get("formattedSource") or "",
            seller_tier=tier,
            raw=node,
        )


@register
class Anibis(Tutti):
    """anibis.ch runs the same Next.js stack. Same parser, own config block."""
    name = "anibis"

    def fetch(self):
        self.cfg["sources"]["tutti"] = self.cfg["sources"].get("anibis", {})
        return super().fetch()
