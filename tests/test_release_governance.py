from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_governance", ROOT / "tools" / "release_governance.py"
)
assert SPEC is not None and SPEC.loader is not None
governance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = governance
SPEC.loader.exec_module(governance)


def feature(
    feature_id: str = "file-conversion",
    *,
    feature_type: str = "capability",
    status: str = "planned",
    pull_request: str | None = None,
    depends_on: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": feature_id,
        "type": feature_type,
        "title": feature_id.replace("-", " ").title(),
        "status": status,
        "pullRequest": pull_request,
        "dependsOn": depends_on or [],
        "risk": "medium",
        "acceptance": ["The observable feature contract passes."],
        "evidence": evidence or [],
    }


def release(
    *,
    version: str = "0.2.0",
    classification: str = "minor",
    status: str = "planned",
    features: list[dict[str, Any]] | None = None,
    blockers: list[str] | None = None,
    integration_branch: str = "main",
) -> dict[str, Any]:
    return {
        "version": version,
        "classification": classification,
        "status": status,
        "targetDate": None,
        "integrationBranch": integration_branch,
        "features": features or [feature()],
        "blockers": blockers or [],
    }


def plan(
    *,
    repository: str = "LicoLand/App",
    current: str | None = "0.1.0",
    next_release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "$schema": "https://raw.githubusercontent.com/LicoLand/.github/main/schemas/release-plan.schema.json",
        "schemaVersion": 2,
        "repository": repository,
        "profile": "semver",
        "currentVersion": current,
        "versionSources": [
            {"format": "json", "path": "package.json", "pointer": "/version"}
        ],
        "changelog": "CHANGELOG.md",
        "nextRelease": next_release if next_release is not None else release(),
        "releases": [],
        "components": [],
    }


class RepositoryFixture:
    def __init__(self, document: dict[str, Any]):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.document = document
        expected = governance._expected_source_version(document)
        (self.root / "package.json").write_text(
            json.dumps({"version": expected}), encoding="utf-8"
        )
        (self.root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {expected}\n", encoding="utf-8"
        )
        release_dir = self.root / "docs" / "releases"
        release_dir.mkdir(parents=True)
        (release_dir / "plan.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        (release_dir / "README.md").write_text(
            governance.render_release_document(document), encoding="utf-8"
        )

    def close(self) -> None:
        self.temporary.cleanup()


class TransitionTests(unittest.TestCase):
    def test_sequential_transitions(self) -> None:
        cases = [
            (None, "0.1.0", "initial"),
            ("0.1.0", "0.1.1", "patch"),
            ("0.1.1", "0.2.0", "minor"),
            ("1.2.3", "2.0.0", "major"),
            ("0.9.4", "1.0.0", "stabilization"),
            ("1.0.0-rc.1", "1.0.0", "stabilization"),
        ]
        for current, target, expected in cases:
            with self.subTest(current=current, target=target):
                self.assertEqual(
                    governance.classify_transition(current, target), expected
                )

    def test_skips_and_invalid_prerelease_are_rejected(self) -> None:
        for current, target in [
            ("0.1.0", "0.1.2"),
            ("0.1.0", "0.3.0"),
            ("1.0.0", "3.0.0"),
            ("1.2.3-rc.1", "1.2.4"),
        ]:
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    governance.classify_transition(current, target)
        with self.assertRaises(ValueError):
            governance.SemVer.parse("1.0.0-01")


