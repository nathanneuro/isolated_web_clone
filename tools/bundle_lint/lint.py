"""The lint rules themselves (bundle-format-spec §6).

The rules are deliberately crude. They are a tripwire against the pipeline drifting
toward "just put the description in a string," not a content classifier. Anything
subtle is the QA agent's job (`skills/site-qa/SKILL.md` Q2), outside, where an LLM
may still read the content.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

from .findings import SCHEMA_DIR, Finding

MAX_STRING_CHARS = 64
MAX_WORDS = 4

# Keys whose values are validated by their own pattern in the schema and are therefore
# exempt from the length cap (§6: "except blob_ref and path fields"). They are exempt
# from the cap only -- the schema patterns are strictly narrower than the cap is.
PATTERN_VALIDATED_KEYS = frozenset(
    {"blob_ref", "seed_blob_ref", "fixture_blob_ref", "shard_refs", "path"}
)

# Which manifest `role` each spec slot may point at (§4, §5: role is the AEAD's AAD,
# so a blob cannot be swapped into a different role even with a valid ciphertext).
ROLE_FOR_SLOT = {
    "templates": "template",
    "assets": "asset",
    "db.seed_blob_ref": "db_seed",
    "search.shard_refs": "bm25_shard",
    "suite.fixture_blob_ref": "fixtures",
}

TIER_FRAMEWORK = {"A": "fastapi-sqlite-v1", "B": "static-v1"}


@cache
def _validator(name: str) -> Draft202012Validator:
    with (SCHEMA_DIR / name).open() as fh:
        return Draft202012Validator(json.load(fh))


def _pointer(path: tuple[Any, ...]) -> str:
    """RFC 6901 JSON pointer from a jsonschema-style path."""
    return "".join(
        "/" + str(p).replace("~", "~0").replace("/", "~1") for p in path
    )


_REQUIRED_PROPERTY = re.compile(r"^'([^']+)' is a required property$")


def _resolve(raw: ValidationError) -> tuple[ValidationError, tuple[Any, ...]]:
    """Descend through oneOf/anyOf to the sub-error that actually explains the failure.

    The suite schema dispatches on test `kind` via oneOf, so a bad field inside a test
    surfaces as a bare oneOf failure at the test's path. Without descending, every
    such finding collapses to LINT-SCHEMA-01 and the repair ticket loses the bit that
    tells the reconstructor whether it invented a field or mistyped a value.

    Returns the explaining error and its absolute path, which has to be rebuilt as we
    descend because a sub-error's own path is relative to its branch.
    """
    err, path = raw, tuple(raw.absolute_path)
    while err.validator in ("oneOf", "anyOf") and err.context:
        better = best_match(err.context)
        if better is None:
            break
        path += tuple(better.relative_path)
        err = better
    return err, path


def _schema_findings(doc: Any, schema_name: str) -> list[Finding]:
    findings = []
    for raw in _validator(schema_name).iter_errors(doc):
        err, path = _resolve(raw)
        # additionalProperties rejections are the "unknown key" rule (§6) and get
        # their own code so a repair ticket distinguishes them from a type error.
        code = (
            "LINT-SCHEMA-02"
            if err.validator == "additionalProperties"
            else "LINT-SCHEMA-01"
        )
        pointer = _pointer(path)
        if err.validator == "required":
            # Point at the missing key, not at the object that lacks it, so a repair
            # ticket names something the reconstructor can act on. Asserted rather
            # than guarded: if jsonschema changes this message, fail loudly.
            match = _REQUIRED_PROPERTY.match(err.message)
            assert match, f"unparsed required-property message: {err.message!r}"
            pointer = f"{pointer}/{match.group(1)}"
        findings.append(Finding(code, pointer))
    return sorted(set(findings))


def _walk_strings(node: Any, path: tuple[Any, ...] = ()):
    """Yield (pointer_path, key_name, value) for every string in the document.

    `key_name` is the nearest enclosing object key, which is what the exemption list
    is keyed on. For an array of strings the key is the array's own key, so
    `shard_refs` covers its items.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk_strings(value, path + (i,))
    elif isinstance(node, str):
        key_name = next(
            (p for p in reversed(path) if isinstance(p, str)), ""
        )
        yield path, key_name, node


