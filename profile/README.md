# LicoLand

LicoLand develops open infrastructure for governed agents, private human–agent
collaboration, and federated communication.

Three public projects form one stack: a secure endpoint client, a neutral
federation protocol, and an untrusted communication station. Each can be
inspected, deployed, and extended on its own, without turning an intermediary
service into a hidden source of trust.

## Projects

- **[LicoUp](https://github.com/LicoLand/LicoUp)** — the endpoint client. An
  open-source, local-first human–agent conversation client in which people and
  visible agents collaborate under user-controlled identity, approval, and
  disclosure.
- **[LicoArc](https://github.com/LicoLand/LicoArc)** — the federation
  protocol. The implementation-neutral Protocol Layer for secure endpoint
  exchange, station-facing delivery, and federation governance.
- **[BadTower](https://github.com/LicoLand/BadTower)** — the communication
  station. A pure, protocol-neutral station that remains an untrusted
  transport by design.

## Trust boundaries

- Identity, approval, key custody, encryption, and decryption stay on
  user-controlled endpoints.
- A station never becomes a source of plaintext, client keys, or local
  approval authority.
- LicoUp and BadTower consume versioned Lico Arc protocol artifacts instead of
  importing sibling source.
- Official hosted services are deployments of public project contracts, not
  alternate product authorities.

## Contribute

Use the owning repository for bugs and feature proposals. See the organization
[contribution guide](https://github.com/LicoLand/.github/blob/main/CONTRIBUTING.md)
for routing and privacy expectations.
