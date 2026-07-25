# Organization Version Governance

This document is the organization-wide authority for planning and releasing
versioned LicoLand projects. Product behavior, acceptance criteria, and release
evidence remain owned by the repository that implements them.

## Governing model

Every release fact has one owner:

| Fact | Authority |
| --- | --- |
| Organization portfolio, cross-repository status, target dates, and risk | The private organization GitHub Project named `LicoLand Release Portfolio` |
| A repository release boundary and completion percentage | A repository milestone named `vMAJOR.MINOR.PATCH` |
| A required capability, breaking change, or fix | A real issue in the owning repository |
| Version scope, acceptance criteria, evidence, and release history | `docs/releases/plan.json` in the owning repository |
| Human-readable repository release status | Generated `docs/releases/README.md` |
| Implementation | Pull requests that close the owning issues |
| Published result | An immutable release-unit tag and GitHub Release |

GitHub Project items, milestone descriptions, generated documentation, and
release notes link to the owning repository plan. They must not restate a
different set of acceptance criteria.

## Release profiles

Every first-party repository declares exactly one profile.

| Profile | Use |
| --- | --- |
| `semver` | One independently released product or package |
| `component-semver` | Independently released components in one repository |
| `continuous-site` | A continuously delivered site that does not own product versions |
| `inactive` | A repository that cannot publish until it owns an implemented release unit |
| `governance` | Organization metadata and shared automation, not a product release |

The last three profiles reject stable product-version tags. A site deployment,
governance change, or empty marketplace must not create a product version.

## Version classification

Stable releases use `MAJOR.MINOR.PATCH`. LicoLand applies the same rules while
the major version is zero; `0.x` is not an exception.

| Classification | Required transition | Required scenarios |
| --- | --- | --- |
| `patch` | `X.Y.Z` to `X.Y.(Z+1)` | One or more fixes; no capability or breaking scenario |
| `minor` | `X.Y.Z` to `X.(Y+1).0` | At least one new, independently acceptable capability scenario; fixes may be included |
| `major` | `X.Y.Z` to `(X+1).0.0` | At least one breaking scenario with migration acceptance; capabilities and fixes may be included |
| `initial` | No released version to `0.1.0` | At least one accepted capability scenario |
| `stabilization` | A prerelease to the same stable core, or `0.y.z` to `1.0.0` | At least one accepted capability scenario and no hidden version skip |

Transitions are sequential. A patch, minor, or major component cannot skip its
next numeric value. Minor and major releases reset lower components to zero.
A bug fix by itself therefore advances `0.1.0` to `0.1.1`, then `0.1.2`; it
cannot justify `0.2.0`.

An existing prerelease baseline may only stabilize to its exact stable core.
This workflow publishes stable tags; candidate builds are CI artifacts, not
additional releases or milestones. A repository-wide release uses
`vMAJOR.MINOR.PATCH`. An independently versioned component uses
`COMPONENT-vMAJOR.MINOR.PATCH`, so two components may own the same version
without sharing a tag.

## Scenario contract

A version plan contains one entry per independently acceptable scenario:

- a stable scenario identifier;
- one of `capability`, `breaking`, or `fix`;
- a concise outcome title;
- a real owning-repository issue;
- explicit acceptance statements;
- a risk level;
- lifecycle status; and
- evidence links when accepted.

The lifecycle is `planned` → `active` → `accepted`. `blocked` is an explicit
side state. A release moves through `planned` → `active` → `ready`, with
`blocked` available when necessary.

`ready` means:

- the version transition and scenario mix satisfy this policy;
- every scenario is accepted and has evidence;
- every scenario issue and the release issue are closed;
- the repository milestone is closed with no open items;
- the version source and changelog name the target version;
- every release item is present in the organization Project; and
- the repository's own build, security, packaging, and acceptance checks pass.

The organization gate does not replace a product repository's release checks.

## GitHub Project