def _text_findings(doc: Any) -> list[Finding]:
    """§6: the two crude caps that enforce the structure/content split."""
    findings = []
    for path, key_name, value in _walk_strings(doc):
        pointer = _pointer(path)
        if key_name not in PATTERN_VALIDATED_KEYS and len(value) > MAX_STRING_CHARS:
            findings.append(Finding("LINT-TEXT-01", pointer))
        if len(value.split()) > MAX_WORDS:
            findings.append(Finding("LINT-TEXT-02", pointer))
    return findings


def _blob_ref_findings(
    doc: Any, manifest_files: dict[str, str] | None, slot_roles: dict[str, str]
) -> list[Finding]:
    """§6: every blob_ref resolves to a manifest entry with a compatible role.

    `manifest_files` maps path -> role. When it is None the bundle has no manifest
    yet (the reconstructor's local `recon-check` runs before `bundle-build` assigns
    hashes), and reference checking is skipped rather than faked.
    """
    if manifest_files is None:
        return []
    findings = []
    for pointer, role in slot_roles.items():
        ref = _at_pointer(doc, pointer)
        refs = ref if isinstance(ref, list) else [ref]
        for i, one in enumerate(refs):
            loc = pointer if not isinstance(ref, list) else f"{pointer}/{i}"
            if one not in manifest_files:
                findings.append(Finding("LINT-REF-01", loc))
            elif manifest_files[one] != role:
                findings.append(Finding("LINT-REF-02", loc))
    return findings


