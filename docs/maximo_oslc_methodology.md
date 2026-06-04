# Maximo OSLC Integration Methodology

> **Status:** Authoritative reference for the in-house Maximo OSLC integration.
>
> **Originality:** The seven techniques described below are original
> in-house work developed for the Kidana Maximo 7.6.x deployment. They
> are not documented in IBM's public material and were not known
> elsewhere inside the company prior to this project. This document is
> the record of that contribution.

## Why this document exists

The Maximo OSLC REST surface is officially documented by IBM as a
single, uniform interface. In practice, talking to a real Maximo
deployment requires defensive techniques that are nowhere in the
public docs: which endpoint actually works, how the JSON is shaped,
how date filters behave, and how the session must be managed. This
document describes those techniques and points to the exact lines of
code that implement them in this repository.

A new engineer reading this should be able to: (1) reproduce the same
behavior against another Maximo deployment, (2) extend the monitor for
additional Maximo objects, and (3) review the security implications.

## Scope

Two repositories implement this methodology:

- **`incident-description-engine`** — synchronous, single-ticket OSLC
  reads.
- **`incident-duplicate-monitor`** (this repository) — bulk OSLC reads
  with pagination, in `src/duplicate_monitor/sources/maximo.py`.

Both implementations follow the same methodology. This repository is
the more demanding consumer because it polls continuously and
paginates through hundreds of pages per scan.

---

## The Seven Pieces

### 1. Six-Way Endpoint and Authentication Fallback Chain

**Problem.** Maximo's REST surface varies by version, license tier,
and admin configuration. A given deployment will only respond on a
subset of three plausible endpoints:

- `/oslc/os/mxsr` — standard OSLC object endpoint, most common
- `/oslc/os/mxapisr` — newer JSON-first alias in some 7.6.x patches
- `/maxrest/rest/os/mxsr` — legacy REST endpoint still enabled on some
  installations

And only on a subset of two authentication schemes:

- `maxauth` header — Maximo's proprietary base64 credential header
- HTTP Basic Auth — standard `Authorization: Basic`

That is six combinations per object. There is no documented way to
discover which one the server accepts short of trying.

**Solution.** Try each combination in order, cache the first one that
returns a 200 with JSON, and reuse it for the rest of the session. If
the cached strategy stops working (typically because the session
cookie expired), invalidate the cache and rediscover.

**Implementation.** `MaximoSource._call` in
`src/duplicate_monitor/sources/maximo.py`. The chain is the
module-level constant `_STRATEGIES`.

### 2. JSON-LD Prefix Tolerance

**Problem.** Maximo's JSON output uses JSON-LD conventions and prefixes
keys with `spi:` or `rdfs:` in some configurations and not in others.
The same field — for example `description` — may arrive as
`"description"`, `"spi:description"`, or `"rdfs:description"`. A
client that reads only the bare name silently drops half the data on
certain deployments.

**Solution.** Every read goes through a helper that tries the bare key
first, then `spi:`-prefixed. The list helper handles the `member` /
`rdfs:member` variation the same way.

**Implementation.** `_g` and the inline `data.get("member") or
data.get("rdfs:member")` patterns in `sources/maximo.py`.

### 3. Long-Description Dual Request

**Problem.** The "Details" field that operations staff fill in the
Maximo UI is sometimes exposed as `longdescription` and sometimes as
`description_longdescription`, depending on the version and the
"long description" feature configuration. Requesting only one of those
names silently misses the Details field on the other deployments.

**Solution.** Request both via `oslc.select`. The server returns
whichever it knows about; the prefix-tolerant reader picks whichever
came back.

**Implementation.** Both fields are listed in `_SELECT_FIELDS` and the
`_normalize` function uses `_g(m, "description_longdescription",
"longdescription")` to pick the non-empty one.

### 4. Kidana-Specific Custom Field Map

**Problem.** The Kidana deployment uses Maximo's custom-field facility
to store contractor, requestor, and SLA data in `zz*`-prefixed fields
that are not part of the standard `mxsr` schema. Without knowing them,
the data appears to be missing.

**Solution.** Reverse-engineered from the live deployment by
inspecting `oslc.select=*` responses on known tickets. The mapping
below is the record of that work and should be carried forward to any
future extension.