`LicoLand Release Portfolio` is private because it can contain items from
non-public repositories. The bootstrap tool creates these fields:

| Field | Type |
| --- | --- |
| `Target Version` | Text |
| `Release Class` | Single select |
| `Scenario Type` | Single select |
| `Readiness` | Single select |
| `Target Date` | Date |
| `Risk` | Single select |
| `Evidence` | Text |
| `Release Unit` | Text |

Repository and milestone remain GitHub system fields. Project items must be
real issues, not draft items. The shared tool adds or updates the release issue
and all scenario issues idempotently.

Project automation uses a credential with organization Projects read/write
permission and repository Issues read/write permission; a narrowly scoped
GitHub App installation token is preferred. The credential is exposed only as
the protected `release-portfolio` environment secret
`RELEASE_PROJECT_TOKEN`. Restrict that environment to the exact trusted base
branch.
Ordinary `pull_request` and tag jobs never receive it; the
`pull_request_target` portfolio job reads the proposed checkout strictly as
data and executes only the immutable organization verifier. Do not put a
personal access token, App key, installation identifier, or private project
metadata in source.

Create or audit that fail-closed environment before provisioning its secret:

```bash
python3 tools/release_governance.py bootstrap-environment \
  --repository LicoLand/<repo> \
  --branch <default-branch>

python3 tools/release_governance.py bootstrap-environment \
  --repository LicoLand/<repo> \
  --branch <default-branch> \
  --apply
```

The first command is read-only. The tool accepts exactly one branch policy and
never reads, creates, lists, or changes environment secrets.

## Repository files

Each repository carries:

- `docs/releases/plan.json`, the structured release authority;
- `docs/releases/README.md`, generated from that plan;
- `tools/release/verify-version-governance`, a thin wrapper pinned to an
  immutable organization-tool revision and digest; and
- `.github/workflows/version-governance.yml`, the credential-free local and tag
  gate pinned to the same reusable workflow revision; and
- `.github/workflows/version-governance-portfolio.yml`, the trusted
  `pull_request_target` and default-branch `workflow_run` checks that can enter
  the protected `release-portfolio` environment. The latter repeats remote
  readiness verification after the credential-free tag check succeeds.

Edit the plan, then regenerate and verify:

```bash
tools/release/verify-version-governance render --apply
tools/release/verify-version-governance verify
```

Synchronize GitHub only after reviewing the dry run:

```bash
python3 tools/release_governance.py sync-github \
  --repository-root <repo-root> \
  --plan docs/releases/plan.json \
  --expected-repository LicoLand/<repo> \
  --project-owner LicoLand \
  --project-title "LicoLand Release Portfolio"

python3 tools/release_governance.py sync-github \
  --repository-root <repo-root> \
  --plan docs/releases/plan.json \
  --expected-repository LicoLand/<repo> \
  --project-owner LicoLand \
  --project-title "LicoLand Release Portfolio" \
  --apply
```

The first command is read-only. `--apply` is required for every GitHub write.

## Release lifecycle

1. Add the target version and scenarios to the owning repository plan.
2. Render the repository documentation and run the local verifier.
3. Run `sync-github` to create or update labels, the milestone, the release
   issue, scenario issues, and Project items.
4. Implement each scenario through a pull request that closes its issue.
5. Record reviewed evidence in the plan and mark accepted scenarios.
6. In the release pull request, set the release to `ready`, update the version
   source and changelog, and pass both the credential-free contract gate and
   the trusted portfolio gate.
7. Close the release issue and milestone, then create the matching tag.
8. Treat the pushed tag as a release candidate until both its credential-free
   check and the subsequent default-branch portfolio check succeed.
9. Publish the GitHub Release only after both tag checks succeed.
10. Finalize the plan so the released contract enters immutable repository
   history and the next release returns to an unplanned state.

Store/channel publication, signing identities, notarization, and third-party
distribution remain separate from GitHub Release readiness.
