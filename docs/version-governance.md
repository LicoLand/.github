# Organization Version Governance

This document defines the organization-wide contract for planning and
releasing versioned LicoLand repositories. Product behavior, acceptance, and
evidence remain owned by the repository that implements them.

## Independent repository versions

There is no organization-wide product version or synchronized release train.
Each product repository or independently versioned component owns its current
version, next release, feature contracts, tag, and GitHub Release.

The organization `.github` repository supplies the common schema, pull-request
template, verifier, Project projection tool, and reusable workflow. The
organization Project is an aggregate view of repository plans; it is never a
release authority.

## Governing model

Every fact has one owner:

| Fact | Authority |
| --- | --- |
| Version scope, feature IDs, dependencies, acceptance, evidence, and history | `docs/releases/plan.json` |
| Human-readable release status | Generated `docs/releases/README.md` |
| Implementation | One Draft pull request per feature, targeting the release's `integrationBranch` |
| Cross-repository ordering | Canonical `Owner/Repository:feature-id` references in `dependsOn` |
| Portfolio status and progress | A projection in `LicoLand Release Portfolio` |
| Published result | An immutable release-unit tag and GitHub Release |

Issues remain available for optional defect reports and discussion. They do
not represent a planned release, feature, dependency, completion percentage,
or release gate. Milestones are not part of version governance.

## Release profiles

Every first-party repository declares exactly one profile:

| Profile | Use |
| --- | --- |
| `semver` | One independently released product or package |
| `component-semver` | Independently released components in one repository |
| `continuous-site` | A continuously delivered site without product versions |
| `inactive` | A repository that cannot currently publish |
| `governance` | Organization metadata and shared automation |

The last three profiles reject stable product-version tags.

## Version classification

Stable releases use `MAJOR.MINOR.PATCH`. The same rules apply while the major
version is zero.

| Classification | Required transition | Required features |
| --- | --- | --- |
| `patch` | `X.Y.Z` to `X.Y.(Z+1)` | One or more fixes only |
| `minor` | `X.Y.Z` to `X.(Y+1).0` | At least one capability; no breaking feature |
| `major` | `X.Y.Z` to `(X+1).0.0` | At least one breaking feature with migration acceptance |
| `initial` | No release to `0.1.0` | At least one capability |
| `stabilization` | A prerelease to its stable core, or `0.y.z` to `1.0.0` | At least one capability |

Transitions are sequential. Minor and major releases reset lower components to
zero. A fix-only release therefore advances `0.1.0` to `0.1.1`; it cannot
justify `0.2.0`.

Repository tags use `vMAJOR.MINOR.PATCH`. Component tags use
`COMPONENT-vMAJOR.MINOR.PATCH`.

## Feature contract

Schema version 2 contains one entry per independently acceptable feature:

- a repository-global, stable lowercase slug ID;
- one of `capability`, `breaking`, or `fix`;
- a concise outcome title;
- lifecycle status;
- an owning-repository pull request when development has started;
- canonical dependency references;
- risk and observable acceptance statements; and
- reviewed, sanitized evidence when accepted.

Feature IDs remain globally unique across the repository's root, components,
next releases, and archived releases. A dependency always uses the full form
`Owner/Repository:feature-id`, including for a dependency in the same
repository. This makes a reference stable and unambiguous across independently
versioned components.

The feature lifecycle is:

1. `planned`: the contract is accepted and `pullRequest` is `null`.
2. `active`: a Draft or review-ready implementation pull request is open.
3. `blocked`: a dependency or explicit release blocker prevents progress; the
   pull request may be absent or open.
4. `accepted`: the implementation pull request is merged to the declared
   `integrationBranch` and the plan records reviewed evidence.

A feature cannot mark itself accepted inside its own implementation pull
request: that pull request is not merged yet. After merge, use a small
plan-only pull request to record `accepted` and its evidence.

The verifier rejects duplicate IDs or pull requests, missing local
dependencies, self-dependencies, dependency cycles, and an accepted feature
whose dependency is not accepted. Remote verification reads cross-repository
plans from their default branches as untrusted bounded JSON and applies the
same rules.

