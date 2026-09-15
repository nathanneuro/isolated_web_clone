"""recon-check: does it catch what QA and the inside would catch, and say why?

The load-bearing test is `test_agrees_with_golive`. recon-check exists so problems
surface outside, where an agent can read a traceback. It is only worth running if it
reaches the same verdict as go-live on the same package -- otherwise the pipeline
ships things that pass outside and fail inside as an unexplained integer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.golive.runner import FAIL, PASS, RUNNER_ERROR, SKIPPED
from tools.recon_check import recon_check
from tools.recon_check.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture
def package(tmp_path):
    target = tmp_path / "pkg"
    shutil.copytree(EXAMPLE, target)
    return target


def check(package, tmp_path):
    return recon_check(package, tmp_path / "work")


def edit_spec(package, mutate):
    path = package / "spec" / "site.json"
    spec = json.loads(path.read_text())
    mutate(spec)
    path.write_text(json.dumps(spec, indent=1))


class TestHappyPath:
    def test_the_example_package_passes(self, package, tmp_path):
        report = check(package, tmp_path)
        assert report.ok, report.summary()
        assert report.summary().startswith("PASS")

    def test_every_test_carries_a_reason(self, package, tmp_path):
        """Outside there is no reason to blind the reconstructor."""
        report = check(package, tmp_path)
        assert all(outcome.detail for outcome in report.tests)

    def test_render_diff_is_skipped_not_passed(self, package, tmp_path):
        report = check(package, tmp_path)
        skipped = [t for t in report.tests if t.code == SKIPPED]
        assert skipped and "browser" in skipped[0].detail

    def test_cli_exits_zero(self, package, tmp_path, capsys):
        assert main([str(package), "--work-dir", str(tmp_path / "w"), "--quiet"]) == 0


class TestHandleRewrite:
    def test_logical_paths_are_rewritten_before_linting(self, package, tmp_path):
        """§6: the reconstructor authors logical paths; handles come from the build.

        If recon-check linted the un-rewritten spec it would fail every blob_ref
        pattern and be useless, which is why it performs the same rewrite.
        """
        authored = json.loads((package / "spec" / "site.json").read_text())
        assert authored["templates"][0]["blob_ref"] == "content/templates/home.html.j2"
        assert check(package, tmp_path).ok

    def test_missing_slot_file_is_named(self, package, tmp_path):
        (package / "content" / "templates" / "home.html.j2").unlink()
        report = check(package, tmp_path)
        assert not report.ok
        assert report.missing_files == ["content/templates/home.html.j2"]
        assert "missing" in report.summary()


class TestCatchesProblems:
    def test_broken_internal_link_is_caught_and_explained(self, package, tmp_path):
        """The defect that shipped in the example site once already: a template
        links somewhere the spec does not route.

        The suite's own r_login tests are removed too, or LINT-XREF-02 fires first
        and the link check never runs -- which is correct ordering, and not what
        this test is about.
        """
        edit_spec(package, lambda s: s["routes"].remove(
            next(r for r in s["routes"] if r["id"] == "r_login")
        ))
        path = package / "tests" / "suite.json"
        suite = json.loads(path.read_text())
        suite["tests"] = [t for t in suite["tests"] if t.get("route") != "r_login"]
        path.write_text(json.dumps(suite, indent=1))

        report = check(package, tmp_path)
        assert not report.ok, report.summary()
        assert report.lint == [], f"lint fired instead: {[str(f) for f in report.lint]}"
        broken = [t for t in report.tests if t.code == FAIL]
        assert broken, "a dangling link did not fail links_resolve"
        assert "/login" in broken[0].detail

    def test_suite_referencing_a_removed_route_is_a_lint_finding(self, package, tmp_path):
        """The ordering the test above works around, asserted on its own."""
        edit_spec(package, lambda s: s["routes"].remove(
            next(r for r in s["routes"] if r["id"] == "r_login")
        ))
        report = check(package, tmp_path)
        assert [f.code for f in report.lint] == ["LINT-XREF-02", "LINT-XREF-02"]
        assert report.tests == [], "tests ran on a package that failed lint"

    def test_dangling_reference_is_a_lint_finding(self, package, tmp_path):
        edit_spec(package, lambda s: s["routes"][0].__setitem__("template", "t_nope"))
        report = check(package, tmp_path)
        assert not report.ok
        assert any(f.code == "LINT-REF-03" for f in report.lint)
        assert report.tests == [], "tests ran on a package that failed lint"

    def test_free_text_smuggled_into_the_spec_is_caught(self, package, tmp_path):
        edit_spec(package, lambda s: s["routes"][0].__setitem__("note", "the home page"))
        report = check(package, tmp_path)
        assert any(f.code == "LINT-SCHEMA-02" for f in report.lint)

    def test_unsupported_framework_is_reported_in_words(self, package, tmp_path):
        """Inside this is code 21 and nothing else. Outside, say what it was."""
        edit_spec(package, lambda s: s.update(framework="django-postgres-v1"))
        report = check(package, tmp_path)
        assert not report.ok
        assert report.lint or report.compose_error

    def test_failing_expectation_reports_both_numbers(self, package, tmp_path):
        path = package / "tests" / "suite.json"
        suite = json.loads(path.read_text())
        suite["tests"][0]["expect_status"] = 404
        path.write_text(json.dumps(suite, indent=1))
        report = check(package, tmp_path)
        failed = next(t for t in report.tests if t.code == FAIL)
        assert "200" in failed.detail and "404" in failed.detail

    def test_cli_exits_one_on_failure(self, package, tmp_path):
        (package / "content" / "assets" / "style.css").unlink()
        assert main([str(package), "--work-dir", str(tmp_path / "w"), "--quiet"]) == 1


class TestAgreesWithGoLive:
    def test_agrees_with_golive(self, package, tmp_path, ):
        """Same generator, same suite, same verdict per test.

        recon-check is only worth running if it predicts what the inside will say.
        A divergence here means the pipeline ships packages that pass outside and
        fail inside as an integer nobody can act on.
        """
        from tools.bundle_build import build_bundle
        from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
        from tools.golive import GoLiveService

        outside = {t.test_id: t.code for t in check(package, tmp_path).tests}

        keys = tmp_path / "keys"
        generate_demo_keyset(keys)
        identity = load_signing_identity(
            keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE
        )
        archive = build_bundle(
            package, tmp_path / "out", identity=identity,
            golive_public_key_path=keys / "golive-wrapping.pub", sequence=1,
        )
        deployment = archive.parent / archive.name.removesuffix(".tar")
        result = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb").go_live(deployment)
        inside = {t.test_id: t.code for t in result.tests}

        assert outside == inside, "recon-check and go-live disagree"

    def test_inside_still_withholds_the_detail(self, package, tmp_path):
        """The asymmetry is the point: same verdict, different disclosure."""
        from tools.bundle_build import build_bundle
        from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
        from tools.golive import GoLiveService

        keys = tmp_path / "keys"
        generate_demo_keyset(keys)
        identity = load_signing_identity(
            keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE
        )
        archive = build_bundle(
            package, tmp_path / "out", identity=identity,
            golive_public_key_path=keys / "golive-wrapping.pub", sequence=1,
        )
        deployment = archive.parent / archive.name.removesuffix(".tar")
        result = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb").go_live(deployment)

        payload = json.dumps(result.for_worker())
        assert "GET /" not in payload and "internal links" not in payload
        for entry in result.for_worker()["tests"]:
            assert len(entry) == 2 and isinstance(entry[1], int)
