"""The access matrix and the schema checker, pinned exactly.

The access matrix (architecture p.10-12) is the security boundary, so it is
written out here in full: widening a principal's scope has to change this test
in the same PR, where a reviewer sees it. The schema checker's messages are
what a discarded run reports, so they are pinned too. Both were added to kill
surviving mutants in pipeline/roles.py and pipeline/readers/validate.py.
"""
import unittest

from pipeline import roles
from pipeline.readers.validate import errors

F = frozenset

# name: (methods, roles, reads_sources, writes_register, writes_audit, reads_prices, egress, requires_supersedes)
MATRIX = {
    "orchestrator": (F(), None, F(), False, False, False, False, False),
    "intake": (F(), None, None, True, False, False, False, False),
    "drawing_reader": (F({"dimensioned", "counted", "scaled", "clause"}), None, F({"drawing"}),
                       False, False, False, False, False),
    "spec_reader": (F({"clause"}), None, F({"spec", "proposal-template", "design", "prior-bid"}),
                    False, False, False, False, False),
    "photo_reader": (F({"observed"}), None, F({"photo"}), False, False, False, False, False),
    "correspondence_reader": (F({"customer"}), None, F({"correspondence"}), False, False, False, False, False),
    "customer_requirements": (F({"customer"}), None, F({"correspondence"}), False, False, False, False, False),
    "takeoff": (F({"counted", "scaled", "FIELD"}), None, F(), False, False, False, False, False),
    "codes": (F({"fetched"}), None, F(), False, False, False, True, False),
    "materials": (F({"fetched"}), None, F(), False, False, False, True, False),
    "scope_writer": (F(), None, F(), False, False, False, False, False),
    "auditor": (F(), None, None, False, True, False, True, False),
    "pricing": (F(), None, F(), False, False, True, False, False),
    "field_crew": (F({"dimensioned"}), F({"quantity"}), F(), False, False, False, False, True),
    "estimator": (F(), None, None, False, False, True, False, False),
}


class MatrixCase(unittest.TestCase):
    def test_every_principal_has_exactly_its_scope(self):
        self.assertEqual(set(roles.PRINCIPALS), set(MATRIX))
        for name, want in MATRIX.items():
            p = roles.get(name)
            got = (p.methods, p.roles, p.reads_sources, p.writes_register, p.writes_audit,
                   p.reads_prices, p.egress, p.requires_supersedes)
            self.assertEqual(got, want, name)
            self.assertEqual(p.name, name)
            self.assertIsInstance(p.methods, frozenset, name)
            self.assertTrue(p.note, name)

    def test_only_intake_writes_the_register_and_only_the_auditor_the_audit_field(self):
        self.assertEqual([n for n, p in roles.PRINCIPALS.items() if p.writes_register], ["intake"])
        self.assertEqual([n for n, p in roles.PRINCIPALS.items() if p.writes_audit], ["auditor"])

    def test_p_builds_frozen_scopes_and_keeps_none_as_any(self):
        p = roles._p("x", methods=["a", "a"], roles=["r"], reads_sources=["s"], egress=True)
        self.assertEqual((p.methods, p.roles, p.reads_sources, p.egress), (F({"a"}), F({"r"}), F({"s"}), True))
        q = roles._p("y")
        self.assertEqual((q.methods, q.roles, q.reads_sources), (F(), None, None))
        self.assertIsInstance(q.methods, frozenset)

    def test_unknown_principal_names_the_known_ones(self):
        with self.assertRaises(KeyError) as e:
            roles.get("nobody")
        self.assertIn("unknown principal 'nobody'; known: ['auditor',", str(e.exception))

    def test_fixture_routing(self):
        self.assertEqual(roles.route("clause", {"drawing"}), "drawing_reader")
        self.assertEqual(roles.route("clause", {"spec"}), "spec_reader")
        self.assertEqual(roles.route("counted", set()), "drawing_reader")
        self.assertEqual(roles.route("counted", set(), has_calc=True), "takeoff")
        self.assertEqual(roles.route("dimensioned", {"drawing"}, has_calc=True), "drawing_reader")
        for method, principal in roles.FIXTURE_ROUTING.items():
            self.assertIn(method, roles.get(principal).methods, method)


class ValidateCase(unittest.TestCase):
    def test_type_errors_stop_at_the_first_mismatch(self):
        self.assertEqual(errors(3, {"type": ["string", "null"], "enum": ["a"]}), ["$: expected string/null, got int"])
        self.assertEqual(errors("x", {"type": "object"}, "$.a"), ["$.a: expected object, got str"])
        self.assertEqual(errors(None, {"type": ["string", "null"]}), [])
        self.assertEqual(errors(True, {"type": "integer"}), ["$: expected integer, got bool"])
        self.assertEqual(errors(True, {"type": "number"}), ["$: expected number, got bool"])
        self.assertEqual(errors(1.5, {"type": "integer"}), ["$: expected integer, got float"])
        self.assertEqual(errors(2, {"type": "number"}), [])
        self.assertEqual(errors(True, {"type": "boolean"}), [])
        self.assertEqual(errors([], {}), [])

    def test_const_and_enum(self):
        self.assertEqual(errors("b", {"const": "a"}), ["$: must be 'a'"])
        self.assertEqual(errors("a", {"const": "a"}), [])
        self.assertEqual(errors("c", {"enum": ["a", "b"]}), ["$: 'c' not in ['a', 'b']"])
        self.assertEqual(errors("a", {"enum": ["a", "b"]}), [])

    def test_string_rules(self):
        s = {"type": "string", "pattern": "^[^0-9]*$", "maxLength": 3}
        self.assertEqual(errors("abc", s), [])
        self.assertEqual(errors("ab1", s), ["$: 'ab1' does not match ^[^0-9]*$"])
        self.assertEqual(errors("abcd", s), ["$: longer than 3 characters"])
        self.assertEqual(errors("abcd5", s), ["$: 'abcd5' does not match ^[^0-9]*$", "$: longer than 3 characters"])
        # The pattern is searched, not matched from the start.
        self.assertEqual(errors("xa", {"pattern": "a"}), [])

    def test_minimum_is_inclusive(self):
        self.assertEqual(errors(0, {"minimum": 0}), [])
        self.assertEqual(errors(-1, {"minimum": 0}), ["$: below 0"])
        self.assertEqual(errors("-1", {"minimum": 0}), [])

    def test_objects(self):
        s = {"type": "object", "required": ["a", "b"], "additionalProperties": False,
             "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
        self.assertEqual(errors({"a": "x", "b": 1}, s), [])
        self.assertEqual(errors({"a": 1, "c": 2}, s),
                         ["$: missing b", "$: unexpected field c", "$.a: expected string, got int"])
        self.assertEqual(errors({"z": 1}, {"type": "object"}), [])  # extra fields allowed unless refused
        self.assertEqual(errors({"z": 1}, {"additionalProperties": True}), [])

    def test_arrays(self):
        s = {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "integer"}}
        self.assertEqual(errors([1], s), [])
        self.assertEqual(errors([1, 2], s), [])
        self.assertEqual(errors([], s), ["$: fewer than 1 items"])
        self.assertEqual(errors([1, "x", 3], s), ["$: more than 2 items", "$[1]: expected integer, got str"])

    def test_nested_paths(self):
        s = {"properties": {"items": {"items": {"properties": {"n": {"type": "integer"}}}}}}
        self.assertEqual(errors({"items": [{"n": 1}, {"n": "x"}]}, s), ["$.items[1].n: expected integer, got str"])


if __name__ == "__main__":
    unittest.main()
