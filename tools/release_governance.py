#!/usr/bin/env python3
"""Verify repository-owned release plans and project them into GitHub Projects.

``docs/releases/plan.json`` is the release authority. Draft pull requests carry
implementation, while the organization Project is a disposable projection.
Issues and milestones are deliberately outside this contract.
"""

from __future__ import annotations

import argparse
import base64
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


DEFAULT_PLAN_PATH = "docs/releases/plan.json"
DOCUMENT_PATH = "docs/releases/README.md"
DEFAULT_PROJECT_TITLE = "LicoLand Release Portfolio"
PROFILES = {
    "governance",
    "continuous-site",
    "inactive",
    "semver",
    "component-semver",
}
CLASSIFICATIONS = {"initial", "stabilization", "major", "minor", "patch"}
RELEASE_STATUSES = {"planned", "active", "blocked", "ready"}
FEATURE_TYPES = {"capability", "breaking", "fix"}
FEATURE_STATUSES = {"planned", "active", "blocked", "accepted"}
RISK_LEVELS = {"low", "medium", "high"}
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
FEATURE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FEATURE_REF_RE = re.compile(
    r"^(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):"
    r"(?P<feature>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
COMPONENT_ID_RE = FEATURE_ID_RE
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DEPENDENCY_REPOSITORIES = 64
MAX_DEPENDENCY_FEATURES = 4096


class GovernanceError(Exception):
    """An operational error safe to present without raw process output."""

    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclasses.dataclass(frozen=True)
class Finding:
    code: str
    message: str
    path: str | None = None
    severity: str = "error"


@dataclasses.dataclass
class VerificationReport:
    findings: list[Finding] = dataclasses.field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str, path: str | None = None) -> None:
        self.findings.append(Finding(code, message, path))

    def warning(self, code: str, message: str, path: str | None = None) -> None:
        self.findings.append(Finding(code, message, path, "warning"))

    def extend(self, other: "VerificationReport") -> None:
        self.findings.extend(other.findings)


@dataclasses.dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @property
    def stable(self) -> bool:
        return not self.prerelease

    @classmethod
    def parse(cls, value: Any) -> "SemVer":
        if not isinstance(value, str):
            raise TypeError("version must be text")
        match = SEMVER_RE.fullmatch(value)
        if match is None:
            raise ValueError("invalid SemVer")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        for item in prerelease:
            if item.isdigit() and len(item) > 1 and item.startswith("0"):
                raise ValueError("numeric prerelease identifiers cannot have leading zeroes")
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
            build,
        )


def classify_transition(current: str | None, target: str) -> str:
    candidate = SemVer.parse(target)
    if not candidate.stable or candidate.build:
        raise ValueError("target must be a stable SemVer")
    if current is None:
        if candidate == SemVer(0, 1, 0):
            return "initial"
        raise ValueError("an initial release must be 0.1.0")
    baseline = SemVer.parse(current)
    if baseline.prerelease:
        if (
            baseline.major,
            baseline.minor,
            baseline.patch,
        ) == (candidate.major, candidate.minor, candidate.patch):
            return "stabilization"
        raise ValueError("a prerelease can only stabilize to its exact core")
    if baseline.build:
        baseline = dataclasses.replace(baseline, build=())
    if baseline.major == 0 and candidate == SemVer(1, 0, 0):
        return "stabilization"
    if candidate == SemVer(baseline.major, baseline.minor, baseline.patch + 1):
        return "patch"
    if candidate == SemVer(baseline.major, baseline.minor + 1, 0):
        return "minor"
    if candidate == SemVer(baseline.major + 1, 0, 0):
        return "major"
    raise ValueError("release transition must be sequential")


def _display_path(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "<outside-repository>"


def _safe_path(root: Path, value: str | Path) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise GovernanceError("unsafe-path", "repository path must be relative")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise GovernanceError("unsafe-path", "repository path escapes repository") from exc
    return candidate


def _read_text(root: Path, value: str | Path) -> str:
    path = _safe_path(root, value)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise GovernanceError(
            "source-missing", "required repository source is unavailable", _display_path(root, path)
        ) from exc
    if size > MAX_SOURCE_BYTES:
        raise GovernanceError(
            "source-size", "repository source exceeds the verification limit", _display_path(root, path)
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GovernanceError(
            "source-read", "repository source could not be read as UTF-8", _display_path(root, path)
        ) from exc


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, Mapping):
            current = current[token]
        else:
            raise KeyError(token)
    return current


def _toml_key(document: Any, key: str) -> Any:
    current = document
    for part in key.split("."):
        if not isinstance(current, Mapping):
            raise KeyError(part)
        current = current[part]
    return current


def read_version_source(repository_root: Path | str, source: Mapping[str, Any]) -> str:
    root = Path(repository_root)
    text = _read_text(root, str(source.get("path", "")))
    source_format = source.get("format")
    try:
        if source_format == "json":
            value = _json_pointer(json.loads(text), str(source["pointer"]))
        elif source_format == "toml":
            value = _toml_key(tomllib.loads(text), str(source["key"]))
        elif source_format == "text":
            value = text.strip()
        elif source_format == "regex":
            match = re.search(str(source["pattern"]), text)
            if match is None:
                raise ValueError("pattern did not match")
            value = match.group(1) if match.lastindex else match.group(0)
        else:
            raise ValueError("unsupported source format")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GovernanceError(
            "version-source", "version source could not be evaluated", str(source.get("path", ""))
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(
            "version-source", "version source did not produce a version string", str(source.get("path", ""))
        )
    return value.strip()


def load_plan(
    repository_root: Path | str, plan_path: str | Path = DEFAULT_PLAN_PATH
) -> tuple[dict[str, Any], Path]:
    root = Path(repository_root)
    path = _safe_path(root, plan_path)
    display = _display_path(root, path)
    try:
        text = _read_text(root, plan_path)
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GovernanceError("invalid-plan", "release plan is not valid JSON", display) from exc
    if not isinstance(loaded, dict):
        raise GovernanceError("invalid-plan", "release plan root must be an object", display)
    return loaded, path


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not any(character.isspace() for character in value)
    )


def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _exact_keys(
    report: VerificationReport,
    value: Mapping[str, Any],
    required: set[str],
    allowed: set[str],
    path: str,
) -> None:
    for name in sorted(required.difference(value)):
        report.error("schema-required", f"required field '{name}' is missing", path)
    for name in sorted(set(value).difference(allowed)):
        report.error("schema-property", f"unsupported field '{name}' is present", path)


ROOT_KEYS = {
    "$schema",
    "schemaVersion",
    "repository",
    "profile",
    "currentVersion",
    "versionSources",
    "changelog",
    "nextRelease",
    "releases",
    "components",
}
RELEASE_KEYS = {
    "version",
    "classification",
    "status",
    "targetDate",
    "integrationBranch",
    "features",
    "blockers",
}
RECORD_KEYS = RELEASE_KEYS | {"releasedAt", "releaseUrl"}
FEATURE_KEYS = {
    "id",
    "type",
    "title",
    "status",
    "pullRequest",
    "dependsOn",
    "risk",
    "acceptance",
    "evidence",
}
COMPONENT_KEYS = {
    "id",
    "currentVersion",
    "versionSources",
    "changelog",
    "nextRelease",
    "releases",
}


def _validate_source(report: VerificationReport, source: Any, path: str) -> None:
    if not isinstance(source, Mapping):
        report.error("schema-type", "version source must be an object", path)
        return
    contracts = {
        "json": {"format", "path", "pointer"},
        "toml": {"format", "path", "key"},
        "text": {"format", "path"},
        "regex": {"format", "path", "pattern"},
    }
    required = contracts.get(source.get("format"), {"format", "path"})
    _exact_keys(report, source, required, required, path)
    if source.get("format") not in contracts:
        report.error("schema-enum", "version-source format is unsupported", path)
    if not _nonempty(source.get("path")):
        report.error("schema-type", "version-source path must be non-empty", path)
    if source.get("format") == "json" and not isinstance(source.get("pointer"), str):
        report.error("source-selector", "JSON version source requires pointer", path)
    if source.get("format") == "toml" and not _nonempty(source.get("key")):
        report.error("source-selector", "TOML version source requires key", path)
    if source.get("format") == "regex" and not _nonempty(source.get("pattern")):
        report.error("source-selector", "regex version source requires pattern", path)


def _pull_number(url: Any, repository: str) -> int | None:
    if not _https_url(url):
        return None
    parsed = urllib.parse.urlparse(str(url))
    if parsed.netloc.lower() != "github.com":
        return None
    expected = f"/{repository}/pull/"
    if not parsed.path.startswith(expected):
        return None
    suffix = parsed.path[len(expected) :].strip("/")
    if not suffix.isdigit() or parsed.query or parsed.fragment:
        return None
    return int(suffix)


def _validate_feature(
    report: VerificationReport,
    feature: Any,
    repository: str,
    path: str,
) -> None:
    if not isinstance(feature, Mapping):
        report.error("schema-type", "feature must be an object", path)
        return
    _exact_keys(report, feature, FEATURE_KEYS, FEATURE_KEYS, path)
    feature_id = feature.get("id")
    if (
        not isinstance(feature_id, str)
        or len(feature_id) > 64
        or FEATURE_ID_RE.fullmatch(feature_id) is None
    ):
        report.error("feature-id", "feature id must be a lowercase slug of at most 64 characters", path)
    if feature.get("type") not in FEATURE_TYPES:
        report.error("schema-enum", "feature type is invalid", path)
    title = feature.get("title")
    if not _nonempty(title) or len(str(title)) > 160:
        report.error("feature-title", "feature title must contain 1-160 characters", path)
    status = feature.get("status")
    if status not in FEATURE_STATUSES:
        report.error("schema-enum", "feature status is invalid", path)
    pull_request = feature.get("pullRequest")
    if pull_request is not None and _pull_number(pull_request, repository) is None:
        report.error(
            "pull-request-url",
            "feature pullRequest must be an owning-repository GitHub pull request URL or null",
            path,
        )
    if status == "planned" and pull_request is not None:
        report.error("planned-pull-request", "planned feature cannot already have a pull request", path)
    if status in {"active", "accepted"} and pull_request is None:
        report.error(
            f"{status}-pull-request",
            f"{status} feature requires its implementation pull request",
            path,
        )
    if feature.get("risk") not in RISK_LEVELS:
        report.error("schema-enum", "feature risk is invalid", path)
    acceptance = feature.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance or not all(_nonempty(item) for item in acceptance):
        report.error(
            "feature-acceptance",
            "feature acceptance must contain at least one non-empty statement",
            path,
        )
    evidence = feature.get("evidence")
    if not isinstance(evidence, list) or not all(_nonempty(item) for item in evidence):
        report.error("feature-evidence", "feature evidence must be a string list", path)
    if status == "accepted" and not evidence:
        report.error("accepted-feature-evidence", "accepted feature requires reviewed evidence", path)
    dependencies = feature.get("dependsOn")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and len(item) <= 160 and FEATURE_REF_RE.fullmatch(item)
        for item in dependencies
    ):
        report.error(
            "feature-dependencies",
            "dependsOn must contain canonical Owner/Repository:feature-id references",
            path,
        )
    elif len(set(dependencies)) != len(dependencies):
        report.error("feature-dependency-duplicate", "dependsOn contains a duplicate reference", path)


