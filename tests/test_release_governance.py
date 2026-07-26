from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "release_governance.py"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "release-plan.schema.json"
WORKFLOW_ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOW_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "repository"
    / ".github"
    / "workflows"
)
NATIVE_WORKFLOW_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1] / "workflow-templates"
)
SPEC = importlib.util.spec_from_file_location("release_governance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
rg = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rg
SPEC.loader.exec_module(rg)


SCHEMA_URI = (
    "https://raw.githubusercontent.com/LicoLand/.github/main/"
    "schemas/release-plan.schema.json"
)


def scenario(
    scenario_id: str = "SCN-001",
    scenario_type: str = "capability",
    *,
    status: str = "accepted",
    issue: str | None = "https://github.com/LicoLand/example/issues/2",
    evidence: list[str] | None = None,
) -> dict:
    return {
        "id": scenario_id,
        "type": scenario_type,
        "title": "An independently acceptable outcome",
        "status": status,
        "issue": issue,
        "risk": "low",
        "acceptance": ["The observable outcome is accepted."],
        "evidence": ["https://github.com/LicoLand/example/actions/runs/1"]
        if evidence is None
        else evidence,
    }


def release(
    version: str = "0.2.0",
    classification: str = "minor",
    *,
    status: str = "ready",
    scenarios: list[dict] | None = None,
) -> dict:
    return {
        "version": version,
        "classification": classification,
        "status": status,
        "targetDate": "2026-08-01",
        "milestone": f"v{version}",
        "releaseIssue": "https://github.com/LicoLand/example/issues/1",
        "scenarios": [scenario()] if scenarios is None else scenarios,
        "blockers": [],
    }


def plan(
    *,
    current: str | None = "0.1.0",
    next_release: dict | None = None,
    profile: str = "semver",
) -> dict:
    return {
        "$schema": SCHEMA_URI,
        "schemaVersion": 1,
        "repository": "LicoLand/example",
        "profile": profile,
        "currentVersion": current,
        "versionSources": [{"format": "json", "path": "package.json", "pointer": "/version"}],
        "changelog": "CHANGELOG.md",
        "nextRelease": release() if next_release is None else next_release,
        "releases": [],
        "components": [],
    }


def component_plan(component_ids: tuple[str, ...] = ("parser",)) -> dict:
    components = []
    for index, component_id in enumerate(component_ids):
        target = release()
        target["releaseIssue"] = (
            f"https://github.com/LicoLand/example/issues/{index * 2 + 1}"
        )
        target["scenarios"][0]["id"] = f"SCN-{index + 1:03d}"
        target["scenarios"][0]["issue"] = (
            f"https://github.com/LicoLand/example/issues/{index * 2 + 2}"
        )
        components.append(
            {
                "id": component_id,
                "currentVersion": "0.1.0",
                "versionSources": [
                    {
                        "format": "json",
                        "path": f"{component_id}.json",
                        "pointer": "/version",
                    }
                ],
                "changelog": f"{component_id}-CHANGELOG.md",
                "nextRelease": target,
                "releases": [],
            }
        )
    document = plan(profile="component-semver")
    document.update(
        {
            "currentVersion": None,
            "versionSources": [],
            "changelog": None,
            "nextRelease": None,
            "releases": [],
            "components": components,
        }
    )
    return document


def write_component_repository(root: Path, document: dict) -> Path:
    plan_path = write_repository(root, document, None)
    for component in document["components"]:
        (root / component["versionSources"][0]["path"]).write_text(
            json.dumps({"version": component["nextRelease"]["version"]}) + "\n",
            encoding="utf-8",
        )
        (root / component["changelog"]).write_text(
            f"# Changelog\n\n## [{component['nextRelease']['version']}]\n",
            encoding="utf-8",
        )
    render_document(root, document)
    return plan_path


def write_repository(root: Path, document: dict, source_version: str | None = None) -> Path:
    (root / "docs" / "releases").mkdir(parents=True, exist_ok=True)
    plan_path = root / "docs" / "releases" / "plan.json"
    plan_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    if source_version is None:
        next_release = document.get("nextRelease")
        source_version = (
            next_release.get("version")
            if isinstance(next_release, dict) and next_release.get("status") == "ready"
            else document.get("currentVersion")
        )
    if source_version is not None:
        (root / "package.json").write_text(
            json.dumps({"version": source_version}) + "\n", encoding="utf-8"
        )
        (root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [{source_version}] - 2026-08-01\n\n- Entry.\n",
            encoding="utf-8",
        )
    return plan_path


def render_document(root: Path, document: dict) -> None:
    output = root / rg.DOCUMENT_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rg.render_release_document(document), encoding="utf-8")


class TransitionTests(unittest.TestCase):
    def test_strict_transition_classification(self) -> None:
        cases = (
            ("0.1.0", "0.1.1", "patch"),
            ("0.1.1", "0.2.0", "minor"),
            ("1.8.4", "2.0.0", "major"),
            (None, "0.1.0", "initial"),
            ("0.2.0-rc.2", "0.2.0", "stabilization"),
            ("0.9.7", "1.0.0", "stabilization"),
        )
        for current, target, expected in cases:
            with self.subTest(current=current, target=target):
                self.assertEqual(rg.classify_transition(current, target), expected)

    def test_skips_and_resets_are_rejected(self) -> None:
        invalid = (
            ("0.1.0", "0.1.2"),
            ("0.1.3", "0.2.1"),
            ("1.4.2", "3.0.0"),
            ("1.4.2", "2.1.0"),
            (None, "1.0.0"),
            ("0.2.0-rc.1", "0.2.1"),
            ("0.2.0", "0.3.0-rc.1"),
        )
        for current, target in invalid:
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    rg.classify_transition(current, target)

    def test_semver_rejects_numeric_prerelease_leading_zero(self) -> None:
        with self.assertRaises(ValueError):
            rg.SemVer.parse("1.0.0-rc.01")


