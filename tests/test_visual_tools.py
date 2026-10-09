"""The run exporter, the price fetcher's parser and the page builder behind the Bid Shop Floor."""
import json
import sqlite3
import sys
import tempfile
import unittest
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
| Claude Opus 4 ([retired, except on Google Cloud](https://x/y)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Sonnet 5.5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.10 / MTok<sup>2</sup> | $10 / MTok |
| Claude Opus 5.5 | $8 / MTok | $40 / MTok |
"""


class PricesCase(unittest.TestCase):
    def test_the_model_table_is_read_and_a_fast_mode_row_is_not(self):
        models = fetch_prices.parse(PRICING_SAMPLE)
        self.assertEqual(sorted(models), ["Claude Haiku 5.5 (for prompts up to 100,000 tokens)", "Claude Opus 4", "Claude Sonnet 5.5"])
        self.assertEqual(models["Claude Haiku 5.5 (for prompts up to 100,000 tokens)"],
                         {"input": 0.1, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01, "output": 0.5})
        self.assertEqual(models["Claude Sonnet 5.5"]["cache_read"], 0.1)
        self.assertEqual(fetch_prices.parse("no table here"), {})


class ExportCase(unittest.TestCase):
    def test_a_replay_exports_every_audit_row_and_claim_and_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nan"
            run_dir.mkdir()
            broker, results, cmp = golden.replay(ROOT / "fixtures" / "nantucket", run_dir / "ledger.db")
            broker.close()
            data = export_run.export(run_dir, prices={"source_url": "u", "fetched_at": "t", "unit": "USD per MTok", "models": {}})
            db = sqlite3.connect(run_dir / "ledger.db")
            self.assertEqual(len(data["events"]), db.execute("select count(*) from audit_log").fetchone()[0])
            self.assertEqual(len(data["claims"]), db.execute("select count(*) from claims").fetchone()[0])
            self.assertEqual((data["job"], data["calls"], data["prices"]["source_url"]), ("NAN", [], "u"))
            call = next(e for e in data["events"] if e["action"] == "model-call")
            self.assertEqual((call["station"], call["run"], call["replay"]), ("drawing", 1, True))
            self.assertEqual({e["station"] for e in data["events"] if e["action"] == "append"}, {"drawing", "takeoff"})
            self.assertEqual([s["id"] for s in data["stations"]][:3], ["packet", "intake", "register"])
            # calls.jsonl is read line by line when a live run wrote one
            (run_dir / "recordings").mkdir()
            (run_dir / "recordings" / "calls.jsonl").write_text(json.dumps({"unit_id": "S-1#Fnd", "run": 1, "usage": {"input_tokens": 3}}) + "\n")
            self.assertEqual(export_run.export(run_dir)["calls"], [{"unit_id": "S-1#Fnd", "run": 1, "usage": {"input_tokens": 3}}])

    def test_the_station_of_every_writing_principal_is_a_dag_node(self):
        from pipeline import dag
        for principal, station in export_run.STATION_OF_PRINCIPAL.items():
            self.assertIn(station, dag.BY_ID, principal)


class BuildCase(unittest.TestCase):
    def test_the_page_embeds_the_runs_and_cannot_be_closed_early(self):
        html = build_visual.build({"a": {"job": "NAN", "events": [{"detail": "</script><b>"}]}})
        self.assertIn('<script id="runs" type="application/json">{"a":', html)
        self.assertNotIn("</script><b>", html)
        self.assertIn("<\\/script><b>", html)
        self.assertNotIn("__RUNS__", html)


if __name__ == "__main__":
    unittest.main()
