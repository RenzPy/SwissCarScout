"""Regression tests.

Every test here pins a bug that actually shipped. They exist so the next
change can't silently reintroduce one — the dead-link bug came back twice
before these existed, and the mileage sign was wrong for the tool's whole
life without anything noticing.

    python -m unittest discover -s tests -v

Standard library only; no pytest needed on the server.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

from radar import bands, db  # noqa: E402
from radar.models import Listing  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def cfg():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class DB(unittest.TestCase):
    """Fresh database per test."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.path)
        self.cfg = cfg()
        self.sweep = db.next_sweep_id(self.conn)

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass

    def add(self, eid, price, year=2012, km=150_000, model="golf",
            make="vw", seller="private", source="autoscout24", band="x"):
        db.upsert(self.conn, Listing(
            source=source, external_id=str(eid), title=f"{make} {model}",
            make=make, model=model, year=year, km=km, price_chf=price,
            seller_type=seller), band=band, sweep_id=self.sweep)


# ------------------------------------------------------------------ pricing

class MileageNormalisation(DB):
    """Bug: the sign was inverted. A lower-mileage comparable was priced UP
    when normalised to a higher-mileage subject, inflating every baseline
    for exactly the cheap high-km cars being bought."""

    def test_lower_mileage_comparable_is_adjusted_down(self):
        for i in range(6):
            self.add(i, 10_000, km=100_000)
        subject = Listing(source="manual", external_id="s", title="vw golf",
                          make="vw", model="golf", year=2012, km=200_000)
        median, n = db.baseline_price(self.conn, subject, self.cfg,
                                      seller_scope="private")
        self.assertIsNotNone(median)
        self.assertLess(median, 10_000,
                        "a 100k car normalised to 200k must be worth LESS")


class OneAnswer(DB):
    """Bug: a pasted car scored against CHF 12'024 while /price said 7'787
    for the same car. Both must now come from db.estimate()."""

    def test_scoring_and_price_command_agree(self):
        import re
        from radar.lookup import price_report
        from radar.scoring import score_listing

        for i in range(12):
            self.add(i, 7_000 + i * 50, year=2012, km=190_000 + i * 500)
        car = Listing(source="manual", external_id="me", title="vw golf",
                      make="vw", model="golf", year=2012, km=195_000,
                      price_chf=5_000, seller_type="private")
        lid, _, _ = db.upsert(self.conn, car)
        scored = score_listing(self.conn, car, lid, self.cfg, "buy").baseline

        report = price_report(self.conn, self.cfg, "vw golf", 2012, 195_000)
        shown = int(re.search(r"<b>CHF ([\d']+)</b>", report)
                    .group(1).replace("'", ""))
        self.assertEqual(scored, shown)


class SelectionBias(DB):
    """Bug: pasted cars -- chosen because they looked cheap -- counted as
    market comparables, dragging every baseline down over time."""

    def test_manual_listings_never_count_as_comparables(self):
        for i in range(6):
            self.add(i, 8_000)
        for i in range(20):
            self.add(f"m{i}", 1_000, source="manual")
        e = db.estimate(self.conn, self.cfg, "vw golf", 2012, 150_000)
        self.assertEqual(e["value"], 8_000)
        self.assertEqual(e["n"], 6)


class DealerSanity(DB):
    def test_mismatched_buckets_refuse_rather_than_invent(self):
        # dealer stock old and cheap, private stock newer and dearer:
        # scaling the dealer median would land below every private car
        for i in range(6):
            self.add(f"d{i}", 3_000, seller="professional")
        for i in range(2):
            self.add(f"p{i}", 9_000)
        e = db.estimate(self.conn, self.cfg, "vw golf", 2012, 150_000)
        self.assertIsNone(e["value"])


# ------------------------------------------------------------- sold data

class BandExpiry(DB):
    def test_only_the_swept_band_expires(self):
        self.add("a", 2_000, band="1500-3000")
        self.add("b", 4_000, band="3000-4500")
        self.add("c", 4_200, band="3000-4500")

        # re-sweep 3000-4500 and see only "b"
        nxt = db.next_sweep_id(self.conn)
        db.upsert(self.conn, Listing(source="autoscout24", external_id="b",
                  title="vw golf", make="vw", model="golf", price_chf=4_000),
                  band="3000-4500", sweep_id=nxt)
        gone = db.expire_band(self.conn, "autoscout24", "3000-4500", nxt)

        rows = {r["external_id"]: r["active"] for r in self.conn.execute(
            "SELECT external_id, active FROM listings")}
        self.assertEqual(gone, 1)
        self.assertEqual(rows, {"a": 1, "b": 1, "c": 0})

    def test_expiry_is_exact_within_one_second(self):
        # Bug: expiry compared timestamps at 1-second resolution, so a fast
        # sweep silently expired nothing.
        self.add("x", 3_500, band="3000-4500")
        nxt = db.next_sweep_id(self.conn)
        self.assertEqual(
            db.expire_band(self.conn, "autoscout24", "3000-4500", nxt), 1)


class HuntDoesNotExpire(unittest.TestCase):
    """Bug: the hunt still called a 3-day expiry, which would mass-mark
    indexed cars as sold on every buy-mode run."""

    def test_old_expiry_is_gone(self):
        self.assertFalse(hasattr(db, "mark_stale_inactive"))
        with open(os.path.join(ROOT, "run.py"), encoding="utf-8") as fh:
            self.assertNotIn("mark_stale_inactive(", fh.read())