class LocalPolicyTests(unittest.TestCase):
    def verify(self, document: dict, source_version: str | None = None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_repository(root, document, source_version)
        render_document(root, document)
        return rg.verify_plan(root, document)

    def test_patch_allows_only_fixes(self) -> None:
        target = release(
            "0.1.1",
            "patch",
            scenarios=[scenario("FIX-001", "capability")],
        )
        report = self.verify(plan(next_release=target), "0.1.1")
        self.assertIn("patch-scenarios", {finding.code for finding in report.errors})

    def test_patch_with_fix_is_valid(self) -> None:
        target = release(
            "0.1.1",
            "patch",
            scenarios=[scenario("FIX-001", "fix")],
        )
        report = self.verify(plan(next_release=target), "0.1.1")
        self.assertTrue(report.ok, report.errors)

    def test_minor_requires_capability_and_prohibits_breaking(self) -> None:
        target = release(
            "0.2.0",
            "minor",
            scenarios=[scenario("BRK-001", "breaking")],
        )
        report = self.verify(plan(next_release=target), "0.2.0")
        self.assertIn("minor-scenarios", {finding.code for finding in report.errors})

    def test_major_requires_breaking_scenario(self) -> None:
        target = release(
            "2.0.0",
            "major",
            scenarios=[scenario("SCN-001", "capability")],
        )
        report = self.verify(
            plan(current="1.2.3", next_release=target),
            "2.0.0",
        )
        self.assertIn("major-scenarios", {finding.code for finding in report.errors})

    def test_declared_classification_must_match_transition(self) -> None:
        target = release(
            "0.1.1",
            "minor",
            scenarios=[scenario("SCN-001", "capability")],
        )
        report = self.verify(plan(next_release=target), "0.1.1")
        self.assertIn("classification", {finding.code for finding in report.errors})

    def test_initial_release_is_exact_and_requires_capability(self) -> None:
        target = release(
            "0.1.0",
            "initial",
            scenarios=[scenario("FIX-001", "fix")],
        )
        report = self.verify(plan(current=None, next_release=target), "0.1.0")
        self.assertIn("initial-scenarios", {finding.code for finding in report.errors})

    def test_ready_requires_closed_local_acceptance_contract(self) -> None:
        target = release(
            scenarios=[
                scenario(
                    status="active",
                    issue=None,
                    evidence=[],
                )
            ],
        )
        target["releaseIssue"] = None
        target["blockers"] = ["Security review is pending."]
        report = self.verify(plan(next_release=target), "0.2.0")
        codes = {finding.code for finding in report.errors}
        self.assertTrue(
            {
                "ready-blockers",
                "ready-release-issue",
                "ready-scenario-status",
                "ready-scenario-issue",
                "ready-scenario-evidence",
            }.issubset(codes),
            codes,
        )

    def test_ready_issue_urls_are_pairwise_unique(self) -> None:
        target = release()
        target["scenarios"][0]["issue"] = target["releaseIssue"]
        report = self.verify(plan(next_release=target), "0.2.0")
        self.assertIn(
            "ready-issue-duplicate", {finding.code for finding in report.errors}
        )

    def test_ready_issue_urls_are_unique_across_components(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        document = component_plan(("parser", "provider"))
        duplicate = document["components"][0]["nextRelease"]["scenarios"][0]["issue"]
        document["components"][1]["nextRelease"]["releaseIssue"] = duplicate
        write_component_repository(root, document)
        report = rg.verify_plan(root, document)
        self.assertIn(
            "ready-issue-duplicate", {finding.code for finding in report.errors}
        )

    def test_non_versioned_profile_rejects_product_state(self) -> None:
        document = plan(profile="continuous-site")
        report = self.verify(document, "0.2.0")
        self.assertIn("profile-version", {finding.code for finding in report.errors})

    def test_component_profile_uses_component_authority(self) -> None:
        component_release = release()
        component = {
            "id": "parser",
            "currentVersion": "0.1.0",
            "versionSources": [
                {"format": "json", "path": "package.json", "pointer": "/version"}
            ],
            "changelog": "CHANGELOG.md",
            "nextRelease": component_release,
            "releases": [],
        }
        document = plan(profile="component-semver")
        document.update(
            {
                "currentVersion": None,
                "versionSources": [],
                "changelog": None,
                "nextRelease": None,
                "releases": [],
                "components": [component],
            }
        )
        report = self.verify(document, "0.2.0")
        self.assertTrue(report.ok, report.errors)

    def test_release_version_rejects_prerelease_and_build(self) -> None:
        for invalid_version in ("0.2.0-rc.1", "0.2.0+build.1"):
            with self.subTest(version=invalid_version):
                target = release(invalid_version)
                target["milestone"] = "v0.2.0"
                report = self.verify(plan(next_release=target), invalid_version)
                self.assertIn(
                    "release-version", {finding.code for finding in report.errors}
                )


class VersionSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_reads_json_toml_text_and_regex_sources(self) -> None:
        (self.root / "package.json").write_text(
            '{"metadata":{"version":"1.2.3"}}\n', encoding="utf-8"
        )
        (self.root / "pyproject.toml").write_text(
            '[project]\nversion = "1.2.3"\n', encoding="utf-8"
        )
        (self.root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "version.py").write_text(
            '__version__ = "1.2.3"\n', encoding="utf-8"
        )
        sources = (
            {"format": "json", "path": "package.json", "pointer": "/metadata/version"},
            {"format": "toml", "path": "pyproject.toml", "key": "project.version"},
            {"format": "text", "path": "VERSION"},
            {
                "format": "regex",
                "path": "version.py",
                "pattern": r'__version__\s*=\s*"(?P<version>[^"]+)"',
            },
        )
        for source in sources:
            with self.subTest(source=source["format"]):
                self.assertEqual(rg.read_version_source(self.root, source), "1.2.3")

    def test_source_cannot_escape_repository(self) -> None:
        with self.assertRaises(rg.GovernanceError) as context:
            rg.read_version_source(
                self.root,
                {"format": "text", "path": "../VERSION"},
            )
        self.assertEqual(context.exception.code, "unsafe-path")
        self.assertNotIn(str(self.root), str(context.exception))

    def test_planned_release_checks_current_not_target_source(self) -> None:
        document = plan(next_release=release(status="planned"))
        write_repository(self.root, document, "0.1.0")
        render_document(self.root, document)
        report = rg.verify_plan(self.root, document)
        self.assertTrue(report.ok, report.errors)

    def test_ready_release_checks_target_source_and_changelog(self) -> None:
        document = plan()
        write_repository(self.root, document, "0.1.0")
        render_document(self.root, document)
        report = rg.verify_plan(self.root, document)
        codes = {finding.code for finding in report.errors}
        self.assertIn("version-source-drift", codes)
        self.assertIn("changelog-version", codes)

    def test_version_source_selector_contract_is_exclusive(self) -> None:
        report = rg.VerificationReport()
        rg._validate_source_shape(
            report,
            {
                "format": "text",
                "path": "VERSION",
                "pointer": "/version",
            },
            "plan.versionSources[0]",
        )
        self.assertIn("schema-property", {finding.code for finding in report.errors})


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = plan()
        write_repository(self.root, self.document)

    def test_verify_detects_missing_and_drifted_projection(self) -> None:
        report = rg.verify_plan(self.root, self.document)
        self.assertIn("document-missing", {finding.code for finding in report.errors})
        output = self.root / rg.DOCUMENT_PATH
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("stale\n", encoding="utf-8")
        report = rg.verify_plan(self.root, self.document)
        self.assertIn("document-drift", {finding.code for finding in report.errors})

    def test_render_dry_run_has_no_side_effect(self) -> None:
        code = rg.main(
            [
                "render",
                "--repository-root",
                str(self.root),
                "--plan",
                rg.DEFAULT_PLAN_PATH,
            ]
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.root / rg.DOCUMENT_PATH).exists())

    def test_render_apply_writes_exact_projection(self) -> None:
        code = rg.main(
            [
                "render",
                "--repository-root",
                str(self.root),
                "--plan",
                rg.DEFAULT_PLAN_PATH,
                "--apply",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.root / rg.DOCUMENT_PATH).read_text(encoding="utf-8"),
            rg.render_release_document(self.document),
        )

    def test_render_check_returns_failure_on_drift(self) -> None:
        code = rg.main(
            [
                "render",
                "--repository-root",
                str(self.root),
                "--check",
            ]
        )
        self.assertEqual(code, 1)


class RepositoryIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = plan()
        write_repository(self.root, self.document)
        render_document(self.root, self.document)

    def test_verify_repository_mismatch_fails_before_github_read(self) -> None:
        client = FakeVerifyGh(self.document)
        report = rg.verify_repository(
            self.root,
            github=True,
            expected_repository="LicoLand/different",
            gh=client,
        )
        self.assertIn("repository-mismatch", {item.code for item in report.errors})
        self.assertEqual(client.read_calls, [])

    def test_github_operations_require_expected_repository(self) -> None:
        client = FakeVerifyGh(self.document)
        report = rg.verify_repository(
            self.root,
            github=True,
            gh=client,
        )
        self.assertIn(
            "expected-repository-required",
            {item.code for item in report.errors},
        )
        self.assertEqual(client.read_calls, [])
        with self.assertRaises(rg.GovernanceError) as sync_error:
            rg.sync_github(
                self.root,
                self.document,
                project_owner="LicoLand",
                apply=False,
                gh=FakeGh(apply=False),
            )
        self.assertEqual(
            sync_error.exception.code, "expected-repository-required"
        )
        with self.assertRaises(rg.GovernanceError) as finalize_error:
            rg.finalize_plan(
                self.root,
                self.document,
                release_url=(
                    "https://github.com/LicoLand/example/releases/tag/v0.2.0"
                ),
                released_at="2026-08-02T09:30:00Z",
            )
        self.assertEqual(
            finalize_error.exception.code, "expected-repository-required"
        )

    def test_sync_repository_mismatch_fails_before_github_read_or_write(self) -> None:
        client = FakeGh(apply=True)
        client.enable_release_project()
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                self.root,
                self.document,
                project_owner="LicoLand",
                expected_repository="LicoLand/different",
                apply=True,
                gh=client,
            )
        self.assertEqual(context.exception.code, "repository-mismatch")
        self.assertEqual(client.read_calls, [])
        self.assertEqual(client.operations, [])

    def test_finalize_repository_mismatch_does_not_change_input(self) -> None:
        original = copy.deepcopy(self.document)
        with self.assertRaises(rg.GovernanceError) as context:
            rg.finalize_plan(
                self.root,
                self.document,
                release_url=(
                    "https://github.com/LicoLand/example/releases/tag/v0.2.0"
                ),
                released_at="2026-08-02T09:30:00Z",
                expected_repository="LicoLand/different",
            )
        self.assertEqual(context.exception.code, "repository-mismatch")
        self.assertEqual(self.document, original)


class RemoteReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = plan()
        write_repository(self.root, self.document)
        render_document(self.root, self.document)

    def verify_with(self, client) -> rg.VerificationReport:
        return rg.verify_repository(
            self.root,
            github=True,
            expected_repository="LicoLand/example",
            gh=client,
        )

    def test_remote_issue_must_use_declared_milestone(self) -> None:
        client = FakeVerifyGh(self.document, issue_milestone="v9.9.9")
        report = self.verify_with(client)
        self.assertIn(
            "github-issue-milestone", {finding.code for finding in report.errors}
        )

    def test_remote_ready_contract_passes_with_closed_issues_and_milestone(self) -> None:
        client = FakeVerifyGh(self.document)
        report = self.verify_with(client)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(
            any("/issues/" in " ".join(arguments) for arguments in client.read_calls)
        )
        self.assertTrue(
            any(
                "milestones?state=all" in " ".join(arguments)
                for arguments in client.read_calls
            )
        )


class ComponentTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = component_plan()
        write_component_repository(self.root, self.document)

    def test_component_local_tag_uses_release_unit_prefix(self) -> None:
        accepted = rg.VerificationReport()
        rg._validate_tag_locally(accepted, self.document, "parser-v0.2.0")
        self.assertTrue(accepted.ok, accepted.errors)
        rejected = rg.VerificationReport()
        rg._validate_tag_locally(rejected, self.document, "v0.2.0")
        self.assertIn("tag-version", {finding.code for finding in rejected.errors})

        semver_report = rg.VerificationReport()
        rg._validate_tag_locally(semver_report, plan(), "v0.2.0")
        self.assertTrue(semver_report.ok, semver_report.errors)

    def test_component_remote_tag_uses_release_unit_prefix(self) -> None:
        client = FakeVerifyGh(self.document)
        report = rg.verify_repository(
            self.root,
            github=True,
            expected_repository="LicoLand/example",
            tag="parser-v0.2.0",
            gh=client,
        )
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(
            any(
                "git/ref/tags/parser-v0.2.0" in " ".join(arguments)
                for arguments in client.read_calls
            )
        )

    def test_component_finalize_and_release_url_use_prefixed_tag(self) -> None:
        release_url = (
            "https://github.com/LicoLand/example/releases/tag/parser-v0.2.0"
        )
        finalized = rg.finalize_plan(
            self.root,
            self.document,
            release_url=release_url,
            released_at="2026-08-02T09:30:00Z",
            component="parser",
            expected_repository="LicoLand/example",
            github=True,
            gh=FakeVerifyGh(self.document),
        )
        self.assertEqual(
            finalized["components"][0]["releases"][0]["releaseUrl"],
            release_url,
        )
        with self.assertRaises(rg.GovernanceError) as context:
            rg.finalize_plan(
                self.root,
                self.document,
                release_url=(
                    "https://github.com/LicoLand/example/releases/tag/v0.2.0"
                ),
                released_at="2026-08-02T09:30:00Z",
                component="parser",
                expected_repository="LicoLand/example",
            )
        self.assertEqual(context.exception.code, "release-url")