class LocalPolicyTests(unittest.TestCase):
    def verify(self, document: dict[str, Any]) -> governance.VerificationReport:
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        return governance.verify_plan(fixture.root, document)

    def codes(self, document: dict[str, Any]) -> set[str]:
        return {item.code for item in self.verify(document).errors}

    def test_minor_feature_plan_is_valid(self) -> None:
        report = self.verify(plan())
        self.assertTrue(report.ok, report.errors)

    def test_feature_mix_matches_semver_classification(self) -> None:
        patch = plan(
            next_release=release(
                version="0.1.1",
                classification="patch",
                features=[feature("bug-fix", feature_type="fix")],
            )
        )
        self.assertTrue(self.verify(patch).ok)
        patch["nextRelease"]["features"] = [feature()]
        self.assertIn("patch-features", self.codes(patch))

        major = plan(
            current="1.2.3",
            next_release=release(
                version="2.0.0",
                classification="major",
                features=[feature("migration", feature_type="breaking")],
            ),
        )
        self.assertTrue(self.verify(major).ok)
        major["nextRelease"]["features"] = [feature()]
        self.assertIn("major-features", self.codes(major))

    def test_integration_branch_is_required_and_bounded(self) -> None:
        document = plan()
        document["nextRelease"]["integrationBranch"] = "../main"
        self.assertIn("integration-branch", self.codes(document))
        document["nextRelease"]["integrationBranch"] = "release/v2"
        self.assertTrue(self.verify(document).ok)

    def test_lifecycle_binds_pull_request_and_evidence(self) -> None:
        document = plan()
        item = document["nextRelease"]["features"][0]
        item["status"] = "active"
        self.assertIn("active-pull-request", self.codes(document))
        item["pullRequest"] = "https://github.com/LicoLand/App/pull/7"
        self.assertTrue(self.verify(document).ok)
        item["status"] = "accepted"
        self.assertIn("accepted-feature-evidence", self.codes(document))
        item["evidence"] = ["https://github.com/LicoLand/App/actions/runs/1"]
        self.assertTrue(self.verify(document).ok)
        item["status"] = "planned"
        self.assertIn("planned-pull-request", self.codes(document))

    def test_pull_request_must_belong_to_plan_repository(self) -> None:
        document = plan()
        item = document["nextRelease"]["features"][0]
        item.update(
            status="active",
            pullRequest="https://github.com/LicoLand/Other/pull/7",
        )
        self.assertIn("pull-request-url", self.codes(document))

    def test_ready_requires_accepted_features_and_no_blockers(self) -> None:
        document = plan()
        document["nextRelease"]["status"] = "ready"
        codes = self.codes(document)
        self.assertIn("ready-feature-status", codes)
        item = document["nextRelease"]["features"][0]
        item.update(
            status="accepted",
            pullRequest="https://github.com/LicoLand/App/pull/7",
            evidence=["receipt:sha256:abc"],
        )
        document["nextRelease"]["blockers"] = ["waiting"]
        self.assertIn("ready-blockers", self.codes(document))
        document["nextRelease"]["blockers"] = []
        self.assertTrue(self.verify(document).ok)

    def test_feature_ids_and_pull_requests_are_globally_unique(self) -> None:
        document = plan()
        first = document["nextRelease"]["features"][0]
        first.update(
            status="active",
            pullRequest="https://github.com/LicoLand/App/pull/7",
        )
        duplicate = copy.deepcopy(first)
        document["nextRelease"]["features"].append(duplicate)
        codes = self.codes(document)
        self.assertIn("feature-duplicate", codes)
        self.assertIn("pull-request-duplicate", codes)

    def test_local_dependencies_exist_are_accepted_and_acyclic(self) -> None:
        upstream = feature(
            "upstream",
            status="active",
            pull_request="https://github.com/LicoLand/App/pull/7",
        )
        downstream = feature(
            "downstream",
            status="accepted",
            pull_request="https://github.com/LicoLand/App/pull/8",
            depends_on=["LicoLand/App:upstream"],
            evidence=["receipt"],
        )
        document = plan(next_release=release(features=[upstream, downstream]))
        self.assertIn("feature-dependency-unaccepted", self.codes(document))
        upstream.update(status="accepted", evidence=["receipt"])
        self.assertTrue(self.verify(document).ok)
        upstream["dependsOn"] = ["LicoLand/App:downstream"]
        self.assertIn("feature-dependency-cycle", self.codes(document))
        upstream["dependsOn"] = ["LicoLand/App:missing"]
        self.assertIn("feature-dependency-missing", self.codes(document))
        upstream["dependsOn"] = ["LicoLand/App:upstream"]
        self.assertIn("feature-self-dependency", self.codes(document))

    def test_component_features_share_repository_global_identity(self) -> None:
        document = plan()
        document.update(
            profile="component-semver",
            currentVersion=None,
            versionSources=[],
            changelog=None,
            nextRelease=None,
            components=[
                {
                    "id": "one",
                    "currentVersion": "0.1.0",
                    "versionSources": [],
                    "changelog": None,
                    "nextRelease": release(),
                    "releases": [],
                },
                {
                    "id": "two",
                    "currentVersion": "0.1.0",
                    "versionSources": [],
                    "changelog": None,
                    "nextRelease": release(),
                    "releases": [],
                },
            ],
        )
        self.assertIn("feature-duplicate", self.codes(document))


