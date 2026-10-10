"""The page cache: a federal regulation stands 90 days without a fetch; any other page stands while its bytes do."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from pipeline import pagecache, web, webread
from pipeline.pagecache import Entry, PageCache
from pipeline.readers.clients import prompt_version
from pipeline.schema import LedgerError
from tests.test_webread import PAGE, PLANS, TURNAROUND, URL, Fake, answer, broker_with, source, table
from tests.webfake import Resp, Sites

AGREE = {"answers": [TURNAROUND, PLANS]}
CFR = replace(source(), revalidate_days=90)


def bid(cache, client, today="2026-10-08", body=PAGE, src=None):
    """One bid's web read: a fresh ledger, as every bid starts with, and the same cache."""
    b = broker_with()
    sites = Sites({URL: Resp(body.encode())})
    f = web.Fetcher({"example.com"}, opener=sites, clock=lambda: today, resolve=False)
    res = webread.run(b, "J", client, f, table=table(src or source()), cache=cache)
    return b, res, sites


def rows(res):
    return [(c.claim_id, c.retrieved, c.quote, c.statement, c.value, c.unit, c.confidence, c.flag, c.locator)
            for c in res.rows]


class EntryCase(unittest.TestCase):
    def test_the_key_is_the_asks_ids_prompt_and_model(self):
        k = pagecache.key(source(), "v1", "m")
        self.assertEqual(k, pagecache.key(source(title="another title"), "v1", "m"))
        for other in (pagecache.key(source(), "v2", "m"), pagecache.key(source(), "v1", "m2"),
                      pagecache.key(source(ids=("1926.501",)), "v1", "m"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # days"),
                                                 webread.Ask("a2", "Plans required"))), "v1", "m"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # weeks", unit="weeks"),
                                                 webread.Ask("a2", "Plans required"))), "v1", "m"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # weeks"),
                                                 webread.Ask("a2", "Plans required", options=("yes", "no")))),
                                    "v1", "m")):
            self.assertNotEqual(k, other)

    def test_fresh_counts_whole_days_inside_the_window(self):
        c, e = PageCache(), Entry(URL, "k", "2026-07-12", "s")
        self.assertEqual(pagecache.age("2026-07-12", "2026-10-10"), 90)
        self.assertIsNone(pagecache.age("July", "2026-10-10"))
        self.assertIsNone(pagecache.age("2026-07-12", ""))
        self.assertTrue(c.fresh(e, 90, "2026-10-09"))           # 89 days
        self.assertTrue(c.fresh(e, 90, "2026-07-12"))           # the same day
        self.assertFalse(c.fresh(e, 90, "2026-10-10"))          # 90 days: fetched again
        self.assertFalse(c.fresh(e, 90, "2026-07-11"))          # read "tomorrow": a clock gone wrong
        self.assertFalse(c.fresh(e, 0, "2026-07-12"))           # 0: fetched on every bid
        self.assertFalse(c.fresh(Entry(URL, "k", "", "s"), 90, "2026-07-12"))

    def test_the_file_keeps_entries_between_bids(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache" / "pages.json"
            c = PageCache(path)
            self.assertEqual(c.entries, {})
            e = Entry(URL, "k", "2026-10-08", "s", {"a1": {"quote": "q"}})
            c.put(e)
            self.assertEqual(PageCache(path).get(URL, "k"), e)
            self.assertIsNone(PageCache(path).get(URL, "other key"))
            self.assertIsNone(PageCache(path).get("https://other.example.com", "k"))
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["pages.json"])   # no temp file left
            c.put(Entry("https://b.example.com", "k", "2026-10-09", "t"))
            self.assertEqual([x["url"] for x in json.loads(path.read_text())["pages"]], [URL, "https://b.example.com"])

    def test_no_path_keeps_nothing_on_disk(self):
        c = PageCache()
        c.put(Entry(URL, "k", "2026-10-08", "s"))
        self.assertEqual(c.get(URL, "k").retrieved, "2026-10-08")
        self.assertIsNone(c.path)


