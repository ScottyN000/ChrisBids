"""The pipeline DAG must describe the code as it is, not as it was planned."""
import unittest
from pathlib import Path

from pipeline import dag, roles, takeoff
from pipeline.readers import run as reader_run

ROOT = Path(__file__).resolve().parent.parent


class DagCase(unittest.TestCase):
    def test_it_is_a_dag_apart_from_the_auditors_send_back(self):
        """The only cycle on the architecture diagram is Auditor -> ledger on a fail."""
        edges = [(a, b) for a, b, label in dag.EDGES if not label.startswith("fail")]
        graph = {}
        for a, b in edges:
            graph.setdefault(a, []).append(b)
        state = {}

        def visit(n):
            state[n] = "open"
            for m in graph.get(n, []):
                if state.get(m) == "open":
                    self.fail(f"cycle through {n} -> {m}")
                if m not in state:
                    visit(m)
            state[n] = "done"

        for n in dag.BY_ID:
            if n not in state:
                visit(n)

    def test_every_edge_joins_known_nodes(self):
        for a, b, _ in dag.EDGES:
            self.assertIn(a, dag.BY_ID)
            self.assertIn(b, dag.BY_ID)

    def test_every_node_is_reachable_from_the_packet(self):
        seen, todo = set(), ["packet"]
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            todo += [b for a, b, _ in dag.EDGES if a == n]
        self.assertEqual(seen, set(dag.BY_ID))

    def test_every_agent_is_a_principal_in_the_access_matrix(self):
        for n in dag.NODES:
            if n.status != dag.DATA:
                self.assertIn(n.principal, roles.PRINCIPALS, n.id)

    def test_only_principals_the_matrix_lets_write_have_a_rows_edge(self):
        for node_id in dag.writers_to_ledger():
            p = roles.get(dag.BY_ID[node_id].principal)
            self.assertTrue(p.methods, f"{node_id} has a rows edge but {p.name} may write nothing")
        for n in dag.NODES:
            if n.principal and roles.get(n.principal).methods and n.status != dag.HUMAN:
                if n.principal == "field_crew":
                    continue
                self.assertIn(n.id, dag.writers_to_ledger(), f"{n.principal} may write rows but has no edge")

    def test_the_scope_writer_and_auditor_only_read_the_ledger(self):
        for node in ("scope_writer", "auditor"):
            self.assertNotIn((node, "ledger", "rows"), dag.EDGES)
        self.assertIn(("ledger", "scope_writer", "reads"), dag.EDGES)

    def test_a_node_marked_built_has_its_code(self):
        for n in dag.NODES:
            if n.status in (dag.BUILT, dag.REPLAY):
                self.assertTrue(n.module, f"{n.id} is {n.status} but names no module")
                self.assertTrue((ROOT / n.module).exists(), f"{n.id}: {n.module} is missing")

    def test_an_agent_marked_replay_only_is_one_a_run_loop_can_drive(self):
        driven = set(reader_run.PRINCIPAL.values()) | {takeoff.PRINCIPAL}
        for n in dag.NODES:
            if n.status == dag.REPLAY:
                self.assertIn(n.principal, driven, n.id)

    def test_a_planned_node_is_not_already_in_the_code(self):
        """If someone builds a planned agent, the DAG must say so in the same PR."""
        for n in dag.NODES:
            if n.status == dag.PLANNED:
                self.assertFalse((ROOT / "pipeline" / f"{n.id}.py").exists(),
                                 f"pipeline/{n.id}.py exists but the DAG still says planned")

    def test_the_rendered_document_is_current(self):
        doc = ROOT / "docs" / "pipeline-dag.md"
        self.assertEqual(doc.read_text(), dag.document(), "run python3 tools/render_dag.py")


if __name__ == "__main__":
    unittest.main()