class Migration(unittest.TestCase):
    def test_existing_rows_survive_upgrade(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            c = sqlite3.connect(path)
            c.executescript("""CREATE TABLE listings (
                id INTEGER PRIMARY KEY, source TEXT, external_id TEXT,
                title TEXT, price_chf INTEGER, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, active INTEGER DEFAULT 1,
                sold_guess INTEGER DEFAULT 0, UNIQUE(source, external_id));
                INSERT INTO listings (source, external_id, title, price_chf,
                    first_seen, last_seen)
                VALUES ('x','1','old',4000,'2026-09-01','2026-09-01');""")
            c.commit(); c.close()
            conn = db.connect(path)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 1)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
            self.assertTrue({"band", "seen_sweep", "enriched_at"} <= cols)
            conn.close()
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(path + ext)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------- the bot

class Matching(unittest.TestCase):
    KEYS = ["bmw 320", "bmw 320-gran-turismo", "audi tt", "audi tts",
            "vw golf", "vw golf-plus", "mercedes-benz a-180", "audi a3"]

    def m(self, q):
        from radar.lookup import match_models, parse_query
        return match_models(parse_query(q)[0], self.KEYS)

    def test_exact_query_resolves_instead_of_offering_itself(self):
        # Bug: "/price bmw 320" offered "/price bmw 320" as an option. Loop.
        hits, exact = self.m("bmw 320")
        self.assertEqual(hits, ["bmw 320"])
        self.assertTrue(exact)

    def test_tts_is_not_tt(self):
        self.assertEqual(self.m("audi tts")[0], ["audi tts"])

    def test_year_and_km_are_filters_not_names(self):
        self.assertEqual(self.m("audi a3 2011 150000")[0], ["audi a3"])


class Buttons(unittest.TestCase):
    def test_callback_round_trips_a_model_with_spaces(self):
        # Bug: space-delimited payload turned "audi tt 2013" into "audi".
        from radar.intake import model_buttons
        data = model_buttons(["audi tt"], 2013, 150_000)[0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        model, year, km = (data[2:].split("|") + ["", ""])[:3]
        self.assertEqual((model, year, km), ("audi tt", "2013", "150000"))

    def test_no_example_renders_as_a_dead_command_link(self):
        # Bug: "/price audi a3" at line start is auto-linked by Telegram and
        # sends a bare "/price". Examples must sit inside <code>.
        import re
        from radar.intake import HELP
        for line in HELP.splitlines():
            if re.match(r"\s*/[a-z]+\s+\S", line):
                self.fail(f"example would render as a dead link: {line!r}")


class Advert(unittest.TestCase):
    def test_chat_is_not_a_listing(self):
        from radar.intake import looks_like_advert
        for msg in ("Hey", "Ok", "Audi a5", "thanks mate, talk later"):
            self.assertFalse(looks_like_advert(msg)[0], msg)

    def test_model_key_is_never_invented_from_filler(self):
        # Bug: "Zum Verkauf steht ein..." became model key "zum verkauf".
        li = Listing(source="x", external_id="1",
                     title="Zum Verkauf steht ein gepflegter Audi S5 Cabriolet")
        self.assertEqual(li.model_key, "audi s5")
        self.assertEqual(Listing(source="x", external_id="2",
                                 title="Hey").model_key, "")


class Consistency(unittest.TestCase):
    def test_one_dealer_definition(self):
        for path in ("run.py", "radar/scoring.py"):
            with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn('("dealer", "professional")', src, path)


# ------------------------------------------------------------------ ledger

class Ledger(DB):
    def test_profit_includes_costs_and_estimate_is_snapshotted(self):
        from radar import ledger
        for i in range(8):
            self.add(i, 4_600, make="seat", model="leon", year=2007,
                     km=120_000 + i * 1000)
        ledger.cmd_bought(self.conn, self.cfg,
                          "3000 seat leon 2007 121000".split())
        ledger.cmd_cost(self.conn, self.cfg, "1 300 glass".split())

        # market moves after purchase; the snapshot must not
        self.add("late", 9_999, make="seat", model="leon", year=2007)
        ledger.cmd_sold(self.conn, self.cfg, "1 4900".split())

        d = self.conn.execute("SELECT * FROM deals WHERE id=1").fetchone()
        self.assertEqual(d["est_value"], 4_600)
        self.assertEqual(d["sold_price"] - d["bought_price"]
                         - ledger._costs(self.conn, 1), 1_600)

    def test_bad_input_is_refused(self):
        from radar import ledger
        self.assertIn("Usage", ledger.cmd_deal(self.conn, self.cfg, ["abc"]))
        self.assertIn("No deal", ledger.cmd_sold(self.conn, self.cfg,
                                                  ["9", "4000"]))


class Bands(unittest.TestCase):
    def test_bands_cover_the_range_without_gaps(self):
        b = bands.compute_bands(1500, 30000, 1500)
        self.assertEqual(b[0][0], 1500)
        self.assertEqual(b[-1][1], 30000)
        for (a, x), (y, _) in zip(b, b[1:]):
            self.assertEqual(x, y)


if __name__ == "__main__":
    unittest.main(verbosity=2)