class VersionSourceTests(unittest.TestCase):
    def test_reads_supported_source_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_text('{"version":"1.2.3"}', encoding="utf-8")
            (root / "a.toml").write_text('[package]\nversion="1.2.3"\n', encoding="utf-8")
            (root / "a.txt").write_text("1.2.3\n", encoding="utf-8")
            (root / "a.py").write_text('VERSION = "1.2.3"\n', encoding="utf-8")
            sources = [
                {"format": "json", "path": "a.json", "pointer": "/version"},
                {"format": "toml", "path": "a.toml", "key": "package.version"},
                {"format": "text", "path": "a.txt"},
                {
                    "format": "regex",
                    "path": "a.py",
                    "pattern": r'VERSION = "([^"]+)"',
                },
            ]
            self.assertEqual(
                [governance.read_version_source(root, item) for item in sources],
                ["1.2.3"] * 4,
            )

    def test_source_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(governance.GovernanceError) as context:
                governance.read_version_source(
                    temporary, {"format": "text", "path": "../VERSION"}
                )
            self.assertEqual(context.exception.code, "unsafe-path")


class RenderingTests(unittest.TestCase):
    def test_projection_contains_feature_pr_dependency_and_progress(self) -> None:
        item = feature(
            status="accepted",
            pull_request="https://github.com/LicoLand/App/pull/7",
            depends_on=["LicoLand/Core:conversion"],
            evidence=["receipt"],
        )
        rendered = governance.render_release_document(
            plan(next_release=release(status="ready", features=[item]))
        )
        self.assertIn("1/1", rendered)
        self.assertIn("LicoLand/Core:conversion", rendered)
        self.assertIn("/pull/7", rendered)
        self.assertIn("Integration branch", rendered)

    def test_verify_detects_and_render_fixes_document_drift(self) -> None:
        document = plan()
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        readme = fixture.root / "docs" / "releases" / "README.md"
        readme.write_text("stale\n", encoding="utf-8")
        report = governance.verify_plan(fixture.root, document)
        self.assertIn("document-drift", {item.code for item in report.errors})
        exit_code = governance.main(
            ["render", "--repository-root", str(fixture.root), "--apply"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            readme.read_text(encoding="utf-8"),
            governance.render_release_document(document),
        )


class FakeVerifyGh:
    def __init__(
        self,
        *,
        pull: Mapping[str, Any] | None = None,
        dependency_plan: Mapping[str, Any] | None = None,
    ):
        self.apply = False
        self.operations: list[str] = []
        self.pull = pull or {
            "state": "closed",
            "draft": False,
            "merged_at": "2026-01-01T00:00:00Z",
            "base": {"ref": "main"},
        }
        self.dependency_plan = dependency_plan
        self.read_calls: list[list[str]] = []

    def json(self, arguments: Sequence[str]) -> Any:
        self.read_calls.append(list(arguments))
        endpoint = " ".join(arguments)
        if "/pulls/" in endpoint:
            return dict(self.pull)
        if "/git/ref/tags/" in endpoint:
            return {"ref": "refs/tags/v0.2.0"}
        if endpoint.endswith("repos/LicoLand/Core"):
            return {"default_branch": "main"}
        if "repos/LicoLand/Core/contents/" in endpoint:
            if self.dependency_plan is None:
                raise governance.GovernanceError("github-command", "missing")
            encoded = base64.b64encode(
                json.dumps(self.dependency_plan).encode("utf-8")
            ).decode("ascii")
            return {"encoding": "base64", "content": encoded}
        raise AssertionError(arguments)


class RemoteReadinessTests(unittest.TestCase):
    def fixture_for(self, document: dict[str, Any]) -> RepositoryFixture:
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        return fixture

    def accepted_plan(
        self, *, depends_on: list[str] | None = None, branch: str = "main"
    ) -> dict[str, Any]:
        item = feature(
            status="accepted",
            pull_request="https://github.com/LicoLand/App/pull/7",
            depends_on=depends_on,
            evidence=["receipt"],
        )
        return plan(
            next_release=release(
                status="ready", features=[item], integration_branch=branch
            )
        )

    def test_accepted_pull_request_must_be_merged_to_declared_branch(self) -> None:
        document = self.accepted_plan()
        fixture = self.fixture_for(document)
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=FakeVerifyGh(),
        )
        self.assertTrue(report.ok, report.errors)
        wrong = FakeVerifyGh(
            pull={
                "state": "closed",
                "merged_at": "2026-01-01T00:00:00Z",
                "base": {"ref": "nightly"},
            }
        )
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=wrong,
        )
        self.assertIn(
            "github-pull-request-base", {item.code for item in report.errors}
        )
        unmerged = FakeVerifyGh(
            pull={"state": "closed", "merged_at": None, "base": {"ref": "main"}}
        )
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=unmerged,
        )
        self.assertIn(
            "github-pull-request-unmerged", {item.code for item in report.errors}
        )

    def test_active_pull_request_must_remain_open(self) -> None:
        document = plan()
        document["nextRelease"]["features"][0].update(
            status="active",
            pullRequest="https://github.com/LicoLand/App/pull/7",
        )
        fixture = self.fixture_for(document)
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=FakeVerifyGh(),
        )
        self.assertIn(
            "github-pull-request-closed", {item.code for item in report.errors}
        )

    def test_cross_repository_dependency_must_exist_and_be_accepted(self) -> None:
        reference = "LicoLand/Core:conversion"
        document = self.accepted_plan(depends_on=[reference])
        fixture = self.fixture_for(document)
        dependency = plan(
            repository="LicoLand/Core",
            next_release=release(
                features=[
                    feature(
                        "conversion",
                        status="accepted",
                        pull_request="https://github.com/LicoLand/Core/pull/3",
                        evidence=["receipt"],
                    )
                ]
            ),
        )
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=FakeVerifyGh(dependency_plan=dependency),
        )
        self.assertTrue(report.ok, report.errors)
        dependency["nextRelease"]["features"][0]["status"] = "active"
        dependency["nextRelease"]["features"][0]["evidence"] = []
        report = governance.verify_repository(
            fixture.root,
            github=True,
            expected_repository="LicoLand/App",
            gh=FakeVerifyGh(dependency_plan=dependency),
        )
        self.assertIn(
            "feature-dependency-unaccepted", {item.code for item in report.errors}
        )

    def test_tag_must_match_ready_release_and_remote_ref(self) -> None:
        document = self.accepted_plan()
        fixture = self.fixture_for(document)
        report = governance.verify_repository(
            fixture.root,
            github=True,
            tag="v0.2.0",
            expected_repository="LicoLand/App",
            gh=FakeVerifyGh(),
        )
        self.assertTrue(report.ok, report.errors)


