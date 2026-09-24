"""Model lookup for /price and /models.

The matching problem: you will type "Audi A3", "a3", "golf gti", "audi a3
2011 150000km". The database holds keys like "audi a3" and "vw golf". Exact
matching fails on nearly all of those, and silently guessing wrong is worse
than asking — a median for the wrong car sends you to a viewing with the wrong
number in your head.

So: match on token overlap, resolve confidently when one model clearly wins,
and ask when it genuinely doesn't. "a3" matches both "audi a3" and
"mercedes-benz a-180", and the right answer there is a question.
"""
from __future__ import annotations

import re

from . import db
from .models import MAKES

YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
BIG_NUMBER = re.compile(r"\b\d{4,7}\b")


def parse_query(q: str) -> tuple[list[str], int | None, int | None]:
    """Split a query into name tokens, a year and a mileage.

    A 4-digit number in model-year range is a year; a larger one is mileage.
    Both are filters, not part of the name — "audi a3 2011" should match the
    same model as "audi a3".
    """
    year = None
    m = YEAR.search(q)
    if m:
        year = int(m.group(1))

    km = None
    for n in BIG_NUMBER.findall(q):
        v = int(n)
        if v > 3000 and (year is None or v != year):
            km = v
            break

    cleaned = YEAR.sub(" ", q)
    cleaned = BIG_NUMBER.sub(" ", cleaned)
    tokens = [t for t in re.split(r"[^a-z0-9]+", cleaned.lower()) if t]
    return tokens, year, km


def match_models(tokens: list[str], keys: list[str]) -> tuple[list[str], bool]:
    """Model keys matching the query, best first.

    Returns (hits, exact). `exact` is True when the query names one model
    outright -- in that case it must resolve immediately. Offering the user
    back the exact string they just typed, as one of several options, is an
    infinite loop: tap it, get the same menu.
    """
    if not tokens:
        return [], False

    query_key = " ".join(tokens)
    normalised = {k: " ".join(re.split(r"[^a-z0-9]+", k.lower())).strip()
                  for k in keys}

    # 1. exact hit wins outright
    for k, norm in normalised.items():
        if norm == query_key:
            return [k], True

    named_make = next((t for t in tokens if t in MAKES), None)

    scored = []
    for key, norm in normalised.items():
        parts = [p for p in norm.split() if p]
        if named_make and parts and parts[0] != named_make:
            continue
        exact_tok = sum(1 for t in tokens if t in parts)
        loose_tok = sum(1 for t in tokens
                        if any(p.startswith(t) or t.startswith(p) for p in parts))
        if not loose_tok:
            continue
        # whole-token matches beat prefix matches, so "bmw 320" ranks above
        # "bmw 320-gran-turismo" instead of tying with it
        scored.append((exact_tok, loose_tok / len(tokens), -len(parts), key))

    if not scored:
        return [], False
    scored.sort(reverse=True)

    # 2. a clear winner resolves; a tie asks
    top = scored[0]
    tied = [r for r in scored if (r[0], r[1]) == (top[0], top[1])]
    if len(tied) == 1:
        return [top[3]], False

    return [r[3] for r in tied][:6], False


def _chf(n) -> str:
    return f"{int(n):,}".replace(",", "'")


def _seen(datestr: str, today: str) -> str:
    """Only show a date when it is not today. Everything carrying '(09-22)'
    is visual noise that pushes lines onto a second row on a phone."""
    if not datestr or datestr == today:
        return ""
    return f" · seen {datestr[5:]}"


def _bucket_block(icon: str, label: str, b: dict, today: str) -> list[str]:
    if not b.get("n"):
        return []
    out = [f"{icon} <b>{label}</b> · {b['n']} car" + ("s" if b["n"] != 1 else "")]
    out.append(f"    median <b>CHF {_chf(b['median'])}</b>")
    if b["n"] > 1:
        out.append(f"    {_chf(b['low'])} – {_chf(b['high'])}"
                   + _seen(b.get("low_seen", ""), today))
    return out + [""]