def _at_pointer(doc: Any, pointer: str) -> Any:
    node = doc
    for part in pointer.lstrip("/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _slot_roles(spec: dict) -> dict[str, str]:
    """Locate every blob_ref in a spec and the role it must point at."""
    slots = {}
    for i, _ in enumerate(spec.get("templates", [])):
        slots[f"/templates/{i}/blob_ref"] = "template"
    for i, _ in enumerate(spec.get("assets", [])):
        slots[f"/assets/{i}/blob_ref"] = "asset"
    if "db" in spec:
        slots["/db/seed_blob_ref"] = "db_seed"
    for i, _ in enumerate(spec.get("search", [])):
        slots[f"/search/{i}/shard_refs"] = "bm25_shard"
    return slots


def _collect_ids(spec: dict) -> dict[str, set[str]]:
    return {
        kind: {item["id"] for item in spec.get(kind, []) if "id" in item}
        for kind in ("routes", "templates", "queries", "forms", "mutations", "search")
    }


def _duplicate_findings(spec: dict) -> list[Finding]:
    findings = []
    for kind in ("routes", "templates", "queries", "forms", "mutations", "search"):
        seen = set()
        for i, item in enumerate(spec.get(kind, [])):
            ident = item.get("id")
            if ident in seen:
                findings.append(Finding("LINT-REF-04", f"/{kind}/{i}/id"))
            seen.add(ident)
    return findings


def _internal_ref_findings(spec: dict) -> list[Finding]:
    """§6: every identifier reference resolves within the document."""
    ids = _collect_ids(spec)
    tables = {t["name"] for t in spec.get("db", {}).get("tables", [])}
    findings = []

    route_fields = {
        "template": "templates",
        "search": "search",
        "form": "forms",
        "mutation": "mutations",
        "redirect_to": "routes",
    }
    for i, route in enumerate(spec.get("routes", [])):
        for field, kind in route_fields.items():
            if field in route and route[field] not in ids[kind]:
                findings.append(Finding("LINT-REF-03", f"/routes/{i}/{field}"))
        for j, query in enumerate(route.get("queries", [])):
            if query not in ids["queries"]:
                findings.append(Finding("LINT-REF-03", f"/routes/{i}/queries/{j}"))

    for i, query in enumerate(spec.get("queries", [])):
        if query["table"] not in tables:
            findings.append(Finding("LINT-REF-03", f"/queries/{i}/table"))
        if "join" in query and query["join"]["table"] not in tables:
            findings.append(Finding("LINT-REF-03", f"/queries/{i}/join/table"))

    for i, form in enumerate(spec.get("forms", [])):
        for j, field in enumerate(form["fields"]):
            if "options_query" in field and field["options_query"] not in ids["queries"]:
                findings.append(
                    Finding("LINT-REF-03", f"/forms/{i}/fields/{j}/options_query")
                )

    for i, mutation in enumerate(spec.get("mutations", [])):
        if mutation["table"] not in tables:
            findings.append(Finding("LINT-REF-03", f"/mutations/{i}/table"))
        if mutation["from_form"] not in ids["forms"]:
            findings.append(Finding("LINT-REF-03", f"/mutations/{i}/from_form"))

    for i, search in enumerate(spec.get("search", [])):
        for j, table in enumerate(search["tables"]):
            if table not in tables:
                findings.append(Finding("LINT-REF-03", f"/search/{i}/tables/{j}"))

    return findings


def _tier_findings(spec: dict) -> list[Finding]:
    """§6: the framework value must match the declared tier."""
    tier, framework = spec.get("tier"), spec.get("framework")
    if tier in TIER_FRAMEWORK and framework != TIER_FRAMEWORK[tier]:
        return [Finding("LINT-SCHEMA-03", "/framework")]
    return []


def lint_spec(
    spec: dict, manifest_files: dict[str, str] | None = None
) -> list[Finding]:
    """Lint `spec/site.json`. Returns findings; empty means clean."""
    findings = _schema_findings(spec, "site.schema.json")
    if findings:
        # Downstream rules index into a document they assume is shaped. Report the
        # schema failures and stop rather than raise a confusing KeyError.
        return sorted(set(findings))
    findings += _tier_findings(spec)
    findings += _text_findings(spec)
    findings += _duplicate_findings(spec)
    findings += _internal_ref_findings(spec)
    findings += _blob_ref_findings(spec, manifest_files, _slot_roles(spec))
    return sorted(set(findings))


def lint_suite(
    suite: dict,
    spec: dict | None = None,
    manifest_files: dict[str, str] | None = None,
) -> list[Finding]:
    """Lint `tests/suite.json`, cross-checking against the spec when one is given."""
    findings = _schema_findings(suite, "suite.schema.json")
    if findings:
        return sorted(set(findings))
    findings += _text_findings(suite)
    findings += _blob_ref_findings(
        suite, manifest_files, {"/fixture_blob_ref": "fixtures"}
    )

    seen: set[str] = set()
    for i, test in enumerate(suite["tests"]):
        if test["id"] in seen:
            findings.append(Finding("LINT-REF-04", f"/tests/{i}/id"))
        seen.add(test["id"])

    if spec is not None:
        ids = _collect_ids(spec)
        for i, test in enumerate(suite["tests"]):
            for field, kind in (
                ("route", "routes"),
                ("form", "forms"),
                ("search", "search"),
                ("verify_query", "queries"),
            ):
                if field in test and test[field] not in ids[kind]:
                    findings.append(Finding("LINT-XREF-02", f"/tests/{i}/{field}"))

    return sorted(set(findings))


def lint_bundle(bundle_dir: Path) -> list[Finding]:
    """Lint an unpacked bundle directory.

    This is what the receiver calls at §8.2 step 5, *after* the signature and every
    file hash have already been verified. It never reads content/ or index/.
    """
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest_files = {
        entry["path"]: entry.get("role") for entry in manifest.get("files", [])
    }

    if manifest["type"] == "command":
        return []  # command bundles have no spec/ or tests/ (§3, §9)

    spec = json.loads((bundle_dir / "spec" / "site.json").read_text())
    findings = lint_spec(spec, manifest_files)

    suite_path = bundle_dir / "tests" / "suite.json"
    if manifest["type"] == "index_only" and not suite_path.exists():
        return sorted(set(findings))

    suite = json.loads(suite_path.read_text())
    findings += lint_suite(suite, spec, manifest_files)

    expected = f"{manifest['site_id']}-r{manifest['revision']}-tests"
    if suite["suite_id"] != expected:
        findings.append(Finding("LINT-XREF-01", "/suite_id"))

    return sorted(set(findings))
