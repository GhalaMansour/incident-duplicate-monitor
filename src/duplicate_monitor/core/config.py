"""Runtime configuration for the duplicate monitor.

All tunables are environment variables loaded once on module import.
A ``.env`` file in the current working directory is consulted via
``python-dotenv`` if available; values already set in the process
environment win.

The configuration is exposed as the module-level singleton :data:`CFG`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Resolve the package directory once. On-disk artifacts (db, log, pkl)
# default to live next to the package so the service has no dependency
# on the working directory.
PACKAGE_DIR: Path = Path(__file__).resolve().parent.parent
REPO_ROOT: Path = PACKAGE_DIR.parent.parent


def _load_dotenv_if_available() -> None:
    """Best-effort load of a local ``.env`` file."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env", override=False)


_load_dotenv_if_available()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class Config:
    # Maximo connection
    maximo_base_url: str = field(default_factory=lambda: _env("MAXIMO_BASE_URL"))
    maximo_user: str = field(
        default_factory=lambda: _env("MAXIMO_USER") or _env("MAXIMO_USERNAME")
    )
    maximo_pass: str = field(
        default_factory=lambda: _env("MAXIMO_PASS") or _env("MAXIMO_PASSWORD")
    )
    maximo_site_id: str = field(default_factory=lambda: _env("MAXIMO_SITE_ID"))

    # Polling behaviour
    poll_interval_sec: int = field(default_factory=lambda: _env_int("LM_POLL_SEC", 15))
    lookback_minutes: int = field(default_factory=lambda: _env_int("LM_LOOKBACK_MIN", 60))
    page_size: int = field(default_factory=lambda: _env_int("LM_PAGE_SIZE", 200))
    request_timeout: int = field(default_factory=lambda: _env_int("LM_TIMEOUT_SEC", 20))

    # Source strategy: auto | maximo | file | both.
    source_mode: str = field(default_factory=lambda: _env("LM_SOURCE_MODE", "auto"))
    file_watch_dir: Optional[Path] = field(default=None)

    # Detection thresholds
    min_score: int = field(default_factory=lambda: _env_int("LM_MIN_SCORE", 7))
    max_days: int = field(default_factory=lambda: _env_int("LM_MAX_DAYS", 2))
    alert_score: int = field(default_factory=lambda: _env_int("LM_ALERT_SCORE", 8))

    # Full-scan window
    quick_scan_sec: int = field(default_factory=lambda: _env_int("LM_QUICK_SCAN_SEC", 15))
    scan_interval_sec: int = field(default_factory=lambda: _env_int("LM_SCAN_SEC", 300))
    open_statuses: str = field(
        default_factory=lambda: _env(
            "LM_OPEN_STATUSES", "OPEN,NEW,QUEUED,INPRG,WPCOND,WAPPR,WMATL"
        )
    )
    scan_start_date: str = field(default_factory=lambda: _env("LM_SCAN_START_DATE", ""))
    scan_end_date: str = field(default_factory=lambda: _env("LM_SCAN_END_DATE", ""))
    # Rolling lookback window in days. The effective scan window is
    # max(scan_start_date, today - full_scan_days). 0 disables the rolling cap.
    full_scan_days: int = field(default_factory=lambda: _env_int("LM_FULL_SCAN_DAYS", 2))
    # Hard cap on page count for fetch_all. At 200 rows per page, 15 pages
    # covers the 3000 most recent SRs; raise for deeper historical scans.
    full_scan_max_pages: int = field(
        default_factory=lambda: _env_int("LM_FULL_SCAN_MAX_PAGES", 15)
    )

    # Storage — all under the package directory by default
    db_path: Path = field(default_factory=lambda: PACKAGE_DIR / "monitor.db")
    log_path: Path = field(default_factory=lambda: PACKAGE_DIR / "monitor.log")
    scan_pkl: Path = field(default_factory=lambda: PACKAGE_DIR / "live_scan.pkl")

    # Reference data bundled with the repo under data/.
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")

    # Dashboard
    dashboard_port: int = field(default_factory=lambda: _env_int("LM_PORT", 8502))

    # Notifications
    webhook_url: str = field(default_factory=lambda: _env("LM_WEBHOOK_URL"))
    enable_toasts: bool = field(default_factory=lambda: _env_bool("LM_TOASTS", True))
    notify_email: str = field(default_factory=lambda: _env("LM_NOTIFY_EMAIL"))
    smtp_host: str = field(default_factory=lambda: _env("LM_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _env_int("LM_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("LM_SMTP_USER"))
    smtp_pass: str = field(default_factory=lambda: _env("LM_SMTP_PASS"))
    smtp_from: str = field(default_factory=lambda: _env("LM_SMTP_FROM"))
    smtp_tls: bool = field(default_factory=lambda: _env_bool("LM_SMTP_TLS", True))

    def __post_init__(self) -> None:
        watch_dir = _env("LM_WATCH_DIR")
        self.file_watch_dir = Path(watch_dir) if watch_dir else self.data_dir

    @property
    def has_maximo_credentials(self) -> bool:
        return bool(self.maximo_base_url and self.maximo_user and self.maximo_pass)

    def summary(self) -> str:
        masked_pass = "***" if self.maximo_pass else "(missing)"
        return (
            f"Maximo URL:    {self.maximo_base_url or '(missing)'}\n"
            f"Maximo user:   {self.maximo_user or '(missing)'}\n"
            f"Maximo pass:   {masked_pass}\n"
            f"Site ID:       {self.maximo_site_id or '(any)'}\n"
            f"Source mode:   {self.source_mode}\n"
            f"Poll every:    {self.poll_interval_sec}s\n"
            f"Quick scan:    every {self.quick_scan_sec}s\n"
            f"Full scan:     every {self.scan_interval_sec}s\n"
            f"Lookback:      {self.lookback_minutes} min\n"
            f"Min score:     {self.min_score} (alert >= {self.alert_score})\n"
            f"Max days gap:  {self.max_days}\n"
            f"Full window:   newest {self.full_scan_max_pages * 200} SRs "
            f"({self.full_scan_max_pages} pages)\n"
            f"Database:      {self.db_path}\n"
            f"Dashboard:     http://localhost:{self.dashboard_port}\n"
        )


CFG: Config = Config()
