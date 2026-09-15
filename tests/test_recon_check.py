"""recon-check: the outside harness returns names and reasons, not codes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tools.recon_check import check_package
from tools.recon_check.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


def package(tmp_path, mutate=None) -> Path:
    target = tmp_path / "pkg"
    shutil.copytree(EXAMPLE, target)
    if mutate:
        mutate(target)
    return target


def test_example_package_checks_clean(tmp_path, capsys):
    report = check_package(package(tmp_path))
    assert report.ok, report.render()
    assert {t.kind for t in report.tests} >= {"route_ok", "search_returns", "form_persists", "no_external_requests"}
    assert main([str(tmp_path / "pkg")]) == 0
    assert "OK" in capsys.readouterr().out


def test_external_fetch_is_named_by_test(tmp_path):
    def mutate(pkg: Path) -> None:
        home = pkg / "content" / "templates" / "home.html.j2"
        home.write_text(home.read_text().replace("</head>", '<script src="https://cdn.example/x.js"></script></head>'))

    report = check_package(package(tmp_path, mutate))
    assert not report.ok
    failed = [t for t in report.tests if t.result == "FAIL"]
    assert failed and all(t.kind == "no_external_requests" for t in failed)
    assert any("r_home" in t.test_id or t.kind == "no_external_requests" for t in failed)


def test_missing_slot_file_is_a_lint_finding(tmp_path):
    def mutate(pkg: Path) -> None:
        (pkg / "content" / "assets" / "style.css").unlink()

    report = check_package(package(tmp_path, mutate))
    assert ("LINT-REF-01", "/assets/0/blob_ref") in [(f.code, f.location) for f in report.lint]
    assert report.tests == [], "lint failures stop before anything is deployed"


def test_prose_in_the_spec_is_a_lint_finding(tmp_path):
    def mutate(pkg: Path) -> None:
        spec_path = pkg / "spec" / "site.json"
        spec = json.loads(spec_path.read_text())
        spec["routes"][0]["notes"] = "the home page, lists recent threads"
        spec_path.write_text(json.dumps(spec))

    report = check_package(package(tmp_path, mutate))
    assert any(f.code.startswith("LINT-SCHEMA") for f in report.lint)


def test_unsupported_element_is_reported_by_id(tmp_path):
    def mutate(pkg: Path) -> None:
        spec_path = pkg / "spec" / "site.json"
        spec = json.loads(spec_path.read_text())
        spec["interactions"]["realtime"] = "poll_stub"
        spec_path.write_text(json.dumps(spec))

    report = check_package(package(tmp_path, mutate))
    assert report.unsupported == ("interactions.realtime",)
    assert not report.ok


def test_every_outcome_carries_a_reason(tmp_path):
    """Outside there is no reason to blind the reconstructor.

    The same string exists inside and crosses the log diode; it simply never
    returns to the worker, which `test_worker_payload_carries_no_detail` pins.
    """
    import shutil
    from pathlib import Path

    from tools.recon_check.check import check_package

    example = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"
    package = tmp_path / "pkg"
    shutil.copytree(example, package)
    report = check_package(package)
    assert report.ok, report.render()
    assert all(outcome.detail for outcome in report.tests)
    assert "no external references" in report.render()
