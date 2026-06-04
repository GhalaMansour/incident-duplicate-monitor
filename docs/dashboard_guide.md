# Dashboard Guide

The dashboard is a FastAPI-served single-page application at
`http://localhost:8502` by default.

## Layout

- **Left panel** — list of detected duplicate groups, sorted by
  highest score first. Each entry shows the group size, the
  representative fault, the site, and the time of the most recent SR
  in the group.
- **Main panel** — the selected group rendered as a side-by-side
  comparison. Each SR is a column; each field is a row. Differences
  are highlighted.
- **Top bar** — filters for time window, site, and minimum score.
- **Decision controls** — for each SR in a group, mark it as: true
  duplicate, false positive, or needs review. Verdicts are persisted
  to SQLite.

## Common workflows

### Triage the latest groups

1. Open the dashboard. The leftmost panel lists new groups in score
   order.
2. Click the top group. The main panel renders the side-by-side
   comparison.
3. For each SR after the first, mark a verdict. The dashboard saves
   automatically.

### Filter to a single site during a Hajj event

1. Open the top-bar filter dropdown.
2. Select the site (Mina / Arafat / Muzdalifah).
3. The group list refreshes; only groups with at least one SR at the
   selected site are shown.

### Investigate a false-positive cluster

1. Identify a group that is repeatedly being flagged as not-a-
   duplicate.
2. Cross-check the score-explainability section (planned — see
   `README.md` "Future Improvements").
3. Until that ships: compare the Details fields manually. Common
   template-only false positives have the same boilerplate ("انقطاع
   كهرباء") with different asset numbers — the dashboard flags those
   with a `template_only` warning.

### Upload a one-off file for ad-hoc detection

1. Use the upload control (top-right). Drop an XLS or XLSX file.
2. The dashboard runs `read_file` + `detect` against the upload and
   shows the resulting groups. The persistent scan pickle is not
   affected unless you confirm the upload.

## Configuration

Dashboard-related environment variables:

| Variable | Default | Notes |
|----------|---------|-------|
| `LM_PORT` | `8502` | Bind port (overridden by `PORT` on Render) |
| `LM_TOASTS` | `true` | In-app toast notifications |
| `LM_WEBHOOK_URL` | (none) | Optional outbound JSON webhook on new alert |

## Troubleshooting

| Symptom | Cause | Action |
|---------|-------|--------|
| "No groups yet" | Poller hasn't completed a scan | Wait for the next `LM_QUICK_SCAN_SEC` tick |
| Stale data | Poller stopped | Check `monitor.log`; restart the service |
| Upload fails with "Cannot read file" | XLS file is HTML-disguised | The reader handles this automatically; if it fails, the file is corrupt |
| Verdict not saved | SQLite write failed | Check disk space and file permissions on `monitor.db` |
