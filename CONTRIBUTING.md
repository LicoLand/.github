# Contributing to LicoLand

Use the repository that owns the affected capability. Cross-project proposals
should still name one canonical authority and describe every consumer that
would need to adopt the change.

## Where to contribute

| Topic | Repository |
| --- | --- |
| Desktop, mobile, native client, key custody, or endpoint encryption | [LicoUp](https://github.com/LicoLand/LicoUp) |
| Federation protocol, governance strategy, compatibility, or certification rules | [Fabrigent](https://github.com/LicoLand/Fabrigent) |
| Relay, mailbox, lease, acknowledgement, quota, or cleanup | [BadTower](https://github.com/LicoLand/BadTower) |
| Independent audit policy or evidence contract | [Lico-Auditor](https://github.com/LicoLand/Lico-Auditor) |

## Make proposals concrete

Describe:

- the capability and repository that should own it;
- the public contract used by other repositories;
- required permissions and protected effects;
- expected failure and recovery behavior;
- the smallest verification that proves the change.

Do not post credentials, private endpoints, machine-specific paths, runtime
dumps, user content, ciphertext, account metadata, or unredacted audit output.

Source availability, local verification, packaging, GitHub Release, and
third-party distribution channels are separate claims. Do not describe one as
proof of another.

## Plan versions through accepted features

Repository releases follow the organization
[version governance policy](https://github.com/LicoLand/.github/blob/main/docs/version-governance.md).
A minor version must deliver at least one independently accepted capability.
A patch version contains fixes only. Record the target version, stable feature
ID, dependencies, acceptance contract, and integration branch in the owning
repository plan, then open one Draft pull request to that branch. Issues are optional
for defects or discussion; releases and dependencies do not require them.
