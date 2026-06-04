# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-04

### Added

- Initial release as an independent repository, split from the
  Kidana monorepo at `C:\Users\USER\Desktop\Incident-Description`.
- 24/7 polling service against Maximo OSLC.
- Quick scan (every 15 s) and full scan (every 5 min) with smart
  early-stop pagination.
- FastAPI dashboard at port 8502 by default.
- Matching layer carrying the Arabic scoring algorithm:
  `normalize_arabic`, `smart_text_compare`, `score_pair`, and the
  bulk `detect` pass. Full algorithm documented in
  `docs/scoring_algorithm.md`.
- Maximo OSLC source implementing the in-house six-way endpoint and
  authentication fallback methodology, JSON-LD prefix tolerance,
  smart early-stop pagination, persistent session, and the Kidana
  `zz*` custom-field map. Full methodology in
  `docs/maximo_oslc_methodology.md`.
- File-source fallback for offline / demo use.
- SQLite-backed alert history, seen-SR cache, and poll audit log.
- Comprehensive `README.md`, `REPORT.md` (Markdown and Word), and
  `docs/` set (architecture, scoring algorithm, Maximo methodology,
  dashboard guide, deployment, security).
- Continuous integration workflows for ruff, mypy, pytest, and CodeQL.

### Removed

- Legacy Streamlit dashboard (`live_monitor/dashboard.py`). The
  FastAPI dashboard is the single supported UI.
- `sys.path` hacks for reaching `scripts/find_duplicates.py`. The
  matching helpers are now first-class public APIs under
  `duplicate_monitor.matching`.
- Hardcoded mirror path to `app/_last_result.pkl`. The scan pickle
  is written inside the package directory only.

### Notes

- Git history was not carried over. The original monorepo is
  preserved as the archival source.