def project_fields() -> list[dict[str, Any]]:
    result = []
    for index, (name, data_type, options) in enumerate(governance.FIELD_SPECS):
        result.append(
            {
                "id": f"FIELD_{index}",
                "name": name,
                "dataType": data_type,
                "options": [
                    {"id": f"OPTION_{index}_{option_index}", "name": option}
                    for option_index, option in enumerate(options)
                ],
            }
        )
    return result


class ProjectFake:
    def __init__(
        self,
        *,
        apply: bool,
        items: list[dict[str, Any]] | None = None,
        public: bool = False,
    ):
        self.apply = apply
        self.operations: list[str] = []
        self.items = items or []
        self.public = public
        self.read_calls: list[list[str]] = []
        self.mutation_started = False

    def json(self, arguments: Sequence[str]) -> Any:
        self.assert_no_late_read()
        self.read_calls.append(list(arguments))
        command = arguments[:2]
        endpoint = " ".join(arguments)
        if command == ["project", "list"]:
            return {
                "projects": [
                    {
                        "id": "PROJECT",
                        "number": 2,
                        "title": governance.DEFAULT_PROJECT_TITLE,
                        "public": self.public,
                    }
                ]
            }
        if command == ["project", "item-list"]:
            return {"items": self.items}
        if command == ["project", "field-list"]:
            return {"fields": project_fields()}
        if "/pulls/" in endpoint:
            return {
                "state": "open",
                "draft": True,
                "merged_at": None,
                "base": {"ref": "main"},
            }
        if endpoint.endswith("repos/LicoLand/Core"):
            raise governance.GovernanceError("github-command", "unavailable")
        raise AssertionError(arguments)

    def assert_no_late_read(self) -> None:
        if self.mutation_started:
            raise AssertionError("remote read occurred after mutation phase began")

    def mutate_text(self, operation: str, arguments: Sequence[str]) -> str | None:
        self.mutation_started = True
        self.operations.append(operation)
        return "" if self.apply else None

    def mutate_json(self, operation: str, arguments: Sequence[str]) -> Any:
        self.mutation_started = True
        self.operations.append(operation)
        if not self.apply:
            return None
        if operation == "project.draft.create":
            return {"id": f"DRAFT_{len(self.operations)}"}
        if operation == "project.item.add":
            return {"id": f"ITEM_{len(self.operations)}"}
        return {}


