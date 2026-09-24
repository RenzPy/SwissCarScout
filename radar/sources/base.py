"""Base class for source adapters.

Adding a platform means writing two methods: fetch() to get raw records, and
parse() to turn one record into a Listing. Everything downstream is shared.
"""
from __future__ import annotations

import time
import random
from typing import Iterable

import requests

from ..models import Listing

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


class Source:
    name = "base"

    def __init__(self, cfg: dict, search: dict):
        self.cfg = cfg
        self.search = search
        self.session = requests.Session()
        # Ask for what we actually want. These adapters read HTML search
        # pages; announcing Accept: application/json for an HTML document was a
        # leftover from an earlier API-based design and is simply the wrong
        # request. Adapters that do want JSON override this themselves.
        self.session.headers.update({
            "User-Agent": cfg.get("user_agent", UA),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-CH,fr;q=0.9,de-CH;q=0.8,en;q=0.7",
            # Deliberately NOT "br": requests only decodes brotli when the
            # brotli package is installed, and announcing an encoding we can't
            # decode gets you compressed bytes and a JSON error at char 0.
            # gzip/deflate are handled natively and are plenty.
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })
        self.min_delay = cfg.get("politeness", {}).get("min_delay_s", 3)
        self.max_delay = cfg.get("politeness", {}).get("max_delay_s", 7)

    def sleep(self):
        """Stay well under any rate limit. You are one person looking for one
        car, not a data-harvesting operation. Act like it."""
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def fetch(self) -> Iterable[dict]:
        raise NotImplementedError

    def parse(self, record: dict) -> Listing | None:
        raise NotImplementedError

    def enrich(self, li: Listing) -> None:
        """Optionally fetch a listing's own page for fields the search results
        don't carry. Default: do nothing. Costs one request per listing, so the
        caller decides whether it's worth it."""
        return None

    def iter_listings(self):
        """Yield listings as they become available.

        The default buffers everything first, because most sources return one
        page. Sources that paginate for minutes should override this and yield
        per page, so a long run that dies partway has still saved its work.
        """
        yield from self.collect()

    def collect(self) -> list[Listing]:
        out = []
        for record in self.fetch():
            try:
                li = self.parse(record)
            except Exception as exc:  # one bad record must not kill the run
                print(f"  [{self.name}] parse error: {exc}")
                continue
            if li:
                out.append(li)
        return out


_REGISTRY: dict[str, type[Source]] = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Source]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown source '{name}'. known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def known() -> list[str]:
    return sorted(_REGISTRY)
