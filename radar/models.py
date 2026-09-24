"""Normalised listing model shared by every source adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# Patterns we scrub before anything hits the database. We do not need seller
# contact details to find deals, and storing them would make us a data
# controller under the revised DSG. Keep it that way.
_PHONE = re.compile(r"(?:\+41|0041|0)\s?[1-9](?:[\s./-]?\d{2,3}){3,4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def scrub(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _PHONE.sub("[tel]", text)
    text = _EMAIL.sub("[email]", text)
    return text.strip()


def parse_int(value) -> Optional[int]:
    """Pull an integer out of '195 000 km', "CHF 3'700.-", 3700.0, etc."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


# Manufacturers seen on the Swiss market, taken from AutoScout24's own brand
# list. Used to decide whether a title actually names a car.
MAKES = {
    "abarth", "alfa", "alpina", "alpine", "aston", "audi", "bentley", "bmw",
    "byd", "cadillac", "chevrolet", "chrysler", "citroen", "citroën", "cupra",
    "dacia", "daewoo", "daihatsu", "dodge", "ds", "ferrari", "fiat", "ford",
    "genesis", "honda", "hummer", "hyundai", "infiniti", "isuzu", "iveco",
    "jaguar", "jeep", "kia", "lada", "lamborghini", "lancia", "land",
    "landrover", "lexus", "lotus", "maserati", "mazda", "mclaren", "mercedes",
    "mercedes-benz", "mg", "mini", "mitsubishi", "nissan", "opel", "peugeot",
    "polestar", "porsche", "renault", "rolls-royce", "rover", "saab", "seat",
    "skoda", "smart", "ssangyong", "subaru", "suzuki", "tesla", "toyota",
    "vauxhall", "volvo", "vw", "volkswagen",
}


@dataclass
class Listing:
    source: str
    external_id: str
    url: str = ""
    title: str = ""
    body: str = ""
    price_chf: Optional[int] = None
    km: Optional[int] = None
    year: Optional[int] = None
    make: str = ""
    model: str = ""
    fuel: str = ""
    gearbox: str = ""
    seller_type: str = "unknown"      # private | dealer | unknown
    region: str = ""
    photo_count: int = 0
    published_at: str = ""      # the site's own publish timestamp, when it gives us one
    external_source: str = ""   # origin platform when the ad isn't native (Ricardo, ...)
    mfk_date: str = ""          # last inspection date, when the source states it
    inspected: bool | None = None
    had_accident: bool | None = None
    previous_price: float | None = None
    seller_tier: str = ""       # tutti subscription badge: pro | plus | ""
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        self.title = scrub(self.title)
        self.body = scrub(self.body)
        self.price_chf = parse_int(self.price_chf)
        self.km = parse_int(self.km)
        self.year = parse_int(self.year)
        self.make = (self.make or "").strip().lower()
        self.model = (self.model or "").strip().lower()

    @property
    def model_key(self) -> str:
        """Bucket used to group comparables for the price baseline.

        Only ever built from a recognised manufacturer. The old behaviour --
        falling back to the first two words of the title -- turned a German
        advert opening "Zum Verkauf steht ein gepflegter Audi S5..." into the
        model key "zum verkauf", which then sat in the comparables table
        forever matching nothing and grouping unrelated cars together.

        No recognised make means no key, and scoring says so plainly rather
        than reporting a confident zero.
        """
        if self.make and self.model:
            return f"{self.make} {self.model}".strip()
        if self.make:
            return self.make.strip()

        words = re.findall(r"[a-z0-9-]+", self.title.lower())
        for i, w in enumerate(words):
            if w in MAKES:
                nxt = words[i + 1] if i + 1 < len(words) else ""
                return f"{w} {nxt}".strip()
        return ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        return d
