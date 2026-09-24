"""Scoring. This is the actual product — scraping is the easy half.

Two modes:
  buy     -> cars you want to purchase (underpriced, expired MFK, motivated seller)
  broker  -> sellers you want as clients (stale, price-cut, weak ad, one language)
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .models import Listing


# Crude but effective language detection for bilingual regions. A French-only
# ad in Biel is invisible to two thirds of the buyer pool — that is the single
# most persuasive thing you can tell a stuck seller.
_FR = {"le", "la", "les", "de", "du", "des", "avec", "pour", "est", "très",
       "vend", "vendre", "voiture", "état", "pneus", "jantes", "prix", "cause",
       "expertisé", "expertise", "boîte", "essence", "sièges", "chauffants"}
_DE = {"der", "die", "das", "und", "mit", "für", "ist", "sehr", "verkaufe",
       "fahrzeug", "zustand", "reifen", "felgen", "preis", "wegen", "ab",
       "geprüft", "getriebe", "benzin", "sitze", "gepflegt", "unfallfrei"}


def detect_language(text: str) -> tuple[str, float]:
    words = set(text.lower().replace(",", " ").replace(".", " ").split())
    fr, de = len(words & _FR), len(words & _DE)
    total = fr + de
    if total < 3:
        return "unknown", 0.0
    if fr > de:
        return "fr", fr / total
    if de > fr:
        return "de", de / total
    return "mixed", 0.5


@dataclass
class Score:
    value: float = 0.0
    reasons: list = None
    baseline: int = None
    n_comps: int = 0
    retail: int = None        # what garages ask for the same car
    retail_n: int = 0
    basis: str = ""           # "private" | "dealer-adjusted"

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []

    def add(self, points: float, reason: str):
        self.value += points
        self.reasons.append(reason)


def _keyword_hits(text: str, cfg: dict, score: Score, mode: str):
    for group, spec in cfg.get("keywords", {}).items():
        weight = spec.get("weight", {}).get(mode, 0)
        if not weight:
            continue
        for term in spec.get("terms", []):
            if term.lower() in text:
                score.add(weight, f"{group}: “{term}”")
                break  # each group counts once, however many synonyms match


def score_listing(conn, li: Listing, listing_id: int, cfg: dict, mode: str) -> Score:
    s = Score()
    text = li.text.lower()
    w = cfg.get("weights", {}).get(mode, {})

    # --- hard filters ------------------------------------------------------
    # Sources spell this differently: tutti gives "dealer", AutoScout24 gives
    # "professional". Both mean a trader.
    if (li.seller_type in db.DEALER_TYPES
            and cfg.get("filters", {}).get("skip_dealers", True)):
        s.add(-999, f"{li.seller_type} listing")
        return s

    budget = cfg.get("filters", {}).get("max_price_chf")
    if mode == "buy" and budget and li.price_chf and li.price_chf > budget:
        s.add(-999, f"above budget ({li.price_chf} > {budget})")
        return s

    # --- keyword signals ---------------------------------------------------
    _keyword_hits(text, cfg, s, mode)

    # --- seller subscription badge ----------------------------------------
    # A tuttiPRO badge on an ad that passed a "private sellers only" filter is
    # the clearest signal available that you are looking at a semi-professional.
    # They price accurately, and they do not need someone to sell the car for
    # them, so they are worth less in both modes.
    if li.seller_tier == "pro":
        spec = cfg.get("keywords", {}).get("seller_tier_pro", {})
        w = spec.get("weight", {}).get(mode, 0)
        if w:
            s.add(w, "tuttiPRO seller (semi-professional)")

    # --- structured facts, where the source states them outright ----------
    # These beat the keyword hunt: a source that tells you the inspection date
    # is better evidence than an ad that happens to use the word "MFK".
    sf = cfg.get("structured", {})

    if li.mfk_date or li.inspected is not None:
        from .sources.autoscout24 import _months_since
        months = _months_since(li.mfk_date)
        valid_for = sf.get("mfk_valid_months", 24)
        if li.inspected is False or (months is not None and months > valid_for):
            when = f"{months:.0f} months ago" if months else "not stated"
            s.add(sf.get("mfk_expired", {}).get(mode, 0),
                  f"MFK expired or due (last inspection {when})")
        elif months is not None and months <= 3:
            s.add(sf.get("mfk_fresh", {}).get(mode, 0),
                  f"fresh MFK ({months:.0f} months ago)")

    if li.had_accident:
        s.add(sf.get("accident", {}).get(mode, 0), "declared accident history")

    if li.previous_price and li.price_chf and li.previous_price > li.price_chf:
        cut = li.previous_price - li.price_chf
        s.add(sf.get("declared_price_cut", {}).get(mode, 0),
              f"price cut by CHF {cut:,.0f}".replace(",", "'"))

    # --- price vs the market ----------------------------------------------
    baseline, n, basis = db.market_estimate(conn, li, cfg)
    s.baseline, s.n_comps, s.basis = baseline, n, basis
    s.retail, s.retail_n = db.baseline_price(conn, li, cfg, seller_scope="dealer")
    if baseline and li.price_chf:
        delta = (baseline - li.price_chf) / baseline
        min_delta = cfg.get("price", {}).get("min_delta", 0.15)
        if mode == "buy" and delta >= min_delta:
            mult = min(delta / min_delta, 3.0)
            # A dealer-derived estimate is softer evidence than real private
            # comparables, so it carries less weight.
            if basis == "dealer-adjusted":
                mult *= 0.7
            s.add(w.get("underpriced", 4) * mult,
                  f"{delta*100:.0f}% under {basis} estimate of "
                  f"{baseline} CHF (n={n})")
        if mode == "broker" and delta <= -min_delta:
            # Overpriced cars are exactly the ones that sit unsold. Good leads.
            s.add(w.get("overpriced", 3),
                  f"{abs(delta)*100:.0f}% above median of {baseline} CHF (n={n})")

    # --- weak listing ------------------------------------------------------
    if li.photo_count and li.photo_count < cfg.get("filters", {}).get("few_photos", 4):
        s.add(w.get("few_photos", 2), f"only {li.photo_count} photos")
    if len(li.body) < cfg.get("filters", {}).get("short_body", 200):
        s.add(w.get("short_body", 1.5), f"thin description ({len(li.body)} chars)")

    # --- age and price cuts ------------------------------------------------
    age = db.listing_age_days(conn, listing_id)
    drops = db.price_drops(conn, listing_id)
    stale_after = cfg.get("filters", {}).get("stale_days", 40)
    if age >= stale_after:
        s.add(w.get("stale", 3) * min(age / stale_after, 2.5),
              f"listed {age:.0f} days")
    if drops:
        s.add(w.get("price_drop", 2.5) * drops,
              f"{drops} price cut{'s' if drops > 1 else ''}")

    # --- single-language ad in a bilingual region --------------------------
    lang, conf = detect_language(li.text)
    bilingual = [r.lower() for r in cfg.get("filters", {}).get("bilingual_regions", [])]
    in_bilingual = any(b in (li.region or "").lower() for b in bilingual)
    if in_bilingual and lang in ("fr", "de") and conf >= 0.8:
        s.add(w.get("single_language", 3),
              f"{lang.upper()}-only ad in a bilingual region")

    return s


def render_alert(li: Listing, s: Score, mode: str) -> str:
    """Plain-text alert body. Telegram is set to HTML parse mode in notify."""
    head = "🚗 DEAL" if mode == "buy" else "🤝 LEAD"
    bits = [f"<b>{head} · score {s.value:.1f}</b>", f"<b>{li.title}</b>"]

    facts = []
    if li.price_chf:
        facts.append(f"CHF {li.price_chf:,}".replace(",", "'"))
    if li.km:
        facts.append(f"{li.km:,} km".replace(",", "'"))
    if li.year:
        facts.append(str(li.year))
    if li.region:
        facts.append(li.region)
    if li.external_source:
        facts.append(f"via {li.external_source}")
    if facts:
        bits.append(" · ".join(facts))

    if s.baseline:
        label = ("Private median" if s.basis == "private"
                 else "Est. private value")
        bits.append(f"{label}: CHF {s.baseline:,}".replace(",", "'")
                    + f" (n={s.n_comps})")
    if s.retail:
        bits.append(f"Garage asking: CHF {s.retail:,}".replace(",", "'")
                    + f" (n={s.retail_n})")

    bits.append("")
    bits += [f"• {r}" for r in s.reasons]
    if li.url:
        bits += ["", li.url]
    return "\n".join(bits)
