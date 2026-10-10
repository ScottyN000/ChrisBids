"""The page cache: a page fetched with unchanged bytes keeps its answers without a model call."""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import pagecache, web, webread
from pipeline.pagecache import Entry, PageCache
from pipeline.readers.clients import prompt_version
from pipeline.schema import LedgerError
from tests.test_webread import PAGE, PLANS, TURNAROUND, URL, Fake, answer, broker_with, source, table
from tests.webfake import Resp, Sites

AGREE = {"answers": [TURNAROUND, PLANS]}
ROW = {"quote": "q", "statement": "s", "value": "", "value_num": None, "unit": ""}


def bid(cache, client, today="2026-10-08", body=PAGE, repeats=2):
    """One bid's web read: a fresh ledger, as every bid starts with, and the same cache."""
    b = broker_with()
    sites = Sites({URL: Resp(body.encode()) if isinstance(body, str) else body})
    f = web.Fetcher({"example.com"}, opener=sites, clock=lambda: today, resolve=False)
    res = webread.run(b, "J", client, f, table=table(), cache=cache, repeats=repeats)
    return b, res, sites


def rows(res):
    return [(c.claim_id, c.retrieved, c.quote, c.statement, c.value, c.unit, c.confidence, c.flag, c.locator)
            for c in res.rows]


def cached_log(b):
    return [(e["principal"], e["action"], e["subject"], e["detail"]) for e in b.ledger.log()
            if e["action"] in ("fetch", "cached")]


class EntryCase(unittest.TestCase):
    def test_the_key_is_the_asks_ids_prompt_model_runs_and_rules(self):
        k = pagecache.key(source(), "v1", "m", 2, "r")
        self.assertEqual(k, pagecache.key(source(title="another title"), "v1", "m", 2, "r"))
        two = (webread.Ask("a2", "Plans required"),)
        for other in (pagecache.key(source(), "v2", "m", 2, "r"), pagecache.key(source(), "v1", "m2", 2, "r"),
                      pagecache.key(source(), "v1", "m", 1, "r"), pagecache.key(source(), "v1", "m", 2, "r2"),
                      pagecache.key(source(ids=("1926.501",)), "v1", "m", 2, "r"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # days"),) + two),
                                    "v1", "m", 2, "r"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # weeks", unit="weeks"),) + two),
                                    "v1", "m", 2, "r"),
                      pagecache.key(source(asks=(webread.Ask("a1", "Permit turnaround # weeks", options=("x",)),)
                                           + two), "v1", "m", 2, "r")):
            self.assertNotEqual(k, other)
        self.assertEqual(len(webread.RULES), 16)

    def test_the_file_is_appended_and_the_last_line_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache" / "pages.jsonl"
            c = PageCache(path)
            self.assertEqual((c.entries, c.skipped), ({}, 0))
            c.append(Entry(URL, "k", "2026-10-08", "s", {"a1": ROW}))
            c.append(Entry(URL, "k", "2026-10-09", "s", {"a1": ROW}))
            c.append(Entry(URL, "k2", "2026-10-07", "t"))
            lines = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([(x["format"], x["retrieved"]) for x in lines],
                             [(1, "2026-10-08"), (1, "2026-10-09"), (1, "2026-10-07")])
            again = PageCache(path)
            self.assertEqual(again.get(URL, "k"), Entry(URL, "k", "2026-10-09", "s", {"a1": ROW}))
            self.assertEqual(again.get(URL, "k2").retrieved, "2026-10-07")
            self.assertIsNone(again.get("https://other.example.com", "k"))

    def test_a_line_that_does_not_load_is_skipped(self):
        good = {"format": 1, "url": URL, "key": "k", "retrieved": "2026-10-08", "sha256": "s", "answers": {"a1": ROW}}
        bad = ['{"format": 1, "url": "cut off', "[]", json.dumps(dict(good, format=2)), json.dumps(dict(good, extra=1)),
               json.dumps({k: v for k, v in good.items() if k != "format"}),
               json.dumps({k: v for k, v in good.items() if k != "sha256"}),
               json.dumps(dict(good, answers=["q"])), json.dumps(dict(good, answers={"a1": "q"})),
               json.dumps(dict(good, answers={"a1": dict(ROW, extra=1)})),
               json.dumps(dict(good, answers={"a1": {k: v for k, v in ROW.items() if k != "unit"}})),
               json.dumps(dict(good, answers={"a1": dict(ROW, quote=None)})),
               json.dumps(dict(good, retrieved=20261008)), json.dumps(dict(good, url=None)),
               json.dumps(dict(good, key=1)), json.dumps(dict(good, sha256=[]))]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pages.jsonl"
            path.write_text("\n".join(bad + [json.dumps(good)]) + "\n")
            c = PageCache(path)
            self.assertEqual(c.skipped, len(bad))
            self.assertEqual(list(c.entries), [(URL, "k")])

    def test_no_path_keeps_nothing_on_disk(self):
        c = PageCache()
        c.append(Entry(URL, "k", "2026-10-08", "s"))
        self.assertEqual(c.get(URL, "k").retrieved, "2026-10-08")
        self.assertIsNone(c.path)


