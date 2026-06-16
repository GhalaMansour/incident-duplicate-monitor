"""Scanner subpackage — full-scan and quick-scan logic.

Re-exports the public surface of :mod:`full_scan` so callers can use
the shorter ``from duplicate_monitor import scanner`` form. The
web app and a few diagnostic paths consume the scanner that way;
the poller uses the longer ``from duplicate_monitor.scanner import
full_scan`` form and is unaffected.
"""

from . import full_scan
from .full_scan import (
    _full_scan_row_count,
    _rows_cache,
    _seed_cache_from_disk,
    load_scan,
    run_quick_scan,
    run_scan,
)

__all__ = (
    "full_scan",
    "run_scan",
    "run_quick_scan",
    "load_scan",
    # Private state that the web app reads at startup. Re-exported here
    # so ``from duplicate_monitor import scanner as _sc`` followed by
    # ``_sc._rows_cache`` etc. resolves correctly.
    "_seed_cache_from_disk",
    "_rows_cache",
    "_full_scan_row_count",
)