def _validate_release(
    report: VerificationReport,
    release: Any,
    repository: str,
    path: str,
    *,
    record: bool,
) -> None:
    if not isinstance(release, Mapping):
        report.error("schema-type", "release must be an object", path)
        return
    allowed = RECORD_KEYS if record else RELEASE_KEYS
    _exact_keys(report, release, allowed, allowed, path)
    try:
        parsed = SemVer.parse(release.get("version"))
        if not parsed.stable or parsed.build:
            raise ValueError
    except (TypeError, ValueError):
        report.error("release-version", "release version must be stable SemVer", path)
    if release.get("classification") not in CLASSIFICATIONS:
        report.error("schema-enum", "release classification is invalid", path)
    if release.get("status") not in RELEASE_STATUSES:
        report.error("schema-enum", "release status is invalid", path)
    target_date = release.get("targetDate")
    if target_date is not None and not _is_date(target_date):
        report.error("target-date", "target date must use YYYY-MM-DD or null", path)
    integration_branch = release.get("integrationBranch")
    if (
        not isinstance(integration_branch, str)
        or not integration_branch
        or len(integration_branch) > 255
        or integration_branch.startswith("/")
        or integration_branch.endswith("/")
        or "//" in integration_branch
        or ".." in integration_branch
        or re.fullmatch(r"[A-Za-z0-9._/-]+", integration_branch) is None
    ):
        report.error("integration-branch", "integrationBranch is invalid", path)
    features = release.get("features")
    if not isinstance(features, list) or not features:
        report.error("release-features", "release must contain at least one feature", path)
    else:
        for index, feature in enumerate(features):
            _validate_feature(report, feature, repository, f"{path}.features[{index}]")
    blockers = release.get("blockers")
    if not isinstance(blockers, list) or not all(_nonempty(item) for item in blockers):
        report.error("release-blockers", "release blockers must be a string list", path)
    if release.get("status") == "ready":
        if blockers:
            report.error("ready-blockers", "ready release cannot contain blockers", path)
        if isinstance(features, list):
            for index, feature in enumerate(features):
                if isinstance(feature, Mapping) and feature.get("status") != "accepted":
                    report.error(
                        "ready-feature-status",
                        "every ready-release feature must be accepted",
                        f"{path}.features[{index}]",
                    )
    if record:
        if not _is_datetime(release.get("releasedAt")):
            report.error("released-at", "releasedAt must be an RFC 3339 date-time", path)
        if not _https_url(release.get("releaseUrl")):
            report.error("release-url", "releaseUrl must be an HTTPS URL", path)


def _validate_unit(
    report: VerificationReport,
    unit: Mapping[str, Any],
    repository: str,
    path: str,
) -> None:
    current = unit.get("currentVersion")
    if current is not None:
        try:
            SemVer.parse(current)
        except (TypeError, ValueError):
            report.error("current-version", "currentVersion must be SemVer or null", path)
    sources = unit.get("versionSources")
    if not isinstance(sources, list):
        report.error("schema-type", "versionSources must be an array", path)
    else:
        for index, source in enumerate(sources):
            _validate_source(report, source, f"{path}.versionSources[{index}]")
    changelog = unit.get("changelog")
    if changelog is not None and not _nonempty(changelog):
        report.error("changelog-path", "changelog must be a path or null", path)
    next_release = unit.get("nextRelease")
    if next_release is not None:
        _validate_release(report, next_release, repository, f"{path}.nextRelease", record=False)
    releases = unit.get("releases")
    if not isinstance(releases, list):
        report.error("schema-type", "releases must be an array", path)
    else:
        seen: set[str] = set()
        for index, release in enumerate(releases):
            release_path = f"{path}.releases[{index}]"
            _validate_release(report, release, repository, release_path, record=True)
            if isinstance(release, Mapping) and isinstance(release.get("version"), str):
                if release["version"] in seen:
                    report.error("release-duplicate", "release version is archived more than once", release_path)
                seen.add(release["version"])


def _units(plan: Mapping[str, Any]) -> list[tuple[str | None, Mapping[str, Any], str]]:
    result: list[tuple[str | None, Mapping[str, Any], str]] = [(None, plan, "plan")]
    components = plan.get("components")
    if isinstance(components, list):
        for index, component in enumerate(components):
            if isinstance(component, Mapping):
                result.append((component.get("id"), component, f"plan.components[{index}]"))
    return result


def _release_entries(
    plan: Mapping[str, Any],
) -> Iterable[tuple[str | None, Mapping[str, Any], str, bool]]:
    for unit_id, unit, unit_path in _units(plan):
        next_release = unit.get("nextRelease")
        if isinstance(next_release, Mapping):
            yield unit_id, next_release, f"{unit_path}.nextRelease", False
        releases = unit.get("releases")
        if isinstance(releases, list):
            for index, release in enumerate(releases):
                if isinstance(release, Mapping):
                    yield unit_id, release, f"{unit_path}.releases[{index}]", True


def _feature_entries(
    plan: Mapping[str, Any],
) -> Iterable[tuple[str, Mapping[str, Any], str, Mapping[str, Any], bool]]:
    for _, release, release_path, archived in _release_entries(plan):
        features = release.get("features")
        if not isinstance(features, list):
            continue
        for index, feature in enumerate(features):
            if isinstance(feature, Mapping) and isinstance(feature.get("id"), str):
                yield (
                    feature["id"],
                    feature,
                    f"{release_path}.features[{index}]",
                    release,
                    archived,
                )