def price_report(conn, cfg: dict, model_key: str, year: int | None = None,
                 km: int | None = None) -> str:
    from .bands import bands_from_cfg, coverage

    span = int(cfg.get("price", {}).get("year_window", 3))
    yf, yt = (year - span, year + span) if year else (None, None)
    st = db.model_stats(conn, model_key, yf, yt)

    if not st["total"]:
        return f"Nothing indexed for <b>{model_key}</b> yet."

    today = (st.get("last_seen") or "")[:10]
    priv, deal, gone = st["private"], st["dealer"], st["gone"]

    swept = [b for b, when in coverage(conn, cfg) if when != "never"]
    all_bands = bands_from_cfg(cfg)
    partial = len(st["bands"]) <= 1 and len(swept) < len(all_bands)

    out = [f"🚗 <b>{model_key.upper()}</b>",
           f"<i>{st['total']} indexed"
           + (f" · {yf}–{yt}" if year else "") + "</i>", ""]

    # --- the headline number ---------------------------------------------
    # Comes from db.estimate(), the same function scoring uses, so the number
    # here and the number on a pasted car can no longer disagree.
    kmrows = db.km_breakdown(conn, model_key, yf, yt)
    meds = [r["median"] for r in kmrows if r["n"] >= 3]
    spread = (max(meds) / min(meds)) if len(meds) >= 2 and min(meds) else 1.0

    if not year:
        out += ["💡 <b>Ask with a year for a value</b>",
                f"    <code>/price {model_key} 2012 150000</code>",
                "    <i>one figure across all years and mileages "
                "isn't a price</i>", ""]
    elif not km and spread > 1.5:
        out += ["📏 <b>Mileage drives this one</b>",
                f"    CHF {_chf(min(meds))} – {_chf(max(meds))} depending on km",
                f"    <code>/price {model_key} {year} 150000</code>", ""]
    else:
        e = db.estimate(conn, cfg, model_key, year, km)
        icon = {"high": "🟢", "medium": "🟡"}.get(e["confidence"], "🔴")
        label = f"At {_chf(km)} km" if km else "Est. private value"
        if e["value"]:
            out += [f"{icon} <b>{label}</b>",
                    f"    <b>CHF {_chf(e['value'])}</b>",
                    f"    <i>{e['basis']}</i>", ""]
        else:
            out += [f"🔴 <b>{label}</b>",
                    f"    <i>no reliable figure — {e['basis']}</i>", ""]

    out += _bucket_block("👤", "Private", priv, today)
    out += _bucket_block("🏢", "Dealer", deal, today)
    if gone.get("n"):
        out += _bucket_block("✅", "Sold / delisted", gone, today)
        out += ["    <i>closest thing to what actually cleared</i>", ""]

    if not year:
        rows = db.year_breakdown(conn, model_key)
        if len(rows) > 1:
            out += ["📅 <b>By year</b>"]
            for r in rows:
                out.append(f"    {r['span']}  ·  {r['n']:>3} cars  ·  "
                           f"<b>CHF {_chf(r['median'])}</b>")
            out.append("")

    kmrows = db.km_breakdown(conn, model_key, yf, yt)
    if len(kmrows) > 1:
        out += ["🛣 <b>By mileage</b>"
                + (f" <i>({yf}–{yt})</i>" if year else "")]
        target = db.km_bucket_for(km) if km else ""
        for r in kmrows:
            mark = " ←" if r["label"] == target else ""
            out.append(f"    {r['label']:<10}  ·  {r['n']:>3} cars  ·  "
                       f"<b>CHF {_chf(r['median'])}</b>{mark}")
        out.append("")

    # --- the honesty line ------------------------------------------------
    if partial:
        band = st["bands"][0] if st["bands"] else ""
        # Bands are "1500-3000", but never assume: a malformed or legacy value
        # must not take the whole report down.
        try:
            lo, hi = band.split("-")
            where = f"the CHF {_chf(lo)}–{_chf(hi)} band"
        except (ValueError, AttributeError):
            where = "a single price band"
        out += ["⚠️ <b>Partial data</b>",
                f"    Every {model_key} indexed so far sits in {where}.",
                f"    {len(swept)} of {len(all_bands)} bands swept — these "
                f"figures will move a lot."]
    elif len(swept) < len(all_bands):
        out += [f"<i>{len(swept)}/{len(all_bands)} price bands swept so far.</i>"]

    return "\n".join(out).rstrip()


def price_candidates(conn, query: str):
    """(hits, year, km) -- lets the caller offer buttons instead of text."""
    tokens, year, km = parse_query(query)
    hits, _exact = match_models(tokens, db.known_model_keys(conn))
    return hits, year, km


def handle_price(conn, cfg: dict, query: str) -> str:
    if not query.strip():
        return ("<b>Usage</b>\n"
                "<code>/price audi a3</code>\n"
                "<code>/price audi a3 2011</code>\n"
                "<code>/price audi a3 2011 150000</code>\n\n"
                "<i>Year and mileage both matter — a 2012 A5 at 90k and at "
                "220k are different cars.</i>\n\n"
                "<i>Tap a model below, or send /models for the full list.</i>")

    tokens, year, km = parse_query(query)
    keys = db.known_model_keys(conn)
    hits, exact = match_models(tokens, keys)

    if not hits:
        return (f"Nothing indexed matching “{query.strip()}”.\n"
                f"{len(keys)} models are indexed — /models to see the main ones.")

    if len(hits) > 1:
        listed = "\n".join(f"    /price {h}" for h in hits)
        return f"🤔 <b>Which one?</b>\n\n{listed}"

    report = price_report(conn, cfg, hits[0], year, km)

    # Resolved to something other than what was asked for: say so. A TTS is
    # not a TT, and quietly answering about the wrong car is worse than
    # answering about none.
    asked = " ".join(tokens)
    if not exact and asked and asked != hits[0]:
        report = (f"<i>Showing <b>{hits[0]}</b> for “{query.strip()}” — "
                  f"closest indexed match.</i>\n\n" + report)
    return report


def handle_models(conn, limit: int = 30) -> str:
    rows = conn.execute(
        """SELECT model_key, COUNT(*) n FROM listings
           WHERE model_key != '' AND price_chf > 0
           GROUP BY model_key ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return "Nothing indexed yet. Run the index first."
    total = conn.execute(
        "SELECT COUNT(DISTINCT model_key) FROM listings "
        "WHERE model_key != ''").fetchone()[0]
    out = [f"📊 <b>Best covered models</b>", f"<i>{total} indexed in total</i>", ""]
    out += [f"  <b>{r['n']:>4}</b>  {r['model_key']}" for r in rows]
    out += ["", "<i>Tap one below, or type "
            "<code>/price audi a3 2011 150000</code></i>"]
    return "\n".join(out)
