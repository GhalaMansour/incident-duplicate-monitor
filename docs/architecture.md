# Architecture

This document describes the runtime architecture of the Incident
Duplicate Monitor. The audience is engineers who will extend, deploy,
or review the codebase.

## Components

```
+--------------------------+   +--------------------------+
|  Maximo OSLC source      |   |  File source (fallback)  |
|  (sources.maximo)        |   |  (sources.file)          |
+-------------+------------+   +-------------+------------+
              |                              |
              +-----------+------------------+
                          |
                          v
              +-----------------------------+
              |  SourceManager              |
              |  (poller.runner)            |
              +-----------+-----------------+
                          |
            +-------------+-------------+
            |                           |
            v                           v
+----------------------+    +-----------------------+
|  Live engine         |    |  Full-scan scanner    |
|  (matching.engine)   |    |  (scanner.full_scan)  |
+----------+-----------+    +-----------+-----------+
           |                            |
           +-------------+--------------+
                         |
                         v
+----------------------------------------------+
|  Matching layer                              |
|  - normalize (normalize_arabic, strip_html)  |
|  - scorer   (smart_text_compare, score_pair) |
|  - legacy   (detect, read_file)              |
+----------------------+-----------------------+
                       |
              +--------+--------+
              |                 |
              v                 v
   +-------------------+   +-------------------+
   |  SQLite storage   |   |  live_scan.pkl    |
   |  (storage.db)     |   |  (dashboard input)|
   +---------+---------+   +---------+---------+
             |                       |
             +-----------+-----------+
                         |
                         v
              +--------------------------+
              |  FastAPI dashboard       |
              |  (web.app:app)           |
              +--------------------------+
```

## Layers

1. **Sources** (`duplicate_monitor.sources`). Two implementations of
   the same contract: fetch a list of normalized SR rows. The Maximo
   source implements the in-house OSLC methodology (see
   `maximo_oslc_methodology.md`). The file source watches a directory
   for the newest XLS/XLSX and is the offline / demo path.

2. **Poller** (`duplicate_monitor.poller`). The 24/7 driver. Uses a
   `SourceManager` that prefers Maximo and falls back to the file
   source after `FAIL_THRESHOLD` consecutive failures. On every tick
   it asks the live engine to score new SRs against the open set, and
   persists alerts to SQLite.

3. **Scanner** (`duplicate_monitor.scanner`). The full-scan path that
   runs every `LM_SCAN_SEC` seconds. Fetches the configured paginated
   window (typically the last 48 hours), runs the bulk detector, and
   writes the live pickle that the dashboard reads.

4. **Matching** (`duplicate_monitor.matching`). The IP of the project.
   `normalize` + `scorer` + the per-pair engine are the live path;
   `legacy.detect` is the bulk grouping pass. See
   `scoring_algorithm.md`.

5. **Storage** (`duplicate_monitor.storage`). A single SQLite file
   with three tables: `sr_seen` (dedup key for polling), `alerts`
   (duplicate-match events with user verdict), `poll_history` (audit
   log of poll attempts).

6. **Web** (`duplicate_monitor.web`). FastAPI dashboard that reads
   `live_scan.pkl` and the SQLite store. Side-by-side comparison UI
   for reviewing groups.

## Configuration boundary

All environment access funnels through `duplicate_monitor.core.config`.
The singleton `CFG` is constructed once on import. No other module
reads `os.environ` directly.

## Threading model

- The default `python -m duplicate_monitor` command runs the poller in
  a daemon thread and the FastAPI dashboard in the main thread via
  `uvicorn.run`.
- The poller acquires a single lock around the scan step so a long
  scan does not overlap with the next tick.
- SQLite access is serialized through a thread-local connection cache
  in `storage.db`.

## Error handling

- Source errors bubble up as `MaximoSourceError` or `FileSourceError`.
  The `SourceManager` catches them, increments a failure counter, and
  switches to the file source after the threshold.
- Detect/scan errors are caught and logged but do not stop the poller;
  the next tick retries.
- The dashboard returns HTTP 500 with a generic message on unhandled
  exceptions; the full traceback goes to `monitor.log`.
