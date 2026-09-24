"""Deal ledger — what you actually paid, spent and got.

WHY THIS EXISTS
---------------
Three numbers in this tool are guesses: the 0.75 dealer-to-private discount,
the keyword weights in scoring, and the mileage bucket boundaries. Nothing in
the market data can tell you whether they're right. Your own deals can.

Every /bought snapshots what db.estimate() said the car was worth at that
moment. Every /sold records what it really fetched. /ledger then reports how
far off the estimates were, which is the only honest answer to "can I trust
this tool" — and after a handful of cars, the evidence for retuning it.

    /deal 3200 seat leon 2007 121000     what-if, nothing saved
    /bought 3200 seat leon 2007 121000   log a purchase
    /cost 1 300 quarter glass            add a cost to deal #1
    /sold 1 4900                         close deal #1
    /ledger                              positions, P&L, accuracy
"""
from __future__ import annotations

import statistics

from . import db
from .lookup import match_models, parse_query

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    model_key     TEXT,
    year          INTEGER,
    km            INTEGER,
    bought_price  INTEGER NOT NULL,
    bought_at     TEXT NOT NULL,
    est_value     INTEGER,
    est_basis     TEXT,
    est_conf      TEXT,
    sold_price    INTEGER,
    sold_at       TEXT
);
CREATE TABLE IF NOT EXISTS deal_costs (
    id       INTEGER PRIMARY KEY,
    deal_id  INTEGER NOT NULL REFERENCES deals(id),
    amount   INTEGER NOT NULL,
    note     TEXT,
    at       TEXT NOT NULL
);
"""


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def _chf(n) -> str:
    return f"{int(n):,}".replace(",", "'")


def _parse_price(tok: str) -> int | None:
    t = tok.replace("'", "").replace("’", "").replace(".-", "").strip()
    return int(t) if t.isdigit() and 0 < int(t) < 1_000_000 else None


def _resolve(conn, words: list[str]):
    """Words -> (model_key, year, km, error_message)."""
    tokens, year, km = parse_query(" ".join(words))
    hits, _exact = match_models(tokens, db.known_model_keys(conn))
    if not hits:
        return None, year, km, (f"Couldn't match “{' '.join(words)}” to an "
                                f"indexed model. Try /models.")
    if len(hits) > 1:
        return None, year, km, ("Which model? " + ", ".join(hits[:5]) +
                                " — be more specific.")
    return hits[0], year, km, None


def _costs(conn, deal_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM deal_costs "
                        "WHERE deal_id=?", (deal_id,)).fetchone()[0]


# --------------------------------------------------------------- /deal

def cmd_deal(conn, cfg: dict, args: list[str]) -> str:
    if len(args) < 2 or _parse_price(args[0]) is None:
        return ("Usage: <code>/deal 3200 seat leon 2007 121000</code>\n"
                "Buy price first, then the car. Nothing is saved.")
    price = _parse_price(args[0])
    model, year, km, err = _resolve(conn, args[1:])
    if err:
        return err
    if not year:
        return "Add the year — resale value is meaningless without it."

    e = db.estimate(conn, cfg, model, year, km)
    expected = int(cfg.get("deal", {}).get("expected_costs", 450))

    out = [f"🧮 <b>{model.upper()} {year}</b>"
           + (f" · {_chf(km)} km" if km else ""), ""]
    out.append(f"Buy at        <b>CHF {_chf(price)}</b>")

    if not e["value"]:
        out += ["", f"🔴 <b>No resale estimate</b>",
                f"<i>{e['basis']}</i>",
                "", "Can't size the margin without one. "
                    "Check <code>/price</code> for the raw spread."]
        return "\n".join(out)

    icon = {"high": "🟢", "medium": "🟡"}.get(e["confidence"], "🔴")
    margin = e["value"] - price - expected
    pct = margin / price * 100 if price else 0

    out.append(f"Resale est.   <b>CHF {_chf(e['value'])}</b> {icon}")
    out.append(f"Costs est.    CHF {_chf(expected)}")
    out.append("")
    sign = "+" if margin >= 0 else "−"
    verdict = ("✅" if pct >= 20 else "🟡" if pct >= 8 else "❌")
    out.append(f"{verdict} <b>Margin {sign}CHF {_chf(abs(margin))}</b> "
               f"({pct:+.0f}%)")
    out.append("")
    out.append(f"<i>{e['basis']}</i>")
    out.append(f"<i>costs are a flat {_chf(expected)} (MFK, cleaning, reserve) "
               f"— deal.expected_costs in config</i>")

    # Walk-away price that still leaves the target margin
    target = float(cfg.get("deal", {}).get("target_margin", 0.20))
    ceiling = int((e["value"] - expected) / (1 + target))
    out.append("")
    out.append(f"💡 Pay at most <b>CHF {_chf(ceiling)}</b> for a "
               f"{target:.0%} margin")
    return "\n".join(out)


# --------------------------------------------------------------- /bought

def cmd_bought(conn, cfg: dict, args: list[str]) -> str:
    ensure(conn)
    if len(args) < 2 or _parse_price(args[0]) is None:
        return "Usage: <code>/bought 3200 seat leon 2007 121000</code>"
    price = _parse_price(args[0])
    model, year, km, err = _resolve(conn, args[1:])
    if err:
        return err

    # Snapshot the estimate NOW. Comparing it with the eventual sale price is
    # the whole point of the ledger, so it must not be recomputed later
    # against a market that has since moved.
    e = db.estimate(conn, cfg, model, year, km)
    cur = conn.execute(
        """INSERT INTO deals (title, model_key, year, km, bought_price,
               bought_at, est_value, est_basis, est_conf)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (" ".join(args[1:])[:80], model, year, km, price, db.now(),
         e["value"], e["basis"], e["confidence"]))
    conn.commit()
    did = cur.lastrowid

    out = [f"📥 <b>Deal #{did} logged</b>",
           f"{model} {year or ''} · bought CHF {_chf(price)}"]
    if e["value"]:
        out.append(f"estimate at purchase: CHF {_chf(e['value'])} "
                   f"({e['confidence']})")
    else:
        out.append("<i>no estimate available — accuracy won't be measured "
                   "for this one</i>")
    out += ["", f"Add costs: <code>/cost {did} 300 quarter glass</code>",
            f"Close it:  <code>/sold {did} 4900</code>"]
    return "\n".join(out)


