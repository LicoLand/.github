## Feature contract

- Feature ID:
- Target release:
- Release plan: `docs/releases/plan.json`
- Depends on:
- Owning Lico-Dev skill or workflow:

Use one Draft pull request for one independently acceptable feature. Keep it
Draft while implementation is active; mark it ready for review only after its
acceptance checks pass. Issues are optional references, not release authority.

## Release impact

- [ ] No product-version impact
- [ ] Patch: backward-compatible fix only
- [ ] Minor: new backward-compatible capability
- [ ] Major: breaking feature with migration acceptance
- [ ] Initial or stabilization release

## Acceptance

- Sanitized task receipts:
- Repository-owned checks:
- General-Auditor contextual review completed locally (do not attach raw reports or source matches):

## Review checklist

- [ ] This pull request implements one feature ID from the owning release plan.
- [ ] Its base branch matches the release plan's `integrationBranch`.
- [ ] The release classification matches the structured repository plan.
- [ ] Generated release documentation is current.
- [ ] Acceptance evidence is synthetic or redacted.
- [ ] After merge, a plan-only pull request will record `accepted` status and reviewed evidence.
- [ ] General-Auditor advisory signals received contextual review from a local Agent; no signal was treated as an automatic content block.
- [ ] Packaging, GitHub Release, and external distribution claims remain separate.
