#!/usr/bin/env python3
"""LicoLand organization release-governance verifier and GitHub synchronizer.

The repository JSON plan is the release authority.  This module intentionally
uses only the Python standard library and invokes ``gh`` with argument vectors
(``shell=False``).  Command failures are reduced to safe error categories; raw
GitHub output, credentials, and absolute machine paths are never reported.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
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
SCENARIO_TYPES = {"capability", "breaking", "fix"}
SCENARIO_STATUSES = {"planned", "active", "blocked", "accepted"}
RISK_LEVELS = {"low", "medium", "high"}
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SCENARIO_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,31}$")
COMPONENT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STABLE_MILESTONE_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
MAX_SOURCE_BYTES = 4 * 1024 * 1024


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
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str, path: str | None = None) -> None:
        self.findings.append(Finding(code, message, path, "error"))

    def warning(self, code: str, message: str, path: str | None = None) -> None:
        self.findings.append(Finding(code, message, path, "warning"))

    def extend(self, other: "VerificationReport") -> None:
        self.findings.extend(other.findings)


@dataclasses.dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        if not isinstance(value, str):
            raise ValueError("version must be a string")
        match = SEMVER_RE.fullmatch(value)
        if match is None:
            raise ValueError("version is not valid SemVer")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise ValueError("numeric prerelease identifiers cannot have leading zeroes")
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
            build,
        )

    @property
    def core(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    @property
    def stable(self) -> bool:
        return not self.prerelease

    def core_text(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        value = self.core_text()
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value


def classify_transition(current: str | None, target: str) -> str:
    """Classify a strict stable release transition.

    Plans always target a stable version.  A prerelease may only stabilize to
    its exact core.  LicoLand also treats the sequential public-stability move
    from any ``0.y.z`` release to ``1.0.0`` as ``stabilization``.
    """

    target_version = SemVer.parse(target)
    if not target_version.stable or target_version.build:
        raise ValueError("the planned release version must be a stable SemVer")
    if current is None:
        if target_version.core != (0, 1, 0):
            raise ValueError("an initial release must be exactly 0.1.0")
        return "initial"

    current_version = SemVer.parse(current)
    if current_version.prerelease:
        if current_version.core != target_version.core:
            raise ValueError(
                "a prerelease can only stabilize to the same version core"
            )
        return "stabilization"

    if current_version.core == target_version.core:
        raise ValueError("the target version does not advance the current version")

    if current_version.major == 0 and target_version.core == (1, 0, 0):
        return "stabilization"

    if (
        target_version.major == current_version.major
        and target_version.minor == current_version.minor
        and target_version.patch == current_version.patch + 1
    ):
        return "patch"
    if (
        target_version.major == current_version.major
        and target_version.minor == current_version.minor + 1
        and target_version.patch == 0
    ):
        return "minor"
    if (
        target_version.major == current_version.major + 1
        and target_version.minor == 0
        and target_version.patch == 0
    ):
        return "major"
    raise ValueError(
        "stable releases must advance exactly one patch, minor, or major step"
    )


def _display_path(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.name or "."


def _safe_path(root: Path, value: str | Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise GovernanceError("invalid-path", "path must be a non-empty string")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise GovernanceError(
            "unsafe-path", "path must remain inside the repository", Path(value).name
        ) from exc
    return resolved


def _read_text(root: Path, value: str | Path) -> str:
    path = _safe_path(root, value)
    display = _display_path(root, path)
    try:
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise GovernanceError(
                "source-too-large", "source exceeds the verification size limit", display
            )
        return path.read_text(encoding="utf-8")
    except GovernanceError:
        raise
    except FileNotFoundError as exc:
        raise GovernanceError("missing-file", "required file is missing", display) from exc
    except (OSError, UnicodeError) as exc:
        raise GovernanceError("unreadable-file", "required file cannot be read", display) from exc


def _json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdigit():
                raise ValueError("JSON pointer list segment must be numeric")
            index = int(part)
            if index >= len(current):
                raise ValueError("JSON pointer does not exist")
            current = current[index]
        elif isinstance(current, Mapping):
            if part not in current:
                raise ValueError("JSON pointer does not exist")
            current = current[part]
        else:
            raise ValueError("JSON pointer traverses a scalar value")
    return current


def _toml_key(document: Any, key: str) -> Any:
    if not key:
        raise ValueError("TOML key is required")
    current = document
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise ValueError("TOML key does not exist")
        current = current[part]
    return current


def read_version_source(repository_root: Path | str, source: Mapping[str, Any]) -> str:
    """Read one configured version source without exposing its raw contents."""

    root = Path(repository_root)
    source_format = source.get("format")
    relative_path = source.get("path")
    if not isinstance(relative_path, str):
        raise GovernanceError("invalid-source", "version source path is invalid")
    text = _read_text(root, relative_path)
    display = _display_path(root, _safe_path(root, relative_path))
    try:
        if source_format == "json":
            document = json.loads(text)
            value = _json_pointer(document, source.get("pointer", ""))
        elif source_format == "toml":
            document = tomllib.loads(text)
            value = _toml_key(document, source.get("key", ""))
        elif source_format == "text":
            value = text.strip()
        elif source_format == "regex":
            pattern = source.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("regex pattern is required")
            match = re.search(pattern, text, re.MULTILINE)
            if match is None:
                raise ValueError("regex pattern did not match")
            if "version" in match.groupdict():
                value = match.group("version")
            elif match.lastindex:
                value = match.group(1)
            else:
                value = match.group(0)
        else:
            raise ValueError("unsupported version-source format")
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError, re.error) as exc:
        raise GovernanceError(
            "invalid-version-source",
            "version source cannot be resolved using its declared format",
            display,
        ) from exc
    if not isinstance(value, (str, int)):
        raise GovernanceError(
            "invalid-version-source",
            "resolved version value must be a string or integer",
            display,
        )
    return str(value).strip()


def load_plan(repository_root: Path | str, plan_path: str | Path) -> tuple[dict[str, Any], Path]:
    root = Path(repository_root)
    path = _safe_path(root, plan_path)
    display = _display_path(root, path)
    text = _read_text(root, path)
    try:
        if path.suffix.lower() == ".toml":
            loaded = tomllib.loads(text)
        else:
            loaded = json.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GovernanceError("invalid-plan", "release plan is not valid JSON", display) from exc
    if not isinstance(loaded, dict):
        raise GovernanceError("invalid-plan", "release plan root must be an object", display)
    return loaded, path


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_exact_keys(
    report: VerificationReport,
    value: Mapping[str, Any],
    required: set[str],
    allowed: set[str],
    path: str,
) -> None:
    missing = sorted(required.difference(value))
    extra = sorted(set(value).difference(allowed))
    for name in missing:
        report.error("schema-required", f"required field '{name}' is missing", path)
    for name in extra:
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
    "milestone",
    "releaseIssue",
    "scenarios",
    "blockers",
}
RECORD_KEYS = RELEASE_KEYS | {"releasedAt", "releaseUrl"}
SCENARIO_KEYS = {
    "id",
    "type",
    "title",
    "status",
    "issue",
    "risk",
    "acceptance",
    "evidence",
}
SOURCE_KEYS = {"format", "path", "pointer", "key", "pattern"}
COMPONENT_KEYS = {
    "id",
    "currentVersion",
    "versionSources",
    "changelog",
    "nextRelease",
    "releases",
}


def _validate_source_shape(
    report: VerificationReport, source: Any, path: str
) -> None:
    if not isinstance(source, Mapping):
        report.error("schema-type", "version source must be an object", path)
        return
    source_format = source.get("format")
    selector_contracts = {
        "json": ({"format", "path", "pointer"}, {"format", "path", "pointer"}),
        "toml": ({"format", "path", "key"}, {"format", "path", "key"}),
        "text": ({"format", "path"}, {"format", "path"}),
        "regex": ({"format", "path", "pattern"}, {"format", "path", "pattern"}),
    }
    required, allowed = selector_contracts.get(
        source_format, ({"format", "path"}, SOURCE_KEYS)
    )
    _check_exact_keys(report, source, required, allowed, path)
    if source_format not in {"json", "toml", "text", "regex"}:
        report.error("schema-enum", "version-source format is unsupported", path)
    if not _is_nonempty_string(source.get("path")):
        report.error("schema-type", "version-source path must be non-empty", path)
    if source_format == "json" and not isinstance(source.get("pointer"), str):
        report.error("source-selector", "JSON version source requires 'pointer'", path)
    if source_format == "toml" and not _is_nonempty_string(source.get("key")):
        report.error("source-selector", "TOML version source requires 'key'", path)
    if source_format == "regex" and not _is_nonempty_string(source.get("pattern")):
        report.error("source-selector", "regex version source requires 'pattern'", path)


def _validate_scenario_shape(
    report: VerificationReport, scenario: Any, path: str
) -> None:
    if not isinstance(scenario, Mapping):
        report.error("schema-type", "scenario must be an object", path)
        return
    _check_exact_keys(report, scenario, SCENARIO_KEYS, SCENARIO_KEYS, path)
    scenario_id = scenario.get("id")
    if not isinstance(scenario_id, str) or SCENARIO_ID_RE.fullmatch(scenario_id) is None:
        report.error("scenario-id", "scenario id is invalid", path)
    if scenario.get("type") not in SCENARIO_TYPES:
        report.error("schema-enum", "scenario type is invalid", path)
    title = scenario.get("title")
    if not _is_nonempty_string(title) or len(title) > 160:
        report.error("scenario-title", "scenario title must contain 1-160 characters", path)
    if scenario.get("status") not in SCENARIO_STATUSES:
        report.error("schema-enum", "scenario status is invalid", path)
    issue = scenario.get("issue")
    if issue is not None and not _is_https_url(issue):
        report.error("issue-url", "scenario issue must be an HTTPS URL or null", path)
    if scenario.get("risk") not in RISK_LEVELS:
        report.error("schema-enum", "scenario risk is invalid", path)
    acceptance = scenario.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance or not all(
        _is_nonempty_string(item) for item in acceptance
    ):
        report.error(
            "scenario-acceptance",
            "scenario acceptance must contain at least one non-empty statement",
            path,
        )
    evidence = scenario.get("evidence")
    if not isinstance(evidence, list) or not all(
        _is_nonempty_string(item) for item in evidence
    ):
        report.error("scenario-evidence", "scenario evidence must be a string list", path)


def _validate_release_shape(
    report: VerificationReport,
    release: Any,
    path: str,
    *,
    record: bool,
) -> None:
    if not isinstance(release, Mapping):
        report.error("schema-type", "release must be an object", path)
        return
    allowed = RECORD_KEYS if record else RELEASE_KEYS
    _check_exact_keys(report, release, allowed, allowed, path)
    version = release.get("version")
    try:
        parsed = SemVer.parse(version)
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
    milestone = release.get("milestone")
    if not isinstance(milestone, str) or STABLE_MILESTONE_RE.fullmatch(milestone) is None:
        report.error("milestone", "milestone must use vMAJOR.MINOR.PATCH", path)
    release_issue = release.get("releaseIssue")
    if release_issue is not None and not _is_https_url(release_issue):
        report.error("issue-url", "release issue must be an HTTPS URL or null", path)
    scenarios = release.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        report.error("release-scenarios", "release must contain at least one scenario", path)
    else:
        seen: set[str] = set()
        for index, scenario in enumerate(scenarios):
            scenario_path = f"{path}.scenarios[{index}]"
            _validate_scenario_shape(report, scenario, scenario_path)
            if isinstance(scenario, Mapping) and isinstance(scenario.get("id"), str):
                if scenario["id"] in seen:
                    report.error("scenario-duplicate", "scenario id is duplicated", scenario_path)
                seen.add(scenario["id"])
    blockers = release.get("blockers")
    if not isinstance(blockers, list) or not all(
        _is_nonempty_string(item) for item in blockers
    ):
        report.error("release-blockers", "release blockers must be a string list", path)
    if record:
        if not _is_datetime(release.get("releasedAt")):
            report.error(
                "released-at", "releasedAt must be an RFC 3339 date-time", path
            )
        if not _is_https_url(release.get("releaseUrl")):
            report.error("release-url", "releaseUrl must be an HTTPS URL", path)


def _validate_component_shape(
    report: VerificationReport, component: Any, path: str
) -> None:
    if not isinstance(component, Mapping):
        report.error("schema-type", "component must be an object", path)
        return
    _check_exact_keys(report, component, COMPONENT_KEYS, COMPONENT_KEYS, path)
    component_id = component.get("id")
    if not isinstance(component_id, str) or COMPONENT_ID_RE.fullmatch(component_id) is None:
        report.error("component-id", "component id is invalid", path)
    _validate_unit_shape(report, component, path)


def _validate_unit_shape(
    report: VerificationReport, unit: Mapping[str, Any], path: str
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
            _validate_source_shape(report, source, f"{path}.versionSources[{index}]")
    changelog = unit.get("changelog")
    if changelog is not None and not _is_nonempty_string(changelog):
        report.error("changelog-path", "changelog must be a path or null", path)
    next_release = unit.get("nextRelease")
    if next_release is not None:
        _validate_release_shape(report, next_release, f"{path}.nextRelease", record=False)
    releases = unit.get("releases")
    if not isinstance(releases, list):
        report.error("schema-type", "releases must be an array", path)
    else:
        seen: set[str] = set()
        for index, release in enumerate(releases):
            release_path = f"{path}.releases[{index}]"
            _validate_release_shape(report, release, release_path, record=True)
            if isinstance(release, Mapping) and isinstance(release.get("version"), str):
                if release["version"] in seen:
                    report.error(
                        "release-duplicate", "release version is archived more than once", release_path
                    )
                seen.add(release["version"])


def _validate_plan_shape(plan: Mapping[str, Any]) -> VerificationReport:
    report = VerificationReport()
    _check_exact_keys(report, plan, ROOT_KEYS, ROOT_KEYS, "plan")
    if not _is_nonempty_string(plan.get("$schema")):
        report.error("schema-uri", "$schema must be a non-empty URI", "plan")
    if plan.get("schemaVersion") != 1:
        report.error("schema-version", "schemaVersion must equal 1", "plan")
    repository = plan.get("repository")
    if not isinstance(repository, str) or REPOSITORY_RE.fullmatch(repository) is None:
        report.error("repository", "repository must use owner/name", "plan")
    profile = plan.get("profile")
    if profile not in PROFILES:
        report.error("profile", "release profile is unsupported", "plan")
    _validate_unit_shape(report, plan, "plan")
    components = plan.get("components")
    if not isinstance(components, list):
        report.error("schema-type", "components must be an array", "plan")
    else:
        seen_components: set[str] = set()
        for index, component in enumerate(components):
            component_path = f"plan.components[{index}]"
            _validate_component_shape(report, component, component_path)
            if isinstance(component, Mapping) and isinstance(component.get("id"), str):
                if component["id"] in seen_components:
                    report.error(
                        "component-duplicate", "component id is duplicated", component_path
                    )
                seen_components.add(component["id"])
    return report


def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt.date.fromisoformat(value)
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    except ValueError:
        return False


def _parse_datetime(value: str) -> dt.datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed


def _is_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _parse_datetime(value)
        return "T" in value
    except ValueError:
        return False


def _is_https_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def _github_issue_number(url: Any, repository: str) -> int | None:
    if not isinstance(url, str):
        return None
    parsed = urllib.parse.urlparse(url)
    expected_path = f"/{repository}/issues/"
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or not parsed.path.startswith(expected_path)
        or parsed.query
        or parsed.fragment
    ):
        return None
    suffix = parsed.path[len(expected_path) :].strip("/")
    if not suffix.isdigit() or "/" in suffix:
        return None
    return int(suffix)


def _units(plan: Mapping[str, Any]) -> list[tuple[str | None, Mapping[str, Any], str]]:
    if plan.get("profile") == "component-semver":
        components = plan.get("components")
        if not isinstance(components, list):
            return []
        return [
            (
                component.get("id") if isinstance(component, Mapping) else None,
                component,
                f"plan.components[{index}]",
            )
            for index, component in enumerate(components)
            if isinstance(component, Mapping)
        ]
    return [(None, plan, "plan")]


def _release_tag(profile: Any, unit_id: str | None, version: Any) -> str:
    if not isinstance(version, str):
        raise GovernanceError("release-version", "release version is invalid")
    if profile == "component-semver":
        if not isinstance(unit_id, str) or COMPONENT_ID_RE.fullmatch(unit_id) is None:
            raise GovernanceError("component-id", "release component id is invalid")
        return f"{unit_id}-v{version}"
    return f"v{version}"


def _repository_matches(actual: Any, expected: Any) -> bool:
    return (
        isinstance(actual, str)
        and isinstance(expected, str)
        and REPOSITORY_RE.fullmatch(expected) is not None
        and actual.casefold() == expected.casefold()
    )


def _require_expected_repository(
    plan: Mapping[str, Any], expected_repository: str | None
) -> None:
    if expected_repository is None:
        raise GovernanceError(
            "expected-repository-required",
            "an expected repository identity is required for this operation",
        )
    if not _repository_matches(plan.get("repository"), expected_repository):
        raise GovernanceError(
            "repository-mismatch",
            "release plan repository does not match the expected repository",
            "plan",
        )


def _check_expected_repository(
    report: VerificationReport,
    plan: Mapping[str, Any],
    expected_repository: str | None,
    *,
    required: bool,
) -> None:
    if expected_repository is None:
        if required:
            report.error(
                "expected-repository-required",
                "an expected repository identity is required for GitHub verification",
                "plan",
            )
        return
    if not _repository_matches(plan.get("repository"), expected_repository):
        report.error(
            "repository-mismatch",
            "release plan repository does not match the expected repository",
            "plan",
        )


def _require_gh_apply_mode(gh: Any, apply: bool) -> None:
    if gh is not None and getattr(gh, "apply", None) is not apply:
        raise GovernanceError(
            "github-apply-mismatch",
            "injected GitHub client mode does not match the requested apply mode",
        )


def _validate_profile_contract(
    report: VerificationReport, plan: Mapping[str, Any]
) -> None:
    profile = plan.get("profile")
    components = plan.get("components")
    if profile == "semver":
        if components:
            report.error(
                "profile-components",
                "semver profile cannot declare independently versioned components",
                "plan",
            )
    elif profile == "component-semver":
        if not isinstance(components, list) or not components:
            report.error(
                "profile-components",
                "component-semver profile requires at least one component",
                "plan",
            )
        for name in ("currentVersion", "changelog", "nextRelease"):
            if plan.get(name) is not None:
                report.error(
                    "profile-root-version",
                    f"component-semver profile requires root {name} to be null",
                    "plan",
                )
        for name in ("versionSources", "releases"):
            if plan.get(name) != []:
                report.error(
                    "profile-root-version",
                    f"component-semver profile requires root {name} to be empty",
                    "plan",
                )
    elif profile in {"governance", "continuous-site", "inactive"}:
        for name in ("currentVersion", "changelog", "nextRelease"):
            if plan.get(name) is not None:
                report.error(
                    "profile-version",
                    f"{profile} profile cannot declare {name}",
                    "plan",
                )
        for name in ("versionSources", "releases", "components"):
            if plan.get(name) != []:
                report.error(
                    "profile-version",
                    f"{profile} profile requires {name} to be empty",
                    "plan",
                )


def _validate_scenario_mix(
    report: VerificationReport,
    release: Mapping[str, Any],
    inferred: str,
    path: str,
) -> None:
    scenarios = release.get("scenarios")
    if not isinstance(scenarios, list):
        return
    types = [
        scenario.get("type")
        for scenario in scenarios
        if isinstance(scenario, Mapping)
    ]
    if inferred == "patch":
        if not types or any(item != "fix" for item in types):
            report.error(
                "patch-scenarios",
                "patch releases require one or more fixes and prohibit capabilities or breaking changes",
                path,
            )
    elif inferred == "minor":
        if "capability" not in types or "breaking" in types:
            report.error(
                "minor-scenarios",
                "minor releases require a capability and prohibit breaking scenarios",
                path,
            )
    elif inferred == "major":
        if "breaking" not in types:
            report.error(
                "major-scenarios",
                "major releases require at least one breaking scenario",
                path,
            )
    elif inferred in {"initial", "stabilization"}:
        if "capability" not in types:
            report.error(
                f"{inferred}-scenarios",
                f"{inferred} releases require at least one capability scenario",
                path,
            )


def _validate_ready(
    report: VerificationReport,
    release: Mapping[str, Any],
    repository: str,
    path: str,
) -> None:
    if release.get("status") != "ready":
        return
    blockers = release.get("blockers")
    if blockers:
        report.error("ready-blockers", "ready release cannot contain blockers", path)
    if _github_issue_number(release.get("releaseIssue"), repository) is None:
        report.error(
            "ready-release-issue",
            "ready release requires an owning-repository release issue URL",
            path,
        )
    scenarios = release.get("scenarios")
    if not isinstance(scenarios, list):
        return
    for index, scenario in enumerate(scenarios):
        scenario_path = f"{path}.scenarios[{index}]"
        if not isinstance(scenario, Mapping):
            continue
        if scenario.get("status") != "accepted":
            report.error(
                "ready-scenario-status",
                "every ready-release scenario must be accepted",
                scenario_path,
            )
        if _github_issue_number(scenario.get("issue"), repository) is None:
            report.error(
                "ready-scenario-issue",
                "every ready-release scenario requires an owning-repository issue URL",
                scenario_path,
            )
        evidence = scenario.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(
            _is_nonempty_string(item) for item in evidence
        ):
            report.error(
                "ready-scenario-evidence",
                "every accepted scenario requires reviewed evidence",
                scenario_path,
            )


def _validate_ready_issue_uniqueness(
    report: VerificationReport, plan: Mapping[str, Any]
) -> None:
    repository = plan.get("repository")
    if not isinstance(repository, str):
        return
    seen: dict[int | str, str] = {}
    for _, unit, unit_path in _units(plan):
        release = unit.get("nextRelease")
        if not isinstance(release, Mapping) or release.get("status") != "ready":
            continue
        candidates: list[tuple[Any, str]] = [
            (release.get("releaseIssue"), f"{unit_path}.nextRelease.releaseIssue")
        ]
        scenarios = release.get("scenarios")
        if isinstance(scenarios, list):
            candidates.extend(
                (
                    scenario.get("issue") if isinstance(scenario, Mapping) else None,
                    f"{unit_path}.nextRelease.scenarios[{index}].issue",
                )
                for index, scenario in enumerate(scenarios)
            )
        for issue_url, issue_path in candidates:
            if not isinstance(issue_url, str):
                continue
            issue_number = _github_issue_number(issue_url, repository)
            identity: int | str = (
                issue_number if issue_number is not None else issue_url
            )
            previous = seen.get(identity)
            if previous is not None:
                report.error(
                    "ready-issue-duplicate",
                    "ready release and scenario issue URLs must be globally unique",
                    issue_path,
                )
                report.error(
                    "ready-issue-duplicate",
                    "ready release and scenario issue URLs must be globally unique",
                    previous,
                )
            else:
                seen[identity] = issue_path


def _expected_source_version(unit: Mapping[str, Any]) -> str | None:
    release = unit.get("nextRelease")
    if isinstance(release, Mapping) and release.get("status") == "ready":
        version = release.get("version")
        return version if isinstance(version, str) else None
    current = unit.get("currentVersion")
    return current if isinstance(current, str) else None


def _validate_changelog(
    report: VerificationReport,
    root: Path,
    changelog: Any,
    expected: str | None,
    path: str,
) -> None:
    if changelog is None or expected is None:
        return
    if not isinstance(changelog, str):
        return
    try:
        text = _read_text(root, changelog)
    except GovernanceError as exc:
        report.error(exc.code, exc.message, exc.path)
        return
    heading = re.compile(
        rf"(?mi)^#{{1,6}}\s+(?:\[\s*)?v?{re.escape(expected)}"
        rf"(?:\s*\])?(?=\s|$|-)"
    )
    if heading.search(text) is None:
        report.error(
            "changelog-version",
            f"changelog has no heading for expected version {expected}",
            _display_path(root, _safe_path(root, changelog)),
        )


def _validate_version_sources(
    report: VerificationReport,
    root: Path,
    unit: Mapping[str, Any],
    unit_path: str,
) -> None:
    expected = _expected_source_version(unit)
    if expected is None:
        return
    sources = unit.get("versionSources")
    if not isinstance(sources, list):
        return
    if not sources:
        report.error(
            "version-source-required",
            "versioned release unit requires at least one version source",
            unit_path,
        )
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            continue
        try:
            actual = read_version_source(root, source)
        except GovernanceError as exc:
            report.error(exc.code, exc.message, exc.path)
            continue
        if actual != expected:
            relative = source.get("path")
            display = (
                _display_path(root, _safe_path(root, relative))
                if isinstance(relative, str)
                else unit_path
            )
            report.error(
                "version-source-drift",
                f"version source must equal expected version {expected}",
                display,
            )
    _validate_changelog(
        report,
        root,
        unit.get("changelog"),
        expected,
        unit_path,
    )


def _validate_release_contracts(
    report: VerificationReport,
    plan: Mapping[str, Any],
    root: Path,
) -> None:
    repository = plan.get("repository")
    if not isinstance(repository, str):
        return
    for _, unit, unit_path in _units(plan):
        release = unit.get("nextRelease")
        if isinstance(release, Mapping):
            current = unit.get("currentVersion")
            target = release.get("version")
            try:
                inferred = classify_transition(current, target)
            except (TypeError, ValueError) as exc:
                report.error("version-transition", str(exc), f"{unit_path}.nextRelease")
            else:
                if release.get("classification") != inferred:
                    report.error(
                        "classification",
                        f"declared classification must be {inferred}",
                        f"{unit_path}.nextRelease",
                    )
                _validate_scenario_mix(
                    report, release, inferred, f"{unit_path}.nextRelease"
                )
            if isinstance(target, str) and release.get("milestone") != f"v{target}":
                report.error(
                    "milestone-version",
                    "milestone must exactly equal v<release version>",
                    f"{unit_path}.nextRelease",
                )
            _validate_ready(
                report, release, repository, f"{unit_path}.nextRelease"
            )
        _validate_version_sources(report, root, unit, unit_path)
    _validate_ready_issue_uniqueness(report, plan)


def _release_rows(release: Mapping[str, Any]) -> list[str]:
    scenarios = release.get("scenarios")
    if not isinstance(scenarios, list):
        return []
    rows: list[str] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            continue
        issue = scenario.get("issue")
        issue_text = f"[issue]({issue})" if _is_https_url(issue) else "—"
        evidence = scenario.get("evidence")
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        values = (
            str(scenario.get("id", "—")),
            str(scenario.get("type", "—")),
            str(scenario.get("title", "—")).replace("|", r"\|").replace("\n", " "),
            str(scenario.get("status", "—")),
            str(scenario.get("risk", "—")),
            issue_text,
            str(evidence_count),
        )
        rows.append("| " + " | ".join(values) + " |")
    return rows


def _render_release_section(
    lines: list[str], heading: str, release: Mapping[str, Any] | None
) -> None:
    lines.extend([f"## {heading}", ""])
    if release is None:
        lines.extend(["No release is currently planned.", ""])
        return
    lines.extend(
        [
            f"- Version: `{release.get('version', '—')}`",
            f"- Classification: `{release.get('classification', '—')}`",
            f"- Status: `{release.get('status', '—')}`",
            f"- Target date: `{release.get('targetDate') or 'not set'}`",
            f"- Milestone: `{release.get('milestone', '—')}`",
        ]
    )
    release_issue = release.get("releaseIssue")
    lines.append(
        f"- Release issue: [owning issue]({release_issue})"
        if _is_https_url(release_issue)
        else "- Release issue: not linked"
    )
    lines.extend(
        [
            "",
            "| ID | Type | Outcome | Status | Risk | Issue | Evidence |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    lines.extend(_release_rows(release))
    blockers = release.get("blockers")
    lines.extend(["", "### Blockers", ""])
    if isinstance(blockers, list) and blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("None.")
    lines.append("")


def render_release_document(plan: Mapping[str, Any]) -> str:
    """Render the fixed human-readable projection of a plan."""

    profile = plan.get("profile", "unknown")
    current = plan.get("currentVersion")
    lines = [
        "<!-- Generated by tools/release_governance.py; edit plan.json, not this file. -->",
        "# Release status",
        "",
        f"- Repository: `{plan.get('repository', 'unknown')}`",
        f"- Profile: `{profile}`",
        f"- Current version: `{current or 'not versioned'}`",
        "",
    ]
    if profile == "component-semver":
        components = plan.get("components")
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, Mapping):
                    continue
                component_id = component.get("id", "unknown")
                lines.extend(
                    [
                        f"## Component `{component_id}`",
                        "",
                        f"- Current version: `{component.get('currentVersion') or 'not released'}`",
                        "",
                    ]
                )
                _render_release_section(
                    lines, "Next release", component.get("nextRelease")
                )
                _render_history(lines, component.get("releases"))
    else:
        _render_release_section(
            lines,
            "Next release",
            plan.get("nextRelease") if isinstance(plan.get("nextRelease"), Mapping) else None,
        )
        _render_history(lines, plan.get("releases"))
    return "\n".join(lines).rstrip() + "\n"


def _render_history(lines: list[str], releases: Any) -> None:
    lines.extend(["## Release history", ""])
    if not isinstance(releases, list) or not releases:
        lines.extend(["No releases have been archived.", ""])
        return
    lines.extend(
        [
            "| Version | Classification | Released at | GitHub Release |",
            "| --- | --- | --- | --- |",
        ]
    )
    for release in releases:
        if not isinstance(release, Mapping):
            continue
        release_reference = release.get("releaseUrl")
        url_text = (
            f"[release]({release_reference})"
            if _is_https_url(release_reference)
            else "—"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    str(release.get("version", "—")),
                    str(release.get("classification", "—")),
                    str(release.get("releasedAt", "—")),
                    url_text,
                )
            )
            + " |"
        )
    lines.append("")


def _document_drift(
    report: VerificationReport, root: Path, plan: Mapping[str, Any]
) -> None:
    expected = render_release_document(plan)
    path = _safe_path(root, DOCUMENT_PATH)
    display = _display_path(root, path)
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report.error("document-missing", "generated release document is missing", display)
        return
    except (OSError, UnicodeError):
        report.error("document-unreadable", "generated release document cannot be read", display)
        return
    if actual != expected:
        report.error(
            "document-drift",
            "generated release document does not match the release plan",
            display,
        )


def verify_plan(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    check_document: bool = True,
) -> VerificationReport:
    root = Path(repository_root)
    report = _validate_plan_shape(plan)
    _validate_profile_contract(report, plan)
    _validate_release_contracts(report, plan, root)
    if check_document:
        _document_drift(report, root, plan)
    return report


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


class GhClient:
    """Minimal safe ``gh`` adapter with dry-run mutation recording."""

    def __init__(
        self,
        *,
        apply: bool,
        executable: str = "gh",
        timeout: int = 30,
    ):
        self.apply = apply
        self.executable = executable
        self.timeout = timeout
        self.operations: list[str] = []

    def _execute(self, arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise GovernanceError(
                "github-unavailable", "GitHub CLI is not available"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GovernanceError("github-timeout", "GitHub CLI request timed out") from exc
        except OSError as exc:
            raise GovernanceError(
                "github-unavailable", "GitHub CLI could not be started"
            ) from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").lower()
            if "authentication" in stderr or "authenticate" in stderr or "http 401" in stderr:
                code = "github-auth"
                message = "GitHub authentication is unavailable"
            elif "forbidden" in stderr or "http 403" in stderr or "resource not accessible" in stderr:
                code = "github-permission"
                message = "GitHub permission is insufficient"
            elif "rate limit" in stderr:
                code = "github-rate-limit"
                message = "GitHub API rate limit was reached"
            elif "not found" in stderr or "http 404" in stderr:
                code = "github-not-found"
                message = "required GitHub resource was not found"
            else:
                code = "github-command"
                message = "GitHub CLI request failed"
            raise GovernanceError(code, message)
        return completed.stdout

    def text(self, arguments: Sequence[str]) -> str:
        return self._execute(arguments)

    def json(self, arguments: Sequence[str]) -> Any:
        output = self._execute(arguments)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GovernanceError(
                "github-response", "GitHub CLI returned an invalid response"
            ) from exc

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
            raise GovernanceError(
                "github-response", "GitHub CLI returned an invalid response"
            ) from exc


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
    versioned_profiles = {"semver", "component-semver"}
    if plan.get("profile") not in versioned_profiles:
        if tag:
            report.error(
                "tag-profile",
                "non-versioned release profiles reject product-version tags",
                "plan",
            )
        return

    ready_releases: list[tuple[str | None, Mapping[str, Any], str]] = []
    for unit_id, unit, unit_path in _units(plan):
        release = unit.get("nextRelease")
        if isinstance(release, Mapping) and release.get("status") == "ready":
            ready_releases.append((unit_id, release, f"{unit_path}.nextRelease"))

    if tag:
        matches = [
            (unit_id, release, path)
            for unit_id, release, path in ready_releases
            if tag
            == _release_tag(
                plan.get("profile"), unit_id, release.get("version")
            )
        ]
        if len(matches) != 1:
            report.error(
                "tag-version",
                "tag must identify exactly one ready release using its profile convention",
                "plan",
            )
        else:
            encoded = urllib.parse.quote(tag, safe="")
            try:
                gh.json(["api", f"repos/{repository}/git/ref/tags/{encoded}"])
            except GovernanceError as exc:
                report.error(exc.code, exc.message, "plan")

    if not ready_releases:
        return
    issue_expectations: dict[str, tuple[str, str]] = {}
    milestones: set[str] = set()
    for _, release, path in ready_releases:
        expected_milestone = release.get("milestone")
        if not isinstance(expected_milestone, str):
            continue
        release_url = release.get("releaseIssue")
        if isinstance(release_url, str):
            issue_expectations[release_url] = (
                expected_milestone,
                f"{path}.releaseIssue",
            )
        scenarios = release.get("scenarios")
        if isinstance(scenarios, list):
            for index, scenario in enumerate(scenarios):
                if isinstance(scenario, Mapping) and isinstance(scenario.get("issue"), str):
                    issue_expectations[scenario["issue"]] = (
                        expected_milestone,
                        f"{path}.scenarios[{index}].issue",
                    )
        milestones.add(expected_milestone)

    for url, (expected_milestone, issue_path) in sorted(
        issue_expectations.items()
    ):
        number = _github_issue_number(url, repository)
        if number is None:
            continue
        try:
            issue = gh.json(["api", f"repos/{repository}/issues/{number}"])
        except GovernanceError as exc:
            report.error(exc.code, exc.message, "plan")
            continue
        if not isinstance(issue, Mapping) or issue.get("pull_request") is not None:
            report.error("github-issue", "linked item is not an owning issue", issue_path)
            continue
        if issue.get("state") != "closed":
            report.error(
                "github-issue-open", "ready-release issue is not closed", issue_path
            )
        issue_milestone = issue.get("milestone")
        actual_milestone = (
            issue_milestone.get("title")
            if isinstance(issue_milestone, Mapping)
            else None
        )
        if actual_milestone != expected_milestone:
            report.error(
                "github-issue-milestone",
                "ready-release issue is not bound to its declared milestone",
                issue_path,
            )

    try:
        milestone_data = gh.json(
            ["api", f"repos/{repository}/milestones?state=all&per_page=100"]
        )
    except GovernanceError as exc:
        report.error(exc.code, exc.message, "plan")
        milestone_data = []
    for title in milestones:
        milestone = next(
            (
                item
                for item in milestone_data
                if isinstance(item, Mapping) and item.get("title") == title
            ),
            None,
        )
        if milestone is None:
            report.error("github-milestone", "required milestone was not found", "plan")
        elif milestone.get("state") != "closed" or milestone.get("open_issues", 0) != 0:
            report.error(
                "github-milestone-open",
                "ready-release milestone must be closed with no open issues",
                "plan",
            )


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
    _check_expected_repository(
        report,
        plan,
        expected_repository,
        required=github or bool(tag),
    )
    if tag and not github:
        _validate_tag_locally(report, plan, tag)
    identity_ok = not any(
        finding.code in {"expected-repository-required", "repository-mismatch"}
        for finding in report.errors
    )
    if github and gh is not None and getattr(gh, "apply", None) is not False:
        report.error(
            "github-apply-mismatch",
            "GitHub verification requires a read-only client",
            "plan",
        )
        identity_ok = False
    if github and identity_ok:
        _github_verify(
            report,
            plan,
            gh or GhClient(apply=False),
            tag=tag,
        )
    return report


def _validate_tag_locally(
    report: VerificationReport, plan: Mapping[str, Any], tag: str
) -> None:
    if plan.get("profile") not in {"semver", "component-semver"}:
        report.error(
            "tag-profile",
            "non-versioned release profiles reject product-version tags",
            "plan",
        )
        return
    matches = 0
    for unit_id, unit, _ in _units(plan):
        release = unit.get("nextRelease")
        if (
            isinstance(release, Mapping)
            and release.get("status") == "ready"
            and tag
            == _release_tag(
                plan.get("profile"), unit_id, release.get("version")
            )
        ):
            matches += 1
    if matches != 1:
        report.error(
            "tag-version",
            "tag must identify exactly one ready release using its profile convention",
            "plan",
        )


def _find_project(
    gh: GhClient, owner: str, title: str
) -> Mapping[str, Any] | None:
    data = gh.json(
        [
            "project",
            "list",
            "--owner",
            owner,
            "--limit",
            "100",
            "--format",
            "json",
        ]
    )
    projects = data.get("projects", []) if isinstance(data, Mapping) else data
    if not isinstance(projects, list):
        raise GovernanceError(
            "github-response", "GitHub project list has an invalid shape"
        )
    matches = [
        project
        for project in projects
        if isinstance(project, Mapping) and project.get("title") == title
    ]
    if len(matches) > 1:
        raise GovernanceError(
            "github-project-duplicate",
            "more than one project uses the required title",
        )
    return matches[0] if matches else None


def _project_item_urls(data: Any) -> set[str]:
    items = data.get("items", []) if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        return set()
    result: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if isinstance(content, Mapping) and isinstance(content.get("url"), str):
            result.add(content["url"])
    return result


def _project_items_by_url(data: Any) -> dict[str, Mapping[str, Any]]:
    items = data.get("items", []) if isinstance(data, Mapping) else data
    if not isinstance(items, list):
        raise GovernanceError(
            "github-response", "project item list has an invalid shape"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise GovernanceError(
                "github-response", "project item list has an invalid shape"
            )
        content = item.get("content")
        if isinstance(content, Mapping) and isinstance(content.get("url"), str):
            url = content["url"]
            if not isinstance(item.get("id"), str):
                raise GovernanceError(
                    "github-response", "project issue item has an invalid shape"
                )
            if url in result:
                raise GovernanceError(
                    "github-project-item-duplicate",
                    "project contains a duplicate issue item",
                )
            result[url] = item
    return result


FIELD_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Target Version", "TEXT", ()),
    (
        "Release Class",
        "SINGLE_SELECT",
        ("initial", "stabilization", "major", "minor", "patch"),
    ),
    ("Scenario Type", "SINGLE_SELECT", ("release", "capability", "breaking", "fix")),
    (
        "Readiness",
        "SINGLE_SELECT",
        ("planned", "active", "blocked", "accepted", "ready", "released"),
    ),
    ("Target Date", "DATE", ()),
    ("Risk", "SINGLE_SELECT", ("low", "medium", "high")),
    ("Evidence", "TEXT", ()),
    ("Release Unit", "TEXT", ()),
)


def bootstrap_project(
    owner: str,
    title: str = DEFAULT_PROJECT_TITLE,
    *,
    apply: bool = False,
    gh: GhClient | None = None,
) -> list[str]:
    """Idempotently create the private organization release portfolio."""

    _require_gh_apply_mode(gh, apply)
    client = gh or GhClient(apply=apply)
    project = _find_project(client, owner, title)
    if project is None:
        created = client.mutate_json(
            "project.create",
            [
                "project",
                "create",
                "--owner",
                owner,
                "--title",
                title,
                "--format",
                "json",
            ],
        )
        if not apply:
            for name, data_type, options in FIELD_SPECS:
                _record_field_creation(client, owner, "pending", name, data_type, options)
            return client.operations
        if not isinstance(created, Mapping):
            raise GovernanceError(
                "github-response", "created project response has an invalid shape"
            )
        project = created

    project_id = project.get("id")
    project_number = project.get("number")
    if project.get("public") is True:
        if not isinstance(project_id, str):
            raise GovernanceError(
                "github-response", "project visibility cannot be safely updated"
            )
        client.mutate_json(
            "project.make-private",
            [
                "api",
                "graphql",
                "-f",
                (
                    "query=mutation($projectId:ID!){"
                    "updateProjectV2(input:{projectId:$projectId,public:false})"
                    "{projectV2{id}}}"
                ),
                "-F",
                f"projectId={project_id}",
            ],
        )
    if not isinstance(project_number, int):
        raise GovernanceError("github-response", "project has no usable number")
    field_data = client.json(
        [
            "project",
            "field-list",
            str(project_number),
            "--owner",
            owner,
            "--limit",
            "100",
            "--format",
            "json",
        ]
    )
    fields = field_data.get("fields", []) if isinstance(field_data, Mapping) else field_data
    if not isinstance(fields, list):
        raise GovernanceError("github-response", "project field list has an invalid shape")
    by_name = {
        field.get("name"): field
        for field in fields
        if isinstance(field, Mapping) and isinstance(field.get("name"), str)
    }
    for name, data_type, options in FIELD_SPECS:
        existing = by_name.get(name)
        if existing is None:
            _record_field_creation(
                client, owner, str(project_number), name, data_type, options
            )
            continue
        actual_type = _field_data_type(existing)
        if actual_type and actual_type != data_type:
            raise GovernanceError(
                "github-project-field",
                f"project field '{name}' has an incompatible type",
            )
        if data_type == "SINGLE_SELECT":
            actual_options = {
                item.get("name")
                for item in existing.get("options", [])
                if isinstance(item, Mapping)
            }
            missing = set(options).difference(actual_options)
            if missing:
                raise GovernanceError(
                    "github-project-field",
                    f"project field '{name}' is missing required options",
                )
    return client.operations


def _field_data_type(field: Mapping[str, Any]) -> str | None:
    data_type = field.get("dataType")
    if isinstance(data_type, str):
        return data_type.upper()
    field_type = field.get("type")
    if field_type == "ProjectV2SingleSelectField":
        return "SINGLE_SELECT"
    if field_type == "ProjectV2IterationField":
        return "ITERATION"
    return None


def _record_field_creation(
    client: GhClient,
    owner: str,
    project_number: str,
    name: str,
    data_type: str,
    options: Sequence[str],
) -> None:
    arguments = [
        "project",
        "field-create",
        project_number,
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
    client.mutate_json(f"project.field.create:{name}", arguments)


LABEL_SPECS: Mapping[str, tuple[str, str]] = {
    "type:release": ("5319e7", "Repository release coordination issue"),
    "type:scenario": ("1d76db", "Independently acceptable release scenario"),
    "type:fix": ("d73a4a", "Backward-compatible defect correction"),
    "type:capability": ("0e8a16", "New independently acceptable capability"),
    "type:breaking": ("b60205", "Breaking behavior or migration"),
    "semver:initial": ("8250df", "Initial 0.1.0 release"),
    "semver:stabilization": ("8250df", "Controlled stability transition"),
    "semver:major": ("b60205", "Major release impact"),
    "semver:minor": ("1d76db", "Minor release impact"),
    "semver:patch": ("d73a4a", "Patch release impact"),
    "release:planned": ("c5def5", "Release is planned"),
    "release:active": ("fbca04", "Release work is active"),
    "release:blocked": ("b60205", "Release is blocked"),
    "release:ready": ("0e8a16", "Release satisfies readiness policy"),
    "scenario:planned": ("c5def5", "Scenario is planned"),
    "scenario:active": ("fbca04", "Scenario work is active"),
    "scenario:blocked": ("b60205", "Scenario is blocked"),
    "scenario:accepted": ("0e8a16", "Scenario acceptance is complete"),
}


def _labels_from_json(data: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(data, list):
        raise GovernanceError("github-response", "label list has an invalid shape")
    result: dict[str, Mapping[str, Any]] = {}
    for item in data:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("color"), str)
            or (
                item.get("description") is not None
                and not isinstance(item.get("description"), str)
            )
        ):
            raise GovernanceError(
                "github-response", "label list has an invalid shape"
            )
        if item["name"] in result:
            raise GovernanceError(
                "github-label-duplicate", "GitHub label identity is ambiguous"
            )
        result[item["name"]] = item
    return result


def _ensure_labels(
    client: GhClient,
    repository: str,
    existing: Mapping[str, Mapping[str, Any]],
) -> None:
    for name, (color, description) in LABEL_SPECS.items():
        current = existing.get(name)
        if current is None:
            client.mutate_text(
                f"label.create:{name}",
                [
                    "label",
                    "create",
                    name,
                    "--repo",
                    repository,
                    "--color",
                    color,
                    "--description",
                    description,
                ],
            )
        elif (
            str(current.get("color", "")).lower() != color
            or current.get("description") != description
        ):
            client.mutate_text(
                f"label.update:{name}",
                [
                    "label",
                    "edit",
                    name,
                    "--repo",
                    repository,
                    "--color",
                    color,
                    "--description",
                    description,
                ],
            )


def _ensure_milestone(
    client: GhClient,
    repository: str,
    release: Mapping[str, Any],
    plan_reference: str,
    existing: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    title = str(release.get("milestone"))
    description = f"Release authority: {plan_reference}"
    due_date = release.get("targetDate")
    due_on = f"{due_date}T23:59:59Z" if isinstance(due_date, str) else None
    if existing is None:
        arguments = [
            "api",
            "-X",
            "POST",
            f"repos/{repository}/milestones",
            "-f",
            f"title={title}",
            "-f",
            f"description={description}",
        ]
        if due_on:
            arguments.extend(["-f", f"due_on={due_on}"])
        created = client.mutate_json("milestone.create", arguments)
        return created if isinstance(created, Mapping) else None
    changes = (
        existing.get("description") != description
        or (due_on is not None and existing.get("due_on") != due_on)
    )
    if changes:
        number = existing.get("number")
        if not isinstance(number, int):
            raise GovernanceError("github-response", "milestone has no usable number")
        arguments = [
            "api",
            "-X",
            "PATCH",
            f"repos/{repository}/milestones/{number}",
            "-f",
            f"description={description}",
        ]
        if due_on:
            arguments.extend(["-f", f"due_on={due_on}"])
        client.mutate_json("milestone.update", arguments)
    return existing


def _preflight_milestones(
    data: Any,
    active_units: Sequence[tuple[str | None, Mapping[str, Any], str]],
) -> dict[str, Mapping[str, Any] | None]:
    if not isinstance(data, list):
        raise GovernanceError("github-response", "milestone list has an invalid shape")
    expected_dates: dict[str, Any] = {}
    for _, unit, _ in active_units:
        release = unit.get("nextRelease")
        if not isinstance(release, Mapping):
            continue
        title = release.get("milestone")
        if not isinstance(title, str):
            continue
        target_date = release.get("targetDate")
        if title in expected_dates and expected_dates[title] != target_date:
            raise GovernanceError(
                "github-milestone-conflict",
                "one milestone cannot represent releases with different target dates",
            )
        expected_dates[title] = target_date

    matches: dict[str, list[Mapping[str, Any]]] = {
        title: [] for title in expected_dates
    }
    for item in data:
        if not isinstance(item, Mapping) or not isinstance(item.get("title"), str):
            raise GovernanceError(
                "github-response", "milestone list has an invalid shape"
            )
        title = item["title"]
        if title in matches:
            if not isinstance(item.get("number"), int):
                raise GovernanceError(
                    "github-response", "milestone has no usable number"
                )
            matches[title].append(item)
    result: dict[str, Mapping[str, Any] | None] = {}
    for title, items in matches.items():
        if len(items) > 1:
            raise GovernanceError(
                "github-milestone-duplicate",
                "required milestone identity is ambiguous",
            )
        result[title] = items[0] if items else None
    return result


def _issue_marker(unit: str, kind: str, identity: str) -> str:
    safe_identity = re.sub(r"[^A-Za-z0-9_.:-]", "-", identity)
    return f"<!-- release-governance:{unit}:{kind}:{safe_identity} -->"


def _release_issue_body(
    release: Mapping[str, Any], marker: str, plan_reference: str
) -> str:
    scenarios = release.get("scenarios")
    scenario_ids = (
        ", ".join(
            str(item.get("id"))
            for item in scenarios
            if isinstance(item, Mapping)
        )
        if isinstance(scenarios, list)
        else ""
    )
    return (
        f"{marker}\n\n"
        f"Release authority: `{plan_reference}`\n\n"
        f"- Version: `{release.get('version')}`\n"
        f"- Classification: `{release.get('classification')}`\n"
        f"- Scenarios: {scenario_ids or 'none'}\n"
    )


def _scenario_issue_body(
    scenario: Mapping[str, Any],
    release: Mapping[str, Any],
    marker: str,
    plan_reference: str,
) -> str:
    acceptance = scenario.get("acceptance")
    acceptance_lines = (
        "\n".join(f"- {item}" for item in acceptance)
        if isinstance(acceptance, list)
        else ""
    )
    evidence = scenario.get("evidence")
    evidence_lines = (
        "\n".join(f"- {item}" for item in evidence)
        if isinstance(evidence, list) and evidence
        else "- Pending"
    )
    return (
        f"{marker}\n\n"
        f"Release authority: `{plan_reference}`\n\n"
        f"Target: `{release.get('milestone')}`  \n"
        f"Scenario: `{scenario.get('id')}`  \n"
        f"Type: `{scenario.get('type')}`  \n"
        f"Risk: `{scenario.get('risk')}`\n\n"
        f"## Acceptance\n\n{acceptance_lines}\n\n"
        f"## Evidence\n\n{evidence_lines}\n"
    )


def _issue_labels(item: Mapping[str, Any]) -> set[str]:
    labels = item.get("labels")
    if not isinstance(labels, list):
        return set()
    result: set[str] = set()
    for label in labels:
        if isinstance(label, str):
            result.add(label)
        elif isinstance(label, Mapping) and isinstance(label.get("name"), str):
            result.add(label["name"])
    return result


def _issue_milestone(item: Mapping[str, Any]) -> str | None:
    milestone = item.get("milestone")
    if isinstance(milestone, Mapping):
        title = milestone.get("title")
        return title if isinstance(title, str) else None
    return milestone if isinstance(milestone, str) else None


def _preflight_issue_list(
    data: Any, repository: str
) -> list[Mapping[str, Any]]:
    if not isinstance(data, list):
        raise GovernanceError("github-response", "issue list has an invalid shape")
    result: list[Mapping[str, Any]] = []
    seen_numbers: set[int] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise GovernanceError("github-response", "issue list has an invalid shape")
        number = item.get("number")
        issue_reference = item.get("url")
        milestone = item.get("milestone")
        labels = item.get("labels")
        if (
            not isinstance(number, int)
            or number in seen_numbers
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("body"), str)
            or not isinstance(issue_reference, str)
            or _github_issue_number(issue_reference, repository) != number
            or not isinstance(item.get("state"), str)
            or not isinstance(labels, list)
            or any(
                not (
                    isinstance(label, str)
                    or (
                        isinstance(label, Mapping)
                        and isinstance(label.get("name"), str)
                    )
                )
                for label in labels
            )
            or not (
                milestone is None
                or isinstance(milestone, str)
                or (
                    isinstance(milestone, Mapping)
                    and isinstance(milestone.get("title"), str)
                )
            )
        ):
            raise GovernanceError("github-response", "issue list has an invalid shape")
        seen_numbers.add(number)
        result.append(item)
    return result


def _preflight_issue(
    repository: str,
    issues: Sequence[Mapping[str, Any]],
    *,
    configured_url: Any,
    marker: str,
    claimed_numbers: MutableMapping[int, str],
) -> Mapping[str, Any] | None:
    configured_number: int | None = None
    if configured_url is not None:
        configured_number = _github_issue_number(configured_url, repository)
        if configured_number is None:
            raise GovernanceError(
                "github-issue",
                "configured issue must belong to the release-plan repository",
            )
    existing: Mapping[str, Any] | None = None
    if configured_number is not None:
        existing = next(
            (item for item in issues if item.get("number") == configured_number),
            None,
        )
        if existing is None:
            raise GovernanceError(
                "github-issue", "configured issue was not found in the owning repository"
            )
    else:
        matches = [
            item
            for item in issues
            if isinstance(item.get("body"), str) and marker in item["body"]
        ]
        if len(matches) > 1:
            raise GovernanceError(
                "github-issue-duplicate", "multiple managed issues use one governance marker"
            )
        existing = matches[0] if matches else None
    if existing is None:
        return None

    number = existing.get("number")
    body = existing.get("body")
    if not isinstance(number, int) or not isinstance(body, str):
        raise GovernanceError("github-response", "managed issue has an invalid shape")
    managed_markers = re.findall(r"<!-- release-governance:[^>]+ -->", body)
    if managed_markers and marker not in managed_markers:
        raise GovernanceError(
            "github-issue-marker",
            "configured issue belongs to a different managed release item",
        )
    previous_marker = claimed_numbers.get(number)
    if previous_marker is not None and previous_marker != marker:
        raise GovernanceError(
            "github-issue-duplicate",
            "one GitHub issue cannot represent multiple release items",
        )
    claimed_numbers[number] = marker
    return existing


def _ensure_issue(
    client: GhClient,
    repository: str,
    *,
    configured_url: Any,
    marker: str,
    title: str,
    body: str,
    labels: Sequence[str],
    milestone: str,
    existing: Mapping[str, Any] | None,
) -> str | None:
    if existing is None:
        output = client.mutate_text(
            "issue.create",
            [
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                title,
                "--body",
                body,
                "--label",
                ",".join(labels),
                "--milestone",
                milestone,
            ],
        )
        return output.strip() if isinstance(output, str) else None

    current_url = existing.get("url")
    issue_ref = (
        str(existing.get("number"))
        if isinstance(existing.get("number"), int)
        else str(current_url)
    )
    managed = isinstance(existing.get("body"), str) and marker in existing["body"]
    required_labels = set(labels)
    arguments = ["issue", "edit", issue_ref, "--repo", repository]
    changed = False
    if managed and existing.get("title") != title:
        arguments.extend(["--title", title])
        changed = True
    if managed and existing.get("body") != body:
        arguments.extend(["--body", body])
        changed = True
    missing_labels = sorted(required_labels.difference(_issue_labels(existing)))
    if missing_labels:
        arguments.extend(["--add-label", ",".join(missing_labels)])
        changed = True
    if _issue_milestone(existing) != milestone:
        arguments.extend(["--milestone", milestone])
        changed = True
    if changed:
        client.mutate_text("issue.update", arguments)
    return current_url if isinstance(current_url, str) else (
        configured_url if isinstance(configured_url, str) else None
    )


def _field_map(data: Any) -> dict[str, Mapping[str, Any]]:
    fields = data.get("fields", []) if isinstance(data, Mapping) else data
    if not isinstance(fields, list):
        raise GovernanceError(
            "github-response", "project field list has an invalid shape"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for field in fields:
        if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
            raise GovernanceError(
                "github-response", "project field list has an invalid shape"
            )
        if field["name"] in result:
            raise GovernanceError(
                "github-project-field",
                "project field identity is ambiguous",
            )
        result[field["name"]] = field
    return result


def _validate_required_project_fields(
    fields: Mapping[str, Mapping[str, Any]],
) -> None:
    for name, data_type, options in FIELD_SPECS:
        field = fields.get(name)
        if field is None or not isinstance(field.get("id"), str):
            raise GovernanceError(
                "github-project-field",
                f"release portfolio is missing required field '{name}'",
            )
        actual_type = _field_data_type(field)
        if actual_type is not None and actual_type != data_type:
            raise GovernanceError(
                "github-project-field",
                f"project field '{name}' has an incompatible type",
            )
        if data_type == "SINGLE_SELECT":
            for option in options:
                if _select_option_id(field, option) is None:
                    raise GovernanceError(
                        "github-project-field",
                        f"project field '{name}' is missing required options",
                    )


def _select_option_id(field: Mapping[str, Any], name: str) -> str | None:
    options = field.get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if (
            isinstance(option, Mapping)
            and option.get("name") == name
            and isinstance(option.get("id"), str)
        ):
            return option["id"]
    return None


def _set_project_fields(
    client: GhClient,
    project_id: str,
    item_id: str,
    fields: Mapping[str, Mapping[str, Any]],
    values: Mapping[str, str | None],
) -> None:
    for field_name, value in values.items():
        if value is None:
            continue
        field = fields.get(field_name)
        if not isinstance(field, Mapping) or not isinstance(field.get("id"), str):
            continue
        arguments = [
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project_id,
            "--field-id",
            field["id"],
        ]
        data_type = _field_data_type(field)
        if data_type == "SINGLE_SELECT":
            option_id = _select_option_id(field, value)
            if option_id is None:
                continue
            arguments.extend(["--single-select-option-id", option_id])
        elif data_type == "DATE":
            arguments.extend(["--date", value])
        else:
            arguments.extend(["--text", value])
        client.mutate_text(f"project.field.set:{field_name}", arguments)


def _add_project_issue(
    client: GhClient,
    *,
    owner: str,
    project: Mapping[str, Any],
    project_items: MutableMapping[str, Mapping[str, Any]],
    fields: Mapping[str, Mapping[str, Any]],
    url: str | None,
    values: Mapping[str, str | None],
) -> None:
    if url is None:
        if not client.apply:
            client.operations.append("project.item.add")
        return
    project_number = project.get("number")
    project_id = project.get("id")
    if not isinstance(project_number, int) or not isinstance(project_id, str):
        raise GovernanceError("github-response", "project identity is incomplete")
    existing = project_items.get(url)
    if existing is None:
        created = client.mutate_json(
            "project.item.add",
            [
                "project",
                "item-add",
                str(project_number),
                "--owner",
                owner,
                "--url",
                url,
                "--format",
                "json",
            ],
        )
        if not client.apply:
            return
        if not isinstance(created, Mapping):
            raise GovernanceError(
                "github-response", "created project item response has an invalid shape"
            )
        existing = created
        project_items[url] = existing
    item_id = existing.get("id")
    if isinstance(item_id, str):
        _set_project_fields(client, project_id, item_id, fields, values)


def sync_github(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    project_owner: str,
    project_title: str = DEFAULT_PROJECT_TITLE,
    apply: bool = False,
    gh: GhClient | None = None,
    plan_reference: str = DEFAULT_PLAN_PATH,
    expected_repository: str | None = None,
) -> list[str]:
    """Idempotently synchronize release labels, milestones, issues, and Project."""

    root = Path(repository_root)
    _require_expected_repository(plan, expected_repository)
    _require_gh_apply_mode(gh, apply)
    report = verify_plan(root, plan)
    structural_errors = [
        finding
        for finding in report.errors
        if finding.code
        not in {
            "document-missing",
            "document-drift",
            "version-source-drift",
            "changelog-version",
            "ready-release-issue",
            "ready-scenario-issue",
        }
    ]
    if structural_errors:
        raise GovernanceError(
            "plan-invalid", "release plan must pass structural policy before synchronization"
        )
    repository = plan.get("repository")
    if not isinstance(repository, str):
        raise GovernanceError("plan-invalid", "release plan repository is invalid")
    client = gh or GhClient(apply=apply)
    active_units = [
        (unit_id, unit, unit_path)
        for unit_id, unit, unit_path in _units(plan)
        if isinstance(unit.get("nextRelease"), Mapping)
    ]
    project: Mapping[str, Any] | None = None
    project_items: dict[str, Mapping[str, Any]] = {}
    fields: dict[str, Mapping[str, Any]] = {}
    # This entire Project preflight intentionally happens before the first
    # remote mutation.  A release sync is not allowed to leave labels or issues
    # half-created when the required private portfolio cannot be maintained.
    if active_units:
        project = _find_project(client, project_owner, project_title)
        if project is None:
            raise GovernanceError(
                "github-project", "required release portfolio was not found"
            )
        if project.get("public") is not False:
            raise GovernanceError(
                "github-project-privacy",
                "release portfolio must be explicitly reported as private",
            )
        if (
            not isinstance(project.get("number"), int)
            or not isinstance(project.get("id"), str)
        ):
            raise GovernanceError(
                "github-response", "release portfolio identity is incomplete"
            )
        item_data = client.json(
            [
                "project",
                "item-list",
                str(project["number"]),
                "--owner",
                project_owner,
                "--limit",
                "1000",
                "--format",
                "json",
            ]
        )
        project_items = _project_items_by_url(item_data)
        field_data = client.json(
            [
                "project",
                "field-list",
                str(project["number"]),
                "--owner",
                project_owner,
                "--limit",
                "100",
                "--format",
                "json",
            ]
        )
        fields = _field_map(field_data)
        _validate_required_project_fields(fields)

    # Complete every remote read before the first mutation.  The mutation
    # phase below consumes only these snapshots; an interrupted later write is
    # recovered by the idempotent next run.
    label_data = client.json(
        [
            "label",
            "list",
            "--repo",
            repository,
            "--limit",
            "200",
            "--json",
            "name,color,description",
        ]
    )
    existing_labels = _labels_from_json(label_data)
    raw_issue_data = client.json(
        [
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,title,body,url,state,milestone,labels",
        ]
    )
    issue_data = _preflight_issue_list(raw_issue_data, repository)
    raw_milestone_data = client.json(
        ["api", f"repos/{repository}/milestones?state=all&per_page=100"]
    )
    existing_milestones = _preflight_milestones(
        raw_milestone_data, active_units
    )
    claimed_issue_numbers: dict[int, str] = {}
    existing_issues: dict[str, Mapping[str, Any] | None] = {}
    for unit_id, unit, _ in active_units:
        release = unit.get("nextRelease")
        if not isinstance(release, Mapping):
            continue
        unit_name = unit_id or repository.split("/", 1)[1]
        marker = _issue_marker(unit_name, "release", str(release.get("version")))
        existing_issues[marker] = _preflight_issue(
            repository,
            issue_data,
            configured_url=release.get("releaseIssue"),
            marker=marker,
            claimed_numbers=claimed_issue_numbers,
        )
        scenarios = release.get("scenarios")
        if not isinstance(scenarios, list):
            continue
        for scenario in scenarios:
            if not isinstance(scenario, Mapping):
                continue
            scenario_marker = _issue_marker(
                unit_name, "scenario", str(scenario.get("id"))
            )
            existing_issues[scenario_marker] = _preflight_issue(
                repository,
                issue_data,
                configured_url=scenario.get("issue"),
                marker=scenario_marker,
                claimed_numbers=claimed_issue_numbers,
            )

    _ensure_labels(client, repository, existing_labels)
    synchronized_milestones: set[str] = set()
    for unit_id, unit, _ in active_units:
        release = unit.get("nextRelease")
        if not isinstance(release, Mapping):
            continue
        milestone_title = str(release.get("milestone"))
        if milestone_title not in synchronized_milestones:
            _ensure_milestone(
                client,
                repository,
                release,
                plan_reference,
                existing_milestones.get(milestone_title),
            )
            synchronized_milestones.add(milestone_title)
        unit_name = unit_id or repository.split("/", 1)[1]
        marker = _issue_marker(unit_name, "release", str(release.get("version")))
        classification = str(release.get("classification"))
        release_status = str(release.get("status"))
        release_url = _ensure_issue(
            client,
            repository,
            configured_url=release.get("releaseIssue"),
            marker=marker,
            title=f"[Release] {release.get('milestone')}",
            body=_release_issue_body(release, marker, plan_reference),
            labels=(
                "type:release",
                f"semver:{classification}",
                f"release:{release_status}",
            ),
            milestone=str(release.get("milestone")),
            existing=existing_issues[marker],
        )
        if (
            client.apply
            and isinstance(release, MutableMapping)
            and isinstance(release_url, str)
        ):
            release["releaseIssue"] = release_url
        if project is not None:
            _add_project_issue(
                client,
                owner=project_owner,
                project=project,
                project_items=project_items,
                fields=fields,
                url=release_url,
                values={
                    "Target Version": str(release.get("version")),
                    "Release Class": classification,
                    "Scenario Type": "release",
                    "Readiness": release_status,
                    "Target Date": release.get("targetDate"),
                    "Risk": _highest_risk(release.get("scenarios")),
                    "Evidence": _release_evidence(release.get("scenarios")),
                    "Release Unit": unit_name,
                },
            )
        scenarios = release.get("scenarios")
        if not isinstance(scenarios, list):
            continue
        for scenario in scenarios:
            if not isinstance(scenario, Mapping):
                continue
            scenario_id = str(scenario.get("id"))
            scenario_marker = _issue_marker(unit_name, "scenario", scenario_id)
            scenario_type = str(scenario.get("type"))
            scenario_status = str(scenario.get("status"))
            scenario_url = _ensure_issue(
                client,
                repository,
                configured_url=scenario.get("issue"),
                marker=scenario_marker,
                title=f"[{scenario_id}] {scenario.get('title')}",
                body=_scenario_issue_body(
                    scenario, release, scenario_marker, plan_reference
                ),
                labels=(
                    "type:scenario",
                    f"type:{scenario_type}",
                    f"semver:{classification}",
                    f"scenario:{scenario_status}",
                ),
                milestone=str(release.get("milestone")),
                existing=existing_issues[scenario_marker],
            )
            if (
                client.apply
                and isinstance(scenario, MutableMapping)
                and isinstance(scenario_url, str)
            ):
                scenario["issue"] = scenario_url
            if project is not None:
                evidence = scenario.get("evidence")
                _add_project_issue(
                    client,
                    owner=project_owner,
                    project=project,
                    project_items=project_items,
                    fields=fields,
                    url=scenario_url,
                    values={
                        "Target Version": str(release.get("version")),
                        "Release Class": classification,
                        "Scenario Type": scenario_type,
                        "Readiness": scenario_status,
                        "Target Date": release.get("targetDate"),
                        "Risk": str(scenario.get("risk")),
                        "Evidence": (
                            "\n".join(evidence)
                            if isinstance(evidence, list) and evidence
                            else None
                        ),
                        "Release Unit": unit_name,
                    },
                )
    return client.operations


def _highest_risk(scenarios: Any) -> str | None:
    if not isinstance(scenarios, list):
        return None
    order = {"low": 0, "medium": 1, "high": 2}
    risks = [
        scenario.get("risk")
        for scenario in scenarios
        if isinstance(scenario, Mapping) and scenario.get("risk") in order
    ]
    return max(risks, key=order.__getitem__) if risks else None


def _release_evidence(scenarios: Any) -> str | None:
    if not isinstance(scenarios, list):
        return None
    evidence: list[str] = []
    for scenario in scenarios:
        if isinstance(scenario, Mapping) and isinstance(scenario.get("evidence"), list):
            evidence.extend(str(item) for item in scenario["evidence"])
    return "\n".join(evidence) if evidence else None


def _select_finalize_unit(
    plan: MutableMapping[str, Any], component: str | None
) -> tuple[MutableMapping[str, Any], str]:
    profile = plan.get("profile")
    if profile == "component-semver":
        if not component:
            raise GovernanceError(
                "component-required",
                "component-semver finalization requires --component",
            )
        components = plan.get("components")
        if not isinstance(components, list):
            raise GovernanceError("plan-invalid", "components are unavailable")
        matches = [
            item
            for item in components
            if isinstance(item, MutableMapping) and item.get("id") == component
        ]
        if len(matches) != 1:
            raise GovernanceError(
                "component-not-found", "requested release component was not found"
            )
        return matches[0], component
    if component:
        raise GovernanceError(
            "component-unexpected", "--component is only valid for component-semver"
        )
    return plan, str(plan.get("repository", "repository")).split("/")[-1]


def _validate_release_url(url: str, repository: str, tag: str) -> None:
    if not _is_https_url(url):
        raise GovernanceError("release-url", "release URL must be an HTTPS URL")
    parsed = urllib.parse.urlparse(url)
    expected_path = f"/{repository}/releases/tag/{urllib.parse.quote(tag, safe='')}"
    if (
        parsed.netloc.lower() != "github.com"
        or urllib.parse.unquote(parsed.path) != urllib.parse.unquote(expected_path)
        or parsed.query
        or parsed.fragment
    ):
        raise GovernanceError(
            "release-url",
            "release URL must identify the matching owning-repository GitHub Release",
        )


def _verify_github_release(
    gh: GhClient,
    repository: str,
    tag: str,
    release_url: str,
) -> None:
    encoded = urllib.parse.quote(tag, safe="")
    gh.json(["api", f"repos/{repository}/git/ref/tags/{encoded}"])
    release = gh.json(["api", f"repos/{repository}/releases/tags/{encoded}"])
    if not isinstance(release, Mapping) or release.get("html_url") != release_url:
        raise GovernanceError(
            "github-release", "GitHub Release does not match the supplied release URL"
        )


def finalize_plan(
    repository_root: Path | str,
    plan: Mapping[str, Any],
    *,
    release_url: str,
    released_at: str,
    component: str | None = None,
    github: bool = False,
    gh: GhClient | None = None,
    expected_repository: str | None = None,
) -> dict[str, Any]:
    """Return a finalized plan copy; no filesystem mutation occurs here."""

    root = Path(repository_root)
    _require_expected_repository(plan, expected_repository)
    if gh is not None:
        _require_gh_apply_mode(gh, False)
    result = copy.deepcopy(dict(plan))
    report = verify_plan(root, result, check_document=False)
    if report.errors:
        first = report.errors[0]
        raise GovernanceError("plan-invalid", first.message, first.path)
    unit, unit_id = _select_finalize_unit(result, component)
    release = unit.get("nextRelease")
    if not isinstance(release, MutableMapping):
        raise GovernanceError("no-next-release", "there is no release to finalize")
    if release.get("status") != "ready":
        raise GovernanceError("release-not-ready", "only a ready release can be finalized")
    version = release.get("version")
    if not isinstance(version, str):
        raise GovernanceError("plan-invalid", "release version is invalid")
    repository = result.get("repository")
    if not isinstance(repository, str):
        raise GovernanceError("plan-invalid", "repository is invalid")
    tag = _release_tag(result.get("profile"), unit_id if component else None, version)
    _validate_release_url(release_url, repository, tag)
    try:
        _parse_datetime(released_at)
    except ValueError as exc:
        raise GovernanceError(
            "released-at", "released-at must be an RFC 3339 date-time with timezone"
        ) from exc
    history = unit.get("releases")
    if not isinstance(history, list):
        raise GovernanceError("plan-invalid", "release history is invalid")
    if any(
        isinstance(item, Mapping) and item.get("version") == version for item in history
    ):
        raise GovernanceError(
            "release-duplicate", "release version is already present in history"
        )
    if github:
        _verify_github_release(gh or GhClient(apply=False), repository, tag, release_url)
    record = copy.deepcopy(dict(release))
    record["releasedAt"] = released_at
    record["releaseUrl"] = release_url
    history.append(record)
    unit["currentVersion"] = version
    unit["nextRelease"] = None
    return result


def _plan_json(plan: Mapping[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2) + "\n"


def _print_report(report: VerificationReport, *, as_json: bool = False) -> None:
    if as_json:
        payload = {
            "ok": report.ok,
            "errors": [
                dataclasses.asdict(finding) for finding in report.errors
            ],
            "warnings": [
                dataclasses.asdict(finding) for finding in report.warnings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if report.ok:
        print("OK release governance verified")
    for finding in report.findings:
        location = f" {finding.path}" if finding.path else ""
        print(
            f"{finding.severity.upper()} [{finding.code}]{location}: {finding.message}"
        )


def _print_operations(operations: Sequence[str], *, applied: bool) -> None:
    action = "applied" if applied else "would apply"
    if not operations:
        print("OK no changes required")
        return
    print(f"OK {action} {len(operations)} idempotent operation(s)")
    for operation in operations:
        print(f"- {operation}")


def _common_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--plan", default=DEFAULT_PLAN_PATH)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and synchronize LicoLand repository release plans"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="verify the repository release plan")
    _common_plan_arguments(verify)
    verify.add_argument("--github", action="store_true")
    verify.add_argument("--expected-repository")
    verify.add_argument("--tag")
    verify.add_argument("--json", action="store_true", dest="as_json")

    render = subparsers.add_parser("render", help="render docs/releases/README.md")
    _common_plan_arguments(render)
    render.add_argument("--apply", action="store_true")
    render.add_argument("--check", action="store_true")

    bootstrap = subparsers.add_parser(
        "bootstrap-project", help="ensure the private organization release portfolio"
    )
    bootstrap.add_argument(
        "--project-owner", "--organization", "--owner", dest="project_owner", default="LicoLand"
    )
    bootstrap.add_argument("--project-title", default=DEFAULT_PROJECT_TITLE)
    bootstrap.add_argument("--apply", action="store_true")

    sync = subparsers.add_parser(
        "sync-github", help="synchronize repository GitHub release objects"
    )
    _common_plan_arguments(sync)
    sync.add_argument("--project-owner", default="LicoLand")
    sync.add_argument("--project-title", default=DEFAULT_PROJECT_TITLE)
    sync.add_argument("--expected-repository")
    sync.add_argument("--apply", action="store_true")

    finalize = subparsers.add_parser(
        "finalize", help="archive a published ready release in the repository plan"
    )
    _common_plan_arguments(finalize)
    finalize.add_argument("--release-url", required=True)
    finalize.add_argument("--released-at", required=True)
    finalize.add_argument("--component", "--release-unit", dest="component")
    finalize.add_argument("--expected-repository")
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
    report = _validate_plan_shape(plan)
    _validate_profile_contract(report, plan)
    _validate_release_contracts(report, plan, root)
    if report.errors:
        _print_report(report)
        return 1
    target = _safe_path(root, DOCUMENT_PATH)
    rendered = render_release_document(plan)
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except (OSError, UnicodeError) as exc:
        raise GovernanceError(
            "document-unreadable",
            "generated release document cannot be read",
            _display_path(root, target),
        ) from exc
    if current == rendered:
        print("OK generated release document is current")
        return 0
    if args.apply:
        _atomic_write(target, rendered)
        print(f"OK updated {_display_path(root, target)}")
        return 0
    print(f"DRY-RUN would update {_display_path(root, target)}")
    return 1 if args.check else 0


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
    plan, plan_path = load_plan(root, args.plan)
    if args.apply and plan_path.suffix.lower() != ".json":
        raise GovernanceError(
            "plan-format",
            "sync-github --apply requires a JSON release plan",
            _display_path(root, plan_path),
        )
    operations = sync_github(
        root,
        plan,
        project_owner=args.project_owner,
        project_title=args.project_title,
        apply=args.apply,
        plan_reference=_display_path(root, plan_path),
        expected_repository=args.expected_repository,
    )
    if args.apply:
        _atomic_write(plan_path, _plan_json(plan))
        _atomic_write(
            _safe_path(root, DOCUMENT_PATH),
            render_release_document(plan),
        )
    _print_operations(operations, applied=args.apply)
    return 0


def _command_finalize(args: argparse.Namespace) -> int:
    root = Path(args.repository_root)
    plan, plan_path = load_plan(root, args.plan)
    if plan_path.suffix.lower() != ".json":
        raise GovernanceError(
            "plan-format", "finalize requires a JSON release plan", _display_path(root, plan_path)
        )
    finalized = finalize_plan(
        root,
        plan,
        release_url=args.release_url,
        released_at=args.released_at,
        component=args.component,
        github=args.github,
        expected_repository=args.expected_repository,
    )
    plan_content = _plan_json(finalized)
    document_content = render_release_document(finalized)
    document_path = _safe_path(root, DOCUMENT_PATH)
    if not args.apply:
        print(
            "DRY-RUN would archive the ready release and render "
            f"{_display_path(root, document_path)}"
        )
        return 0
    _atomic_write(plan_path, plan_content)
    _atomic_write(document_path, document_content)
    print("OK finalized release history and regenerated release document")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            return _command_verify(args)
        if args.command == "render":
            return _command_render(args)
        if args.command == "bootstrap-project":
            return _command_bootstrap(args)
        if args.command == "sync-github":
            return _command_sync(args)
        if args.command == "finalize":
            return _command_finalize(args)
        parser.error("unsupported command")
    except GovernanceError as exc:
        location = f" {exc.path}" if exc.path else ""
        print(f"ERROR [{exc.code}]{location}: {exc.message}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