# --------------------------------------------------------------- /cost

def cmd_cost(conn, cfg: dict, args: list[str]) -> str:
    ensure(conn)
    if len(args) < 2 or not args[0].isdigit() or _parse_price(args[1]) is None:
        return "Usage: <code>/cost 1 300 quarter glass</code>"
    did, amount = int(args[0]), _parse_price(args[1])
    row = conn.execute("SELECT id, sold_price FROM deals WHERE id=?",
                       (did,)).fetchone()
    if not row:
        return f"No deal #{did}. /ledger lists them."
    note = " ".join(args[2:])[:80]
    conn.execute("INSERT INTO deal_costs (deal_id, amount, note, at) "
                 "VALUES (?,?,?,?)", (did, amount, note, db.now()))
    conn.commit()
    return (f"🔧 CHF {_chf(amount)} added to deal #{did}"
            + (f" — {note}" if note else "")
            + f"\nTotal costs so far: CHF {_chf(_costs(conn, did))}")


# --------------------------------------------------------------- /sold

def cmd_sold(conn, cfg: dict, args: list[str]) -> str:
    ensure(conn)
    if len(args) < 2 or not args[0].isdigit() or _parse_price(args[1]) is None:
        return "Usage: <code>/sold 1 4900</code>"
    did, price = int(args[0]), _parse_price(args[1])
    d = conn.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
    if not d:
        return f"No deal #{did}. /ledger lists them."
    if d["sold_price"]:
        return f"Deal #{did} is already closed at CHF {_chf(d['sold_price'])}."

    conn.execute("UPDATE deals SET sold_price=?, sold_at=? WHERE id=?",
                 (price, db.now(), did))
    conn.commit()

    costs = _costs(conn, did)
    profit = price - d["bought_price"] - costs
    pct = profit / d["bought_price"] * 100 if d["bought_price"] else 0

    out = [f"💰 <b>Deal #{did} closed</b>", "",
           f"Bought  CHF {_chf(d['bought_price'])}",
           f"Costs   CHF {_chf(costs)}",
           f"Sold    CHF {_chf(price)}", "",
           f"<b>Profit {'+' if profit >= 0 else '−'}CHF {_chf(abs(profit))}"
           f"</b> ({pct:+.0f}%)"]

    if d["est_value"]:
        err = price - d["est_value"]
        out += ["", f"📐 Estimate was CHF {_chf(d['est_value'])} — actual sale "
                    f"{'+' if err >= 0 else '−'}CHF {_chf(abs(err))} "
                    f"({err / d['est_value'] * 100:+.0f}%)",
                "<i>this is the evidence that recalibrates the tool</i>"]
    return "\n".join(out)


