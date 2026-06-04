"""File-based source — fallback when the Maximo API is unreachable.

Watches a directory for the newest XLS/XLSX file, reads it using the
same loader as the bulk detector, and yields SRs whose report date
falls within the lookback window.

This is the offline-demo and safety-net mode used when Maximo
credentials are missing or the network is down.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from duplicate_monitor.core.config import CFG

log = logging.getLogger("duplicate_monitor.file")


class FileSourceError(Exception):
    """Raised when no usable file is found or parsing fails."""


def _newest_excel(folder: Path) -> Optional[Path]:
    if not folder.exists():
        return None
    candidates = []
    for p in folder.iterdir():
        if not p.is_file():           continue
        if p.name.startswith("~$"):   continue   # Excel lock files
        if p.suffix.lower() not in (".xls", ".xlsx"):
            continue
        # Skip obvious output files
        name = p.name.lower()
        if any(skip in name for skip in (
            "_مكررات", "نتائج_المراجعة", "asset_description",
            "loction_decription", "enriched"
        )):
            continue
        candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


class FileSource:
    def __init__(self):
        self.folder = Path(CFG.file_watch_dir or CFG.data_dir)
        self._last_path:  Optional[Path] = None
        self._last_mtime: float = 0.0

    def configured(self) -> bool:
        return self.folder.exists()

    def fetch_recent(self, lookback_minutes: Optional[int] = None) -> list[dict]:
        path = _newest_excel(self.folder)
        if path is None:
            raise FileSourceError(
                f"No XLS/XLSX file found in {self.folder}"
            )

        from duplicate_monitor.matching.legacy import read_file

        try:
            df = read_file(str(path))
        except Exception as e:
            raise FileSourceError(f"Failed to read {path.name}: {e}") from e

        self._last_path  = path
        self._last_mtime = path.stat().st_mtime

        # Resolve the columns we care about — case-insensitive
        col_map = {c.lower(): c for c in df.columns}

        def _col(*names: str) -> Optional[str]:
            for n in names:
                if n.lower() in col_map:
                    return col_map[n.lower()]
            return None

        col_sr       = _col("Service Request", "Ticket ID", "ticketid", "رقم البلاغ", "SR")
        col_reported = _col("تاريخ فتح البلاغ", "Reported Date", "reportdate")
        col_status   = _col("Status", "status", "الحالة")
        col_loc      = _col("LOCATION", "Location", "location")
        col_asset    = _col("ASSET", "Asset", "assetnum")
        col_summ     = _col("Summary", "summary", "العطل")
        col_detail   = _col("Details", "Description", "التفاصيل")

        if not col_sr:
            found = list(df.columns[:20])  # first 20 cols for diagnosis
            log.error("SR column not found. Columns in %s: %s", path.name, found)
            raise FileSourceError(
                f"No SR column in {path.name}. Found: {found}"
            )

        lookback = lookback_minutes if lookback_minutes is not None else CFG.lookback_minutes
        since    = datetime.now(timezone.utc) - timedelta(minutes=lookback)

        rows: list[dict] = []
        for _, row in df.iterrows():
            sr = str(row.get(col_sr, "")).strip()
            if not sr or sr.lower() == "nan":
                continue

            reported_raw = str(row.get(col_reported, "")) if col_reported else ""
            # Best-effort date parse — file rows may include older SRs too.
            # We don't filter strictly by date here because the file may be
            # a full daily dump; let the polling layer dedupe on sr_seen.
            rows.append({
                "sr":       sr,
                "siteid":   "",
                "location": str(row.get(col_loc, "")) if col_loc else "",
                "asset":    str(row.get(col_asset, "")) if col_asset else "",
                "summary":  str(row.get(col_summ, "")) if col_summ else "",
                "status":   str(row.get(col_status, "")) if col_status else "",
                "priority": "",
                "reported": reported_raw,
                "detail":   str(row.get(col_detail, "")) if col_detail else "",
                "class_id": "",
                "_raw":     {},
            })

        log.info("File source | path=%s | rows=%d", path.name, len(rows))
        return rows

    def last_file(self) -> Optional[str]:
        return str(self._last_path) if self._last_path else None
