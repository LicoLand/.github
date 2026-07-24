# LicoLand

LicoLand develops open infrastructure for governed agents, private human–agent
collaboration, and federated communication.

The organization keeps governance, endpoint security, federation authority,
and relay transport in separate repositories. Each project can be inspected,
deployed, and extended without turning an intermediary service into a hidden
source of trust.

## Projects

| Project | Responsibility | License |
| --- | --- | --- |
| [Meshrix](https://github.com/LicoLand/Meshrix) | Compact, self-hosted agent behavior governance platform with stable extension boundaries. | GPL-3.0-or-later |
| [Meshrix-Services](https://github.com/LicoLand/Meshrix-Services) | Optional upstream services deployed independently from the Meshrix core. | Apache-2.0 |
| [Meshrix-Plugins](https://github.com/LicoLand/Meshrix-Plugins) | Optional providers, adapters, datastores, and agent integrations. | Apache-2.0 |
| [LicoUp](https://github.com/LicoLand/LicoUp) | Offline-capable human–agent client with endpoint-owned security and optional federation. | GPL-3.0-or-later |
| [Fabrigent](https://github.com/LicoLand/Fabrigent) | Normative federation protocol, strategy, governance, and conformance authority. | GPL-3.0-or-later |
| [BadTower](https://github.com/LicoLand/BadTower) | Untrusted relay node for opaque delivery, mailbox, lease, acknowledgement, quota, and cleanup. | AGPL-3.0-or-later |
| [LicoArc-Plugins](https://github.com/LicoLand/LicoArc-Plugins) | Long-lived LicoArc ecosystem integrations that do not own the LicoUp client or Fabrigent protocol. | Repository-specific |

## Public sites and official services

| Address | Role |
| --- | --- |
| [lico.land](https://lico.land/) | Organization introduction |
| [licoland.com](https://licoland.com/) | Official service directory |
| [meshrix.io](https://meshrix.io/) | Meshrix product site |
| [licomesh.com](https://licomesh.com/) | Official Meshrix MCP gateway |
| [licoup.com](https://licoup.com/) | LicoUp product site |
| [licoup.net](https://licoup.net/) | Official LicoUp network |
| [licoarc.com](https://licoarc.com/) | Official federation governance authority |

## Trust boundaries

- Meshrix governs agent behavior and MCP access; it is not the LicoUp relay
  network.
- LicoUp keeps identity, approval, key custody, encryption, and decryption on
  user-controlled endpoints.
- BadTower is designed as an untrusted transport and never becomes a source of
  plaintext, client keys, or local approval authority.
- Fabrigent owns neutral federation rules and conformance material. LicoUp and
  BadTower consume versioned protocol artifacts instead of importing sibling
  source.
- Official hosted services are deployments of public project contracts, not
  alternate product authorities.

## Organization governance

[Lico-Auditor](https://github.com/LicoLand/Lico-Auditor) maintains independent
public audit policies and evidence contracts. Lico-Dev is the private
governance source for repository routing, agent skills, and reusable
verification workflows.

Use the owning repository for bugs and feature proposals. See the organization
[contribution guide](https://github.com/LicoLand/.github/blob/main/CONTRIBUTING.md)
for routing and privacy expectations.