def _validate_profile(report: VerificationReport, plan: Mapping[str, Any]) -> None:
    profile = plan.get("profile")
    components = plan.get("components")
    if profile == "semver":
        if components:
            report.error("profile-components", "semver profile cannot declare components", "plan")
    elif profile == "component-semver":
        if not isinstance(components, list) or not components:
            report.error("profile-components", "component-semver requires components", "plan")
        for name in ("currentVersion", "changelog", "nextRelease"):
            if plan.get(name) is not None:
                report.error("profile-root-version", f"component-semver requires root {name} null", "plan")
        for name in ("versionSources", "releases"):
            if plan.get(name) != []:
                report.error("profile-root-version", f"component-semver requires root {name} empty", "plan")
    elif profile in {"governance", "continuous-site", "inactive"}:
        for name in ("currentVersion", "changelog", "nextRelease"):
            if plan.get(name) is not None:
                report.error("profile-version", f"{profile} cannot declare {name}", "plan")
        for name in ("versionSources", "releases", "components"):
            if plan.get(name) != []:
                report.error("profile-version", f"{profile} requires {name} empty", "plan")


def _validate_feature_mix(
    report: VerificationReport, release: Mapping[str, Any], classification: str, path: str
) -> None:
    features = release.get("features")
    if not isinstance(features, list):
        return
    types = [item.get("type") for item in features if isinstance(item, Mapping)]
    if classification == "patch" and (not types or any(item != "fix" for item in types)):
        report.error("patch-features", "patch releases require fixes only", path)
    elif classification == "minor" and ("capability" not in types or "breaking" in types):
        report.error("minor-features", "minor releases require a capability and prohibit breaking features", path)
    elif classification == "major" and "breaking" not in types:
        report.error("major-features", "major releases require a breaking feature", path)
    elif classification in {"initial", "stabilization"} and "capability" not in types:
        report.error(
            f"{classification}-features",
            f"{classification} releases require a capability feature",
            path,
        )


def _validate_release_contracts(report: VerificationReport, plan: Mapping[str, Any]) -> None:
    for _, unit, unit_path in _units(plan):
        current = unit.get("currentVersion")
        next_release = unit.get("nextRelease")
        if isinstance(next_release, Mapping) and isinstance(next_release.get("version"), str):
            try:
                inferred = classify_transition(current, next_release["version"])
            except (TypeError, ValueError):
                report.error("version-transition", "next release transition is invalid", f"{unit_path}.nextRelease")
            else:
                if next_release.get("classification") != inferred:
                    report.error(
                        "classification",
                        "declared classification does not match the version transition",
                        f"{unit_path}.nextRelease",
                    )
                _validate_feature_mix(report, next_release, inferred, f"{unit_path}.nextRelease")


def _validate_feature_graph(report: VerificationReport, plan: Mapping[str, Any]) -> None:
    repository = plan.get("repository")
    if not isinstance(repository, str):
        return
    entries = list(_feature_entries(plan))
    by_id: dict[str, tuple[Mapping[str, Any], str]] = {}
    pull_requests: dict[str, str] = {}
    for feature_id, feature, path, _, _ in entries:
        if feature_id in by_id:
            report.error("feature-duplicate", "feature id must be globally unique in one repository plan", path)
        else:
            by_id[feature_id] = (feature, path)
        pull_request = feature.get("pullRequest")
        if isinstance(pull_request, str):
            if pull_request in pull_requests:
                report.error(
                    "pull-request-duplicate",
                    "one pull request cannot implement multiple features",
                    path,
                )
            else:
                pull_requests[pull_request] = path

    adjacency: dict[str, list[str]] = {feature_id: [] for feature_id in by_id}
    for feature_id, (feature, path) in by_id.items():
        own_reference = f"{repository}:{feature_id}"
        dependencies = feature.get("dependsOn")
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if dependency == own_reference:
                report.error("feature-self-dependency", "feature cannot depend on itself", path)
                continue
            match = FEATURE_REF_RE.fullmatch(dependency) if isinstance(dependency, str) else None
            if match is None or match.group("repository") != repository:
                continue
            target = match.group("feature")
            if target not in by_id:
                report.error("feature-dependency-missing", "local feature dependency does not exist", path)
                continue
            adjacency[feature_id].append(target)
            if feature.get("status") == "accepted" and by_id[target][0].get("status") != "accepted":
                report.error(
                    "feature-dependency-unaccepted",
                    "accepted feature depends on a local feature that is not accepted",
                    path,
                )

    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        for target in adjacency[node]:
            if state.get(target) == 1:
                report.error("feature-dependency-cycle", "feature dependency graph contains a cycle", by_id[node][1])
            elif state.get(target, 0) == 0:
                visit(target)
        state[node] = 2

    for feature_id in adjacency:
        if state.get(feature_id, 0) == 0:
            visit(feature_id)


def _expected_source_version(unit: Mapping[str, Any]) -> str | None:
    release = unit.get("nextRelease")
    if isinstance(release, Mapping) and release.get("status") == "ready":
        return release.get("version") if isinstance(release.get("version"), str) else None
    current = unit.get("currentVersion")
    return current if isinstance(current, str) else None


def _validate_sources_and_changelog(
    report: VerificationReport, root: Path, plan: Mapping[str, Any]
) -> None:
    for _, unit, unit_path in _units(plan):
        expected = _expected_source_version(unit)
        sources = unit.get("versionSources")
        if isinstance(sources, list):
            for index, source in enumerate(sources):
                if not isinstance(source, Mapping):
                    continue
                try:
                    actual = read_version_source(root, source)
                except GovernanceError as exc:
                    report.error(exc.code, exc.message, exc.path or f"{unit_path}.versionSources[{index}]")
                    continue
                if expected is not None and actual != expected:
                    report.error(
                        "version-source-drift",
                        "version source does not match the required release version",
                        f"{unit_path}.versionSources[{index}]",
                    )
        changelog = unit.get("changelog")
        release = unit.get("nextRelease")
        if (
            isinstance(changelog, str)
            and isinstance(release, Mapping)
            and release.get("status") == "ready"
            and isinstance(release.get("version"), str)
        ):
            try:
                text = _read_text(root, changelog)
            except GovernanceError as exc:
                report.error(exc.code, exc.message, exc.path)
            else:
                if release["version"] not in text:
                    report.error("changelog-version", "changelog does not name the ready version", changelog)


def _validate_shape(plan: Mapping[str, Any]) -> VerificationReport:
    report = VerificationReport()
    _exact_keys(report, plan, ROOT_KEYS, ROOT_KEYS, "plan")
    if not _nonempty(plan.get("$schema")):
        report.error("schema-uri", "$schema must be a non-empty URI", "plan")
    if plan.get("schemaVersion") != 2:
        report.error("schema-version", "schemaVersion must equal 2", "plan")
    repository = plan.get("repository")
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        report.error("repository", "repository must use owner/name", "plan")
        repository = ""
    if plan.get("profile") not in PROFILES:
        report.error("profile", "release profile is unsupported", "plan")
    _validate_unit(report, plan, repository, "plan")
    components = plan.get("components")
    if not isinstance(components, list):
        report.error("schema-type", "components must be an array", "plan")
    else:
        seen: set[str] = set()
        for index, component in enumerate(components):
            path = f"plan.components[{index}]"
            if not isinstance(component, Mapping):
                report.error("schema-type", "component must be an object", path)
                continue
            _exact_keys(report, component, COMPONENT_KEYS, COMPONENT_KEYS, path)
            component_id = component.get("id")
            if not isinstance(component_id, str) or COMPONENT_ID_RE.fullmatch(component_id) is None:
                report.error("component-id", "component id is invalid", path)
            elif component_id in seen:
                report.error("component-duplicate", "component id is duplicated", path)
            else:
                seen.add(component_id)
            _validate_unit(report, component, repository, path)
    return report


def _feature_progress(release: Mapping[str, Any]) -> tuple[int, int, int]:
    features = release.get("features")
    if not isinstance(features, list):
        return 0, 0, 0
    total = len(features)
    accepted = sum(
        1 for feature in features if isinstance(feature, Mapping) and feature.get("status") == "accepted"
    )
    percent = round(accepted * 100 / total) if total else 0
    return accepted, total, percent