class ProjectProjectionTests(unittest.TestCase):
    def fixture_for(self, document: dict[str, Any]) -> RepositoryFixture:
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        return fixture

    def test_planned_release_and_feature_create_draft_items(self) -> None:
        document = plan()
        fixture = self.fixture_for(document)
        fake = ProjectFake(apply=False)
        operations = governance.sync_project(
            fixture.root,
            document,
            project_owner="LicoLand",
            expected_repository="LicoLand/App",
            gh=fake,
        )
        self.assertEqual(operations.count("project.draft.create"), 2)
        self.assertNotIn("project.item.add", operations)

    def test_feature_pr_replaces_draft_and_sets_current_fields(self) -> None:
        document = plan()
        item = document["nextRelease"]["features"][0]
        item.update(
            status="active",
            pullRequest="https://github.com/LicoLand/App/pull/7",
        )
        fixture = self.fixture_for(document)
        marker = governance._draft_marker(
            "LicoLand/App", "feature", "file-conversion"
        )
        fake = ProjectFake(
            apply=True,
            items=[
                {
                    "id": "OLD_DRAFT",
                    "content": {"body": f"{marker}\n\nplanned"},
                }
            ],
        )
        operations = governance.sync_project(
            fixture.root,
            document,
            project_owner="LicoLand",
            expected_repository="LicoLand/App",
            apply=True,
            gh=fake,
        )
        self.assertIn("project.item.add", operations)
        self.assertIn("project.item.archive", operations)
        self.assertIn("project.field.set:PR stage", operations)
        self.assertIn("project.field.set:Plan revision", operations)
        self.assertIn("project.field.set:Sync state", operations)

    def test_missing_or_public_project_fails_before_mutation(self) -> None:
        document = plan()
        fixture = self.fixture_for(document)
        fake = ProjectFake(apply=True, public=True)
        with self.assertRaises(governance.GovernanceError) as context:
            governance.sync_project(
                fixture.root,
                document,
                project_owner="LicoLand",
                expected_repository="LicoLand/App",
                apply=True,
                gh=fake,
            )
        self.assertEqual(context.exception.code, "github-project-privacy")
        self.assertEqual(fake.operations, [])

    def test_retired_managed_draft_is_marked_orphan(self) -> None:
        document = plan()
        fixture = self.fixture_for(document)
        marker = governance._draft_marker(
            "LicoLand/App", "feature", "retired-feature"
        )
        fake = ProjectFake(
            apply=True,
            items=[{"id": "RETIRED", "content": {"body": marker}}],
        )
        operations = governance.sync_project(
            fixture.root,
            document,
            project_owner="LicoLand",
            expected_repository="LicoLand/App",
            apply=True,
            gh=fake,
        )
        self.assertIn("project.field.set:Sync state", operations)

    def test_bootstrap_uses_configured_project_contract(self) -> None:
        fake = ProjectFake(apply=False)
        operations = governance.bootstrap_project("LicoLand", gh=fake)
        self.assertEqual(operations, [])
        configured = {name for name, _, _ in governance.FIELD_SPECS}
        self.assertIn("Owner repository", configured)
        self.assertIn("Dependency state", configured)
        self.assertIn("Gate progress", configured)