## Release readiness

A release moves through `planned`, `active`, `blocked`, and `ready`. `ready`
means:

- its version transition and feature mix satisfy this policy;
- it has no unresolved release blocker;
- every feature is accepted with reviewed evidence;
- every implementation pull request is merged to its release's
  `integrationBranch`;
- every transitive dependency exists and is accepted;
- the version source and changelog name the target version;
- repository-owned build, security, packaging, and acceptance checks pass; and
- the independent Lico-Auditor final gate passes.

Project availability, Project fields, Project views, projection lag, Issues,
and Milestones cannot approve or block a release.

## Draft pull-request implementation

Use this implementation sequence:

1. Merge a plan-only pull request that adds a `planned` feature with a stable
   ID and `pullRequest: null`.
2. Create a temporary branch and immediately open one Draft pull request to
   the release's `integrationBranch`.
3. In that Draft pull request, set the feature to `active` and record its PR
   URL. Continue implementation and targeted verification on the same PR.
4. When acceptance checks pass, mark the PR ready for review.
5. Pass repository gates and Lico-Auditor, then merge to the declared branch.
6. Merge a plan-only acceptance PR that records evidence and changes the
   feature to `accepted`.

Do not combine unrelated feature IDs in one implementation pull request.
Repositories using the canonical promotion flow set `integrationBranch` to
`nightly` and then promote `nightly` → `stable` → `release`; repositories with
a different maintained branch topology declare their actual integration branch
instead.

## Project projection

`LicoLand Release Portfolio` is an operator-managed projection. `sync-project`
creates one Draft Project item for each release summary and for each planned
feature without a pull request. When a feature gains a pull request, the tool
adds that PR and archives the superseded Draft Item.

The projection records the configured Project fields:

- `Status`, `Item type`, `Feature ID`, and `Owner repository`;
- `Feature type`, `Target version`, `Release class`, and `Risk`;
- `Depends on`, `Dependency state`, and `PR stage`;
- `Readiness`, `Gate progress`, and `Evidence`; and
- `Release unit`, `Plan revision`, and `Sync state`.

GitHub's system `Target date` and `Start date` fields remain system-owned.
The tool does not create or edit Project views. It marks retired managed Draft
Items as `Orphan`; a Project operator may archive or delete them separately.

Bootstrap or inspect the Project fields:

```bash
python3 tools/release_governance.py bootstrap-project \
  --project-owner LicoLand \
  --project-title "LicoLand Release Portfolio"
```

Add `--apply` only after reviewing the dry run.

Project a repository plan:

```bash
python3 tools/release_governance.py sync-project \
  --repository-root <repo-root> \
  --plan docs/releases/plan.json \
  --expected-repository LicoLand/<repo> \
  --project-owner LicoLand \
  --project-title "LicoLand Release Portfolio"
```

Again, `--apply` is required for writes. Project synchronization uses an
already authenticated operator CLI session and never stores its credential in
Actions.

## Required repository files

Each repository carries:

- `docs/releases/plan.json`, the structured authority;
- `docs/releases/README.md`, generated from that plan;
- `tools/release/verify-version-governance`, a thin wrapper pinned to an
  immutable organization verifier revision and digest;
- `.github/workflows/version-governance.yml`, the read-only plan, PR, and tag
  gate pinned to the same revision; and
- `.github/workflows/lico-auditor-release-gate.yml`, the independent audit
  caller.

The reusable gate needs only `contents: read` and `pull-requests: read`.
It uses the repository-provided GitHub token. Cross-repository dependencies in
private repositories require an explicitly provisioned read-only GitHub App
token; without one, remote dependency verification fails closed.

Edit, render, and verify:

```bash
tools/release/verify-version-governance render --apply
tools/release/verify-version-governance verify
```

## Publication boundary

After a ready release passes repository acceptance and Lico-Auditor, create its
matching tag. Treat the tag as a candidate until the tag governance and
full-history audit checks pass. Publish a GitHub Release only after applicable
tag checks succeed, then finalize the plan so the release enters immutable
history.

Development, verification, packaging, GitHub Release, signing, and every
external store or distribution channel remain separate claims.