def _release_rows(release: Mapping[str, Any]) -> list[str]:
    rows: list[str] = []
    features = release.get("features")
    if not isinstance(features, list):
        return rows
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        pull_request = feature.get("pullRequest")
        pull_text = f"[PR]({pull_request})" if _https_url(pull_request) else "—"
        dependencies = feature.get("dependsOn")
        dependency_text = "<br>".join(str(item) for item in dependencies) if dependencies else "—"
        evidence = feature.get("evidence")
        evidence_text = "<br>".join(str(item) for item in evidence) if evidence else "—"
        cells = [
            str(feature.get("id", "—")),
            str(feature.get("type", "—")),
            str(feature.get("title", "—")).replace("|", r"\|").replace("\n", " "),
            str(feature.get("status", "—")),
            str(feature.get("risk", "—")),
            pull_text,
            dependency_text,
            evidence_text,
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def _render_release(lines: list[str], release: Any, heading: str) -> None:
    lines.extend([f"## {heading}", ""])
    if not isinstance(release, Mapping):
        lines.extend(["No release is currently planned.", ""])
        return
    accepted, total, percent = _feature_progress(release)
    lines.extend(
        [
            f"- Version: `{release.get('version', '—')}`",
            f"- Classification: `{release.get('classification', '—')}`",
            f"- Status: `{release.get('status', '—')}`",
            f"- Target date: `{release.get('targetDate') or 'not set'}`",
            f"- Integration branch: `{release.get('integrationBranch', '—')}`",
            f"- Progress: `{accepted}/{total}` accepted (`{percent}%`)",
            "",
            "| ID | Type | Feature | Status | Risk | Pull request | Depends on | Evidence |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(_release_rows(release))
    blockers = release.get("blockers")
    if isinstance(blockers, list) and blockers:
        lines.extend(["", "### Blockers", ""])
        lines.extend(f"- {item}" for item in blockers)
    lines.append("")


def _render_history(lines: list[str], releases: Any) -> None:
    lines.extend(["## Release history", ""])
    if not isinstance(releases, list) or not releases:
        lines.extend(["No releases have been archived.", ""])
        return
    for release in releases:
        if not isinstance(release, Mapping):
            continue
        release_url = release.get("releaseUrl")
        title = (
            f"[{release.get('version')}]({release_url})"
            if _https_url(release_url)
            else str(release.get("version", "—"))
        )
        lines.extend(
            [
                f"### {title}",
                "",
                f"- Released: `{release.get('releasedAt', '—')}`",
                f"- Classification: `{release.get('classification', '—')}`",
                "",
            ]
        )


def render_release_document(plan: Mapping[str, Any]) -> str:
    """Render the fixed human-readable projection of a plan."""

    lines = [
        "<!-- Generated by tools/release_governance.py; edit plan.json, not this file. -->",
        "# Release status",
        "",
        f"- Repository: `{plan.get('repository', '—')}`",
        f"- Profile: `{plan.get('profile', '—')}`",
        f"- Current version: `{plan.get('currentVersion') or 'not versioned'}`",
        "",
    ]
    if plan.get("profile") == "component-semver":
        components = plan.get("components")
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, Mapping):
                    continue
                lines.extend([f"## Component `{component.get('id', '—')}`", ""])
                _render_release(lines, component.get("nextRelease"), "Next release")
                _render_history(lines, component.get("releases"))
    else:
        _render_release(lines, plan.get("nextRelease"), "Next release")
        _render_history(lines, plan.get("releases"))
    return "\n".join(lines).rstrip() + "\n"


def _document_drift(root: Path, plan: Mapping[str, Any]) -> VerificationReport:
    report = VerificationReport()
    expected = render_release_document(plan)
    path = _safe_path(root, DOCUMENT_PATH)
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError:
        report.error("document-missing", "generated release document is missing", DOCUMENT_PATH)
    else:
        if actual != expected:
            report.error("document-drift", "generated release document is stale", DOCUMENT_PATH)
    return report


def verify_plan(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    check_document: bool = True,
) -> VerificationReport:
    root = Path(repository_root)
    report = _validate_shape(plan)
    _validate_profile(report, plan)
    _validate_release_contracts(report, plan)
    _validate_feature_graph(report, plan)
    _validate_sources_and_changelog(report, root, plan)
    if check_document:
        report.extend(_document_drift(root, plan))
    return report


def _release_tag(profile: Any, unit_id: str | None, version: Any) -> str:
    if profile == "component-semver":
        return f"{unit_id}-v{version}"
    return f"v{version}"


def _validate_tag_locally(report: VerificationReport, plan: Mapping[str, Any], tag: str) -> None:
    if plan.get("profile") not in {"semver", "component-semver"}:
        report.error("tag-profile", "non-versioned release profiles reject product tags", "plan")
        return
    matches = [
        release
        for unit_id, unit, _ in _units(plan)
        for release in [unit.get("nextRelease")]
        if isinstance(release, Mapping)
        and release.get("status") == "ready"
        and tag == _release_tag(plan.get("profile"), unit_id, release.get("version"))
    ]
    if len(matches) != 1:
        report.error("tag-version", "tag must identify exactly one ready release", "plan")


class GhClient:
    """A narrow, dry-run-aware GitHub CLI adapter."""

    def __init__(self, *, apply: bool = False):
        self.apply = apply
        self.operations: list[str] = []

    def _execute(self, arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                ["gh", *arguments],
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GovernanceError("github-command", "GitHub CLI request failed") from exc
        return completed.stdout

    def text(self, arguments: Sequence[str]) -> str:
        return self._execute(arguments)

    def json(self, arguments: Sequence[str]) -> Any:
        try:
            return json.loads(self._execute(arguments))
        except json.JSONDecodeError as exc:
            raise GovernanceError("github-response", "GitHub CLI returned invalid JSON") from exc

    def mutate_text(self, operation: str, arguments: Sequence[str]) -> str | None:
        self.operations.append(operation)
        if not self.apply:
            return None
        return self._execute(arguments)

    def mutate_json(self, operation: str, arguments: Sequence[str]) -> Any:
        output = self.mutate_text(operation, arguments)
        if output is None:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GovernanceError("github-response", "GitHub CLI returned invalid JSON") from exc


def _require_mode(gh: Any, apply: bool) -> None:
    if gh is not None and getattr(gh, "apply", None) is not apply:
        raise GovernanceError("github-apply-mismatch", "injected GitHub client mode does not match")


def _check_expected_repository(
    report: VerificationReport,
    plan: Mapping[str, Any],
    expected: str | None,
    *,
    required: bool,
) -> None:
    if required and not expected:
        report.error("expected-repository-required", "expected repository identity is required", "plan")
    elif expected and plan.get("repository") != expected:
        report.error("repository-mismatch", "plan repository does not match expected repository", "plan")


def _require_expected_repository(plan: Mapping[str, Any], expected: str | None) -> None:
    if not expected:
        raise GovernanceError("expected-repository-required", "expected repository identity is required")
    if plan.get("repository") != expected:
        raise GovernanceError("repository-mismatch", "plan repository does not match expected repository")


def _read_remote_plan(gh: GhClient, repository: str) -> Mapping[str, Any]:
    try:
        metadata = gh.json(["api", f"repos/{repository}"])
        branch = metadata.get("default_branch") if isinstance(metadata, Mapping) else None
        if not isinstance(branch, str) or not branch:
            raise GovernanceError("github-response", "dependency repository metadata is invalid")
        encoded_branch = urllib.parse.quote(branch, safe="")
        content = gh.json(
            [
                "api",
                f"repos/{repository}/contents/{DEFAULT_PLAN_PATH}?ref={encoded_branch}",
            ]
        )
    except GovernanceError:
        raise
    if not isinstance(content, Mapping) or content.get("encoding") != "base64":
        raise GovernanceError("github-response", "dependency release plan response is invalid")
    encoded = content.get("content")
    if not isinstance(encoded, str) or len(encoded) > MAX_SOURCE_BYTES * 2:
        raise GovernanceError("dependency-plan-size", "dependency release plan exceeds verification limit")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError
        loaded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise GovernanceError("dependency-plan", "dependency release plan is invalid") from exc
    if not isinstance(loaded, Mapping) or loaded.get("repository") != repository:
        raise GovernanceError("dependency-plan", "dependency release plan identity is invalid")
    remote_report = _validate_shape(loaded)
    _validate_profile(remote_report, loaded)
    _validate_release_contracts(remote_report, loaded)
    _validate_feature_graph(remote_report, loaded)
    if not remote_report.ok:
        raise GovernanceError(
            "dependency-plan",
            "dependency release plan does not satisfy the current schema and policy",
        )
    return loaded


def _remote_dependency_graph(
    report: VerificationReport,
    plan: Mapping[str, Any],
    gh: GhClient,
) -> dict[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]]:
    root_repository = plan.get("repository")
    if not isinstance(root_repository, str):
        return {}
    plans: dict[str, Mapping[str, Any]] = {root_repository: plan}
    indexes: dict[str, dict[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]]] = {}
    total_indexed_features = 0

    def index_repository(
        repository: str,
    ) -> dict[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]] | None:
        nonlocal total_indexed_features
        if repository in indexes:
            return indexes[repository]
        if repository not in plans:
            if len(plans) >= MAX_DEPENDENCY_REPOSITORIES:
                report.error("dependency-limit", "dependency graph exceeds repository limit", "plan")
                return None
            try:
                plans[repository] = _read_remote_plan(gh, repository)
            except GovernanceError as exc:
                report.error(exc.code, exc.message, "plan")
                return None
        index: dict[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]] = {}
        for feature_id, feature, path, release, _ in _feature_entries(plans[repository]):
            index[feature_id] = (feature, path, release)
            total_indexed_features += 1
            if total_indexed_features > MAX_DEPENDENCY_FEATURES:
                report.error("dependency-limit", "dependency graph exceeds feature limit", "plan")
                return None
        indexes[repository] = index
        return index

    graph: dict[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]] = {}
    adjacency: dict[str, list[str]] = {}
    pending: list[str] = []
    for feature_id, _, _, release, archived in _feature_entries(plan):
        if not archived and release is plan.get("nextRelease"):
            pending.append(f"{root_repository}:{feature_id}")
    components = plan.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            next_release = component.get("nextRelease")
            if not isinstance(next_release, Mapping):
                continue
            features = next_release.get("features")
            if isinstance(features, list):
                for feature in features:
                    if isinstance(feature, Mapping) and isinstance(feature.get("id"), str):
                        pending.append(f"{root_repository}:{feature['id']}")

    visited: set[str] = set()
    while pending:
        reference = pending.pop()
        if reference in visited:
            continue
        visited.add(reference)
        match = FEATURE_REF_RE.fullmatch(reference)
        if match is None:
            continue
        repository = match.group("repository")
        index = index_repository(repository)
        if index is None:
            continue
        data = index.get(match.group("feature"))
        if data is None:
            report.error("feature-dependency-missing", "feature dependency does not exist", "plan")
            continue
        graph[reference] = data
        adjacency[reference] = []
        dependencies = data[0].get("dependsOn")
        if isinstance(dependencies, list):
            pending.extend(item for item in dependencies if isinstance(item, str))

    for reference, (feature, path, _) in graph.items():
        dependencies = feature.get("dependsOn")
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if dependency not in graph:
                report.error("feature-dependency-missing", "feature dependency does not exist", path)
                continue
            adjacency[reference].append(dependency)
            if feature.get("status") == "accepted" and graph[dependency][0].get("status") != "accepted":
                report.error(
                    "feature-dependency-unaccepted",
                    "accepted feature depends on a feature that is not accepted",
                    path,
                )
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        for target in adjacency[node]:
            if state.get(target) == 1:
                report.error("feature-dependency-cycle", "cross-repository dependency graph contains a cycle", graph[node][1])
            elif state.get(target, 0) == 0:
                visit(target)
        state[node] = 2

    for reference in adjacency:
        if state.get(reference, 0) == 0:
            visit(reference)
    return graph


