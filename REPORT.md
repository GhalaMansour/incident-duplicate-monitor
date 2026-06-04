# Incident Duplicate Monitor - Project Report

**Repository:** `incident-duplicate-monitor`
**Version:** 1.0.0
**Date:** 2026-06-04
**Owner:** Kidana Operations Engineering

---

## Executive Summary

The Incident Duplicate Monitor is a stand-alone 24/7 service that
polls the Kidana Maximo deployment, detects duplicate service
requests at the moment of their arrival, and surfaces the duplicate
groups for review in an interactive dashboard. It is the second of
two repositories carved out of the original Kidana monorepo and is
deployable as an independent unit.

The service exists to solve a specific operational problem: during
the Hajj operations peak, the same incident is routinely reported
multiple times within minutes by different callers and contractors.
Without an automated check, the same field crew is dispatched twice
or asset-level reporting double-counts the incident. The monitor
catches the duplicates within seconds and routes them to a reviewer.

The two technical contributions credited as in-house original work:

1. The **Maximo OSLC integration methodology** — seven techniques
   that make the OSLC integration reliable across Maximo versions
   and configurations, documented in detail in
   `docs/maximo_oslc_methodology.md`.
2. The **Arabic duplicate-scoring algorithm** — a multi-signal scorer
   tuned for the Kidana ticket vocabulary, documented in
   `docs/scoring_algorithm.md`.

