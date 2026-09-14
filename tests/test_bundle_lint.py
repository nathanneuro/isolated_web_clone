"""Every lint code has a test that fires it, and the golden fixtures fire none.

The point of this file is the negative cases. A linter that passes clean input but
misses the thing it exists to catch is worse than no linter, because the pipeline
trusts it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from tools.bundle_lint import lint_spec, lint_suite
from tools.bundle_lint.findings import SCHEMA_DIR, Finding, lint_codes
from tools.bundle_lint.lint import MAX_STRING_CHARS, MAX_WORDS

H64 = "9" * 64


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


class TestClean:
    def test_golden_spec_is_clean(self, spec, manifest_files):
        assert lint_spec(spec, manifest_files) == []

    def test_golden_suite_is_clean(self, suite, spec, manifest_files):
        assert lint_suite(suite, spec, manifest_files) == []

    def test_blob_refs_unchecked_without_a_manifest(self, spec):
        """Pre-build, hashes do not exist yet; skip the check, never fake it."""
        spec["templates"][0]["blob_ref"] = f"content/{H64}.blob"
        assert lint_spec(spec, None) == []


class TestSchema:
    def test_missing_required_key_names_the_key(self, spec):
        del spec["interactions"]
        findings = lint_spec(spec)
        assert findings == [Finding("LINT-SCHEMA-01", "/interactions")]

    def test_unknown_key_is_schema_02(self, spec):
        spec["notes"] = "x"
        assert "LINT-SCHEMA-02" in codes(lint_spec(spec))

    def test_nested_unknown_key_is_schema_02(self, spec):
        """The free-text smuggling attempt this rule exists for."""
        spec["routes"][0]["description"] = "the home page"
        findings = lint_spec(spec)
        assert Finding("LINT-SCHEMA-02", "/routes/0") in findings

    def test_tier_framework_mismatch_is_schema_03(self, spec):
        spec["tier"] = "B"
        assert "LINT-SCHEMA-03" in codes(lint_spec(spec))

    def test_unknown_framework_is_rejected_by_schema(self, spec):
        spec["framework"] = "django-postgres-v1"
        assert "LINT-SCHEMA-01" in codes(lint_spec(spec))

    def test_schema_failure_short_circuits(self, spec):
        """Later rules index into the document; do not run them on a broken shape."""
        spec["routes"] = "not-a-list"
        assert codes(lint_spec(spec)) == {"LINT-SCHEMA-01"}


class TestStructureContentSplit:
    """§6: the two crude caps. These are the tripwire the whole design leans on."""

    def test_long_string_caught_by_schema_before_the_cap(self, spec):
        """The schema is strictly narrower than the cap, so it fires first.

        §6 specifies both, and both are implemented, but on a schema-valid spec the
        caps are unreachable -- see test_no_unconstrained_strings_in_site_schema for
        why. Keeping them is defense in depth against a future loosely-typed field.
        """
        spec["queries"][0]["order_by"] = "a" * (MAX_STRING_CHARS + 1) + " desc"
        assert codes(lint_spec(spec)) == {"LINT-SCHEMA-01"}

    def test_wordy_string_caught_by_schema_before_the_cap(self, spec):
        spec["hostname"] = "why i quit my job.internal"
        assert codes(lint_spec(spec)) == {"LINT-SCHEMA-01"}

    def test_caps_fire_on_a_document_the_schema_would_admit(self):
        """The caps still work; nothing in the current schemas can reach them."""
        from tools.bundle_lint.lint import _text_findings

        loose = {"k": "a" * (MAX_STRING_CHARS + 1), "j": "one two three four five"}
        assert codes(_text_findings(loose)) == {"LINT-TEXT-01", "LINT-TEXT-02"}

    def test_word_cap_boundary_is_inclusive(self):
        from tools.bundle_lint.lint import _text_findings

        assert _text_findings({"k": " ".join(["w"] * MAX_WORDS)}) == []
        assert codes(_text_findings({"k": " ".join(["w"] * (MAX_WORDS + 1))})) == {
            "LINT-TEXT-02"
        }

    def test_length_cap_boundary_is_inclusive(self):
        from tools.bundle_lint.lint import _text_findings

        assert _text_findings({"k": "a" * MAX_STRING_CHARS}) == []
        assert codes(_text_findings({"k": "a" * (MAX_STRING_CHARS + 1)})) == {
            "LINT-TEXT-01"
        }

    def test_blob_refs_are_exempt_from_the_length_cap(self, spec):
        """A blob_ref is 77 chars and pattern-validated; the cap must not fire."""
        assert len(spec["templates"][0]["blob_ref"]) > MAX_STRING_CHARS
        assert lint_spec(spec) == []

    def test_shard_refs_items_are_exempt(self, spec):
        assert len(spec["search"][0]["shard_refs"][0]) > MAX_STRING_CHARS
        assert lint_spec(spec) == []

    def test_literal_value_in_where_is_rejected(self, spec):
        """A literal in a query is content leaking into structure (§6)."""
        spec["queries"][1]["where"] = {"id": "why-i-quit-my-job"}
        assert "LINT-SCHEMA-01" in codes(lint_spec(spec))

    def test_real_slug_in_route_path_survives_lint(self, spec):
        """Documents a known gap: this is QA's Q2 job, not lint's.

        `/post/why-i-quit` is short, is 1 word, and matches the path pattern. Lint
        cannot catch it; `site-qa` QA-LEAK-01 is the control. If this test ever
        starts failing because lint got smarter, that is good news -- update it.
        """
        spec["routes"][2]["path"] = "/post/why-i-quit"
        assert lint_spec(spec) == []


class TestReferences:
    def test_unresolvable_blob_ref_is_ref_01(self, spec, manifest_files):
        spec["templates"][0]["blob_ref"] = f"content/{H64}.blob"
        findings = lint_spec(spec, manifest_files)
        assert Finding("LINT-REF-01", "/templates/0/blob_ref") in findings

    def test_wrong_role_is_ref_02(self, spec, manifest_files):
        """§5: role is the AEAD's AAD, so a role swap must be caught here too."""
        spec["templates"][0]["blob_ref"] = spec["db"]["seed_blob_ref"]
        findings = lint_spec(spec, manifest_files)
        assert Finding("LINT-REF-02", "/templates/0/blob_ref") in findings

    def test_wrong_role_in_shard_list_names_the_index(self, spec, manifest_files):
        spec["search"][0]["shard_refs"] = [f"index/{H64}.shard"]
        findings = lint_spec(spec, manifest_files)
        assert Finding("LINT-REF-01", "/search/0/shard_refs/0") in findings

    @pytest.mark.parametrize(
        ("pointer", "field"),
        [
            ("routes/0", "template"),
            ("routes/1", "search"),
            ("routes/3", "form"),
            ("routes/3", "mutation"),
        ],
    )
    def test_dangling_route_reference_is_ref_03(self, spec, pointer, field):
        kind, i = pointer.split("/")
        spec[kind][int(i)][field] = "x_nope"
        findings = lint_spec(spec)
        assert Finding("LINT-REF-03", f"/{kind}/{i}/{field}") in findings

    def test_dangling_query_reference_is_ref_03(self, spec):
        spec["routes"][0]["queries"][0] = "q_nope"
        assert Finding("LINT-REF-03", "/routes/0/queries/0") in lint_spec(spec)

    def test_dangling_table_reference_is_ref_03(self, spec):
        spec["queries"][0]["table"] = "nonexistent"
        assert Finding("LINT-REF-03", "/queries/0/table") in lint_spec(spec)

    def test_dangling_mutation_form_is_ref_03(self, spec):
        spec["mutations"][0]["from_form"] = "f_nope"
        assert Finding("LINT-REF-03", "/mutations/0/from_form") in lint_spec(spec)

    def test_dangling_search_table_is_ref_03(self, spec):
        spec["search"][0]["tables"] = ["nonexistent"]
        assert Finding("LINT-REF-03", "/search/0/tables/0") in lint_spec(spec)

    def test_duplicate_id_is_ref_04(self, spec):
        spec["routes"][1]["id"] = "r_home"
        assert Finding("LINT-REF-04", "/routes/1/id") in lint_spec(spec)

    def test_duplicate_test_id_is_ref_04(self, suite, spec):
        suite["tests"][1]["id"] = "T001"
        assert Finding("LINT-REF-04", "/tests/1/id") in lint_suite(suite, spec)


