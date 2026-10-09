"""The run exporter, the price fetcher's parser and the page builder behind the Bid Shop Floor."""
import json
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_visual  # noqa: E402
import export_run  # noqa: E402
import fetch_prices  # noqa: E402
from pipeline.readers import golden  # noqa: E402

PRICING_SAMPLE = """\
| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.20 / MTok | $0.25 / MTok | $0.40 / MTok | $0.02 / MTok | $1 / MTok |
| Claude Opus 4 ([retired, except on Google Cloud](https://x/y)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Sonnet 5.5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.10 / MTok<sup>2</sup> | $10 / MTok |
| Claude Opus 5.5 | $8 / MTok | $40 / MTok |
"""
PRICES = {"source_url": "u", "fetched_at": "t", "unit": "USD per MTok", "models": fetch_prices.parse(PRICING_SAMPLE)}
# one line of recordings/calls.jsonl as pipeline/readers/live.py writes it
LIVE_CALL = {"reader": "drawing", "unit_id": "S-1#Fnd", "run": 1, "model": "claude-haiku-5-5-20260301", "request_id": "req_1",
             "stop_reason": "end_turn", "input_sha256": "ab" * 32,
             "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 3000, "output_tokens": 400}}


class PricesCase(unittest.TestCase):
    def test_the_model_table_is_read_and_a_fast_mode_row_is_not(self):
        models = fetch_prices.parse(PRICING_SAMPLE)
        self.assertEqual(sorted(models), ["Claude Haiku 5.5 (for prompts over 100,000 tokens)", "Claude Haiku 5.5 (for prompts up to 100,000 tokens)",
                                          "Claude Opus 4", "Claude Sonnet 5.5"])
        self.assertEqual(models["Claude Haiku 5.5 (for prompts up to 100,000 tokens)"],
                         {"input": 0.1, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01, "output": 0.5})
        self.assertEqual(models["Claude Sonnet 5.5"]["cache_read"], 0.1)
        self.assertEqual(fetch_prices.parse("no table here"), {})

    def test_the_fetch_follows_a_redirect_only_on_the_pricing_host(self):
        handler = fetch_prices.OnHost()
        req = urllib.request.Request(fetch_prices.URL, headers={"User-Agent": "t"})
        moved = handler.redirect_request(req, None, 302, "Found", {}, fetch_prices.HOST + "docs/en/pricing.md")
        self.assertEqual(moved.full_url, fetch_prices.HOST + "docs/en/pricing.md")
        for off in ("https://example.com/pricing.md", "http://platform.claude.com/x", "https://platform.claude.com.example.com/x"):
            with self.subTest(off), self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(req, None, 302, "Found", {}, off)
        self.assertTrue(fetch_prices.URL.startswith("https://"))
        self.assertNotIn("--url", Path(fetch_prices.__file__).read_text())


class PriceRowCase(unittest.TestCase):
    def test_a_call_is_priced_from_the_row_named_like_its_model_and_its_prompt_size(self):
        models = PRICES["models"]
        self.assertEqual(export_run.row_key("Claude Haiku 5.5 (for prompts up to 100,000 tokens)"), "claude-haiku-5-5")
        self.assertEqual(export_run.price_row(models, "claude-haiku-5-5", 50)[0], "Claude Haiku 5.5 (for prompts up to 100,000 tokens)")
        self.assertEqual(export_run.price_row(models, "claude-haiku-5-5-20260301", 100001)[0], "Claude Haiku 5.5 (for prompts over 100,000 tokens)")
        self.assertEqual(export_run.price_row(models, "claude-haiku-5-5", 100000)[0], "Claude Haiku 5.5 (for prompts up to 100,000 tokens)")
        self.assertEqual(export_run.price_row(models, "Claude-Sonnet-5-5", 1)[0], "Claude Sonnet 5.5")
        # the longest matching row wins: Sonnet 4.5 is not Sonnet 4, and a dated id still matches its family
        both = {"Claude Sonnet 4": {}, "Claude Sonnet 4.5": {}}
        self.assertEqual(export_run.price_row(both, "claude-sonnet-4-5", 1)[0], "Claude Sonnet 4.5")
        self.assertEqual(export_run.price_row(both, "claude-sonnet-4-20250514", 1)[0], "Claude Sonnet 4")
        for missing in ("claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5-55", "replay", ""):
            with self.subTest(missing):
                self.assertIsNone(export_run.price_row(models, missing, 1))

    def test_a_priced_call_carries_its_cost_and_row_and_an_unpriced_one_its_reason(self):
        c = export_run.priced(LIVE_CALL, PRICES)
        self.assertEqual(c["priced_as"], "Claude Haiku 5.5 (for prompts up to 100,000 tokens)")
        self.assertAlmostEqual(c["cost"], (1000 * 0.10 + 2000 * 0.125 + 3000 * 0.01 + 400 * 0.5) / 1e6)
        self.assertIsNone(c["unpriced"])
        self.assertEqual(c["usage"], LIVE_CALL["usage"])
        for call, prices, why in [
            (LIVE_CALL, None, "no price table in this export"),
            (LIVE_CALL, {"models": {}}, "no price table in this export"),
            ({**LIVE_CALL, "model": "claude-opus-5-5"}, PRICES, "no price row for claude-opus-5-5"),
            ({**LIVE_CALL, "model": ""}, PRICES, "the call names no model"),
            ({k: v for k, v in LIVE_CALL.items() if k != "model"}, PRICES, "the call names no model"),
        ]:
            with self.subTest(why):
                u = export_run.priced(call, prices)
                self.assertEqual((u["cost"], u["priced_as"], u["unpriced"]), (None, None, why))


class ExportCase(unittest.TestCase):
    def test_a_replay_exports_every_audit_row_and_claim_and_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nan"
            run_dir.mkdir()
            broker, results, cmp = golden.replay(ROOT / "fixtures" / "nantucket", run_dir / "ledger.db")
            broker.close()
            data = export_run.export(run_dir, prices=PRICES)
            db = sqlite3.connect(run_dir / "ledger.db")
            self.assertEqual(len(data["events"]), db.execute("select count(*) from audit_log").fetchone()[0])
            self.assertEqual(len(data["claims"]), db.execute("select count(*) from claims").fetchone()[0])
            self.assertEqual((data["job"], data["calls"], data["prices"]["source_url"]), ("NAN", [], "u"))
            call = next(e for e in data["events"] if e["action"] == "model-call")
            self.assertEqual((call["station"], call["run"], call["replay"], call["model"]), ("drawing", 1, True, "replay (recorded expected output)"))
            self.assertEqual({e["station"] for e in data["events"] if e["action"] == "append"}, {"drawing", "takeoff"})
            self.assertEqual([s["id"] for s in data["stations"]][:3], ["packet", "intake", "register"])
            # calls.jsonl is read line by line when a live run wrote one, and each call is priced here
            (run_dir / "recordings").mkdir()
            (run_dir / "recordings" / "calls.jsonl").write_text(json.dumps(LIVE_CALL) + "\n\n" + json.dumps({**LIVE_CALL, "model": "claude-opus-5-5"}) + "\n")
            calls = export_run.export(run_dir, prices=PRICES)["calls"]
            self.assertEqual([c["unit_id"] for c in calls], ["S-1#Fnd", "S-1#Fnd"])
            self.assertEqual([c["priced_as"] for c in calls], ["Claude Haiku 5.5 (for prompts up to 100,000 tokens)", None])
            self.assertEqual([c["unpriced"] for c in calls], [None, "no price row for claude-opus-5-5"])
            self.assertEqual([c["unpriced"] for c in export_run.export(run_dir)["calls"]], ["no price table in this export"] * 2)

    def test_a_live_model_call_row_is_not_marked_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            run_dir.mkdir()
            db = sqlite3.connect(run_dir / "ledger.db")
            db.execute("create table meta(key text, value text)")
            db.execute("create table audit_log(seq integer primary key, at text, principal text, action text, subject text, detail text)")
            db.execute("create table claims(seq integer primary key, claim_id, principal, method, role, value, unit, flag, source_id, locator, statement, derivation, written_at)")
            db.execute("insert into meta values('job', 'X'), ('run_id', 'r1')")
            db.execute("insert into audit_log(at, principal, action, subject, detail) values"
                       "('t', 'drawing_reader', 'model-call', 'S-1#Fnd', 'claude-haiku-5-5; drawing@1; run 2'),"
                       "('t', 'drawing_reader', 'model-call', 'S-1#Fnd', 'replay; drawing@1; run 1'),"
                       "('t', 'codes', 'denied', 'fetch', 'https://example.com/x: not allowlisted'),"
                       "('t', 'auditor', 'audit', 'NAN-001', 'pass')")
            db.commit(); db.close()
            data = export_run.export(run_dir)
            self.assertEqual([(e["model"], e["run"], e["replay"]) for e in data["events"] if e["action"] == "model-call"],
                             [("claude-haiku-5-5", 2, False), ("replay", 1, True)])
            self.assertEqual([(e["action"], e["subject"]) for e in data["events"]][2:], [("denied", "fetch"), ("audit", "NAN-001")])
            self.assertEqual((data["job"], data["run_id"]), ("X", "r1"))

    def test_the_exporter_opens_the_ledger_read_only_and_never_creates_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "empty"
            run_dir.mkdir()
            with self.assertRaises(sqlite3.OperationalError):
                export_run.export(run_dir)
            self.assertEqual(list(run_dir.iterdir()), [])
            self.assertIn("mode=ro", Path(export_run.__file__).read_text())

    def test_the_station_of_every_writing_principal_is_a_dag_node(self):
        from pipeline import dag
        for principal, station in export_run.STATION_OF_PRINCIPAL.items():
            self.assertIn(station, dag.BY_ID, principal)

    def test_every_dag_node_is_a_station_on_the_floor_plan(self):
        from pipeline import dag
        plan = re.search(r"const PLAN = \{(.*?)\n  \};", build_visual.TEMPLATE.read_text(), re.S).group(1)
        for node in dag.NODES:
            with self.subTest(node.id):
                self.assertTrue(re.search(rf"^\s*{node.id}:\s*\{{", plan, re.M), node.id)


class BuildCase(unittest.TestCase):
    def test_the_page_embeds_the_runs_and_cannot_be_closed_early(self):
        html = build_visual.build({"a": {"job": "NAN", "events": [{"detail": "</script><b>"}, {"detail": "<!--<script>"}]}})
        self.assertIn('<script id="runs" type="application/json">{"a":', html)
        self.assertNotIn("</script><b>", html)
        self.assertNotIn("<!--<script>", html)
        self.assertIn("\\u003c/script>\\u003cb>", html)
        self.assertIn("\\u003c!--\\u003cscript>", html)
        self.assertNotIn("__RUNS__", html)
        # the data block is the only `<` the document gets from a run
        body = html.split('<script id="runs" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", body)

    def test_the_page_loads_nothing_from_the_network(self):
        html = build_visual.build({})
        self.assertNotRegex(html, r"""(href|src)\s*=\s*["']?\s*(https?:)?//""")
        self.assertNotRegex(html, r"""url\(\s*["']?\s*(https?:)?//""")
        self.assertNotIn("@import", html)
        self.assertNotIn("fonts.googleapis", html)


if __name__ == "__main__":
    unittest.main()
