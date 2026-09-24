"""Turn a pasted listing into a manual.yaml entry.

The friction in manual intake was never the finding, it was the typing. Copy the
listing text out of Facebook (or anywhere), pipe it in, and this drafts the
entry for you:

    python run.py --paste

Paste, then Ctrl-D. It extracts what it can and leaves the rest for you to fill.

The useful trick: marketplace listings print relative dates ("Publié il y a
9 semaines dans Delémont, JU"). That single line gives both the seller's real
post date and the region -- and post date is what drives the staleness score,
which is the whole point of tracking a listing you found by hand.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# "Publié il y a 9 semaines dans Delémont, JU" / "vor 9 Wochen in Biel, BE"
_RELATIVE = re.compile(
    r"(?:il y a|vor)\s+(\d+)\s*"
    r"(jours?|semaines?|mois|ans?|Tage?n?|Wochen?|Monaten?|Jahren?)"
    r"(?:\s+(?:dans|in)\s+([^\n,]+(?:,\s*[A-Z]{2})?))?",
    re.I)

_UNIT_DAYS = {
    "jour": 1, "jours": 1, "tag": 1, "tage": 1, "tagen": 1,
    "semaine": 7, "semaines": 7, "woche": 7, "wochen": 7,
    "mois": 30, "monat": 30, "monaten": 30,
    "an": 365, "ans": 365, "jahr": 365, "jahren": 365,
}

# 3 500 CHF / CHF 3'500.- / 3500.-
# Note [^\S\n] not \s: a bare \s spans newlines, which glued the "2007" on a
# title line onto the "3 500" price below it and produced 20073500.
_NUM = r"\d[\d'’.\u202f]*(?:[^\S\n][\d]{3})*"
_PRICE = re.compile(rf"(?:CHF[^\S\n]*)?({_NUM})[^\S\n]*(?:CHF|\.-|francs?)", re.I)
_KM = re.compile(rf"({_NUM})[^\S\n]*km\b", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")


def _digits(raw: str) -> int | None:
    d = re.sub(r"[^\d]", "", raw)
    return int(d) if d else None


def parse_pasted(text: str, today: date | None = None) -> dict:
    """Best-effort field extraction. Never guesses silently -- anything it
    couldn't find is simply absent, so you can see what needs filling in."""
    today = today or date.today()
    out: dict = {}

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        out["title"] = lines[0]

    m = _RELATIVE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower().rstrip("n")
        days = _UNIT_DAYS.get(unit) or _UNIT_DAYS.get(unit + "s")
        if days:
            out["published"] = str(today - timedelta(days=n * days))
        if m.group(3):
            out["region"] = m.group(3).strip()

    # Take the largest plausible price: struck-through "was" prices are common,
    # and the current asking price is what matters for the baseline.
    prices = [p for p in (_digits(x) for x in _PRICE.findall(text))
              if p and 100 <= p <= 500_000]
    if prices:
        out["price"] = min(prices)          # current price, not the old one

    kms = [k for k in (_digits(x) for x in _KM.findall(text))
           if k and 1_000 <= k <= 900_000]
    if kms:
        out["km"] = max(kms)

    years = [int(y) for y in _YEAR.findall(text)]
    # A registration year is the oldest 4-digit year in the text; inspection
    # dates and service dates are always later than it.
    if years:
        out["year"] = min(years)

    out["body"] = " ".join(lines[1:])[:600] if len(lines) > 1 else ""
    return out


def to_yaml_entry(fields: dict, entry_id: str) -> str:
    """Render a manual.yaml block, with TODO markers on what wasn't found."""
    order = ["title", "body", "price", "km", "year", "make", "model",
             "region", "url", "photos", "published", "platform", "seller_type"]
    todo = {"make": "TODO", "model": "TODO", "url": "TODO",
            "photos": 0, "platform": "Facebook Marketplace",
            "seller_type": "private"}

    out = [f"- id: {entry_id}"]
    for key in order:
        val = fields.get(key, todo.get(key))
        if val is None or val == "":
            continue
        if key == "body":
            out.append(f"  body: >\n    {val}")
        elif isinstance(val, str) and (":" in val or val == "TODO"):
            out.append(f'  {key}: "{val}"')
        else:
            out.append(f"  {key}: {val}")
    return "\n".join(out) + "\n"