def _github_verify(
    report: VerificationReport,
    plan: Mapping[str, Any],
    gh: GhClient,
    *,
    tag: str | None,
) -> None:
    repository = plan.get("repository")
    if not isinstance(repository, str):
        return
    _validate_tag_locally(report, plan, tag) if tag else None
    if tag and plan.get("profile") in {"semver", "component-semver"}:
        encoded = urllib.parse.quote(tag, safe="")
        try:
            gh.json(["api", f"repos/{repository}/git/ref/tags/{encoded}"])
        except GovernanceError as exc:
            report.error(exc.code, exc.message, "plan")
    graph = _remote_dependency_graph(report, plan, gh)
    for reference, (feature, path, release) in graph.items():
        if not reference.startswith(f"{repository}:"):
            continue
        pull_request = feature.get("pullRequest")
        if not isinstance(pull_request, str):
            continue
        number = _pull_number(pull_request, repository)
        if number is None:
            continue
        try:
            data = gh.json(["api", f"repos/{repository}/pulls/{number}"])
        except GovernanceError as exc:
            report.error(exc.code, exc.message, path)
            continue
        if not isinstance(data, Mapping):
            report.error("github-pull-request", "pull request response is invalid", path)
            continue
        status = feature.get("status")
        if status in {"active", "blocked"} and data.get("state") != "open":
            report.error("github-pull-request-closed", "in-progress feature pull request must be open", path)
        if status == "accepted":
            if not data.get("merged_at"):
                report.error("github-pull-request-unmerged", "accepted feature pull request must be merged", path)
            base = data.get("base")
            base_ref = base.get("ref") if isinstance(base, Mapping) else None
            expected_base = release.get("integrationBranch")
            if base_ref != expected_base:
                report.error(
                    "github-pull-request-base",
                    "accepted feature pull request must target the release integrationBranch",
                    path,
                )
        if release.get("status") == "ready" and status != "accepted":
            report.error("ready-feature-status", "ready release contains an unaccepted feature", path)


def verify_repository(
    repository_root: Path | str,
    plan_path: str | Path = DEFAULT_PLAN_PATH,
    *,
    github: bool = False,
    tag: str | None = None,
    gh: GhClient | None = None,
    expected_repository: str | None = None,
) -> VerificationReport:
    root = Path(repository_root)
    plan, _ = load_plan(root, plan_path)
    report = verify_plan(root, plan)
    _check_expected_repository(report, plan, expected_repository, required=github or bool(tag))
    identity_ok = not any(
        item.code in {"expected-repository-required", "repository-mismatch"} for item in report.errors
    )
    if github and gh is not None and gh.apply:
        report.error("github-apply-mismatch", "GitHub verification requires a read-only client", "plan")
    elif github and identity_ok:
        _github_verify(report, plan, gh or GhClient(apply=False), tag=tag)
    elif tag and identity_ok:
        _validate_tag_locally(report, plan, tag)
    return report


FIELD_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Status",
        "SINGLE_SELECT",
        ("Planned", "Ready", "In progress", "Blocked", "In review", "Awaiting acceptance", "Accepted"),
    ),
    ("Item type", "SINGLE_SELECT", ("Release", "Feature")),
    ("Feature ID", "TEXT", ()),
    ("Owner repository", "TEXT", ()),
    ("Feature type", "SINGLE_SELECT", ("Capability", "Breaking", "Fix")),
    ("Target version", "TEXT", ()),
    (
        "Release class",
        "SINGLE_SELECT",
        ("Patch", "Minor", "Major", "Initial"),
    ),
    (
        "Risk",
        "SINGLE_SELECT",
        ("Low", "Medium", "High"),
    ),
    ("Depends on", "TEXT", ()),
    ("Dependency state", "SINGLE_SELECT", ("Clear", "Blocked", "Unresolved", "Cycle")),
    ("PR stage", "SINGLE_SELECT", ("None", "Draft", "Review", "Merged", "Closed")),
    (
        "Readiness",
        "SINGLE_SELECT",
        (
            "Blocked",
            "Ready to start",
            "Developing",
            "Ready for review",
            "Waiting for gates",
            "Ready to merge",
            "Awaiting acceptance",
            "Accepted",
        ),
    ),
    ("Gate progress", "NUMBER", ()),
    ("Evidence", "TEXT", ()),
    ("Release unit", "TEXT", ()),
    ("Plan revision", "TEXT", ()),
    ("Sync state", "SINGLE_SELECT", ("Current", "Stale", "Orphan", "Conflict")),
)


def _find_project(gh: GhClient, owner: str, title: str) -> Mapping[str, Any] | None:
    data = gh.json(["project", "list", "--owner", owner, "--limit", "100", "--format", "json"])
    projects = data.get("projects", []) if isinstance(data, Mapping) else data
    if not isinstance(projects, list):
        raise GovernanceError("github-response", "GitHub project list has invalid shape")
    matches = [item for item in projects if isinstance(item, Mapping) and item.get("title") == title]
    if len(matches) > 1:
        raise GovernanceError("github-project-duplicate", "more than one project uses required title")
    return matches[0] if matches else None


def _field_type(field: Mapping[str, Any]) -> str | None:
    for key in ("dataType", "type"):
        value = field.get(key)
        if isinstance(value, str):
            return value.upper()
    return None


