"""The run exporter, the price fetcher's parser and the page builder behind the Bid Shop Floor."""
import contextlib
import io
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
from pipeline import dag, roles  # noqa: E402
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
USAGE = {"input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 3000, "output_tokens": 400}
HAIKU_COST = (1000 * 0.10 + 2000 * 0.125 + 3000 * 0.01 + 400 * 0.5) / 1e6
# one line of recordings/calls.jsonl as pipeline/readers/live.py writes it (run numbers start at 1, like the audit log's)
LIVE_CALL = {"reader": "drawing", "unit_id": "S-1#Fnd", "run": 1, "model": "claude-haiku-5-5-20260301", "request_id": "req_1",
             "stop_reason": "end_turn", "input_sha256": "ab" * 32, "usage": USAGE}


def live_ledger(run_dir: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """A ledger with the three tables the exporter reads and the given (principal, action, subject, detail) rows."""
    db = sqlite3.connect(run_dir / "ledger.db")
    db.execute("create table meta(key text, value text)")
    db.execute("create table audit_log(seq integer primary key, at text, principal text, action text, subject text, detail text)")
    db.execute("create table claims(seq integer primary key, claim_id, principal, method, role, value, unit, flag, source_id, locator, statement, derivation, written_at)")
    db.execute("insert into meta values('job', 'X'), ('run_id', 'r1')")
    db.executemany("insert into audit_log(at, principal, action, subject, detail) values('t', ?, ?, ?, ?)", rows)
    db.commit()
    db.close()


class PricesCase(unittest.TestCase):
    def test_the_model_table_is_read_and_a_fast_mode_row_is_not(self):
        models = fetch_prices.parse(PRICING_SAMPLE)
        self.assertEqual(sorted(models), ["Claude Haiku 5.5 (for prompts over 100,000 tokens)", "Claude Haiku 5.5 (for prompts up to 100,000 tokens)",
                                          "Claude Opus 4", "Claude Sonnet 5.5"])
        self.assertEqual(models["Claude Haiku 5.5 (for prompts up to 100,000 tokens)"],
                         {"input": 0.1, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01, "output": 0.5})
        self.assertEqual(models["Claude Sonnet 5.5"]["cache_read"], 0.1)
        self.assertEqual(fetch_prices.parse("no table here"), {})
        # a name in two five-price rows is kept from neither
        twice = PRICING_SAMPLE + "| Claude Sonnet 5.5 | $1 / MTok | $1 / MTok | $1 / MTok | $1 / MTok | $1 / MTok |\n"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            models = fetch_prices.parse(twice)
        self.assertNotIn("Claude Sonnet 5.5", models)
        self.assertIn("Claude Opus 4", models)
        self.assertIn("two price rows named Claude Sonnet 5.5", err.getvalue())

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
    def test_a_call_is_priced_from_the_one_row_named_like_its_model_for_its_prompt_size(self):
        models = PRICES["models"]
        self.assertEqual(export_run.row_key("Claude Haiku 5.5 (for prompts up to 100,000 tokens)"), "claude-haiku-5-5")
        one = lambda model, tokens, table=models: [n for n, _ in export_run.price_rows(table, model, tokens)]  # noqa: E731
        self.assertEqual(one("claude-haiku-5-5", 50), ["Claude Haiku 5.5 (for prompts up to 100,000 tokens)"])
        self.assertEqual(one("claude-haiku-5-5-20260301", 100001), ["Claude Haiku 5.5 (for prompts over 100,000 tokens)"])
        self.assertEqual(one("claude-haiku-5-5", 100000), ["Claude Haiku 5.5 (for prompts up to 100,000 tokens)"])
        self.assertEqual(one("Claude-Sonnet-5-5", 1), ["Claude Sonnet 5.5"])
        # a row matches its exact id or that id plus a snapshot date: Sonnet 4.5 is not Sonnet 4
        both = {"Claude Sonnet 4": {}, "Claude Sonnet 4.5": {}}
        self.assertEqual(one("claude-sonnet-4-5", 1, both), ["Claude Sonnet 4.5"])
        self.assertEqual(one("claude-sonnet-4-20250514", 1, both), ["Claude Sonnet 4"])
        # an id with another suffix is another model: it never falls back to the shorter family row
        for missing in ("claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5-55", "claude-opus-4-5", "claude-opus-4-1", "claude-haiku-5-5-x", "replay", ""):
            with self.subTest(missing):
                self.assertEqual(one(missing, 1), [])
        self.assertEqual(one("claude-sonnet-4-5", 1, {"Claude Sonnet 4": {}}), [])
        self.assertTrue(export_run.same_model("claude-haiku-5-5", "claude-haiku-5-5-20260301"))
        self.assertFalse(export_run.same_model("claude-haiku-5-5", "claude-haiku-5-5-2026030"))
        self.assertFalse(export_run.same_model("claude-haiku-5-5-20260301", "claude-haiku-5-5"))
        # two rows the tier wording cannot tell apart are both returned, and price nothing
        vague = {"Claude Sonnet 4.5 (prompts up to 200K tokens)": {}, "Claude Sonnet 4.5 (prompts over 200K tokens)": {}}
        self.assertEqual(len(one("claude-sonnet-4-5", 300000, vague)), 2)

    def test_a_priced_call_carries_its_cost_and_row_and_an_unpriced_one_its_reason(self):
        c = export_run.priced(LIVE_CALL, PRICES)
        self.assertEqual(c["priced_as"], "Claude Haiku 5.5 (for prompts up to 100,000 tokens)")
        self.assertAlmostEqual(c["cost"], HAIKU_COST)
        self.assertIsNone(c["unpriced"])
        self.assertEqual(c["usage"], USAGE)
        vague = {"models": {"Claude Sonnet 4.5 (prompts up to 200K tokens)": {}, "Claude Sonnet 4.5 (prompts over 200K tokens)": {}}}
        for call, prices, why in [
            (LIVE_CALL, None, "no price table in this export"),
            (LIVE_CALL, {"models": {}}, "no price table in this export"),
            ({**LIVE_CALL, "model": "claude-opus-5-5"}, PRICES, "no price row for claude-opus-5-5"),
            ({**LIVE_CALL, "model": "claude-sonnet-4-5"}, vague, "2 price rows match claude-sonnet-4-5"),
            ({**LIVE_CALL, "model": ""}, PRICES, "the call names no model"),
            ({k: v for k, v in LIVE_CALL.items() if k != "model"}, PRICES, "the call names no model"),
            ({**LIVE_CALL, "usage": None}, PRICES, "the usage line has no token counts"),
            ({**LIVE_CALL, "usage": {}}, PRICES, "the usage line has no token counts"),
            ({**LIVE_CALL, "usage": {k: None for k in USAGE}}, PRICES, "the usage line has no token counts"),
            ({**LIVE_CALL, "usage": {**USAGE, "output_tokens": None}}, PRICES, "the usage line has no token counts"),
            ({k: v for k, v in LIVE_CALL.items() if k != "usage"}, PRICES, "the usage line has no token counts"),
        ]:
            with self.subTest(why):
                u = export_run.priced(call, prices)
                self.assertEqual((u["cost"], u["priced_as"], u["unpriced"]), (None, None, why))


class ExportCase(unittest.TestCase):
    def test_a_replay_exports_every_audit_row_and_claim_and_bills_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nan"
            run_dir.mkdir()
            broker, results, cmp = golden.replay(ROOT / "fixtures" / "nantucket", run_dir / "ledger.db")
            broker.close()
            data = export_run.export(run_dir, prices=PRICES)
            db = sqlite3.connect(run_dir / "ledger.db")
            self.assertEqual(len(data["events"]), db.execute("select count(*) from audit_log").fetchone()[0])
            self.assertEqual(len(data["claims"]), db.execute("select count(*) from claims").fetchone()[0])
            self.assertEqual((data["job"], data["prices"]["source_url"], data["draft"], data["verdict"]), ("NAN", "u", False, None))
            self.assertNotIn("calls", data)
            calls = [e for e in data["events"] if e["action"] == "model-call"]
            self.assertEqual((calls[0]["station"], calls[0]["run"], calls[0]["model"]), ("drawing", 1, "replay (recorded expected output)"))
            self.assertTrue(all(e["replay"] and e["cost"] is None and e["unpriced"] is None and e["usage"] is None for e in calls))
            self.assertEqual({e["station"] for e in data["events"] if e["action"] == "append"}, {"drawing", "takeoff"})
            self.assertEqual([s["id"] for s in data["stations"]][:3], ["packet", "intake", "register"])
            # every principal that wrote a row in this run has a station the floor plan knows
            plan = re.search(r"const PLAN = \{(.*?)\n  \};", build_visual.TEMPLATE.read_text(), re.S).group(1)
            for station in {e["station"] for e in data["events"]}:
                self.assertTrue(re.search(rf"^\s*{station}:\s*\{{", plan, re.M), station)

    def test_a_live_row_is_matched_to_its_usage_line_by_unit_and_run_and_priced(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            run_dir.mkdir()
            # the ledger carries the id the client asked for (`cli live --model`); the usage line the dated id the API served
            live_ledger(run_dir, [
                ("drawing_reader", "model-call", "S-1#Fnd", "claude-haiku-5-5; drawing@1; run 1"),            # one usage line: priced
                ("drawing_reader", "model-call", "S-1#Fnd", "claude-haiku-5-5; drawing@1; run 2"),            # no usage line
                ("drawing_reader", "model-call", "S-1#Fnd", "replay; drawing@1; run 3"),                      # a replay bills nothing
                ("takeoff", "model-call", "NAN#takeoff", "claude-opus-5-5; takeoff@1; run 1"),                 # no price row
                ("takeoff", "model-call", "NAN#takeoff", "claude-haiku-5-5; takeoff@1; run 2"),                # two usage lines (the folder was run twice)
                ("spec_reader", "model-call", "SW#1", "claude-haiku-5-5; spec@1; run 1"),                      # the usage line names another model
                ("codes", "denied", "fetch", "https://example.com/x: not allowlisted"),
                ("auditor", "audit", "NAN-001", "pass: found on S-1 p.1"),
            ])
            (run_dir / "recordings").mkdir()
            lines = [LIVE_CALL, {**LIVE_CALL, "reader": "takeoff", "unit_id": "NAN#takeoff", "run": 1, "model": "claude-opus-5-5"},
                     {**LIVE_CALL, "reader": "takeoff", "unit_id": "NAN#takeoff", "run": 2, "model": "claude-haiku-5-5"},
                     {**LIVE_CALL, "reader": "takeoff", "unit_id": "NAN#takeoff", "run": 2, "model": "claude-haiku-5-5"},
                     {**LIVE_CALL, "reader": "spec", "unit_id": "SW#1", "run": 1, "model": "claude-sonnet-5-5"}]
            (run_dir / "recordings" / "calls.jsonl").write_text("\n".join(json.dumps(c) for c in lines) + "\n\n")
            self.assertEqual((export_run.export(run_dir)["draft"], export_run.export(run_dir)["verdict"]), (False, None))
            (run_dir / "proposal.md").write_text("# draft\n")   # a draft left over from an earlier run into this folder
            (run_dir / "bid.txt").write_text("plan\nPROBLEM no proposal was rendered: x\nbid: NOT OK\n")
            data = export_run.export(run_dir, prices=PRICES)
            self.assertEqual((data["draft"], data["verdict"]), (True, "NOT OK"))
            (run_dir / "bid.txt").write_text("plan\nbid: OK")
            self.assertEqual(export_run.export(run_dir)["verdict"], "OK")
            (run_dir / "bid.txt").write_text("plan\nsomething else\n")
            self.assertIsNone(export_run.export(run_dir)["verdict"])
            calls = [e for e in data["events"] if e["action"] == "model-call"]
            self.assertEqual([(e["model"], e["run"], e["replay"]) for e in calls],
                             [("claude-haiku-5-5", 1, False), ("claude-haiku-5-5", 2, False), ("replay", 3, True),
                              ("claude-opus-5-5", 1, False), ("claude-haiku-5-5", 2, False), ("claude-haiku-5-5", 1, False)])
            self.assertAlmostEqual(calls[0]["cost"], HAIKU_COST)
            self.assertEqual((calls[0]["priced_as"], calls[0]["usage"], calls[0]["unpriced"]),
                             ("Claude Haiku 5.5 (for prompts up to 100,000 tokens)", USAGE, None))
            self.assertEqual([(e["cost"], e["unpriced"]) for e in calls[1:]],
                             [(None, "no usage line in calls.jsonl"), (None, None), (None, "no price row for claude-opus-5-5"),
                              (None, "2 usage lines for this call"), (None, "the ledger says claude-haiku-5-5, calls.jsonl says claude-sonnet-5-5")])
            self.assertEqual([(e["action"], e["subject"], e["station"]) for e in data["events"]][6:],
                             [("denied", "fetch", "codes"), ("audit", "NAN-001", "auditor")])
            self.assertEqual((data["job"], data["run_id"]), ("X", "r1"))
            # with no price table every live row is unpriced for that reason; the replay row stays silent
            table_less = [e["unpriced"] for e in export_run.export(run_dir)["events"] if e["action"] == "model-call"]
            self.assertEqual(table_less, ["no price table in this export", "no usage line in calls.jsonl", None,
                                          "no price table in this export", "2 usage lines for this call",
                                          "the ledger says claude-haiku-5-5, calls.jsonl says claude-sonnet-5-5"])

    def test_the_exporter_opens_the_ledger_read_only_and_never_creates_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "empty"
            run_dir.mkdir()
            with self.assertRaises(sqlite3.OperationalError):
                export_run.export(run_dir)
            self.assertEqual(list(run_dir.iterdir()), [])
            self.assertIn("mode=ro", Path(export_run.__file__).read_text())

    def test_every_principal_that_writes_has_a_station_on_the_floor(self):
        # a principal that can append a row, register a source, audit or fetch must land somewhere on the floor:
        # a node of its own or a named off-floor spot; pricing (Phase 5, not built) writes nothing yet
        writers = {name for name, p in roles.PRINCIPALS.items() if p.methods} | {"intake", "auditor", "codes", "materials"}
        self.assertIn("field_crew", writers)
        for name in sorted(writers):
            with self.subTest(name):
                self.assertIn(export_run.station_for(name), dag.BY_ID)
        self.assertEqual(export_run.station_for("field_crew"), "ships")
        self.assertEqual(export_run.station_for("nobody"), "nobody")

    def test_every_dag_node_is_a_station_on_the_floor_plan_and_says_what_it_reads(self):
        plan = re.search(r"const PLAN = \{(.*?)\n  \};", build_visual.TEMPLATE.read_text(), re.S).group(1)
        for node in dag.NODES:
            with self.subTest(node.id):
                self.assertTrue(re.search(rf"^\s*{node.id}:\s*\{{", plan, re.M), node.id)
        inputs = {n.id: export_run.station_input(n) for n in dag.NODES}
        self.assertEqual({k: v for k, v in inputs.items() if v in ("sources", "web", "ledger") and dag.BY_ID[k].principal not in ("intake", "auditor", "estimator")},
                         {"drawing": "sources", "spec": "sources", "photo": "sources", "correspondence": "sources", "customer": "sources",
                          "takeoff": "ledger", "scope_writer": "ledger", "codes": "web", "materials": "web"})
        self.assertEqual({inputs[k] for k in ("packet", "register", "ledger", "draft", "orchestrator")}, {""})


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
        self.assertNotRegex(html, r"(?i)(https?|wss?)://")   # nothing in the script either: no fetch, socket or import of a URL
        self.assertNotIn("@import", html)
        self.assertNotIn("fonts.googleapis", html)


if __name__ == "__main__":
    unittest.main()