class RunCase(unittest.TestCase):
    def test_unchanged_bytes_keep_their_answers_without_a_call(self):
        cache = PageCache()
        b, first, _ = bid(cache, Fake(AGREE, AGREE))
        sha = first.fetched[0].sha256
        self.assertEqual((first.calls, first.cached), (2, []))
        self.assertEqual(cached_log(b), [("codes", "fetch", URL, f"2026-10-08; sha256 {sha}"),
                                         ("codes", "cached", URL, "read; 2 answers cached")])
        b, res, sites = bid(cache, Fake(), today="2026-12-01")
        self.assertEqual((res.calls, len(sites.requests)), (0, 1))     # fetched, not read
        why = "unchanged since 2026-10-08 (same sha256); not read again"
        self.assertEqual(res.cached, [f"{URL}: {why}"])
        self.assertIn(f"  cached {URL}: {why}", res.text())
        self.assertEqual([r[1] for r in rows(res)], ["2026-12-01", "2026-12-01"])     # today's date
        self.assertEqual([r[:1] + r[2:] for r in rows(res)], [r[:1] + r[2:] for r in rows(first)])
        self.assertEqual(cached_log(b), [("codes", "fetch", URL, f"2026-12-01; sha256 {sha}"),
                                         ("codes", "cached", URL, why)])
        k = pagecache.key(source(), prompt_version("web_reader"), "fake", 2, webread.RULES)
        self.assertEqual(cache.get(URL, k).retrieved, "2026-12-01")     # the last check is appended
        _, res, _ = bid(cache, Fake(), today="2026-12-02")
        self.assertEqual(res.cached, [f"{URL}: unchanged since 2026-12-01 (same sha256); not read again"])

    def test_changed_bytes_are_read_anew(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        changed = PAGE.replace("<h1>Permits</h1>", "<h1>Permits and fees</h1>")
        _, res, _ = bid(cache, Fake(AGREE, AGREE), today="2026-10-09", body=changed)
        self.assertEqual((res.calls, res.cached), (2, []))

    def test_a_cached_quote_no_longer_on_the_page_is_read_anew(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        e = next(iter(cache.entries.values()))
        e.answers["a1"]["quote"] = "Permits will be issued in 3 days"
        _, res, _ = bid(cache, Fake(AGREE, AGREE), today="2026-10-09")
        self.assertEqual((res.calls, res.cached), (2, []))

    def test_an_entry_missing_an_ask_is_read_anew(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        del next(iter(cache.entries.values())).answers["a2"]
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

    def test_a_reading_by_fewer_runs_or_another_model_does_not_stand_in(self):
        cache = PageCache()
        bid(cache, Fake(AGREE), repeats=1)
        _, res, _ = bid(cache, Fake(AGREE, AGREE), today="2026-10-09")
        self.assertEqual((res.calls, res.cached), (2, []))         # one run's reading is not two runs' agreement
        _, res, _ = bid(cache, Fake(AGREE, AGREE, model_id="other"), today="2026-10-09")
        self.assertEqual((res.calls, res.cached), (2, []))

    def test_a_page_that_does_not_open_is_a_gap_even_when_cached(self):
        cache = PageCache()
        bid(cache, Fake(AGREE, AGREE))
        _, res, _ = bid(cache, Fake(), today="2026-10-09", body=OSError("down"))
        self.assertEqual([(c.flag, c.confidence) for c in res.rows], [("unverified", "missing")])
        self.assertEqual(res.cached, [])

    def test_lines_that_did_not_load_are_noted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pages.jsonl"
            path.write_text("not json\n")
            _, res, _ = bid(PageCache(path), Fake(AGREE, AGREE))
            self.assertEqual(res.notes, ["page cache: 1 lines did not load; their pages are read anew"])
            self.assertEqual(len(PageCache(path).entries), 1)

    def test_only_codes_and_materials_append_and_only_egress_reads(self):
        b, c = broker_with(), PageCache()
        e = Entry(URL, "k", "2026-10-08", "s")
        for name in ("spec_reader", "auditor"):
            with self.subTest(name), self.assertRaises(LedgerError):
                b.as_principal(name).cache_append(c, e, "read")
        self.assertEqual(c.entries, {})
        with self.assertRaises(LedgerError):
            b.as_principal("spec_reader").cache_lookup(c, URL, "k")
        b.as_principal("materials").cache_append(c, e, "read")
        self.assertEqual(b.as_principal("auditor").cache_lookup(c, URL, "k"), e)
        self.assertEqual([(x["principal"], x["action"]) for x in b.ledger.log() if x["action"] in ("cached", "denied")],
                         [("spec_reader", "denied"), ("auditor", "denied"), ("spec_reader", "denied"),
                          ("materials", "cached")])


if __name__ == "__main__":
    unittest.main()
