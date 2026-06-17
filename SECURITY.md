# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x | Yes |

## Reporting a vulnerability

If you believe you have found a security vulnerability in this
repository, please do **not** open a public GitHub issue. Instead,
email the project owner directly through the corporate channel listed
in the team directory.

Include in the report:

- A description of the vulnerability and the affected component.
- Reproduction steps or a proof-of-concept.
- The version (commit hash) you tested against.
- Any suggested mitigation.

You can expect an acknowledgement within two business days and a
substantive update within ten business days.

## Defensive posture

The full security posture, threat model, and known limitations with
their proposed mitigations are documented in
[`docs/security.md`](docs/security.md). The two most important
guarantees:

- **The Maximo OSLC source exposes no write operations.** Enforced at
  the code level.
- **Read access is bounded by an `oslc.select` allowlist.** The
  service pulls only the operational fields the dashboard renders,
  including the contact and GPS fields reviewers need to act on a
  duplicate. The full list and the boundaries around how that data
  is stored and re-shared are in
  [`docs/security.md`](docs/security.md).
