# Security

This document describes the security posture of the Incident
Duplicate Monitor. It is written for a security reviewer who has not
seen the project before; every claim here is verifiable against the
code.

## Threat model summary

The service polls Maximo for service-request data and renders it in
a local dashboard. It does not authenticate end users (network trust)
and persists only locally (SQLite + a pickle).

The principal threats and the controls in place:

| Threat | Control | Location |
|--------|---------|----------|
| Credential disclosure | Secrets in environment variables, never committed | `.env.example`, `.gitignore` |
| Unauthorized Maximo writes | Source exposes no write operations | `sources/maximo.py` |
| Unbounded PII exfiltration via Maximo | Read allowlist via `oslc.select`; only the operational fields below are requested, no broad `*` selector | `sources/maximo.py` (`_SELECT_FIELDS`) |
| Tampering with the SQLite database | File permissions restricted to the service user | Deployment |
| Pickle deserialization attacks | `live_scan.pkl` is local-only and written by the service itself | `scanner/full_scan.py` |
| Dashboard exposure | Bound to `0.0.0.0` only behind a trusted reverse proxy | Deployment |
| Dependency vulnerabilities | Pinned ranges; CodeQL + dependabot in CI | `pyproject.toml`, `.github/workflows/codeql.yml` |

## Authentication and authorization

The service does not authenticate dashboard callers. It assumes one of:

- **Trusted internal network access**.
- **A reverse proxy** (nginx, Cloudflare Access) that authenticates
  callers before forwarding to the service.

This is appropriate for the current internal-tool deployment. See
*Future improvements* for the planned upgrade to first-class auth.

## Secrets management

Current state: secrets live in a `.env` file on the production host
with filesystem permissions restricted to the service user. The file
is loaded by `python-dotenv` at startup.

This is acceptable for the current deployment scale but is the first
item flagged for upgrade — see *Future improvements*.

## Pickle handling

`live_scan.pkl` is written by the scanner and read by the dashboard
within the same trust boundary. It is **never** populated from caller
input. The dashboard upload path accepts Excel files only and runs
them through `read_file` (HTML/XLSX parsers); the resulting DataFrame
is serialized as a fresh pickle.

## Database

SQLite at `monitor.db`. No remote access path exists; the file is
opened with the standard sqlite3 driver, which prevents SQL injection
via parameter binding in every query in `storage/db.py`.

## Transport security

- Maximo: all calls use `https://` per `MAXIMO_BASE_URL`. No insecure
  fallback is configured.
- Dashboard: bound to plain HTTP behind a TLS-terminating reverse
  proxy in production.

## Personal data handled by the service

The service is an operational tool for reviewers and exposes the
contact and location fields that reviewers need to act on a
duplicate. The following personal-data fields are requested from
Maximo, stored in the local SQLite cache and the in-memory live
scan, and rendered in the dashboard — they are **not** redacted
from the system:

- Reporter and caller phone numbers (`reportedphone`, `affectedphone`).
- Reporter and caller email addresses (`reportedemail`, `affectedemail`).
- Reporter and assignee usernames / display names
  (`reportedby`, `zzrequestor`, `ownergroup_description`).
- Incident GPS coordinates (`latitudey`, `longitudex`) for the map
  view.

The boundaries the service does respect:

- **No broad field selector.** The OSLC request enumerates fields
  via `oslc.select`; anything not on the allowlist is never pulled
  out of Maximo. Adding a field requires a code change.
- **No outbound re-share.** The fields above are only written to
  the local SQLite (`monitor.db`) and the local pickle
  (`live_scan.pkl`). They are never forwarded to a third-party
  service, never emailed out, and never logged.
- **No PII in log records.** Log lines carry `ticket_id`, source
  name, timing and counts. They do not carry the Details text or
  any of the contact fields above.
- **TLS-only egress** to the Maximo host.

Operators who do not want phone / email / GPS in their deployment
can shrink the field list in
`src/duplicate_monitor/sources/maximo.py::_SELECT_FIELDS`; the
dashboard renders any missing field as blank without crashing.

## Dependency hygiene

- Runtime dependencies use compatible release specifiers. Major
  versions are reviewed manually.
- The `ruff` static analysis pass on every pull request catches the
  most common security smells.
- **Dependabot** is recommended at the repository settings level for
  monthly version-bump pull requests.
- **GitHub Advanced Security with CodeQL** is recommended once the
  GHAS license is provisioned for this repository. The workflow can
  be reintroduced under `.github/workflows/codeql.yml` at that point.

## Future improvements

### 1. Migrate secrets to a managed backend

**Risk.** Secrets stored in plaintext `.env` files on the production
host. **Current control.** Filesystem permissions restricted to the
service user. **Proposed future mitigation.** Migrate to Azure Key
Vault (or AWS Secrets Manager) loaded via OIDC at process start.

### 2. First-class authentication of dashboard callers

**Risk.** The dashboard trusts its network boundary. If that boundary
is compromised, any internal client can read every alert.
**Current control.** Internal-only deployment behind a reverse proxy.
**Proposed future mitigation.** Add OIDC token validation as a
FastAPI dependency on every route.

### 3. Audit log of dashboard verdicts

**Risk.** User verdicts on alerts are persisted to SQLite but the
identity of the user who made the verdict is not recorded.
**Current control.** None.
**Proposed future mitigation.** After item 2, persist the OIDC
subject with every verdict.

### 4. Encrypt the SQLite store at rest

**Risk.** The SQLite file is plaintext on disk. An attacker with
filesystem access can read every alert.
**Current control.** Filesystem permissions.
**Proposed future mitigation.** Use SQLite Encryption Extension
(SEE) or wrap the file in a per-host encrypted volume.

### 5. Outbound egress allowlist

**Risk.** A compromised service could exfiltrate data to an arbitrary
host. **Current control.** Outbound TLS to Maximo and the configured
notification webhook only. **Proposed future mitigation.** Configure
the deployment platform's egress firewall to allow only
`MAXIMO_BASE_URL` and the webhook host.