| Custom field | Human-facing label | Purpose |
|--------------|--------------------|---------|
| `zzrequestorno` | REQUESTOR NO. | Numeric requestor identifier |
| `zzrequestor` | REPORTED NAME | Arabic full name of the requestor |
| `zzpcontract` | Contract | Service contract code |
| `zzpcontract_description` | Contract description | Service contract name |
| `zzextparty` | الجهة (code) | External party code |
| `zzextparty_description` | الجهة (Arabic) | Contractor display name |
| `zzbreachedtime` | زمن الاستجابة | SLA breached time |
| `zzesclation` | Response Escalation | Escalation flag |
| `zzcallerparty` | Caller party | Caller party identifier |

**Implementation.** All `zz*` fields appear in `_SELECT_FIELDS` and
are read into the normalized record by `_normalize`.

### 5. Smart Early-Stop Pagination

**Problem.** Maximo deployments frequently ignore OSLC `WHERE` filters
on date columns. A naive client would traverse years of records to
find the last 24 hours.

**Solution.** Always sort newest-first (`oslc.orderBy=-reportdate`)
and after each page check whether the oldest row falls outside the
requested window. If it does, keep only the in-range rows from that
page and stop. Combined with a hard page cap, this guarantees a
bounded number of API calls per scan window.

**Implementation.** `MaximoSource.fetch_all` and `fetch_latest` in
`sources/maximo.py`.

**Worked example.** A 48-hour scan with `LM_FULL_SCAN_MAX_PAGES=15`
and `page_size=200` reads at most 3000 of the newest SRs. On a
well-behaved deployment, the scan typically returns after 1-2 pages.
On a deployment that ignores `WHERE`, the scan terminates as soon as
a page contains records older than 48 hours.

### 6. Persistent HTTP Client with Session Cookie Reuse

**Problem.** Maximo issues a session cookie (`JSESSIONID` or
`LtpaToken2`) on the first authenticated request. Subsequent requests
must present it. Constructing a new HTTP client for every page would
discard the cookie and force a fresh authentication on every page —
for a 15-page scan, that is 15 authentication round trips that the
deployment may rate-limit or block.

**Solution.** Keep the `httpx.Client` alive for the life of the
source object. The cookie jar is part of the client and is reused
automatically. Close it explicitly when the source goes out of scope.

**Implementation.** `MaximoSource._client` is the persistent client.
The cached `_best_strategy` is reused with this client.

### 7. nextPage Host Rewriting

**Problem.** Maximo returns `nextPage` cursor URLs that point at the
internal app-server host (e.g., `http://10.13.0.99/...`). Following
that URL from outside the data center fails; following it without
rewriting the scheme also fails when the public base requires TLS.

**Solution.** Rewrite the scheme and host of every nextPage URL to
match the configured public base before issuing the request.

**Implementation.** `MaximoSource._rewrite_host` and the `_extract_next_page`
helper.

---

## Operational Considerations

### Credentials

The methodology requires a Maximo service account with HTTP Basic Auth
enabled. The account must bypass SSO and OTP because the integration
is non-interactive. Personal user accounts will not work.

### Rate limiting

Maximo enforces a server-side rate limit that varies by deployment.
The defaults (`LM_PAGE_SIZE=200`, `LM_FULL_SCAN_MAX_PAGES=15`) are set
conservatively. Aggressive testing against production should be
coordinated with the Maximo administrator.

### Failure modes

- **All strategies fail with 401.** Credentials wrong or expired.
- **All strategies fail with timeout.** Network path is blocked.
- **Cached strategy returns 401 mid-session.** Session cookie expired;
  the client invalidates and rediscovers transparently.
- **Empty member list with no error.** The `WHERE` clause matched no
  rows.

### Logging

The source emits one log line per strategy attempt at `DEBUG` and one
line per successful lock at `INFO`.

---

## Future Extensions

The methodology generalizes to other Maximo OSLC objects. To add
support for `mxworkorder`, `mxpo`, or any other object:

1. Add the new endpoint variants to `_STRATEGIES`.
2. Add an object-specific `_SELECT_FIELDS`.
3. Add custom-field mappings if applicable.
4. Add a public method that calls the existing `_call`
   infrastructure.

Pieces 2, 4, 5, and 6 carry over unchanged.

---

## References to External Documentation

- IBM Knowledge Center — *Maximo Asset Management 7.6.x OSLC REST API*
- IBM Knowledge Center — *Maximo Service Request Object (`mxsr`)*
- OSLC Core Specification — *Open Services for Lifecycle Collaboration*

These are listed under "Formal External References" in the project
`README.md` and `REPORT.md`.