Both are listed and credited under
[Sources and References](#sources-and-references) below.

---

## 1. Background and Goals

Hajj operations process thousands of service requests per day across
the three sites (Mina, Arafat, Muzdalifah). Operators enter SRs
through multiple channels: the Maximo Web UI, the call center, and
contractor mobile apps. A single incident — a leaking water tank, a
power outage in a sector — is routinely reported simultaneously by
several callers, generating several SRs against the same underlying
problem.

Before this project there was no automated way to catch this. The
operational consequences:

- Field crews dispatched twice to the same incident.
- Contractor SLA reporting double-counts work.
- Reviewers cannot tell whether two SRs are the same incident or
  genuinely different.

The project goals:

- Detect duplicates within seconds of their arrival in Maximo.
- Provide a dashboard for human review and verdict.
- Persist verdicts for downstream SLA reconciliation.
- Integrate with Kidana Maximo without requiring Maximo configuration
  changes.

The last goal forced the in-house OSLC methodology that is the
project's most reusable artifact.

---

## 2. Architecture

The service is organized as a pipeline driven by a 24/7 poller. The
poller's source is replaceable: Maximo OSLC in production, an Excel
file watcher for offline / demo use. Scoring is split between a fast
live engine (per-pair) and a bulk detector (per-scan).

A full architectural diagram and sequence flows are in
`docs/architecture.md`.

---

## 3. Key Technical Contributions

### 3.1 Maximo OSLC Integration Methodology (original in-house work)

Seven techniques developed empirically against the Kidana production
deployment:

1. Six-way endpoint and authentication fallback chain.
2. JSON-LD prefix tolerance.
3. Long-description dual request.
4. Kidana-specific custom field map (`zz*` fields).
5. Smart early-stop pagination.
6. Persistent HTTP client with session cookie reuse.
7. nextPage host rewriting.

Full write-up in `docs/maximo_oslc_methodology.md`.

### 3.2 Arabic Duplicate Scoring Algorithm (original in-house work)

A multi-signal scorer tuned for the Kidana ticket corpus. Combines:

1. Template similarity (description with numbers stripped).
2. Numeric overlap (asset ids, signpost references, areas).
3. Token similarity with fuzzy containment.
4. Time-gap weighting.

The scoring vocabulary includes Arabic-aware normalization (alef
variants, tatweel removal, hamza handling, ta marbuta -> ha) and a
specifically-named `template_only` class that surfaces boilerplate
matches with mismatched numbers as warnings rather than as
duplicates.

Full write-up in `docs/scoring_algorithm.md`.

### 3.3 Architectural Independence

Live engine (per-pair) and bulk detector (per-scan) are two paths
into the same scoring vocabulary. The live engine is O(open_srs) per
new SR and is the dashboard's primary refresh signal. The bulk
detector is O(N^2) within a (fault, location) block and is the
authoritative grouping pass.

---

## 4. Implementation Overview

| Component | Purpose | Key files |
|-----------|---------|-----------|
| Maximo source | OSLC integration with the seven-piece methodology | `sources/maximo.py` |
| File source | Offline / demo fallback | `sources/file.py` |
| Live engine | Per-pair scoring used by the poller | `matching/engine.py` |
| Bulk detector | Per-scan grouping pass | `matching/legacy.py` |
| Scoring vocabulary | `normalize_arabic`, `smart_text_compare`, `score_pair` | `matching/normalize.py`, `matching/scorer.py` |
| Storage | SQLite (alerts, seen, poll history) | `storage/db.py` |
| Poller | 24/7 driver with source manager and failure fallback | `poller/runner.py` |
| Scanner | Quick scan and full scan | `scanner/full_scan.py` |
| Dashboard | FastAPI server | `web/app.py` |
| CLI | `python -m duplicate_monitor` subcommands | `__main__.py` |

---

## 5. Security Analysis

A full security review is in `docs/security.md`. Headline guarantees:

- **No write access to Maximo.** The OSLC source exposes no write
  operations.
- **No PII fetched.** Read allowlist enforced at `oslc.select`.
- **Local-only persistence.** SQLite + a pickle, both inside the
  package directory.
- **TLS-only outbound.** No insecure fallback.
- **CodeQL + dependabot in CI.**

Known limitations and proposed mitigations are listed in
`docs/security.md` so the review is constructive rather than
defensive.

---

## 6. Deployment Plan

`docs/deployment.md` documents three deployment paths:

- **Render (managed).** `render.yaml` is included; environment
  variables are set in the Render dashboard.
- **Docker.** A reference Dockerfile is provided.
- **On-premise behind nginx.** Two systemd units (poller, web) plus
  an nginx reverse-proxy location block.

Health checks hit the dashboard `/health` endpoint. Logs go to
stdout for the platform's log aggregator.

---

## 7. Future Improvements

| Priority | Item | Rationale |
|---------|------|-----------|
| High | Managed secrets backend | Reduce `.env` exposure |
| High | First-class authentication for the dashboard | Move from network trust to caller trust |
| Medium | Email and Slack notification channels | Reach reviewers outside the dashboard |
| Medium | Score-explainability panel | Make false-positive review faster |
| Medium | Configurable scorer weights | Tune to the specific operational tradeoff |
| Low | Postgres backend option | Multi-instance / centralized historical store |

---

## 8. Verification and Testing

| Check | Tool | Acceptance |
|-------|------|-----------|
| Linting | `ruff check` | No errors |
| Formatting | `ruff format --check` | Consistent |
| Type checking | `mypy` on `core/` and `matching/normalize|scorer` | No errors |
| Unit tests | `pytest tests/unit` | All pass |
| Coverage | `pytest --cov` | At least 60% on matching layer |

All four checks run on every pull request via
`.github/workflows/ci.yml`.

---

## 9. Operational Notes

- The service is stateful (SQLite + pickle) but both files live
  inside the package directory; restarts are non-destructive.
- The poller acquires a single lock so a long scan does not overlap
  with the next tick.
- The Maximo session cookie expires after a window set by the Maximo
  administrator; the client invalidates and rediscovers
  transparently.

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
  field reference for the standard `mxsr` business object.
- **OSLC Core Specification (Open Services for Lifecycle
  Collaboration).** Industry standard underlying Maximo's OSLC
  interface.
- **FastAPI, httpx, uvicorn, pandas, openpyxl, xlrd.** Framework and
  library documentation.
- **Python `difflib.SequenceMatcher` algorithm reference.** The
  Ratcliff / Obershelp algorithm used for template similarity is
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
  write-up: `docs/maximo_oslc_methodology.md`.
- **Smart early-stop pagination.** Works around Maximo deployments
  that ignore OSLC date filters by sorting newest-first and stopping
  as soon as the page falls out of the requested window.
- **JSON-LD prefix tolerance layer.** Handles the `spi:` / `rdfs:` /
  unprefixed key variation across Maximo versions.
- **Long-description field dual-request technique.** Discovered
  empirically.
- **Kidana-specific custom field map.** Mapping of the Kidana `zz*`
  custom fields (`zzrequestorno`, `zzpcontract`, `zzextparty`,
  `zzbreachedtime`, `zzesclation`, ...) to their human-facing
  labels.
- **Persistent HTTP client with session cookie reuse.** Avoids
  per-page re-authentication during paginated calls.
- **Arabic duplicate-scoring algorithm.** Designed for the Kidana
  ticket vocabulary, including Arabic-specific normalization rules
  and the multi-signal scorer (template similarity, numeric overlap,
  token containment, time gap). Implementation:
  `src/duplicate_monitor/matching/scorer.py` and `matching/legacy.py`.
  Full write-up: `docs/scoring_algorithm.md`.

### 3. Internal Codebase Lineage

This repository was split from the monorepo at
`C:\Users\USER\Desktop\Incident-Description` on 2026-06-04. The
original repository is preserved unchanged as the archival source.
Git history was not carried over - each new repository begins with a
clean initial commit by design.