# --------------------------------------------------------------- /ledger

def cmd_ledger(conn, cfg: dict, args: list[str]) -> str:
    ensure(conn)
    deals = conn.execute("SELECT * FROM deals ORDER BY id").fetchall()
    if not deals:
        return ("No deals logged yet.\n"
                "<code>/bought 3200 seat leon 2007 121000</code> to start.")

    open_, closed = [], []
    for d in deals:
        (closed if d["sold_price"] else open_).append(d)

    out = ["📒 <b>Ledger</b>", ""]

    if open_:
        tied = sum(d["bought_price"] + _costs(conn, d["id"]) for d in open_)
        out.append(f"<b>Open</b> · CHF {_chf(tied)} tied up")
        for d in open_:
            c = _costs(conn, d["id"])
            out.append(f"  #{d['id']} {d['model_key']} {d['year'] or ''} · "
                       f"in CHF {_chf(d['bought_price'] + c)}"
                       + (f" · est CHF {_chf(d['est_value'])}"
                          if d["est_value"] else ""))
        out.append("")

    if closed:
        profits, errors = [], []
        out.append("<b>Closed</b>")
        for d in closed:
            c = _costs(conn, d["id"])
            p = d["sold_price"] - d["bought_price"] - c
            profits.append(p)
            out.append(f"  #{d['id']} {d['model_key']} · "
                       f"{'+' if p >= 0 else '−'}CHF {_chf(abs(p))}")
            if d["est_value"]:
                errors.append((d["sold_price"] - d["est_value"])
                              / d["est_value"])
        out += ["", f"<b>Total profit CHF {_chf(sum(profits))}</b> across "
                    f"{len(closed)} deal{'s' if len(closed) != 1 else ''}"]

        if errors:
            mean = statistics.mean(errors) * 100
            mae = statistics.mean(abs(e) for e in errors) * 100
            out += ["", "📐 <b>How good were the estimates?</b>",
                    f"  off by {mae:.0f}% on average "
                    f"({len(errors)} sale{'s' if len(errors) != 1 else ''})"]
            if abs(mean) >= 5:
                direction = "under" if mean > 0 else "over"
                # "Consistently" needs a pattern. One sale is an anecdote.
                word = "consistently" if len(errors) >= 3 else "so far"
                out.append(f"  {word} {direction}-estimating by "
                           f"{abs(mean):.0f}%")
            if len(errors) < 4:
                out.append("  <i>too few sales to act on yet — "
                           "need 4 or more</i>")
            elif abs(mean) >= 5:
                out.append(f"  <i>consider moving price.retail_discount "
                           f"{'up' if mean > 0 else 'down'}</i>")
    return "\n".join(out)