class RunCase(unittest.TestCase):
    def test_a_federal_regulation_read_within_90_days_is_not_fetched_or_read(self):
        cache = PageCache()
        _, first, _ = bid(cache, Fake(AGREE, AGREE), src=CFR)
        self.assertEqual((first.calls, first.cached), (2, []))
        k = pagecache.key(CFR, prompt_version("web_reader"), "fake")
        self.assertEqual(cache.get(URL, k).retrieved, "2026-10-08")
        b, res, sites = bid(cache, Fake(), today="2026-12-01", src=CFR)
        self.assertEqual((res.calls, res.fetched, sites.requests), (0, [], []))
        self.assertEqual(res.cached, [f"{URL}: read 2026-10-08, within 90 days; not fetched"])
        self.assertEqual(rows(res), [r for r in rows(first)])       # the rows carry the date the page was read
        self.assertEqual(res.rows[0].url, URL)
        log = [(e["action"], e["subject"], e["detail"]) for e in b.ledger.log() if e["action"] in ("fetch", "cached")]
        sha = first.fetched[0].sha256
        self.assertEqual(log, [("cached", URL, f"read 2026-10-08, within 90 days; not fetched; sha256 {sha}")])
        self.assertIn(f"  cached {URL}: read 2026-10-08", res.text())

    def test_at_90_days_it_is_fetched_and_unchanged_bytes_stand_without_a_call(self):
        cache = PageCache()
        _, first, _ = bid(cache, Fake(AGREE, AGREE), src=CFR)
        b, res, sites = bid(cache, Fake(), today="2027-01-06", src=CFR)
        self.assertEqual((res.calls, len(sites.requests)), (0, 1))
        self.assertEqual(res.cached, [f"{URL}: unchanged since 2026-10-08 (same sha256); not read again"])
        self.assertEqual([r[1] for r in rows(res)], ["2027-01-06", "2027-01-06"])
        self.assertEqual([r[2:] for r in rows(res)], [r[2:] for r in rows(first)])
        self.assertEqual(cache.get(URL, pagecache.key(CFR, prompt_version("web_reader"), "fake")).retrieved,
                         "2027-01-06")                              # a new 90 days from the last check
        self.assertEqual([e["action"] for e in b.ledger.log() if e["action"] in ("fetch", "cached")],
                         ["fetch", "cached"])

    def test_any_other_page_is_fetched_every_bid(self):
        cache = PageCache()
        _, first, _ = bid(cache, Fake(AGREE, AGREE))
        _, res, sites = bid(cache, Fake(), today="2026-10-09")
        self.assertEqual((res.calls, len(sites.requests)), (0, 1))
        self.assertEqual([r[1] for r in rows(res)], ["2026-10-09", "2026-10-09"])
        changed = PAGE.replace("<h1>Permits</h1>", "<h1>Permits and fees</h1>")
        client = Fake(AGREE, AGREE)
        _, res, _ = bid(cache, client, today="2026-10-10", body=changed)
        self.assertEqual((res.calls, res.cached), (2, []))         # new bytes: read anew

    def test_a_cached_quote_no_longer_on_the_page_is_read_anew(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        e = next(iter(cache.entries.values()))
        e.answers["a1"]["quote"] = "Permits will be issued in 3 days"
        _, res, _ = bid(cache, Fake(AGREE, AGREE), today="2026-10-09")
        self.assertEqual((res.calls, res.cached), (2, []))

    def test_a_split_or_a_gap_is_not_cached(self):
        cache = PageCache()
        other = answer("a1", "All applications REQUIRE PLANS OR DRAWINGS.", "Plans come first.", figures=[""])
        _, res, _ = bid(cache, Fake(AGREE, {"answers": [other, PLANS]}))
        self.assertEqual(len(res.unanswered), 1)                   # a2 agreed, a1 split: the page is not cached
        self.assertEqual(cache.entries, {})
        _, res, _ = bid(cache, Fake(AGREE, "not json", "not json", "not json"), today="2026-10-09")
        self.assertEqual(len(res.unread), 1)
        self.assertEqual(cache.entries, {})

    def test_a_new_model_or_ask_reads_the_page_anew(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE), src=CFR)
        _, res, _ = bid(cache, Fake(AGREE, AGREE, model_id="other"), today="2026-10-09", src=CFR)
        self.assertEqual((res.calls, res.cached), (2, []))
        self.assertEqual(len(cache.entries), 1)                    # keyed by URL: the newer reading replaces it

    def test_a_page_that_does_not_open_is_a_gap_even_when_cached(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        b = broker_with()
        f = web.Fetcher({"example.com"}, opener=Sites({URL: OSError("down")}), clock=lambda: "2026-10-09",
                        resolve=False)
        res = webread.run(b, "J", Fake(), f, table=table(), cache=cache)
        self.assertEqual([(c.flag, c.confidence) for c in res.rows], [("unverified", "missing")])
        self.assertEqual(res.cached, [])

    def test_only_a_principal_with_egress_may_use_the_cache(self):
        b = broker_with()
        with self.assertRaises(LedgerError):
            b.as_principal("spec_reader").log_cached(URL, "read 2026-10-08")


class TableCase(unittest.TestCase):
    def test_only_federal_regulations_stand_90_days(self):
        t = webread.load()
        days = {s.url: s.revalidate_days for s in t.pages if s.revalidate_days}
        self.assertTrue(days)
        self.assertEqual(set(days.values()), {90})
        for s in t.pages:
            self.assertEqual(s.revalidate_days > 0, s.title.startswith("29 CFR"), s.url)

    def test_revalidate_days_must_be_a_whole_number(self):
        page = {"url": URL, "title": "t", "agent": "codes", "when": [["x"]], "asks": [{"id": "a1", "ask": "q"}]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.yaml"
            for bad in (-1, "90", 1.5, True):
                path.write_text(yaml.safe_dump({"named_domains": [], "pages": [dict(page, revalidate_days=bad)]}))
                with self.subTest(bad), self.assertRaises(ValueError):
                    webread.load(path)
            path.write_text(yaml.safe_dump({"named_domains": [], "pages": [dict(page, revalidate_days=90)]}))
            self.assertEqual(webread.load(path).pages[0].revalidate_days, 90)
            path.write_text(yaml.safe_dump({"named_domains": [], "pages": [page]}))
            self.assertEqual(webread.load(path).pages[0].revalidate_days, 0)


if __name__ == "__main__":
    unittest.main()