class DryRunGitHubTests(unittest.TestCase):
    def test_gh_mutation_dry_run_never_starts_process(self) -> None:
        client = rg.GhClient(apply=False)
        with mock.patch.object(subprocess, "run") as run:
            result = client.mutate_text(
                "issue.create",
                ["issue", "create", "--repo", "LicoLand/example"],
            )
        self.assertIsNone(result)
        run.assert_not_called()
        self.assertEqual(client.operations, ["issue.create"])

    def test_injected_client_apply_mode_must_match(self) -> None:
        client = FakeGh(apply=False)
        with self.assertRaises(rg.GovernanceError) as bootstrap_error:
            rg.bootstrap_project("LicoLand", apply=True, gh=client)
        self.assertEqual(bootstrap_error.exception.code, "github-apply-mismatch")
        self.assertEqual(client.read_calls, [])

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        document = plan(next_release=release(status="planned"))
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        with self.assertRaises(rg.GovernanceError) as sync_error:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/example",
                apply=True,
                gh=client,
            )
        self.assertEqual(sync_error.exception.code, "github-apply-mismatch")
        self.assertEqual(client.read_calls, [])

    def test_bootstrap_missing_project_records_all_writes_only(self) -> None:
        client = FakeGh(apply=False)
        client.projects = []
        operations = rg.bootstrap_project(
            "LicoLand",
            apply=False,
            gh=client,
        )
        self.assertIn("project.create", operations)
        self.assertEqual(
            len([item for item in operations if item.startswith("project.field.create:")]),
            len(rg.FIELD_SPECS),
        )
        self.assertEqual(client.executed_mutations, 0)

    def test_sync_dry_run_records_writes_without_executing(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=False)
        client.enable_release_project()
        before_plan = (root / rg.DEFAULT_PLAN_PATH).read_bytes()
        before_document = (root / rg.DOCUMENT_PATH).read_bytes()
        operations = rg.sync_github(
            root,
            document,
            project_owner="LicoLand",
            apply=False,
            gh=client,
            expected_repository="LicoLand/example",
        )
        self.assertTrue(any(item.startswith("label.create:") for item in operations))
        self.assertIn("milestone.create", operations)
        self.assertIn("issue.create", operations)
        self.assertEqual(client.executed_mutations, 0)
        self.assertEqual((root / rg.DEFAULT_PLAN_PATH).read_bytes(), before_plan)
        self.assertEqual((root / rg.DOCUMENT_PATH).read_bytes(), before_document)

    def test_sync_apply_preflight_failure_has_zero_remote_writes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                apply=True,
                gh=client,
                expected_repository="LicoLand/example",
            )
        self.assertEqual(context.exception.code, "github-project")
        self.assertEqual(client.executed_mutations, 0)
        self.assertEqual(client.operations, [])

    def test_issue_list_failure_happens_before_every_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        client.fail_read_command = ("issue", "list")
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/example",
                apply=True,
                gh=client,
            )
        self.assertEqual(context.exception.code, "github-command")
        self.assertEqual(client.executed_mutations, 0)
        self.assertEqual(client.operations, [])
        self.assertFalse(
            any(kind == "mutation" for kind, _ in client.events)
        )

    def test_configured_issue_missing_fails_before_every_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        document = plan(next_release=release(status="planned"))
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/example",
                apply=True,
                gh=client,
            )
        self.assertEqual(context.exception.code, "github-issue")
        self.assertEqual(client.operations, [])
        self.assertEqual(client.executed_mutations, 0)

    def test_duplicate_marker_fails_before_every_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        marker = rg._issue_marker("example", "release", "0.2.0")
        client.issues = [
            {
                "number": number,
                "title": "Managed release",
                "body": marker,
                "url": f"https://github.com/LicoLand/example/issues/{number}",
                "state": "open",
                "milestone": None,
                "labels": [],
            }
            for number in (10, 11)
        ]
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/example",
                apply=True,
                gh=client,
            )
        self.assertEqual(context.exception.code, "github-issue-duplicate")
        self.assertEqual(client.operations, [])
        self.assertEqual(client.executed_mutations, 0)

    def test_malformed_matching_milestone_fails_before_every_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        client.milestones = [{"title": "v0.2.0", "number": "invalid"}]
        with self.assertRaises(rg.GovernanceError) as context:
            rg.sync_github(
                root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/example",
                apply=True,
                gh=client,
            )
        self.assertEqual(context.exception.code, "github-response")
        self.assertEqual(client.operations, [])
        self.assertEqual(client.executed_mutations, 0)

    def test_successful_sync_completes_all_reads_before_first_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        rg.sync_github(
            root,
            document,
            project_owner="LicoLand",
            expected_repository="LicoLand/example",
            apply=True,
            gh=client,
        )
        first_mutation = next(
            index
            for index, (kind, _) in enumerate(client.events)
            if kind == "mutation"
        )
        self.assertFalse(
            any(
                kind == "read"
                for kind, _ in client.events[first_mutation + 1 :]
            ),
            client.events,
        )
        reads = {
            value for kind, value in client.events[:first_mutation] if kind == "read"
        }
        self.assertTrue(
            {
                "project list",
                "project item-list",
                "project field-list",
                "label list",
                "issue list",
                "api milestones",
            }.issubset(reads),
            reads,
        )

    def test_sync_rejects_public_or_unknown_project_before_writes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        for public_value in (True, None):
            with self.subTest(public=public_value):
                client = FakeGh(apply=True)
                client.enable_release_project()
                client.projects[0]["public"] = public_value
                with self.assertRaises(rg.GovernanceError) as context:
                    rg.sync_github(
                        root,
                        document,
                        project_owner="LicoLand",
                        expected_repository="LicoLand/example",
                        apply=True,
                        gh=client,
                    )
                self.assertEqual(
                    context.exception.code, "github-project-privacy"
                )
                self.assertEqual(client.executed_mutations, 0)
                self.assertEqual(client.operations, [])

    def test_sync_apply_persists_created_issue_urls_and_projection(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = release(status="planned")
        target["releaseIssue"] = None
        target["scenarios"][0]["issue"] = None
        document = plan(next_release=target)
        write_repository(root, document, "0.1.0")
        render_document(root, document)
        client = FakeGh(apply=True)
        client.enable_release_project()
        with mock.patch.object(rg, "GhClient", return_value=client):
            code = rg.main(
                [
                    "sync-github",
                    "--repository-root",
                    str(root),
                    "--project-owner",
                    "LicoLand",
                    "--expected-repository",
                    "LicoLand/example",
                    "--apply",
                ]
            )
        self.assertEqual(code, 0)
        persisted = json.loads(
            (root / rg.DEFAULT_PLAN_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(
            persisted["nextRelease"]["releaseIssue"],
            "https://github.com/LicoLand/example/issues/101",
        )
        self.assertEqual(
            persisted["nextRelease"]["scenarios"][0]["issue"],
            "https://github.com/LicoLand/example/issues/102",
        )
        self.assertEqual(
            (root / rg.DOCUMENT_PATH).read_text(encoding="utf-8"),
            rg.render_release_document(persisted),
        )


class FinalizeTests(unittest.TestCase):
    RELEASE_URL = "https://github.com/LicoLand/example/releases/tag/v0.2.0"
    RELEASED_AT = "2026-08-02T09:30:00Z"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = plan()
        self.previous = release(
            "0.1.0",
            "initial",
            status="ready",
            scenarios=[scenario("SCN-000", "capability")],
        )
        self.previous.update(
            {
                "releasedAt": "2026-07-01T00:00:00Z",
                "releaseUrl": "https://github.com/LicoLand/example/releases/tag/v0.1.0",
            }
        )
        self.document["releases"] = [self.previous]
        write_repository(self.root, self.document)
        render_document(self.root, self.document)

    def test_finalize_preserves_history_and_complete_next_release(self) -> None:
        original = copy.deepcopy(self.document)
        result = rg.finalize_plan(
            self.root,
            self.document,
            release_url=self.RELEASE_URL,
            released_at=self.RELEASED_AT,
            expected_repository="LicoLand/example",
        )
        self.assertEqual(self.document, original, "input plan must not be mutated")
        self.assertEqual(result["currentVersion"], "0.2.0")
        self.assertIsNone(result["nextRelease"])
        self.assertEqual(result["releases"][0], self.previous)
        archived = result["releases"][1]
        for key, value in original["nextRelease"].items():
            self.assertEqual(archived[key], value)
        self.assertEqual(archived["releasedAt"], self.RELEASED_AT)
        self.assertEqual(archived["releaseUrl"], self.RELEASE_URL)

    def test_finalize_rejects_non_ready_release(self) -> None:
        self.document["nextRelease"]["status"] = "active"
        self.document["nextRelease"]["scenarios"][0]["status"] = "active"
        write_repository(self.root, self.document, "0.1.0")
        with self.assertRaises(rg.GovernanceError) as context:
            rg.finalize_plan(
                self.root,
                self.document,
                release_url=self.RELEASE_URL,
                released_at=self.RELEASED_AT,
                expected_repository="LicoLand/example",
            )
        self.assertEqual(context.exception.code, "release-not-ready")

    def test_finalize_rejects_duplicate_archive(self) -> None:
        duplicate = copy.deepcopy(self.document["nextRelease"])
        duplicate.update(
            {
                "releasedAt": self.RELEASED_AT,
                "releaseUrl": self.RELEASE_URL,
            }
        )
        self.document["releases"].append(duplicate)
        with self.assertRaises(rg.GovernanceError) as context:
            rg.finalize_plan(
                self.root,
                self.document,
                release_url=self.RELEASE_URL,
                released_at=self.RELEASED_AT,
                expected_repository="LicoLand/example",
            )
        self.assertIn(context.exception.code, {"plan-invalid", "release-duplicate"})

    def test_finalize_dry_run_does_not_write_plan_or_document(self) -> None:
        plan_path = self.root / rg.DEFAULT_PLAN_PATH
        document_path = self.root / rg.DOCUMENT_PATH
        before_plan = plan_path.read_bytes()
        before_document = document_path.read_bytes()
        code = rg.main(
            [
                "finalize",
                "--repository-root",
                str(self.root),
                "--release-url",
                self.RELEASE_URL,
                "--released-at",
                self.RELEASED_AT,
                "--expected-repository",
                "LicoLand/example",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(plan_path.read_bytes(), before_plan)
        self.assertEqual(document_path.read_bytes(), before_document)

    def test_finalize_apply_writes_history_and_rerenders(self) -> None:
        code = rg.main(
            [
                "finalize",
                "--repository-root",
                str(self.root),
                "--release-url",
                self.RELEASE_URL,
                "--released-at",
                self.RELEASED_AT,
                "--expected-repository",
                "LicoLand/example",
                "--apply",
            ]
        )
        self.assertEqual(code, 0)
        finalized = json.loads(
            (self.root / rg.DEFAULT_PLAN_PATH).read_text(encoding="utf-8")
        )
        self.assertIsNone(finalized["nextRelease"])
        self.assertEqual(len(finalized["releases"]), 2)
        self.assertEqual(
            (self.root / rg.DOCUMENT_PATH).read_text(encoding="utf-8"),
            rg.render_release_document(finalized),
        )


class WorkflowContractTests(unittest.TestCase):
    INPUT_NAMES = (
        "governance_ref",
        "target_repository",
        "target_ref",
        "expected_repository",
        "plan_path",
        "tag",
        "require_tag",
    )
    RETIRED_INPUT_NAMES = tuple(
        name.replace("_", "-") for name in INPUT_NAMES if "_" in name
    )

    def test_reusable_workflow_uses_expression_safe_input_names(self) -> None:
        reusable = (
            WORKFLOW_ROOT / "reusable-version-governance.yml"
        ).read_text(encoding="utf-8")
        for name in self.INPUT_NAMES:
            with self.subTest(name=name):
                self.assertIn(f"      {name}:", reusable)
        for name in self.RETIRED_INPUT_NAMES:
            with self.subTest(retired=name):
                self.assertNotIn(f"      {name}:", reusable)
                self.assertNotIn(f"inputs.{name}", reusable)

    def test_callers_use_only_canonical_input_names(self) -> None:
        callers = (
            WORKFLOW_ROOT / "version-governance.yml",
            WORKFLOW_TEMPLATE_ROOT / "version-governance.yml",
        )
        for caller in callers:
            text = caller.read_text(encoding="utf-8")
            for name in self.RETIRED_INPUT_NAMES:
                with self.subTest(caller=caller.name, retired=name):
                    self.assertNotIn(f"      {name}:", text)

    def test_reusable_workflow_uses_minimal_repository_token_permissions(self) -> None:
        reusable = (
            WORKFLOW_ROOT / "reusable-version-governance.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  contents: read", reusable)
        self.assertIn("  issues: read", reusable)
        self.assertNotIn("      pull-requests: read", reusable)
        self.assertIn("          GH_TOKEN: ${{ github.token }}", reusable)
        self.assertIn("            --github", reusable)

    def test_auditor_template_is_input_free_and_uses_trusted_events(self) -> None:
        caller = (
            WORKFLOW_TEMPLATE_ROOT / "lico-auditor-release-gate.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  pull_request_target:", caller)
        self.assertNotIn("\n  pull_request:", caller)
        self.assertIn('      - "@DEFAULT_BRANCH@"', caller)
        self.assertIn('      - "v*.*.*"', caller)
        self.assertIn('      - "*-v*.*.*"', caller)
        self.assertIn("  workflow_dispatch:", caller)
        self.assertIn(
            "uses: LicoLand/Lico-Auditor/.github/workflows/"
            "release-audit.yml@only",
            caller,
        )
        self.assertIn("permissions:\n  contents: read", caller)
        self.assertNotIn("\n    with:", caller)
        self.assertNotIn("secrets:", caller)

    def test_templates_preserve_agent_and_auditor_separation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        governance = (root / "docs" / "version-governance.md").read_text(
            encoding="utf-8"
        )
        issue = (
            root / ".github" / "ISSUE_TEMPLATE" / "release-plan.yml"
        ).read_text(encoding="utf-8")
        pull_request = (
            root / ".github" / "PULL_REQUEST_TEMPLATE.md"
        ).read_text(encoding="utf-8")
        self.assertIn("There is no organization-wide product version", governance)
        self.assertIn("Each product repository", governance)
        self.assertIn("$lico-release-engineering-workflow", governance)
        self.assertIn("Lico-Dev cannot approve its own final audit", governance)
        self.assertIn("never executes target", governance)
        self.assertIn("lico-auditor / final-gate", governance)
        self.assertIn("Lico-Dev implemented each scenario", issue)
        self.assertIn("independent Lico-Auditor", issue)
        self.assertIn("Owning Lico-Dev skill or workflow", pull_request)
        self.assertIn("Lico-Auditor remains an independent final gate", pull_request)

    def test_native_organization_template_combines_both_required_jobs(self) -> None:
        workflow = (
            NATIVE_WORKFLOW_TEMPLATE_ROOT
            / "licoland-repository-release-governance.yml"
        ).read_text(encoding="utf-8")
        metadata = json.loads(
            (
                NATIVE_WORKFLOW_TEMPLATE_ROOT
                / "licoland-repository-release-governance.properties.json"
            ).read_text(encoding="utf-8")
        )
        governance_ref = "0875118889c39df343afeb6f4aa6e82f9690cc4d"
        self.assertIn("  pull_request_target:", workflow)
        self.assertNotIn("\n  pull_request:", workflow)
        self.assertIn("      - $default-branch", workflow)
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertIn("  version-governance:", workflow)
        self.assertIn("  lico-auditor:", workflow)
        self.assertIn(
            "uses: LicoLand/.github/.github/workflows/"
            f"reusable-version-governance.yml@{governance_ref}",
            workflow,
        )
        self.assertIn(f'      governance_ref: "{governance_ref}"', workflow)
        self.assertIn(
            "uses: LicoLand/Lico-Auditor/.github/workflows/"
            "release-audit.yml@only",
            workflow,
        )
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("  issues: read", workflow)
        self.assertNotIn("secrets:", workflow)
        self.assertEqual(
            metadata,
            {
                "name": "LicoLand Repository Release Governance",
                "description": (
                    "Validate an independent repository version contract and "
                    "run the Lico-Auditor final gate."
                ),
                "categories": ["Automation"],
            },
        )


class SchemaConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.definitions = cls.schema["$defs"]

    def test_semver_pattern_matches_tool_prerelease_rules(self) -> None:
        pattern = re_compile(self.definitions["semver"]["pattern"])
        values = (
            "0.1.0",
            "1.2.3-rc.1",
            "1.2.3-0",
            "1.2.3-alpha-01",
            "1.2.3+001",
            "1.2.3-01",
            "01.2.3",
            "1.2.3-rc.01",
        )
        for value in values:
            with self.subTest(value=value):
                schema_accepts = pattern.fullmatch(value) is not None
                try:
                    rg.SemVer.parse(value)
                    tool_accepts = True
                except ValueError:
                    tool_accepts = False
                self.assertEqual(schema_accepts, tool_accepts)

    def test_planned_release_uses_stable_semver_without_build(self) -> None:
        release_version = self.definitions["releaseCore"]["properties"]["version"]
        self.assertEqual(release_version["$ref"], "#/$defs/stableSemver")
        stable_pattern = re_compile(self.definitions["stableSemver"]["pattern"])
        self.assertIsNotNone(stable_pattern.fullmatch("0.2.0"))
        self.assertIsNone(stable_pattern.fullmatch("0.2.0-rc.1"))
        self.assertIsNone(stable_pattern.fullmatch("0.2.0+build.1"))

    def test_version_source_schema_has_one_exclusive_selector_per_format(self) -> None:
        branches = self.definitions["versionSource"]["oneOf"]
        by_format = {
            branch["properties"]["format"]["const"]: branch for branch in branches
        }
        expected = {
            "json": {"format", "path", "pointer"},
            "toml": {"format", "path", "key"},
            "text": {"format", "path"},
            "regex": {"format", "path", "pattern"},
        }
        self.assertEqual(set(by_format), set(expected))
        for source_format, keys in expected.items():
            branch = by_format[source_format]
            self.assertTrue(branch["additionalProperties"] is False)
            self.assertEqual(set(branch["required"]), keys)
            self.assertEqual(set(branch["properties"]), keys)
            report = rg.VerificationReport()
            source = {key: "value" for key in keys}
            source["format"] = source_format
            source["path"] = "VERSION"
            if source_format == "json":
                source["pointer"] = "/version"
            rg._validate_source_shape(report, source, "source")
            self.assertTrue(report.ok, report.errors)
        pointer_pattern = re_compile(
            by_format["json"]["properties"]["pointer"]["pattern"]
        )
        for pointer in ("", "/version", "/metadata/version", "version", "0/version"):
            with self.subTest(pointer=pointer):
                schema_accepts = pointer_pattern.fullmatch(pointer) is not None
                try:
                    rg._json_pointer(
                        {
                            "version": "0.1.0",
                            "metadata": {"version": "0.1.0"},
                        },
                        pointer,
                    )
                    tool_accepts = True
                except (rg.GovernanceError, ValueError):
                    tool_accepts = False
                self.assertEqual(schema_accepts, tool_accepts)

    def test_schema_and_tool_require_https_release_links(self) -> None:
        https_ref = "#/$defs/httpsUri"
        self.assertEqual(
            self.definitions["scenario"]["properties"]["issue"]["oneOf"][1]["$ref"],
            https_ref,
        )
        self.assertEqual(
            self.definitions["releaseCore"]["properties"]["releaseIssue"]["oneOf"][1][
                "$ref"
            ],
            https_ref,
        )
        self.assertEqual(
            self.definitions["releaseRecord"]["allOf"][1]["properties"]["releaseUrl"][
                "$ref"
            ],
            https_ref,
        )
        pattern = re_compile(self.definitions["httpsUri"]["pattern"])
        for url in (
            "https://github.com/LicoLand/example/issues/1",
            "http://github.com/LicoLand/example/issues/1",
            "https:///missing-host",
            "https://user@github.com/LicoLand/example/issues/1",
            "https://@example.invalid/path",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    pattern.fullmatch(url) is not None,
                    rg._is_https_url(url),
                )


def re_compile(pattern: str):
    # JSON Schema regular expressions used here are also valid Python regular
    # expressions; keeping this tiny adapter makes the consistency intent clear.
    import re

    return re.compile(pattern)


class FakeGh:
    """Read-fixture gh adapter that proves dry-run writes are never executed."""

    def __init__(self, *, apply: bool):
        self.apply = apply
        self.operations: list[str] = []
        self.executed_mutations = 0
        self.read_calls: list[list[str]] = []
        self.events: list[tuple[str, str]] = []
        self.fail_read_command: tuple[str, ...] | None = None
        self.projects: list[dict] = []
        self.fields: list[dict] = []
        self.issues: list[dict] = []
        self.milestones: list[dict] = []
        self.issue_counter = 100

    def enable_release_project(self) -> None:
        self.projects = [
            {
                "id": "PVT_project",
                "number": 7,
                "title": rg.DEFAULT_PROJECT_TITLE,
                "public": False,
            }
        ]
        self.fields = []
        for index, (name, data_type, options) in enumerate(rg.FIELD_SPECS):
            field = {
                "id": f"PVTF_{index}",
                "name": name,
                "dataType": data_type,
            }
            if options:
                field["options"] = [
                    {"id": f"OPT_{index}_{option}", "name": option}
                    for option in options
                ]
            self.fields.append(field)

    def json(self, arguments):
        self.read_calls.append(list(arguments))
        joined = " ".join(arguments)
        if "milestones?state=all" in joined:
            read_name = "api milestones"
        else:
            read_name = " ".join(arguments[:2])
        self.events.append(("read", read_name))
        if (
            self.fail_read_command is not None
            and tuple(arguments[: len(self.fail_read_command)])
            == self.fail_read_command
        ):
            raise rg.GovernanceError(
                "github-command", "GitHub CLI request failed"
            )
        if arguments[:2] == ["project", "list"]:
            return {"projects": self.projects}
        if arguments[:2] == ["project", "item-list"]:
            return {"items": []}
        if arguments[:2] == ["project", "field-list"]:
            return {"fields": self.fields}
        if arguments[:2] == ["label", "list"]:
            return []
        if arguments[:2] == ["issue", "list"]:
            return self.issues
        if "milestones?state=all" in joined:
            return self.milestones
        raise AssertionError(f"unexpected read call category: {arguments[:2]}")

    def mutate_text(self, operation, arguments):
        self.operations.append(operation)
        self.events.append(("mutation", operation))
        if not self.apply:
            return None
        self.executed_mutations += 1
        if operation == "issue.create":
            self.issue_counter += 1
            return (
                "https://github.com/LicoLand/example/issues/"
                f"{self.issue_counter}\n"
            )
        return ""

    def mutate_json(self, operation, arguments):
        result = self.mutate_text(operation, arguments)
        if result is None:
            return None
        return {}


class FakeVerifyGh:
    def __init__(
        self,
        document: dict,
        *,
        issue_milestone: str | None = None,
    ):
        self.apply = False
        self.document = document
        self.issue_milestone = issue_milestone
        self.read_calls: list[list[str]] = []
        self.operations: list[str] = []

    def _ready_releases(self):
        if self.document["profile"] == "component-semver":
            return [
                component["nextRelease"]
                for component in self.document["components"]
                if component["nextRelease"] is not None
            ]
        return (
            [self.document["nextRelease"]]
            if self.document["nextRelease"] is not None
            else []
        )

    def json(self, arguments):
        self.read_calls.append(list(arguments))
        joined = " ".join(arguments)
        if arguments[:2] == ["api", "graphql"]:
            return {}
        if "milestones?state=all" in joined:
            return [
                {
                    "title": target["milestone"],
                    "state": "closed",
                    "open_issues": 0,
                }
                for target in self._ready_releases()
            ]
        match = next(
            (
                target
                for target in self._ready_releases()
                if f"/issues/{target['releaseIssue'].rsplit('/', 1)[-1]}" in joined
            ),
            None,
        )
        if match is None:
            for target in self._ready_releases():
                for item in target["scenarios"]:
                    if f"/issues/{item['issue'].rsplit('/', 1)[-1]}" in joined:
                        match = target
                        break
                if match is not None:
                    break
        if match is not None:
            return {
                "state": "closed",
                "milestone": {
                    "title": self.issue_milestone or match["milestone"]
                },
            }
        if "git/ref/tags/" in joined:
            return {"ref": joined.rsplit("/", 1)[-1]}
        if "/releases/tags/" in joined:
            tag = joined.rsplit("/", 1)[-1]
            return {
                "html_url": (
                    "https://github.com/LicoLand/example/releases/tag/" + tag
                )
            }
        raise AssertionError(f"unexpected read call category: {arguments[:2]}")


if __name__ == "__main__":
    unittest.main()
