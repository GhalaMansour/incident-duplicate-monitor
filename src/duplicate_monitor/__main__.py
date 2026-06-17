"""Entry point: ``python -m duplicate_monitor [SUBCOMMAND]``.

Subcommands:

* ``poller`` — Start the background polling loop (blocks).
* ``web``    — Launch the FastAPI dashboard.
* ``both``   — Run the poller in a background thread and the dashboard
               in the foreground. This is the default.
* ``tick``   — Run a single poll cycle and exit. Useful for cron and
               diagnostics.
* ``diag``   — Print the configuration and probe each Maximo strategy
               once to verify connectivity.

Run ``python -m duplicate_monitor --help`` for the full argument list.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from duplicate_monitor.core.config import CFG


def _configure_logging() -> None:
    """Configure stdout + file logging, forcing UTF-8 where possible.

    Windows consoles default to cp1256/cp1252, which raises on the
    Arabic strings emitted by the matching layer. Forcing UTF-8 on the
    standard streams avoids a UnicodeEncodeError tearing the process
    down on the first log line.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    CFG.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-25s %(message)s",
        handlers=[
            logging.FileHandler(CFG.log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_poller() -> None:
    from duplicate_monitor.poller.runner import main_loop

    main_loop()


def cmd_web() -> None:
    """Launch the FastAPI dashboard server.

    Port resolution order, highest priority first:

      1. ``PORT`` environment variable (set by managed platforms like
         Render) — wins so cloud deployments bind correctly out of
         the box without extra config.
      2. ``LM_PORT`` — the documented operator override, surfaced in
         ``.env.example`` and ``docs/dashboard_guide.md`` and loaded
         into ``CFG.dashboard_port``.
      3. The default ``8502``.
    """
    web_port = int(os.environ.get("PORT") or CFG.dashboard_port or 8502)
    print(f"Launching web dashboard on http://localhost:{web_port}")
    try:
        import uvicorn

        uvicorn.run(
            "duplicate_monitor.web.app:app",
            host="0.0.0.0",
            port=web_port,
            reload=False,
            log_level="warning",
        )
    except ImportError:
        print("uvicorn not installed; run: pip install uvicorn")
        sys.exit(1)


def cmd_both() -> None:
    """Run the poller in a daemon thread and the FastAPI dashboard."""
    thread = threading.Thread(target=cmd_poller, daemon=True, name="poller")
    thread.start()
    cmd_web()


def cmd_tick() -> None:
    """Run one poll cycle and exit."""
    from duplicate_monitor.poller.runner import SourceManager, run_tick
    from duplicate_monitor.storage import db

    db.init_db()
    sources = SourceManager()
    print(f"Current source mode: {sources.current_mode()}")
    summary = run_tick(sources)
    print("\nTick summary:")
    for key, value in summary.items():
        print(f"  {key:10s}: {value}")


def cmd_diag() -> None:
    """Print the configuration and probe each Maximo strategy once."""
    # Overlay any values the operator saved through the dashboard
    # settings page before printing the summary, so what diag shows
    # matches what the running service will use.
    from duplicate_monitor.storage import settings as _settings

    _settings.apply_to_cfg()

    print("=" * 60)
    print("Duplicate Monitor - Diagnostics")
    print("=" * 60)
    print(CFG.summary())

    # Detect a malformed .env file early — the most common operator
    # mistake is line-ending corruption that smuggles the next variable
    # name into the URL or leaves an invisible Arabic mark in a field.
    _env_warnings: list[str] = []
    if CFG.maximo_base_url and any(c in CFG.maximo_base_url for c in ("=", " ", "\t")):
        _env_warnings.append(
            "MAXIMO_BASE_URL يحتوي على محرف غريب (= أو مسافة). "
            "غالباً ملف .env فيه فاصل أسطر تالف. أعد إنشاء .env من "
            ".env.example وحرّره بمحرر يحفظ UTF-8 + CRLF (مثل VS Code)."
        )
    if CFG.maximo_user and "=" in CFG.maximo_user:
        _env_warnings.append("MAXIMO_USER فيه = — راجع ملف .env.")
    if _env_warnings:
        print("-" * 60)
        print("⚠️  تحذيرات في إعدادات .env:")
        for w in _env_warnings:
            print(f"   • {w}")
    print("-" * 60)

    if not CFG.has_maximo_credentials:
        print("Maximo credentials missing; only the file source will be tried.")
    else:
        from duplicate_monitor.sources.maximo import MaximoSource, MaximoSourceError

        maximo = MaximoSource()
        try:
            rows = maximo.fetch_recent(lookback_minutes=60)
            print(f"Maximo reachable - fetched {len(rows)} SR(s) in the last 60 min")
            for row in rows[:3]:
                print(
                    "   - SR {sr} | {summary} | {loc} | {reported}".format(
                        sr=row.get("sr"),
                        summary=(row.get("summary") or "")[:60],
                        loc=row.get("location") or "",
                        reported=row.get("reported") or "",
                    )
                )
        except MaximoSourceError as exc:
            print(f"Maximo unreachable: {exc}")

    from duplicate_monitor.sources.file import FileSource, FileSourceError

    file_source = FileSource()
    try:
        rows = file_source.fetch_recent()
        last_file = getattr(file_source, "last_file", lambda: "")()
        print(f"File source OK - {len(rows)} rows from {last_file}")
    except FileSourceError as exc:
        print(f"File source: {exc}")

    from duplicate_monitor.storage import db

    db.init_db()
    print(f"\nDatabase initialised at {CFG.db_path}")
    print(f"   seen={db.count_seen()} | alerts_open={db.alert_counts().get('open', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live duplicate-detection monitor for Maximo service requests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "cmd",
        nargs="?",
        default="both",
        choices=["poller", "web", "both", "tick", "diag"],
        help="Subcommand to run (default: both).",
    )
    args = parser.parse_args()
    _configure_logging()
    {
        "poller": cmd_poller,
        "web": cmd_web,
        "both": cmd_both,
        "tick": cmd_tick,
        "diag": cmd_diag,
    }[args.cmd]()


if __name__ == "__main__":
    main()
