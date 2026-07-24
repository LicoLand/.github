# Contributing to LicoLand

Use the repository that owns the affected capability. Cross-project proposals
should still name one canonical authority and describe every consumer that
would need to adopt the change.

## Where to contribute

| Topic | Repository |
| --- | --- |
| Agent governance platform, console, gateway, authorization, or storage | [Meshrix](https://github.com/LicoLand/Meshrix) |
| Optional upstream service | [Meshrix-Services](https://github.com/LicoLand/Meshrix-Services) |
| Optional provider, adapter, datastore, or agent plugin | [Meshrix-Plugins](https://github.com/LicoLand/Meshrix-Plugins) |
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
