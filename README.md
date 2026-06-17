# Incident Duplicate Monitor

Live duplicate-detection service for Kidana Maximo service requests.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![License: Proprietary](https://img.shields.io/badge/license-Proprietary-lightgrey)](LICENSE)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Maximo Integration Methodology](#maximo-integration-methodology)
4. [Scoring Algorithm](#scoring-algorithm)
5. [Prerequisites](#prerequisites)
6. [Installation](#installation)
7. [Configuration](#configuration)
8. [Running the Service](#running-the-service)
9. [Dashboard Guide](#dashboard-guide)
10. [Testing](#testing)
11. [Security](#security)
12. [Operations and Troubleshooting](#operations-and-troubleshooting)
13. [License](#license)
14. [Sources and References](#sources-and-references)

---

## Overview

The Incident Duplicate Monitor is a stand-alone 24/7 service that
polls the Kidana Maximo deployment, detects duplicate service requests
at the moment of their arrival, and surfaces the duplicate groups in an
interactive dashboard for review.

The service exists to solve a specific operational problem: during the
Hajj operations peak, dozens of operators enter service requests
simultaneously across three sites (Mina, Arafat, Muzdalifah). The same
incident is routinely reported multiple times within minutes —
sometimes within seconds — by different callers and different
contractors. Without an automated check, the same field crew is
dispatched twice or asset-level reporting double-counts the incident.

The monitor handles this end to end:

- **Polls Maximo** through the OSLC interface at a configurable cadence
  (default 15 seconds for the quick scan).
- **Scores every new SR** against the open SR set using an Arabic-aware
  multi-signal similarity scorer.
- **Persists** the duplicate groups it finds in a local SQLite store.
- **Renders** them in a dashboard that lets the reviewer compare SRs
  side by side and mark verdicts (real duplicate, false positive,
  needs review).

The service is built around two technical contributions that are
documented in detail:

1. The **Maximo OSLC integration methodology** — an in-house seven-piece
   set of techniques that makes the OSLC integration reliable across
   Maximo versions and configurations. Documented in
   [`docs/maximo_oslc_methodology.md`](docs/maximo_oslc_methodology.md).
2. The **Arabic duplicate-scoring algorithm** — a multi-signal scorer
   tuned for the Kidana ticket vocabulary, including Arabic script
   normalization, numeric overlap, token containment, and time-gap
   weighting. Documented in
   [`docs/scoring_algorithm.md`](docs/scoring_algorithm.md).

---

## Architecture

```
                +----------------------+
                |  Maximo OSLC source  |
                |  (six-way fallback)  |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |  Poller (15s quick,  |
                |   5min full scan)    |
                +----------+-----------+
                           |
                           v
+----------------------------------------------+
|  Matching layer                              |
|  - normalize_arabic (Arabic normalization)   |
|  - smart_text_compare (template + numbers)   |
|  - score_pair (multi-signal scorer)          |
|  - legacy.detect (bulk grouping)             |
+----------------------+-----------------------+
                       |
              +--------+--------+
              |                 |
              v                 v
   +-------------------+   +-------------------+
   |  SQLite storage   |   |  Live scan pickle |
   |  (alerts, seen,   |   |  (dashboard input)|
   |   poll history)   |   |                   |
   +-------------------+   +--------+----------+
                                    |
                                    v
                       +-----------------------+
                       |  FastAPI dashboard    |
                       |  (port 8502 by default)|
                       +-----------------------+
```

The Maximo source is replaceable: a file watcher implementation is
included for offline demos and as a safety net when the network or
credentials are unavailable. See `docs/architecture.md` for the full
sequence flows.

---

## Maximo Integration Methodology

The Maximo source in this repository implements an in-house methodology
developed for the Kidana Maximo 7.6.x deployment. The methodology is
not documented in IBM's public material and is original work by the
project owner. It is the project's most valuable technical
contribution alongside the scoring algorithm and is documented in full
in [`docs/maximo_oslc_methodology.md`](docs/maximo_oslc_methodology.md).

The headline pieces:

- **Six-way endpoint and authentication fallback chain** across
  `{/oslc/os/mxsr, /oslc/os/mxapisr, /maxrest/rest/os/mxsr}` x
  `{maxauth, Basic}`.
- **JSON-LD prefix tolerance** (`spi:` / `rdfs:` / unprefixed key
  variants).
- **Long-description dual request** (`longdescription` and
  `description_longdescription`).
- **Kidana-specific custom field map** for the `zz*` Kidana custom
  fields (contractor, requestor, SLA, escalation).
- **Smart early-stop pagination** that works around deployments that
  ignore OSLC WHERE filters.
- **Persistent HTTP client** with session cookie reuse.
- **Read-only field allowlist**.

Read the methodology document for the full write-up, worked examples,
and the exact implementation locations.

---

## Scoring Algorithm

For every candidate pair of service requests, the algorithm asks four
questions in order. A pair must pass all four hard requirements to be
considered a duplicate; the dashboard then ranks survivors by how
strongly they match.

### The four hard requirements

A pair is treated as a duplicate candidate **only if all four of these
hold**. If any one fails, the pair is dropped from duplicate detection
entirely.

1. **Same location.** The two SRs must share the same `location` value
   in Maximo. Different locations means different sites — never a
   duplicate.
2. **Same fault category.** The two SRs must report the same fault
   type (the last two segments of Maximo's comma-separated
   taxonomy — typically L3 + L4). Different fault categories means
   different problems.
3. **Same asset id — when both sides have one.** If both SRs carry an
   asset id and the two ids differ, the pair is dropped. The reasoning
   is simple: two physically different assets cannot be the same
   incident. **If one side has no asset id** (operators sometimes
   omit it), the system is lenient and lets the pair proceed; the
   other signals carry the decision.
4. **Description wording matches at 90 % or more.** The two
   descriptions are compared by sentence structure and word overlap.
   Anything below 90 % similarity is treated as a different report
   and dropped.

### Description matching — what 90 % really means

When two descriptions clear the 90 % bar, the algorithm classifies the
pair into one of three positive categories based on whether the
numbers inside the descriptions (asset ids, grid references,
signposts) line up:

- **Identical.** Wording matches at 90 % or more **and** the numbers
  also match. The two SRs clearly describe the same incident.
- **Same template, different numbers.** Wording matches at 90 % or
  more **but** the numbers differ. This catches the case where two
  operators use the same boilerplate sentence ("تسرب في شبكة المياه
  عند المربع 5" vs "تسرب في شبكة المياه عند المربع 47") to describe
  two genuinely different incidents. The pair is dropped — the system
  treats "different asset / grid numbers in the description" as proof
  that the two reports are about physically different things, even
  when the wording is identical.
- **Similar.** Wording matches at 90 % or more and the pair does not
  fall into either of the categories above.

### The score itself

Once a pair passes all four hard requirements, the algorithm assigns
it a score by adding up evidence from the underlying signals:

| Signal | Points |
|---|---|
| Same location | 4 |
| Same fault | 3 |
| Same asset (both present and matching) | 4 |
| Description category — identical | 5 |
| Description category — similar | 3 |
| Same requestor number | 2 |
| Reported on the same day | 3 |
| Reported one day apart | 2 |
| Reported two days apart | 1 |

The pair is **retained** in the duplicate store once its total
reaches `LM_MIN_SCORE` (default 7) and **raises an alert** in the
dashboard once it reaches `LM_ALERT_SCORE` (default 8). Surviving
pairs that share an SR are then collapsed into one group, so that if
A matches B and B matches C, the three appear together for the
reviewer.

### Where the rules live in the code

All four hard requirements and the full additive scoring live in a
single function — `scorer.score_pair` — so the live path and the bulk
path produce identical results for the same pair. The other two files
are thin wrappers that adapt their input shape and add their own
output shape:

- `src/duplicate_monitor/matching/scorer.py` — **the source of
  truth.** Defines `smart_text_compare`, the four hard gates, and
  `score_pair`. Every other code path delegates here.
- `src/duplicate_monitor/matching/engine.py` — the **live path**.
  Used by the poller every 15 seconds when a new SR arrives. Takes
  the raw poller record, normalises it, calls `scorer.score_pair`,
  and writes pair-level alerts to the database.
- `src/duplicate_monitor/matching/legacy.py` — the **bulk path**.
  Used by the scanner and the Excel-upload feature. Applies blocking
  for performance, calls `scorer.score_pair` per pair, then runs
  Union-Find to produce duplicate groups (so a triple A↔B↔C appears
  as one group instead of three pairs).

For the full discussion of design decisions and the tuning history,
see [`docs/scoring_algorithm.md`](docs/scoring_algorithm.md).

---

## Prerequisites

- **Python 3.11** or newer.
- **Network access** to the Maximo deployment.
- **A Maximo service account** with HTTP basic auth enabled. SSO and
  OTP-enforced personal accounts will not work — the integration is
  non-interactive.
- **Windows, macOS, or Linux**. Development is on Windows; production
  deployment is on Linux.

---

## Installation

```powershell
# 1. Clone the repository
git clone https://github.com/GhalaMansour/incident-duplicate-monitor.git
cd incident-duplicate-monitor

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1     # On macOS/Linux: source .venv/bin/activate

# 3. Install the package (development install, editable)
pip install -e ".[dev]"

# 4. Copy the example environment file and fill in your secrets
Copy-Item .env.example .env    # On macOS/Linux: cp .env.example .env

# 5. Verify the install
ruff check .
pytest

# 6. Probe the Maximo connection
python -m duplicate_monitor diag
```

---

## Configuration

Every supported environment variable is documented inline in
[`.env.example`](.env.example). The variables fall into five groups:

| Group | Required | Purpose |
|-------|---------|---------|
| `MAXIMO_*` | Yes | OSLC base URL and service-account credentials |
| `LM_POLL_*`, `LM_PAGE_SIZE`, `LM_TIMEOUT_SEC` | No | Polling cadence and request tuning |
| `LM_SOURCE_MODE`, `LM_WATCH_DIR` | No | Choose between Maximo, file watcher, or auto |
| `LM_MIN_SCORE`, `LM_MAX_DAYS`, `LM_ALERT_SCORE` | No | Detection thresholds |
| `LM_WEBHOOK_URL`, `LM_SMTP_*` | No | Outbound notifications |

See [`docs/security.md`](docs/security.md) for the recommended way to
manage these variables in production.

### File-source folder — where SR exports go

When the file watcher is active (either explicitly via
`LM_SOURCE_MODE=file` or as the automatic fallback when Maximo is
unreachable), the service reads the **newest** `.xls` / `.xlsx` file
in `LM_WATCH_DIR` and treats it as a Service Request export.

**Where SR exports come from:**
From Maximo's Service Requests view, choose *Save as Excel*. The
resulting file is the export the watcher needs.

**Required columns in the export:**
`Service Request` (or `Ticket ID`), `LOCATION`, `Summary`,
`Details`, `Reported Date`. The watcher is case-insensitive and
also accepts the equivalent Arabic headings.

**Where to place the file:**
Create a dedicated folder for SR exports and point `LM_WATCH_DIR` at
it. For example:

```
LM_WATCH_DIR=C:\Users\<you>\Documents\maximo-exports
```

Drop the newest export into that folder; the watcher will pick it up
on the next tick.

**Common mistakes to avoid:**

- **Do not point `LM_WATCH_DIR` at the bundled `data/` folder.**
  `data/` holds reference lookup files (`asset_description.xls`,
  `location_description.xls`) used for Arabic display names. Those
  are not SR exports. The service auto-skips them by name, but the
  folder is otherwise empty of SR data, so the watcher will report
  *"لا يوجد ملفّ بلاغات"* until a real export is placed beside them.
- **Do not leave Excel lock files (`~$something.xlsx`) in the
  watcher folder.** Closed Excel windows leave them behind. The
  watcher skips them automatically.

---

## Running the Service

The service ships with five subcommands:

```powershell
# Default: poller + dashboard
python -m duplicate_monitor

# Poller only (no dashboard)
python -m duplicate_monitor poller

# Dashboard only (assumes poller runs elsewhere)
python -m duplicate_monitor web

# Run a single poll cycle and exit
python -m duplicate_monitor tick

# Probe Maximo strategies and print configuration
python -m duplicate_monitor diag
```

The dashboard is at `http://localhost:8502` by default.

---

## Dashboard Guide

See [`docs/dashboard_guide.md`](docs/dashboard_guide.md) for a full
walkthrough. The headline experience:

- **Left panel**: list of detected duplicate groups, sorted by score.
- **Main panel**: side-by-side comparison of the SRs in the selected
  group with every field on a separate row.
- **Decision controls**: mark each SR as true duplicate, false
  positive, or needs review. Decisions are persisted to SQLite.
- **Filters**: time window (24h / 48h / 7d), minimum score, site.

---

## Testing

```powershell
# Unit tests
pytest tests/unit

# With coverage
pytest --cov=duplicate_monitor --cov-report=term-missing
```

The matching layer is the part most worth unit testing because it is
self-contained and deterministic. The OSLC source is integration-tested
against a recorded Maximo response fixture.

---

## Security

The security posture is documented in detail in
[`docs/security.md`](docs/security.md) and summarized in
[`SECURITY.md`](SECURITY.md). The headline guarantees:

- **No write access to Maximo.** The OSLC source exposes no write
  operations.
- **Bounded read allowlist.** The OSLC request enumerates fields
  via `oslc.select`; only the operational fields the dashboard
  renders are pulled. The full list (including the contact and
  location fields reviewers need to act on a duplicate) is in
  [`docs/security.md`](docs/security.md).
- **SQLite is local-only.** The database file is owned and read by the
  service user only; no remote access path exists.
- **TLS-only egress** to Maximo.
- **CodeQL + dependabot + pre-commit** in CI.

---

## Operations and Troubleshooting

| Symptom | Likely cause | First action |
|---------|--------------|--------------|
| `All Maximo strategies failed` | Wrong credentials or firewall change | Run `python -m duplicate_monitor diag`; verify creds against the Maximo Web UI |
| Dashboard shows stale data | Poller stopped | Inspect `monitor.log`; restart the service |
| Group count keeps oscillating | Window too narrow against arrival rate | Raise `LM_FULL_SCAN_DAYS` and `LM_FULL_SCAN_MAX_PAGES` |
| False positives on boilerplate descriptions | Template-only matches | Raise `LM_MIN_SCORE`; check `scoring_algorithm.md` for the template-only rule |

---

## License

Proprietary. See [`LICENSE`](LICENSE).

---

## Sources and References

### 1. Formal External References (Public / Vendor Documentation)

These are publicly available, officially documented sources that the
project consumes or builds on. They establish the baseline that any
engineer can verify independently.

- **IBM Maximo Asset Management - OSLC REST API Specification.**
  Official IBM documentation for Maximo's OSLC REST surface: objects,
  query syntax, pagination model, and JSON-LD payloads.
- **IBM Maximo Service Request (`mxsr`) object definition.** Official
  field reference for the standard `mxsr` business object —
  `ticketid`, `status`, `reportdate`, `longdescription`, `doclinks`,
  and the rest.
- **OSLC Core Specification (Open Services for Lifecycle
  Collaboration).** Industry standard underlying Maximo's OSLC
  interface.
- **FastAPI, httpx, uvicorn, pandas, openpyxl, xlrd.** Framework and
  library documentation.
- **Python `difflib.SequenceMatcher` algorithm reference.** The Ratcliff
  / Obershelp algorithm used for template similarity is well
  documented in the Python standard library.
- **Python Packaging User Guide (PEP 517 / PEP 621).** Reference for
  the `pyproject.toml` layout used here.

### 2. Distinguished Internal Contributions (Original In-House Work)

These items are not in any public reference and were developed
in-house for this project. They are the project's intellectual
contribution and are credited to the project owner.

- **Maximo OSLC six-way endpoint and authentication fallback
  strategy.** Original methodology for connecting reliably to the
  Kidana Maximo deployment across version and configuration
  differences. Not documented in IBM's public material.
  Implementation: `src/duplicate_monitor/sources/maximo.py`. Full
  write-up: [`docs/maximo_oslc_methodology.md`](docs/maximo_oslc_methodology.md).
- **Smart early-stop pagination.** Designed to work around Maximo
  deployments that ignore OSLC `WHERE` date filters. Sorts by
  `-reportdate` and stops as soon as the page falls out of the
  requested window. Implementation: `MaximoSource.fetch_all` and
  `fetch_latest`.
- **JSON-LD prefix tolerance layer.** Handles the `spi:` / `rdfs:` /
  unprefixed key variation across Maximo versions.
- **Long-description field dual-request technique.** Requests both
  `longdescription` and `description_longdescription`. Discovered
  empirically.
- **Kidana-specific custom field map.** Mapping of the Kidana `zz*`
  custom fields (`zzrequestorno`, `zzpcontract`, `zzextparty`,
  `zzbreachedtime`, `zzesclation`, ...) to their human-facing labels.
- **Persistent HTTP client with session cookie reuse.** Avoids
  per-page re-authentication during paginated calls.
- **Arabic duplicate-scoring algorithm.** Designed for the Kidana
  ticket vocabulary, including Arabic-specific normalization rules
  (alef variants, tatweel removal, hamza handling) and the multi-
  signal scorer (template similarity, numeric overlap, token
  containment, time gap). Implementation:
  `src/duplicate_monitor/matching/scorer.py` and
  `src/duplicate_monitor/matching/legacy.py`. Full write-up:
  [`docs/scoring_algorithm.md`](docs/scoring_algorithm.md).