def _record_field_creation(
    gh: GhClient,
    owner: str,
    number: str,
    name: str,
    data_type: str,
    options: Sequence[str],
) -> None:
    arguments = [
        "project",
        "field-create",
        number,
        "--owner",
        owner,
        "--name",
        name,
        "--data-type",
        data_type,
        "--format",
        "json",
    ]
    if options:
        arguments.extend(["--single-select-options", ",".join(options)])
    gh.mutate_json(f"project.field.create:{name}", arguments)


def bootstrap_project(
    owner: str,
    title: str = DEFAULT_PROJECT_TITLE,
    *,
    apply: bool = False,
    gh: GhClient | None = None,
) -> list[str]:
    _require_mode(gh, apply)
    client = gh or GhClient(apply=apply)
    project = _find_project(client, owner, title)
    if project is None:
        created = client.mutate_json(
            "project.create",
            ["project", "create", "--owner", owner, "--title", title, "--format", "json"],
        )
        if not apply:
            for name, data_type, options in FIELD_SPECS:
                _record_field_creation(client, owner, "pending", name, data_type, options)
            return client.operations
        if not isinstance(created, Mapping):
            raise GovernanceError("github-response", "created project response is invalid")
        project = created
    project_id = project.get("id")
    number = project.get("number")
    if project.get("public") is True:
        if not isinstance(project_id, str):
            raise GovernanceError("github-response", "project identity is incomplete")
        client.mutate_json(
            "project.make-private",
            [
                "api",
                "graphql",
                "-f",
                (
                    "query=mutation($projectId:ID!){"
                    "updateProjectV2(input:{projectId:$projectId,public:false}){projectV2{id}}}"
                ),
                "-F",
                f"projectId={project_id}",
            ],
        )
    if not isinstance(number, int):
        raise GovernanceError("github-response", "project number is unavailable")
    data = client.json(
        ["project", "field-list", str(number), "--owner", owner, "--limit", "100", "--format", "json"]
    )
    fields = data.get("fields", []) if isinstance(data, Mapping) else data
    if not isinstance(fields, list):
        raise GovernanceError("github-response", "project field list has invalid shape")
    by_name = {
        item.get("name"): item
        for item in fields
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    for name, data_type, options in FIELD_SPECS:
        existing = by_name.get(name)
        if existing is None:
            _record_field_creation(client, owner, str(number), name, data_type, options)
            continue
        actual = _field_type(existing)
        if actual is not None and actual != data_type:
            raise GovernanceError("github-project-field", f"project field '{name}' has incompatible type")
        if options:
            actual_options = {
                item.get("name")
                for item in existing.get("options", [])
                if isinstance(item, Mapping)
            }
            if set(options).difference(actual_options):
                raise GovernanceError("github-project-field", f"project field '{name}' lacks required options")
    return client.operations


def _project_fields(data: Any) -> dict[str, Mapping[str, Any]]:
    fields = data.get("fields", []) if isinstance(data, Mapping) else data
    if not isinstance(fields, list):
        raise GovernanceError("github-response", "project field list has invalid shape")
    result: dict[str, Mapping[str, Any]] = {}
    for field in fields:
        if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
            raise GovernanceError("github-response", "project field list has invalid shape")
        if field["name"] in result:
            raise GovernanceError("github-project-field", "project field identity is ambiguous")
        result[field["name"]] = field
    for name, data_type, options in FIELD_SPECS:
        field = result.get(name)
        if field is None or not isinstance(field.get("id"), str):
            raise GovernanceError("github-project-field", f"project is missing required field '{name}'")
        actual = _field_type(field)
        if actual is not None and actual != data_type:
            raise GovernanceError("github-project-field", f"project field '{name}' has incompatible type")
        if options:
            names = {
                option.get("name")
                for option in field.get("options", [])
                if isinstance(option, Mapping)
            }
            if set(options).difference(names):
                raise GovernanceError("github-project-field", f"project field '{name}' lacks required options")
    return result


def _project_items(
    data: Any,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    items = data.get("items", []) if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        raise GovernanceError("github-response", "project item list has invalid shape")
    by_url: dict[str, Mapping[str, Any]] = {}
    by_marker: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise GovernanceError("github-response", "project item has invalid shape")
        content = item.get("content")
        if not isinstance(content, Mapping):
            continue
        url = content.get("url")
        if isinstance(url, str):
            if url in by_url:
                raise GovernanceError("github-project-item-duplicate", "project contains duplicate URL item")
            by_url[url] = item
        body = content.get("body")
        if isinstance(body, str):
            for marker in re.findall(r"<!-- release-plan:[^>]+ -->", body):
                if marker in by_marker:
                    raise GovernanceError("github-project-item-duplicate", "project contains duplicate plan marker")
                by_marker[marker] = item
    return by_url, by_marker


def _option_id(field: Mapping[str, Any], value: str) -> str | None:
    for option in field.get("options", []):
        if isinstance(option, Mapping) and option.get("name") == value and isinstance(option.get("id"), str):
            return option["id"]
    return None


def _set_project_fields(
    gh: GhClient,
    project_id: str,
    item_id: str,
    fields: Mapping[str, Mapping[str, Any]],
    values: Mapping[str, str | int | None],
) -> None:
    for name, value in values.items():
        if value is None:
            continue
        field = fields[name]
        arguments = [
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project_id,
            "--field-id",
            str(field["id"]),
        ]
        data_type = _field_type(field)
        if data_type == "SINGLE_SELECT":
            option = _option_id(field, str(value))
            if option is None:
                raise GovernanceError("github-project-field", f"field '{name}' lacks value '{value}'")
            arguments.extend(["--single-select-option-id", option])
        elif data_type == "DATE":
            arguments.extend(["--date", str(value)])
        elif data_type == "NUMBER":
            arguments.extend(["--number", str(value)])
        else:
            arguments.extend(["--text", str(value)])
        gh.mutate_text(f"project.field.set:{name}", arguments)


def _draft_marker(repository: str, kind: str, identity: str) -> str:
    return f"<!-- release-plan:{repository}:{kind}:{identity} -->"


def _ensure_draft(
    gh: GhClient,
    *,
    owner: str,
    number: int,
    title: str,
    body: str,
    marker: str,
    existing: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if existing is not None:
        return existing
    created = gh.mutate_json(
        "project.draft.create",
        [
            "project",
            "item-create",
            str(number),
            "--owner",
            owner,
            "--title",
            title,
            "--body",
            f"{marker}\n\n{body}",
            "--format",
            "json",
        ],
    )
    return created if isinstance(created, Mapping) else None


def _ensure_url_item(
    gh: GhClient,
    *,
    owner: str,
    number: int,
    url: str,
    existing: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if existing is not None:
        return existing
    created = gh.mutate_json(
        "project.item.add",
        [
            "project",
            "item-add",
            str(number),
            "--owner",
            owner,
            "--url",
            url,
            "--format",
            "json",
        ],
    )
    return created if isinstance(created, Mapping) else None


def _archive_item(gh: GhClient, owner: str, number: int, item: Mapping[str, Any]) -> None:
    item_id = item.get("id")
    if isinstance(item_id, str):
        gh.mutate_json(
            "project.item.archive",
            [
                "project",
                "item-archive",
                str(number),
                "--owner",
                owner,
                "--id",
                item_id,
                "--format",
                "json",
            ],
        )


def _release_unit_name(repository: str, unit_id: str | None) -> str:
    return unit_id or repository.split("/", 1)[1]


def _plan_revision(plan: Mapping[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _project_release_class(value: Any) -> str | None:
    mapping = {
        "patch": "Patch",
        "minor": "Minor",
        "major": "Major",
        "initial": "Initial",
    }
    return mapping.get(value)


def _project_risk(value: Any) -> str | None:
    return {"low": "Low", "medium": "Medium", "high": "High"}.get(value)


def _project_feature_type(value: Any) -> str | None:
    return {
        "capability": "Capability",
        "breaking": "Breaking",
        "fix": "Fix",
    }.get(value)


def _project_release_status(value: Any) -> str:
    return {
        "planned": "Planned",
        "active": "In progress",
        "blocked": "Blocked",
        "ready": "Ready",
    }.get(value, "Planned")


def _project_feature_status(value: Any, pr_stage: str) -> str:
    if value == "blocked":
        return "Blocked"
    if value == "accepted":
        return "Accepted"
    if pr_stage == "Merged":
        return "Awaiting acceptance"
    if pr_stage == "Review":
        return "In review"
    if value == "active":
        return "In progress"
    return "Planned"


def _project_readiness(status: Any, pr_stage: str, dependency_state: str) -> str:
    if status == "accepted":
        return "Accepted"
    if status == "blocked" or dependency_state in {"Blocked", "Unresolved", "Cycle"}:
        return "Blocked"
    if pr_stage == "None":
        return "Ready to start"
    if pr_stage == "Draft":
        return "Developing"
    if pr_stage == "Review":
        return "Ready for review"
    if pr_stage == "Merged":
        return "Awaiting acceptance"
    return "Blocked"


def _gate_progress(status: Any, pr_stage: str, dependency_state: str) -> int:
    if status == "accepted":
        return 100
    if dependency_state in {"Blocked", "Unresolved", "Cycle"}:
        return 0
    return {
        "None": 0,
        "Draft": 25,
        "Review": 50,
        "Merged": 75,
        "Closed": 0,
    }[pr_stage]


def _dependency_projection(
    feature: Mapping[str, Any],
    graph: Mapping[str, tuple[Mapping[str, Any], str, Mapping[str, Any]]],
    dependency_report: VerificationReport,
) -> str:
    dependencies = feature.get("dependsOn")
    if not isinstance(dependencies, list) or not dependencies:
        return "Clear"
    if any(item.code == "feature-dependency-cycle" for item in dependency_report.errors):
        return "Cycle"
    if any(dependency not in graph for dependency in dependencies):
        return "Unresolved"
    if any(graph[dependency][0].get("status") != "accepted" for dependency in dependencies):
        return "Blocked"
    return "Clear"


def _pull_stage(data: Any) -> str:
    if not isinstance(data, Mapping):
        return "Closed"
    if data.get("merged_at"):
        return "Merged"
    if data.get("state") != "open":
        return "Closed"
    return "Draft" if data.get("draft") is True else "Review"


def sync_project(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    project_owner: str,
    project_title: str = DEFAULT_PROJECT_TITLE,
    apply: bool = False,
    gh: GhClient | None = None,
    expected_repository: str | None = None,
) -> list[str]:
    """Idempotently project releases and feature PRs without changing authority."""

    _require_expected_repository(plan, expected_repository)
    _require_mode(gh, apply)
    root = Path(repository_root)
    report = verify_plan(root, plan)
    structural = [
        item
        for item in report.errors
        if item.code not in {"document-missing", "document-drift", "version-source-drift", "changelog-version"}
    ]
    if structural:
        raise GovernanceError("plan-invalid", "release plan must pass structural policy before projection")
    repository = str(plan["repository"])
    client = gh or GhClient(apply=apply)

    # Finish every remote read before the first mutation.
    project = _find_project(client, project_owner, project_title)
    if project is None:
        raise GovernanceError("github-project", "required release portfolio was not found")
    if project.get("public") is not False:
        raise GovernanceError("github-project-privacy", "release portfolio must be private")
    project_id = project.get("id")
    project_number = project.get("number")
    if not isinstance(project_id, str) or not isinstance(project_number, int):
        raise GovernanceError("github-response", "project identity is incomplete")
    item_data = client.json(
        [
            "project",
            "item-list",
            str(project_number),
            "--owner",
            project_owner,
            "--limit",
            "1000",
            "--format",
            "json",
        ]
    )
    by_url, by_marker = _project_items(item_data)
    field_data = client.json(
        [
            "project",
            "field-list",
            str(project_number),
            "--owner",
            project_owner,
            "--limit",
            "100",
            "--format",
            "json",
        ]
    )
    fields = _project_fields(field_data)
    dependency_report = VerificationReport()
    dependency_graph = _remote_dependency_graph(dependency_report, plan, client)
    pull_stages: dict[str, str] = {}
    for _, feature, _, _, _ in _feature_entries(plan):
        pull_request = feature.get("pullRequest")
        if not isinstance(pull_request, str) or pull_request in pull_stages:
            continue
        number = _pull_number(pull_request, repository)
        if number is None:
            continue
        try:
            pull_data = client.json(["api", f"repos/{repository}/pulls/{number}"])
        except GovernanceError:
            pull_stages[pull_request] = "Closed"
        else:
            pull_stages[pull_request] = _pull_stage(pull_data)

    revision = _plan_revision(plan)
    desired_markers: set[str] = set()
    for unit_id, unit, _ in _units(plan):
        release = unit.get("nextRelease")
        if not isinstance(release, Mapping):
            continue
        unit_name = _release_unit_name(repository, unit_id)
        version = str(release.get("version"))
        accepted, total, percent = _feature_progress(release)
        release_marker = _draft_marker(repository, "release", f"{unit_name}:{version}")
        desired_markers.add(release_marker)
        release_item = _ensure_draft(
            client,
            owner=project_owner,
            number=project_number,
            title=f"[{repository}] {unit_name} v{version}",
            body=f"Release projection from `{DEFAULT_PLAN_PATH}`. {accepted}/{total} features accepted.",
            marker=release_marker,
            existing=by_marker.get(release_marker),
        )
        if isinstance(release_item, Mapping) and isinstance(release_item.get("id"), str):
            _set_project_fields(
                client,
                project_id,
                str(release_item["id"]),
                fields,
                {
                    "Status": _project_release_status(release.get("status")),
                    "Item type": "Release",
                    "Feature ID": None,
                    "Owner repository": repository,
                    "Feature type": None,
                    "Target version": version,
                    "Release class": _project_release_class(release.get("classification")),
                    "Risk": _project_risk(_highest_risk(release.get("features"))),
                    "Depends on": None,
                    "Dependency state": "Clear",
                    "PR stage": "None",
                    "Readiness": "Accepted" if release.get("status") == "ready" else (
                        "Blocked" if release.get("status") == "blocked" else "Developing"
                    ),
                    "Gate progress": percent,
                    "Evidence": _release_evidence(release.get("features")),
                    "Release unit": unit_name,
                    "Plan revision": revision,
                    "Sync state": "Current",
                },
            )
        features = release.get("features")
        if not isinstance(features, list):
            continue
        for feature in features:
            if not isinstance(feature, Mapping):
                continue
            feature_id = str(feature.get("id"))
            marker = _draft_marker(repository, "feature", feature_id)
            if not isinstance(feature.get("pullRequest"), str):
                desired_markers.add(marker)
            pull_request = feature.get("pullRequest")
            pr_stage = pull_stages.get(str(pull_request), "None")
            dependency_state = _dependency_projection(feature, dependency_graph, dependency_report)
            draft = by_marker.get(marker)
            if isinstance(pull_request, str):
                item = _ensure_url_item(
                    client,
                    owner=project_owner,
                    number=project_number,
                    url=pull_request,
                    existing=by_url.get(pull_request),
                )
                if draft is not None:
                    _archive_item(client, project_owner, project_number, draft)
            else:
                item = _ensure_draft(
                    client,
                    owner=project_owner,
                    number=project_number,
                    title=f"[{feature_id}] {feature.get('title')}",
                    body=f"Planned feature projection from `{DEFAULT_PLAN_PATH}`.",
                    marker=marker,
                    existing=draft,
                )
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                evidence = feature.get("evidence")
                dependencies = feature.get("dependsOn")
                _set_project_fields(
                    client,
                    project_id,
                    str(item["id"]),
                    fields,
                    {
                        "Status": _project_feature_status(feature.get("status"), pr_stage),
                        "Item type": "Feature",
                        "Feature ID": feature_id,
                        "Owner repository": repository,
                        "Feature type": _project_feature_type(feature.get("type")),
                        "Target version": version,
                        "Release class": _project_release_class(release.get("classification")),
                        "Risk": _project_risk(feature.get("risk")),
                        "Depends on": "\n".join(dependencies) if isinstance(dependencies, list) else None,
                        "Dependency state": dependency_state,
                        "PR stage": pr_stage,
                        "Readiness": _project_readiness(feature.get("status"), pr_stage, dependency_state),
                        "Gate progress": _gate_progress(feature.get("status"), pr_stage, dependency_state),
                        "Evidence": "\n".join(evidence) if isinstance(evidence, list) and evidence else None,
                        "Release unit": unit_name,
                        "Plan revision": revision,
                        "Sync state": "Current",
                    },
                )
    managed_prefix = f"<!-- release-plan:{repository}:"
    for marker, item in by_marker.items():
        if marker.startswith(managed_prefix) and marker not in desired_markers:
            item_id = item.get("id")
            if isinstance(item_id, str):
                _set_project_fields(
                    client,
                    project_id,
                    item_id,
                    fields,
                    {
                        "Sync state": "Orphan",
                    },
                )
    return client.operations


def _highest_risk(features: Any) -> str | None:
    order = {"low": 0, "medium": 1, "high": 2}
    if not isinstance(features, list):
        return None
    risks = [
        item.get("risk")
        for item in features
        if isinstance(item, Mapping) and item.get("risk") in order
    ]
    return max(risks, key=order.__getitem__) if risks else None


def _release_evidence(features: Any) -> str | None:
    if not isinstance(features, list):
        return None
    evidence: list[str] = []
    for feature in features:
        if isinstance(feature, Mapping) and isinstance(feature.get("evidence"), list):
            evidence.extend(str(item) for item in feature["evidence"])
    return "\n".join(evidence) if evidence else None


def _validate_release_url(url: str, repository: str, tag: str) -> None:
    if not _https_url(url):
        raise GovernanceError("release-url", "release URL must use HTTPS")
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "github.com" or parsed.path.rstrip("/") != f"/{repository}/releases/tag/{tag}":
        raise GovernanceError("release-url", "release URL does not match repository and tag")


def _select_finalize_unit(
    plan: MutableMapping[str, Any], component: str | None
) -> tuple[MutableMapping[str, Any], str | None]:
    if plan.get("profile") == "component-semver":
        if not component:
            raise GovernanceError("component-required", "component-semver finalization requires --component")
        components = plan.get("components")
        matches = [
            item
            for item in components
            if isinstance(item, MutableMapping) and item.get("id") == component
        ] if isinstance(components, list) else []
        if len(matches) != 1:
            raise GovernanceError("component", "component identity is unavailable")
        return matches[0], component
    if component:
        raise GovernanceError("component", "--component is only valid for component-semver")
    return plan, None


def finalize_plan(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    release_url: str,
    released_at: str,
    component: str | None = None,
    expected_repository: str | None = None,
    github: bool = False,
    gh: GhClient | None = None,
) -> dict[str, Any]:
    _require_expected_repository(plan, expected_repository)
    result = copy.deepcopy(plan)
    unit, unit_id = _select_finalize_unit(result, component)
    release = unit.get("nextRelease")
    if not isinstance(release, MutableMapping):
        raise GovernanceError("release-missing", "no next release is available")
    if release.get("status") != "ready":
        raise GovernanceError("release-not-ready", "only a ready release can be finalized")
    if not _is_datetime(released_at):
        raise GovernanceError("released-at", "releasedAt must be an RFC 3339 date-time")
    version = release.get("version")
    tag = _release_tag(result.get("profile"), unit_id, version)
    repository = str(result["repository"])
    _validate_release_url(release_url, repository, tag)
    if github:
        client = gh or GhClient(apply=False)
        try:
            data = client.json(["api", f"repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"])
        except GovernanceError as exc:
            raise GovernanceError(exc.code, exc.message) from exc
        if not isinstance(data, Mapping) or data.get("html_url") != release_url:
            raise GovernanceError("github-release", "GitHub Release does not match declared URL")
    releases = unit.get("releases")
    if not isinstance(releases, list):
        raise GovernanceError("plan-invalid", "release history is unavailable")
    if any(isinstance(item, Mapping) and item.get("version") == version for item in releases):
        raise GovernanceError("release-duplicate", "release version is already archived")
    archived = dict(release)
    archived["releasedAt"] = released_at
    archived["releaseUrl"] = release_url
    releases.append(archived)
    unit["currentVersion"] = version
    unit["nextRelease"] = None
    report = verify_plan(repository_root, result, check_document=False)
    if not report.ok:
        first = report.errors[0]
        raise GovernanceError(first.code, first.message, first.path)
    return result


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _plan_json(plan: Mapping[str, Any]) -> str:
    return json.dumps(plan, indent=2, ensure_ascii=False) + "\n"


def _print_report(report: VerificationReport, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([dataclasses.asdict(item) for item in report.findings], indent=2))
        return
    for item in report.findings:
        location = f" [{item.path}]" if item.path else ""
        print(f"{item.severity}: {item.code}{location}: {item.message}")
    if report.ok:
        print("version-governance: ok")


def _print_operations(operations: Sequence[str], *, applied: bool) -> None:
    prefix = "APPLIED" if applied else "DRY-RUN"
    if not operations:
        print(f"{prefix}: no changes")
    for operation in operations:
        print(f"{prefix}: {operation}")


def _common_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--plan", default=DEFAULT_PLAN_PATH)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify LicoLand repository release plans")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    _common_plan_arguments(verify)
    verify.add_argument("--github", action="store_true")
    verify.add_argument("--expected-repository")
    verify.add_argument("--tag")
    verify.add_argument("--json", action="store_true", dest="as_json")
    render = subparsers.add_parser("render")
    _common_plan_arguments(render)
    render.add_argument("--apply", action="store_true")
    render.add_argument("--check", action="store_true")
    bootstrap = subparsers.add_parser("bootstrap-project")
    bootstrap.add_argument("--project-owner", "--organization", "--owner", dest="project_owner", default="LicoLand")
    bootstrap.add_argument("--project-title", default=DEFAULT_PROJECT_TITLE)
    bootstrap.add_argument("--apply", action="store_true")
    sync = subparsers.add_parser("sync-project")
    _common_plan_arguments(sync)
    sync.add_argument("--project-owner", default="LicoLand")
    sync.add_argument("--project-title", default=DEFAULT_PROJECT_TITLE)
    sync.add_argument("--expected-repository", required=True)
    sync.add_argument("--apply", action="store_true")
    finalize = subparsers.add_parser("finalize")
    _common_plan_arguments(finalize)
    finalize.add_argument("--release-url", required=True)
    finalize.add_argument("--released-at", required=True)
    finalize.add_argument("--component")
    finalize.add_argument("--expected-repository", required=True)
    finalize.add_argument("--github", action="store_true")
    finalize.add_argument("--apply", action="store_true")
    return parser


def _command_verify(args: argparse.Namespace) -> int:
    report = verify_repository(
        args.repository_root,
        args.plan,
        github=args.github,
        tag=args.tag,
        expected_repository=args.expected_repository,
    )
    _print_report(report, as_json=args.as_json)
    return 0 if report.ok else 1


def _command_render(args: argparse.Namespace) -> int:
    root = Path(args.repository_root)
    plan, _ = load_plan(root, args.plan)
    content = render_release_document(plan)
    target = _safe_path(root, DOCUMENT_PATH)
    current = target.read_text(encoding="utf-8") if target.exists() else None
    if current == content:
        print("version-governance: release document is current")
        return 0
    if args.check:
        print("version-governance: release document is stale", file=sys.stderr)
        return 1
    if args.apply:
        _atomic_write(target, content)
        print("APPLIED: release document rendered")
    else:
        print(content, end="")
    return 0


def _command_bootstrap(args: argparse.Namespace) -> int:
    operations = bootstrap_project(
        args.project_owner,
        args.project_title,
        apply=args.apply,
    )
    _print_operations(operations, applied=args.apply)
    return 0


def _command_sync(args: argparse.Namespace) -> int:
    root = Path(args.repository_root)
    plan, _ = load_plan(root, args.plan)
    operations = sync_project(
        root,
        plan,
        project_owner=args.project_owner,
        project_title=args.project_title,
        expected_repository=args.expected_repository,
        apply=args.apply,
    )
    _print_operations(operations, applied=args.apply)
    return 0


def _command_finalize(args: argparse.Namespace) -> int:
    root = Path(args.repository_root)
    plan, plan_path = load_plan(root, args.plan)
    finalized = finalize_plan(
        root,
        plan,
        release_url=args.release_url,
        released_at=args.released_at,
        component=args.component,
        expected_repository=args.expected_repository,
        github=args.github,
    )
    document = render_release_document(finalized)
    if args.apply:
        _atomic_write(plan_path, _plan_json(finalized))
        _atomic_write(_safe_path(root, DOCUMENT_PATH), document)
        print("APPLIED: release plan finalized")
    else:
        print("DRY-RUN: release plan would be finalized")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return _command_verify(args)
        if args.command == "render":
            return _command_render(args)
        if args.command == "bootstrap-project":
            return _command_bootstrap(args)
        if args.command == "sync-project":
            return _command_sync(args)
        if args.command == "finalize":
            return _command_finalize(args)
    except GovernanceError as exc:
        location = f" [{exc.path}]" if exc.path else ""
        print(f"error: {exc.code}{location}: {exc.message}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
