# Organization Version Governance

This document is the organization-wide authority for planning and releasing
versioned LicoLand projects. Product behavior, acceptance criteria, and release
evidence remain owned by the repository that implements them.

## Independent repository versions

There is no organization-wide product version, synchronized release train, or
shared release scope. Each product repository or independently versioned
component owns its current version, next release, scenarios, milestone, tag,
and GitHub Release. For example, one repository may publish `0.2.0` while
another remains at `0.1.4`.

The organization `.github` repository supplies only the common schema, issue
and pull-request templates, verifier, and reusable workflow callers. The
private organization Project is an aggregate portfolio: its `Target Version`
field records the owning repository item's version and never creates an
organization version. `governance`, `continuous-site`, and `inactive` profiles
do not become product versions merely because they use the common template.

For interactive adoption, GitHub exposes one native Actions starter named
`LicoLand Repository Release Governance`. It combines the credential-free
version-contract job and the independent Lico-Auditor job, and replaces
`$default-branch` with the adopting repository's default branch. Automated
rollout may use the equivalent files under `templates/repository/`.

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
- the repository's own build, security, packaging, and acceptance checks pass;
  and
- the independent Lico-Auditor final gate passes.

The organization version-contract gate does not replace a product repository's
release checks or the independent audit.

## Agent implementation and independent audit

Lico-Dev and Lico-Auditor have deliberately separate authorities:

1. Lico-Dev's `$lico-release-engineering-workflow` reads the owning
   `docs/releases/plan.json`, routes the scenario to its canonical repository
   owner, implements one independently acceptable closure, and produces
   sanitized acceptance receipts.
2. The organization template standardizes the version contract and verifies
   that the declared transition, scenarios, issues, milestone, evidence, and
   release state agree.
3. Lico-Auditor independently audits the candidate change and, for a stable tag
   or explicit release-readiness dispatch, all reachable content history.

Lico-Dev cannot approve its own final audit. The trusted repository caller
passes no repository, ref, profile, or secret input to Lico-Auditor. The
Auditor derives those facts from the GitHub event, verifies its canonical
`only` source, treats the target checkout as data, and never executes target
repository code.

Implementation receipts, version-contract verification, repository acceptance,
and the Auditor answer different questions. None can substitute for another,
and no administrator bypass may turn a failed claim into a release.

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

Interactive project bootstrap and synchronization use an operator credential
with organization Projects read/write permission and repository Issues
read/write permission. Never store this write-capable credential in Actions.

The protected `release-portfolio` environment secret
`RELEASE_PROJECT_TOKEN` is a separate verifier credential with only
organization Projects read permission and repository Issues read permission.
A short-lived GitHub App installation token is preferred; a narrowly scoped
fine-grained token is acceptable when token minting happens outside the
workflow. Restrict that environment to the exact trusted base branch.

Ordinary `pull_request` and tag jobs never receive the verifier credential; the
`pull_request_target` portfolio job reads the proposed checkout strictly as
data and executes only the immutable organization verifier. Do not put either
credential, an App private key, an installation identifier, or private project
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

## Branch enforcement

Every first-party repository must have an active repository ruleset named
`LicoLand version governance`. It targets only the repository's default branch
and strictly requires the exact check context
`version-governance / version-governance`. Keep this rule separate from
repository-specific audit, review, linear-history, and deletion rules so adding
version governance never rewrites or weakens an existing protection.

Bootstrap enforcement in this order:

1. Merge the pinned repository caller workflow.
2. Dispatch `version-governance.yml` once on the default branch and confirm the
   exact required check succeeds.
3. Create the active ruleset with no bypass actors.
4. Read the ruleset back and confirm its branch, context, and strict mode.

Install `.github/workflows/lico-auditor-release-gate.yml` separately. After its
default-branch candidate check succeeds, require the exact additional context
`lico-auditor / final-gate` with no bypass actors. Do not activate that required
context before the caller exists and has a successful run. Before a stable tag,
an explicit dispatch of the same workflow must also pass its full-history
phase.

Do not require the trusted Portfolio context until the private Project and its
protected environment credential are provisioned. Once available, the
additional pull-request context is
`pull-request-portfolio / version-governance-portfolio`. A release must never
use an administrator bypass to evade either required context.

## Repository files

Each repository carries:

- `docs/releases/plan.json`, the structured release authority;
- `docs/releases/README.md`, generated from that plan;
- `tools/release/verify-version-governance`, a thin wrapper pinned to an
  immutable organization-tool revision and digest; and
- `.github/workflows/version-governance.yml`, the credential-free local and tag
  gate pinned to the same reusable workflow revision;
- `.github/workflows/lico-auditor-release-gate.yml`, the input-free trusted
  caller for Lico-Auditor candidate and full-history final gates; and
- `.github/workflows/version-governance-portfolio.yml`, the trusted
  `pull_request_target` and default-branch `workflow_run` checks that can enter
  the protected `release-portfolio` environment. The latter repeats remote
  readiness verification after the credential-free tag check succeeds.

The native organization starter lives at
`workflow-templates/licoland-repository-release-governance.yml` with its
matching `.properties.json` metadata file. It is the single GitHub UI entry for
the version-contract and Auditor baseline. The Portfolio caller remains
separate because it must not exist until its protected read-only credential is
provisioned.

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
2. Invoke Lico-Dev's `$lico-release-engineering-workflow`; bind the declared
   release contract, render the repository documentation, and run the local
   verifier before implementation.
3. Run `sync-github` to create or update labels, the milestone, the release
   issue, scenario issues, and Project items.
4. Implement one independently acceptable scenario at a time through the
   canonical Lico-Dev owner workflow and a pull request that closes its issue.
5. Record reviewed, sanitized Lico-Dev receipts in the plan and mark accepted
   scenarios.
6. In the release pull request, set the release to `ready`, update the version
   source and changelog, and pass repository acceptance, the credential-free
   contract gate, the Lico-Auditor candidate gate, and the trusted portfolio
   gate when provisioned.
7. Explicitly dispatch `lico-auditor-release-gate.yml` and require its
   full-history phase to pass. Then close the release issue and milestone and
   create the matching tag.
8. Treat the pushed tag as a release candidate until its credential-free
   contract check, Lico-Auditor full-history gate, and subsequent default-branch
   portfolio check succeed.
9. Publish the GitHub Release only after every applicable tag check succeeds.
10. Finalize the plan so the released contract enters immutable repository
   history and the next release returns to an unplanned state.

Store/channel publication, signing identities, notarization, and third-party
distribution remain separate from GitHub Release readiness.
