"""Lint for the two population documents (synthetic-population-spec §4.3, §4.4).

Same rules as the site spec, same finding codes: schema conformance, the text
caps, unique ids, and every reference resolving. Cross-checked against the site
spec when one is given, because a behaviour that names a form the site does not
have is a spec that will fail inside where nobody can read why.
"""

from __future__ import annotations

from .findings import Finding
from .lint import _blob_ref_findings, _collect_ids, _schema_findings, _text_findings


def _pool_findings(doc: dict, manifest_files: dict[str, str] | None) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for i, pool in enumerate(doc.get("content_pools", [])):
        if pool["id"] in seen:
            findings.append(Finding("LINT-REF-04", f"/content_pools/{i}/id"))
        seen.add(pool["id"])
    findings += _blob_ref_findings(
        doc, manifest_files,
        {f"/content_pools/{i}/blob_ref": "page_text" for i in range(len(doc.get("content_pools", [])))},
    )
    return findings


def _action_findings(pointer: str, step: dict, pools: set[str], spec: dict | None) -> list[Finding]:
    findings = []
    if step["content_pool"] not in pools:
        findings.append(Finding("LINT-REF-03", f"{pointer}/content_pool"))
    if spec is not None:
        ids = _collect_ids(spec)
        if step["form"] not in ids["forms"]:
            findings.append(Finding("LINT-XREF-02", f"{pointer}/form"))
        if step["route"] not in ids["routes"]:
            findings.append(Finding("LINT-XREF-02", f"{pointer}/route"))
        if "target_selector" in step and step["target_selector"] not in ids["queries"]:
            findings.append(Finding("LINT-XREF-02", f"{pointer}/target_selector"))
    return findings


def lint_population(
    doc: dict, spec: dict | None = None, manifest_files: dict[str, str] | None = None
) -> list[Finding]:
    """Lint an ambient population document. Empty means clean."""
    findings = _schema_findings(doc, "population.schema.json")
    if findings:
        return sorted(set(findings))
    findings += _text_findings(doc)
    findings += _pool_findings(doc, manifest_files)
    if spec is not None and doc["site_id"] != spec["site_id"]:
        findings.append(Finding("LINT-XREF-01", "/site_id"))

    pools = {p["id"] for p in doc["content_pools"]}
    seen_cohorts: set[str] = set()
    seen_behaviours: set[str] = set()
    for i, cohort in enumerate(doc["cohorts"]):
        if cohort["id"] in seen_cohorts:
            findings.append(Finding("LINT-REF-04", f"/cohorts/{i}/id"))
        seen_cohorts.add(cohort["id"])
        for j, behaviour in enumerate(cohort["behaviours"]):
            if behaviour["id"] in seen_behaviours:
                findings.append(Finding("LINT-REF-04", f"/cohorts/{i}/behaviours/{j}/id"))
            seen_behaviours.add(behaviour["id"])
            findings += _action_findings(f"/cohorts/{i}/behaviours/{j}", behaviour, pools, spec)
    return sorted(set(findings))


def lint_choreography(
    doc: dict, spec: dict | None = None, manifest_files: dict[str, str] | None = None
) -> list[Finding]:
    """Lint an eval choreography document. Empty means clean."""
    findings = _schema_findings(doc, "choreography.schema.json")
    if findings:
        return sorted(set(findings))
    findings += _text_findings(doc)
    findings += _pool_findings(doc, manifest_files)
    if spec is not None and doc["site_id"] != spec["site_id"]:
        findings.append(Finding("LINT-XREF-01", "/site_id"))

    pools = {p["id"]: p["row_count"] for p in doc["content_pools"]}
    seen: set[str] = set()
    for i, actor in enumerate(doc["actors"]):
        if actor["id"] in seen:
            findings.append(Finding("LINT-REF-04", f"/actors/{i}/id"))
        seen.add(actor["id"])
        for j, step in enumerate(actor["script"]):
            pointer = f"/actors/{i}/script/{j}"
            findings += _action_findings(pointer, step, set(pools), spec)
            if step["content_pool"] in pools and step["pool_row"] >= pools[step["content_pool"]]:
                # An explicit row past the pool's declared end is a reference that
                # cannot resolve, and the driver would silently wrap it.
                findings.append(Finding("LINT-REF-03", f"{pointer}/pool_row"))
    return sorted(set(findings))