class TestSuite:
    def test_unknown_test_kind_is_rejected(self, suite, spec):
        suite["tests"][0]["kind"] = "screenshot_looks_nice"
        assert "LINT-SCHEMA-01" in codes(lint_suite(suite, spec))

    def test_inline_expected_value_is_rejected(self, suite, spec):
        """§7: anything a test compares against is a fixture key, never a literal."""
        suite["tests"][2]["expect_contains"] = "a real post title"
        assert "LINT-SCHEMA-02" in codes(lint_suite(suite, spec))

    def test_test_referencing_absent_route_is_xref_02(self, suite, spec):
        suite["tests"][0]["route"] = "r_nope"
        assert Finding("LINT-XREF-02", "/tests/0/route") in lint_suite(suite, spec)

    def test_test_referencing_absent_query_is_xref_02(self, suite, spec):
        suite["tests"][3]["verify_query"] = "q_nope"
        assert Finding("LINT-XREF-02", "/tests/3/verify_query") in lint_suite(suite, spec)

    def test_xref_skipped_without_a_spec(self, suite):
        suite["tests"][0]["route"] = "r_nope"
        assert lint_suite(suite, None) == []

    def test_fixture_id_pattern_enforced(self, suite, spec):
        suite["tests"][2]["query_fixture"] = "the search query"
        assert "LINT-SCHEMA-01" in codes(lint_suite(suite, spec))