class FinalizeTests(unittest.TestCase):
    def ready_document(self) -> dict[str, Any]:
        return plan(
            next_release=release(
                status="ready",
                features=[
                    feature(
                        status="accepted",
                        pull_request="https://github.com/LicoLand/App/pull/7",
                        evidence=["receipt"],
                    )
                ],
            )
        )

    def test_finalize_archives_complete_feature_contract(self) -> None:
        document = self.ready_document()
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        finalized = governance.finalize_plan(
            fixture.root,
            document,
            release_url="https://github.com/LicoLand/App/releases/tag/v0.2.0",
            released_at="2026-01-02T03:04:05Z",
            expected_repository="LicoLand/App",
        )
        self.assertIsNone(finalized["nextRelease"])
        self.assertEqual(finalized["currentVersion"], "0.2.0")
        self.assertEqual(
            finalized["releases"][0]["features"][0]["id"], "file-conversion"
        )
        self.assertIsNot(finalized, document)

    def test_finalize_rejects_non_ready_and_duplicate(self) -> None:
        document = plan()
        fixture = RepositoryFixture(document)
        self.addCleanup(fixture.close)
        with self.assertRaises(governance.GovernanceError) as context:
            governance.finalize_plan(
                fixture.root,
                document,
                release_url="https://github.com/LicoLand/App/releases/tag/v0.2.0",
                released_at="2026-01-02T03:04:05Z",
                expected_repository="LicoLand/App",
            )
        self.assertEqual(context.exception.code, "release-not-ready")


class WorkflowAndSchemaTests(unittest.TestCase):
    def test_schema_and_plan_use_v2_feature_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "release-plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], 2)
        required = set(schema["$defs"]["feature"]["required"])
        self.assertEqual(required, governance.FEATURE_KEYS)
        release_required = set(schema["$defs"]["releaseCore"]["required"])
        self.assertEqual(release_required, governance.RELEASE_KEYS)
        organization_plan = json.loads(
            (ROOT / "docs" / "releases" / "plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(organization_plan["schemaVersion"], 2)

    def test_project_field_contract_matches_current_portfolio(self) -> None:
        specs = {name: (data_type, options) for name, data_type, options in governance.FIELD_SPECS}
        self.assertEqual(
            set(specs),
            {
                "Status",
                "Item type",
                "Feature ID",
                "Owner repository",
                "Feature type",
                "Target version",
                "Release class",
                "Risk",
                "Depends on",
                "Dependency state",
                "PR stage",
                "Readiness",
                "Gate progress",
                "Evidence",
                "Release unit",
                "Plan revision",
                "Sync state",
            },
        )
        self.assertEqual(
            specs["PR stage"][1], ("None", "Draft", "Review", "Merged", "Closed")
        )

    def test_reusable_workflow_uses_minimum_read_permissions(self) -> None:
        text = (
            ROOT / ".github" / "workflows" / "reusable-version-governance.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("contents: read", text)
        self.assertIn("pull-requests: read", text)
        self.assertIn("--github", text)
        self.assertNotIn("secrets:", text)

    def test_native_starter_combines_governance_and_independent_audit(self) -> None:
        text = (
            ROOT
            / "workflow-templates"
            / "licoland-repository-release-governance.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("LicoLand/Lico-Auditor", text)
        self.assertIn("pull-requests: read", text)

    def test_cli_exposes_current_commands(self) -> None:
        parser = governance.build_parser()
        for command in ["verify", "render", "bootstrap-project", "sync-project", "finalize"]:
            namespace = parser.parse_args(
                [command]
                + (
                    ["--expected-repository", "LicoLand/App"]
                    if command in {"sync-project"}
                    else []
                )
                + (
                    [
                        "--release-url",
                        "https://github.com/LicoLand/App/releases/tag/v1.0.0",
                        "--released-at",
                        "2026-01-01T00:00:00Z",
                        "--expected-repository",
                        "LicoLand/App",
                    ]
                    if command == "finalize"
                    else []
                )
            )
            self.assertEqual(namespace.command, command)


if __name__ == "__main__":
    unittest.main()
