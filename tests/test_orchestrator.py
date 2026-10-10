"""Orchestrator: the work plan from the Source Register, and a bid run end to end."""
import json
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from pipeline import fixtures, orchestrator as orch, scope_writer, web, webread
from pipeline.readers import validate
from tests.pdfgen import write_pdf
from tests.webfake import OneHTML

ROOT = Path(__file__).resolve().parent.parent


def src(sid, kind, status="present", pages="", file=None):
    return {"source_id": sid, "kind": kind, "status": status, "pages": pages, "file": file or f"{sid}.pdf",
            "sha256": "0" * 64, "title": sid}


def write_png(path: Path) -> Path:
    """A 2 x 2 white PNG, built by hand."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * 2 for _ in range(2))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


class PlanCase(unittest.TestCase):
    def test_each_kind_gets_its_reader_and_every_source_is_accounted_for(self):
        register = [
            src("S-1", "drawing", pages="1"), src("SPEC", "spec", pages="2"), src("IMG_1", "photo", file="a.jpg"),
            src("MSG", "correspondence", file="m.txt"), src("CT", "message", status="missing"),
            src("PFE", "proposal-template", pages="17"), src("TB", "prior-bid"), src("ARCH", "design"),
            src("XL", "spreadsheet"), src("Q", "unknown"), src("Z", "zip"), src("IMG_1-dup", "photo", "duplicate"),
            src("N", "spec", status=""),
        ]
        p = orch.plan("J", register)
        self.assertEqual(p.job, "J")
        self.assertEqual(p.sources, [
            "S-1: drawing reader, 6 units",
            "SPEC: spec reader, 2 units",
            "IMG_1: photo reader, 1 units",
            "MSG: correspondence reader, 1 units",
            "CT: missing; not read, and rows that need it stay FIELD or unverified",
            "PFE: not read (the Contractor Co. template; its paragraphs are the phrase library, kept by hand)",
            "TB: not read (a past bid is a regression fixture, not a source for this bid)",
            "ARCH: not read (a design document about the pipeline, not about the job)",
            "XL: not read (no reader for spreadsheets yet)",
            "Q: not read (Intake could not tell what it is; a person must classify it)",
            "Z: not read (no reader for kind 'zip')",
            "IMG_1-dup: duplicate, collapsed at Intake; not read twice",
            "N: no status; not read, and rows that need it stay FIELD or unverified",
        ])
        self.assertEqual([(s.agent, s.run) for s in p.steps], [
            ("drawing", True), ("spec", True), ("photo", True), ("correspondence", True), ("takeoff", True),
            ("customer", False), ("codes", True), ("materials", True), ("scope_writer", True), ("auditor", True),
        ])
        self.assertEqual(p.step("spec").units, [
            {"unit_id": "SPEC#p1", "source_id": "SPEC", "locator": "p.1", "tag": "SPEC p.1", "page": 1},
            {"unit_id": "SPEC#p2", "source_id": "SPEC", "locator": "p.2", "tag": "SPEC p.2", "page": 2},
        ])
        self.assertEqual(p.step("photo").units, [{"unit_id": "IMG_1", "source_id": "IMG_1", "locator": "",
                                                  "tag": "IMG_1"}])
        self.assertEqual(p.step("correspondence").units[0]["unit_id"], "MSG")
        self.assertEqual(p.step("spec").reason, "2 units from the register")
        self.assertEqual(sorted(p.units_spec()), ["correspondence", "drawing", "photo", "spec"])

    def test_a_job_with_nothing_to_read_skips_every_reader(self):
        p = orch.plan("J", [src("PFE", "proposal-template")])
        self.assertEqual(p.units_spec(), {})
        self.assertEqual([s.reason for s in p.steps[:5]], [
            "no drawing source present", "no spec source present", "no photo source present",
            "no correspondence source present", "no drawing or photo rows; writes nothing"])
        self.assertTrue(p.step("takeoff").run)
        self.assertEqual(p.text(), "\n".join([
            "plan for J:",
            "  skip drawing: no drawing source present",
            "  skip spec: no spec source present",
            "  skip photo: no photo source present",
            "  skip correspondence: no correspondence source present",
            "  run  takeoff: no drawing or photo rows; writes nothing",
            "  skip customer: not built yet; it waits on the estimator's Oct 6 email to be tested",
            "  run  codes: fetches the code, permit and licensing pages the page table matches to the job",
            "  run  materials: fetches the product data sheets the page table matches to the job",
            "  run  scope_writer: lays out the proposal from the ledger",
            "  run  auditor: re-hashes the sources, checks every row and traces every figure in the proposal",
            "sources:",
            "  PFE: not read (the Contractor Co. template; its paragraphs are the phrase library, kept by hand)",
        ]))

    def test_a_photo_alone_still_runs_takeoff_for_its_field_rows(self):
        p = orch.plan("J", [src("IMG_1", "photo")])
        self.assertEqual(p.step("takeoff").reason,
                         "derives quantities from the drawing rows and writes FIELD rows")
        self.assertIn("  run  photo, 1 units: 1 units from the register", p.text())

    def test_without_network_the_plan_says_the_web_agents_do_not_run(self):
        p = orch.plan("J", [src("IMG_1", "photo")])
        self.assertTrue(p.step("codes").run and p.step("materials").run)
        p = orch.plan("J", [src("IMG_1", "photo")], web=False)
        for name in ("codes", "materials"):
            self.assertEqual((p.step(name).run, p.step(name).reason), (False, orch.NO_WEB))

    def test_a_drawing_sheet_is_read_in_grid_tiles(self):
        units = orch.units_for_source("drawing", src("S-1", "drawing", pages="1"))
        self.assertEqual([u["unit_id"] for u in units],
                         ["S-1#r1c1", "S-1#r1c2", "S-1#r1c3", "S-1#r2c1", "S-1#r2c2", "S-1#r2c3"])
        self.assertEqual(units[0], {"unit_id": "S-1#r1c1", "source_id": "S-1", "locator": "grid tile r1c1",
                                    "tag": "S-1 r1c1", "page": 1, "box": [0.0, 0.0, 0.35, 0.525]})
        self.assertEqual(units[4]["box"], [0.3167, 0.475, 0.6833, 1.0])

    def test_a_drawing_set_names_the_page_in_each_tile(self):
        units = orch.units_for_source("drawing", src("A", "drawing", pages="2"))
        self.assertEqual(len(units), 12)
        self.assertEqual((units[6]["unit_id"], units[6]["locator"], units[6]["tag"], units[6]["page"]),
                         ("A#p.2-r1c1", "grid tile p.2 r1c1", "A p.2 r1c1", 2))

    def test_page_counts_default_to_one(self):
        for pages in ("", None, "x", "0", "-3"):
            with self.subTest(pages=pages):
                self.assertEqual(len(orch.units_for_source("spec", src("S", "spec", pages=pages))), 1)
        self.assertEqual(orch._pages({}), 1)
        self.assertEqual(orch._pages({"pages": "3"}), 3)

    def test_the_plan_for_each_golden_job(self):
        for job, want in (("nantucket", {"drawing": 6}), ("ocean-beach", {"spec": 18, "photo": 5})):
            with self.subTest(job=job):
                _, register = fixtures.read_fixture(ROOT / "fixtures" / job)
                p = orch.plan(job, register)
                self.assertEqual({k: len(v) for k, v in p.units_spec().items()}, want)
                self.assertEqual(len(p.sources), len(register))

    def test_as_json_holds_the_whole_plan(self):
        p = orch.plan("J", [src("IMG_1", "photo")])
        data = json.loads(p.as_json())
        self.assertEqual(data["job"], "J")
        self.assertEqual(data["steps"][2], {"agent": "photo", "run": True, "reason": "1 units from the register",
                                            "units": [{"unit_id": "IMG_1", "source_id": "IMG_1", "locator": "",
                                                       "tag": "IMG_1"}]})
        self.assertTrue(p.as_json().endswith("}\n"))


class Fake:
    """Answers every reader with one valid item drawn from the unit itself."""

    def __init__(self, model_id="fake"):
        self.model_id = model_id
        self.calls = []

    def complete(self, reader, unit, system, schema, run):
        self.calls.append((reader, unit.unit_id, run))
        if reader == "spec":
            first = next(line.strip() for line in unit.text.splitlines() if line.strip())
            return {"items": [{"clause": None, "requirement": first, "division": "09", "product": None}]}
        if reader == "photo":
            return {"items": [{"location": "wall", "condition": "crack", "severity": None,
                               "description": "crack in the wall"}]}
        if reader == "correspondence":
            first = next(line.strip() for line in unit.text.splitlines() if line.strip())
            return {"items": [{"sender": "Estimator", "date": "2026-10-06", "instruction": first}]}
        if reader == "drawing":
            if unit.unit_id.endswith("r1c1"):
                return {"items": [{"kind": "dimension", "label": "Wall run", "text": "10'-0\"", "count": None,
                                   "unit": None}]}
            return {"items": []}
        if reader in ("takeoff", "materials"):
            return {"items": []}
        if reader == "web_reader":
            first = next(line.strip() for line in unit.text.splitlines() if line.strip())
            asks = re.findall(r"^(a\d+): ", unit.brief, re.M)
            return {"answers": [{"ask": a, "found": True, "quote": first, "figures": [], "choice": "", "statement": "The page says so."}
                                for a in asks]}
        if reader == "scope_writer":
            rows = re.findall(r"^(\S+) \| scope \| (\S+) \|", unit.text, re.M)
            by_div = {}
            for cid, div in rows:
                by_div.setdefault(div, []).append({"kind": "row", "ref": cid})
            return {"header": {"project": "", "address": "", "client": ""},
                    "sections": [{"division": d, "tasks": [{"title": "Work", "items": items, "allowance": [],
                                                            "close": ""}]} for d, items in by_div.items()],
                    "exclusion_phrases": ["excl_mold"], "terms": ["warranty"]}
        raise AssertionError(reader)


def make_packet(root: Path, *, drawing: bool = True) -> Path:
    packet = root / "packet"
    packet.mkdir()
    write_pdf(packet / "paint-spec.pdf", ["Pressure wash all walls before coating", "Prime bare stucco"])
    if drawing:
        write_pdf(packet / "sheet-plans.pdf", ["GENERAL NOTES"])
    write_png(packet / "IMG_0001.png")
    (packet / "email-from-estimator.txt").write_text("Keep the base bid simple\n")
    write_pdf(packet / "proposal-format-example.pdf", ["Template"])
    return packet


class Bound(Fake):
    def bind(self, broker):
        self.bound = getattr(self, "bound", 0) + 1
        self.principal = broker.principal.name


class BidCase(unittest.TestCase):
    """Two bid runs in all: each one renders drawing tiles and runs every agent, so
    they are few and each checks a lot (the mutation job runs them per mutant)."""

    def test_a_packet_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            packet = make_packet(Path(d))
            out = Path(d) / "nested" / "run"
            out.mkdir(parents=True)
            (out / "ledger.db").write_text("junk")                # an old ledger is replaced
            clients = [Bound("r"), Bound("t"), Bound("s")]
            table = webread.Table(named=frozenset(), pages=[webread.Source(
                url="https://permits.example.gov/wash", title="Wash permits", agent="codes",
                when=(("pressure wash",),), asks=(webread.Ask("a1", "Whether washing needs a permit"),))])
            fetcher = web.Fetcher(set(), opener=OneHTML("<p>Washing needs no permit.</p>"),
                                  clock=lambda: "2026-10-08", resolve=False)
            res = orch.bid(packet, "J", out, reader_client=clients[0], takeoff_client=clients[1],
                           scope_client=clients[2], fetcher=fetcher, web_table=table)  # default repeats: 2
            self.assertEqual(res.problems, [])
            self.assertEqual(sorted(res.results), ["correspondence", "drawing", "materials", "photo", "spec", "takeoff",
                                                   "web"])
            web_rows = res.results["web"].rows
            self.assertEqual([(c.claim_id, c.quote, c.url, c.retrieved) for c in web_rows],
                             [("J-WEB-001", "Washing needs no permit.", "https://permits.example.gov/wash",
                               "2026-10-08")])
            self.assertTrue(res.scope.ok, res.scope.report())
            self.assertTrue(res.audit.ok, res.audit.text())
            self.assertTrue(res.ok, res.text())
            self.assertEqual([(c.bound, c.principal) for c in clients], [(1, "intake")] * 3)
            text = (out / "proposal.md").read_text()
            self.assertTrue(text.startswith("# FIELD: project name not in the ledger\n"))
            self.assertIn("Job Address:  \nFIELD: job address not in the ledger\n", text)
            self.assertIn("[client: not in the ledger]", text)
            self.assertIn("- Pressure wash all walls before coating (PS p.1)\n", text)
            self.assertIn("## Division 09 Finishes\n", text)
            for name in ("plan.json", "ledger.db", "ledger.csv", "xref.csv", "bid.txt"):
                self.assertTrue((out / name).exists(), name)
            self.assertTrue((out / "bid.txt").read_text().endswith("bid: OK\n"))
            plan = json.loads((out / "plan.json").read_text())
            self.assertEqual([s["agent"] for s in plan["steps"] if s["run"]],
                             ["drawing", "spec", "photo", "correspondence", "takeoff", "codes", "materials",
                              "scope_writer", "auditor"])
            self.assertEqual(len(clients[0].calls), (6 + 2 + 1 + 1 + 1) * 2)   # readers and the web pages (Haiku)
            # the order step reconciles the spec, the takeoff and the sheet, so it runs on the takeoff client (p.13)
            self.assertEqual([c[:2] for c in clients[1].calls],
                             [("takeoff", "J#takeoff")] * 2 + [("materials", "J#materials")] * 2)
            self.assertEqual([c[2] for c in clients[1].calls[:2]], [0, 1])      # Takeoff: the wall run is an input
            self.assertEqual(clients[2].calls, [("scope_writer", "J#scope", 0), ("scope_writer", "J#scope", 1)])
            self.assertEqual(res.results["takeoff"].calls, 2)
            self.assertEqual(res.audit.verdicts, {"pass": 3, "unverified": 4})
            from pipeline.ledger import Ledger
            with Ledger(out / "ledger.db") as led:
                self.assertEqual(led.meta("job"), "J")
                self.assertEqual(led.meta("status_line"), orch.STATUS_LINE)
                claims = led.claims()
                self.assertTrue(claims and all(c.claim_id.startswith("J-") for c in claims))
                self.assertTrue(all(c.run_id == "bid-J" for c in claims))
                self.assertIn("FIELD", {c.method for c in claims})
            self.assertIn("FIELD", (out / "ledger.csv").read_text())

    def test_a_layout_that_never_agrees_is_a_problem_not_a_proposal(self):
        class Split(Fake):
            def complete(self, reader, unit, system, schema, run):
                out = super().complete(reader, unit, system, schema, run)
                if reader == "scope_writer" and run == 1:
                    out["sections"] = []
                return out

        with tempfile.TemporaryDirectory() as d:
            packet = make_packet(Path(d), drawing=False)
            out = Path(d) / "out"
            res = orch.bid(packet, "J", out, reader_client=Split(), takeoff_client=Split(), scope_client=Split(),
                           repeats=2)
            self.assertFalse(res.ok)
            self.assertEqual(res.problems, ["no proposal was rendered: J#scope: the runs agree on no placement"])
            self.assertFalse((out / "proposal.md").exists())
            self.assertIn("PROBLEM no proposal was rendered", res.text())
            self.assertTrue(res.text().endswith("bid: NOT OK"))


class BidResultCase(unittest.TestCase):
    def test_the_orchestrator_reads_no_document(self):
        # The plan takes register rows only; nothing in it opens a file.
        p = orch.plan("J", [src("SPEC", "spec", pages="2", file="/nonexistent/spec.pdf")])
        self.assertEqual(len(p.step("spec").units), 2)

    def test_bid_result_ok_needs_every_part(self):
        p = orch.plan("J", [])
        self.assertFalse(orch.BidResult(plan=p).ok)
        good = scope_writer.ScopeResult(layout={}, text="x")
        self.assertFalse(orch.BidResult(plan=p, scope=good).ok)

    def test_bid_text_lists_each_part_in_order(self):
        p = orch.plan("J", [])
        r = orch.BidResult(plan=p, problems=["x"], scope=scope_writer.ScopeResult(), audit=None)
        self.assertEqual(r.text(), p.text() + "\nPROBLEM x\n" + scope_writer.ScopeResult().report() + "\nbid: NOT OK")
        self.assertEqual(orch.BidResult(plan=p).text(), p.text() + "\nbid: NOT OK")

class MorePlanCase(unittest.TestCase):
    def test_as_json_is_indented_and_keeps_unicode(self):
        p = orch.plan("Jé", [src("IMG_1", "photo")])
        self.assertEqual(p.as_json(), json.dumps(
            {"job": "Jé", "steps": [vars(s) for s in p.steps], "sources": p.sources},
            indent=1, ensure_ascii=False) + "\n")

    def test_a_register_row_without_kind_or_status_is_not_read(self):
        p = orch.plan("J", [{"source_id": "A", "status": "present"}, {"source_id": "B", "kind": "photo"}])
        self.assertEqual(p.sources, ["A: not read (no reader for kind '')",
                                     "B: no status; not read, and rows that need it stay FIELD or unverified"])


class HeaderRenderCase(unittest.TestCase):
    def test_an_empty_header_renders_field_placeholders(self):
        from pipeline import proposal
        data = {"status_line": "S.", "rows": [{"id": "A1", "statement": "BOX", "source": "S", "method": "counted",
                                                 "role": "allowance", "value": 2}],
                "proposal": {"header": {"project": "", "address": "", "client": ""},
                             "sections": [{"title": "Division 09 Finishes", "tasks": [{"allowance": ["A1"]}]}]}}
        phrases = {"license_line": {"text": "L", "loc": "p.1"}, "contractor_block": {"text": "M", "loc": "p.1"},
                   "greeting": {"text": "Hi {client}.", "loc": "p.1"},
                   "allowance": {"text": "Allow {qty} {unit} for {what}.", "loc": "p.2"}}
        text, xref = proposal.render(data, {"A1": data["rows"][0]}, phrases, {})
        self.assertTrue(text.startswith(
            "# FIELD: project name not in the ledger\n\n_S._\n\nL\n\nJob Address:  \nFIELD: job address not in the "
            "ledger\n\nM\n\nHi FIELD. [client: not in the ledger]\n\n**Scope of Work:**\n\n"
            "## Division 09 Finishes\n\n\n_(Allow 2 for BOX (S).)_\n"))
        self.assertEqual([(x["section"], x["claims"], x["phrases"]) for x in xref][:3], [
            ("header", "", "license_line"), ("header", "", "contractor_block"), ("header", "", "greeting")])
        self.assertEqual(xref[3]["section"], "Division 09 Finishes")


class ReadmeCase(unittest.TestCase):
    def test_validate_accepts_the_fake_layout_shape(self):
        lay = Fake().complete("scope_writer", type("U", (), {"text": "J-1 | scope | 09 | - | clause | x", "unit_id": "J#scope"})(), "", {}, 0)
        self.assertEqual(validate.errors(lay, scope_writer.SCHEMA), [])


if __name__ == "__main__":
    unittest.main()