class TestSchemaTightness:
    """The structure/content split is held by the schemas, not by the caps.

    bundle-format-spec §6 lists crude string caps as the enforcement mechanism. In
    this implementation every string in the site and suite schemas is bound by a
    pattern or an enum, which is strictly narrower. That is a stronger guarantee,
    and this test is what keeps it true: adding a free-form string field anywhere
    fails here, loudly, at the moment it is added.
    """

    @staticmethod
    def _string_leaves(node, path="/"):
        if not isinstance(node, dict):
            return
        if "enum" in node or "const" in node or "$ref" in node:
            return
        if node.get("type") == "string":
            yield path, "pattern" in node
            return
        for key in ("properties", "$defs"):
            for name, sub in node.get(key, {}).items():
                yield from TestSchemaTightness._string_leaves(sub, f"{path}{name}/")
        for key in ("items", "additionalProperties", "propertyNames"):
            if isinstance(node.get(key), dict):
                yield from TestSchemaTightness._string_leaves(node[key], f"{path}{key}/")
        for key in ("allOf", "anyOf", "oneOf"):
            for i, sub in enumerate(node.get(key, [])):
                yield from TestSchemaTightness._string_leaves(sub, f"{path}{key}{i}/")

    @pytest.mark.parametrize(
        "schema_name", ["site.schema.json", "suite.schema.json", "manifest.schema.json"]
    )
    def test_no_unconstrained_strings(self, schema_name):
        schema = json.loads((SCHEMA_DIR / schema_name).read_text())
        leaves = list(self._string_leaves(schema))
        assert leaves, f"{schema_name}: found no string leaves; walker is broken"
        loose = [path for path, constrained in leaves if not constrained]
        assert loose == [], f"{schema_name} admits free-form strings at: {loose}"


class TestRegistries:
    def test_every_finding_code_is_registered(self):
        with pytest.raises(AssertionError):
            Finding("LINT-MADE-UP", "/x")

    def test_registries_parse_and_are_versioned(self):
        for path in sorted(SCHEMA_DIR.glob("*.toml")):
            with path.open("rb") as fh:
                assert tomllib.load(fh)["registry_version"] >= 1, path

    def test_schemas_are_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
            Draft202012Validator.check_schema(json.loads(path.read_text()))

    def test_status_codes_match_the_spec_table(self):
        """The receiver, worker, and go-live all compile against this registry."""
        with (SCHEMA_DIR / "status-codes.toml").open("rb") as fh:
            status = tomllib.load(fh)["status"]
        assert status["14"] == "reject_lint"
        assert status["30"] == "golive_pass"
        assert set(status) >= {"0", "10", "11", "12", "13", "14", "20", "21",
                               "30", "31", "32", "33", "40", "41"}
