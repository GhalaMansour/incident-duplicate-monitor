"""Full-scan duplicate detector.

Two scan modes:

* :func:`run_quick_scan` is called every ``LM_QUICK_SCAN_SEC`` seconds.
  It fetches only the newest page from Maximo, merges into an in-memory
  row cache, runs detect when new SRs are found, and writes the live
  pickle so the dashboard refreshes within a few seconds.

* :func:`run_scan` is called every ``LM_SCAN_SEC`` seconds. It fetches
  the configured paginated window (typically the last ~48 hours),
  replaces the row cache, runs detect unconditionally to catch
  old-vs-new pairs the quick scan may miss, and writes the live pickle.
"""

from __future__ import annotations

import logging
import pickle
import traceback
from datetime import datetime
from typing import Optional

from duplicate_monitor.core.config import CFG

log = logging.getLogger("duplicate_monitor.scanner")


def _enrich_contractor(rows: list[dict], maximo_source=None) -> None:
    """Fill missing contractor names directly from Maximo.

    Strategy:
      1. Try contract codes (zzpcontract) → mxcontract/mxpo → description/vendor
      2. Try ownergroup codes             → mxpersongroup   → description
    """
    if not maximo_source:
        return

    # ── Pass 1: contract code → company name ─────────────────────────────
    missing_contract = list(
        {r["contract"] for r in rows if r.get("contract") and not r.get("contractor")}
    )
    if missing_contract:
        try:
            cmap = maximo_source.fetch_contract_names(missing_contract)
            for r in rows:
                if not r.get("contractor") and r.get("contract"):
                    name = cmap.get(r["contract"], "")
                    if name:
                        r["contractor"] = name
                        r["party"] = name
        except Exception as e:
            log.warning("fetch_contract_names failed: %s", e)

    # ── Pass 2: ownergroup code → person-group description ───────────────
    missing_og = list(
        {r["ownergroup"] for r in rows if r.get("ownergroup") and not r.get("contractor")}
    )
    if missing_og:
        try:
            ogmap = maximo_source.fetch_ownergroup_names(missing_og)
            for r in rows:
                if not r.get("contractor") and r.get("ownergroup"):
                    name = ogmap.get(r["ownergroup"], "")
                    if name:
                        r["contractor"] = name
                        r["party"] = name
        except Exception as e:
            log.warning("fetch_ownergroup_names failed: %s", e)

    filled = sum(1 for r in rows if r.get("contractor"))
    log.info("Contractor enrich done — %d/%d rows have a name", filled, len(rows))


def _enrich_reporter(rows: list[dict], maximo_source=None) -> None:
    """Replace username codes in reporter field with Arabic display names.

    reporter (reportedby) contains Maximo usernames like 'A.SHAQDAR'.
    We query mxperson to get the Arabic displayname and store it in
    reporter_display so the comparison card can show a real name.
    """
    if not maximo_source:
        return
    usernames = list(
        {r["reporter"] for r in rows if r.get("reporter") and not r.get("reporter_display")}
    )
    if not usernames:
        return
    try:
        pmap = maximo_source.fetch_person_names(usernames)
        filled = 0
        for r in rows:
            uname = r.get("reporter", "")
            if uname and uname in pmap:
                r["reporter_display"] = pmap[uname]
                filled += 1
        log.info("Reporter enrich done — %d/%d rows got display names", filled, len(rows))
    except Exception as e:
        log.warning("fetch_person_names failed: %s", e)


# ─── In-memory row cache (persists across scans in the same process) ─────────
# Maps sr (str) → raw row dict.  Populated by run_scan; updated by run_quick_scan.
_rows_cache: dict[str, dict] = {}

# User-selectable comparison window (days).  None = fall back to CFG.max_days.
# Set by run_scan() when the dashboard passes an explicit max_days, so every
# subsequent quick scan (every 15 s) uses the SAME window.  Without this, a
# manual fetch with max_days=7 would be followed by 15-s quick scans that
# silently revert to the default 2, and newly-added SRs would no longer match
# the older ones the user just pulled.
_active_max_days: Optional[int] = None


def _effective_max_days(override: Optional[int] = None) -> int:
    """Return the comparison window to use, updating the active value when an
    explicit override is supplied (clamped to a sane 1–30 day range)."""
    global _active_max_days
    if override is not None:
        try:
            ov = int(override)
            if ov > 0:
                _active_max_days = max(1, min(30, ov))
        except (TypeError, ValueError):
            pass
    return _active_max_days if _active_max_days is not None else CFG.max_days


# How many SRs the last *full* scan returned.  Used by run_quick_scan() as a
# reference so the guard never compares against a potentially-small pkl written
# by a previous quick scan.  Reset to 0 on process start; stays 0 until the
# first full scan completes in this process (or is overridden by startup seeding).
_full_scan_row_count: int = 0

# Tracks group IDs seen in the PREVIOUS scan so we can surface truly-new groups
# to the notification system.  Seeded from disk on first run.
_last_group_ids: set[str] = set()


def _seed_cache_from_disk() -> None:
    """Seed quick-scan cache (and known group IDs) from the last saved full scan.

    Also updates _full_scan_row_count so the quick-scan guard has a reliable
    reference even before the first full scan completes in this process.

    Re-seeds if the saved pkl has significantly more rows than the current cache
    — this prevents quick-scan from working with stale partial data after a
    server restart that happens before the first full scan.
    """
    global _last_group_ids, _full_scan_row_count
    if not CFG.scan_pkl.exists():
        return
    try:
        saved = pickle.loads(CFG.scan_pkl.read_bytes())
    except Exception:
        return
    rows = saved.get("raw_rows") or saved.get("all_rows") or []
    disk_count = len(rows)
    disk_source = saved.get("source", "")

    # Only update _full_scan_row_count from a genuine FULL scan result.
    # Quick-scan pkls (source="maximo_quick") can have far fewer rows than
    # a real full scan — using their count as a reference lets small results
    # pass the guard on the NEXT quick scan, which is the core bug.
    if disk_source in ("maximo", "file") and disk_count > _full_scan_row_count:
        _full_scan_row_count = disk_count
        log.info(
            "Seeded _full_scan_row_count=%d from pkl (source=%s)", _full_scan_row_count, disk_source
        )

    # Always seed the row cache from disk when it is significantly smaller.
    # (We still seed even from a quick-scan pkl so the cache has SOMETHING
    # to work with while we wait for the first full scan.)
    if len(_rows_cache) >= max(1, disk_count) * 0.70:
        return

    for r in rows:
        sr = str(r.get("sr", "")).strip()
        if sr:
            _rows_cache[sr] = r
    if _rows_cache:
        log.info(
            "Seeded cache from disk (%d SRs, source=%s, _full_scan_row_count=%d)",
            len(_rows_cache),
            disk_source,
            _full_scan_row_count,
        )

    # Also seed known group IDs so first scan after server restart doesn't spam
    if not _last_group_ids:
        for g in saved.get("groups", []):
            members = g.get("members", [])
            gid = "_".join(sorted(str(m.get("sr", "")) for m in members if m.get("sr")))
            if gid:
                _last_group_ids.add(gid)
        if _last_group_ids:
            log.info("Seeded %d known group IDs from disk", len(_last_group_ids))


def _compute_new_groups(groups: list[dict]) -> list[dict]:
    """Return groups whose IDs weren't in the previous scan, then update _last_group_ids."""
    global _last_group_ids
    current_ids: set[str] = set()
    new_groups = []
    for g in groups:
        members = g.get("members", [])
        gid = "_".join(sorted(str(m.get("sr", "")) for m in members if m.get("sr")))
        if gid:
            current_ids.add(gid)
            if gid not in _last_group_ids:
                # Build a compact summary for notification
                first = members[0] if members else {}
                new_groups.append(
                    {
                        "gid": gid,
                        "score": g.get("score", 0),
                        "tier": g.get("tier", ""),
                        "size": len(members),
                        "fault": (
                            first.get("fault_full")
                            or first.get("fault_orig")
                            or first.get("fault")
                            or ""
                        )[:60],
                        "loc": (first.get("location") or first.get("loc") or "")[:40],
                        "srs": [str(m.get("sr", "")) for m in members[:4]],
                    }
                )
    _last_group_ids = current_ids
    return new_groups


# ─── DataFrame builder ────────────────────────────────────────────────────────


def _rows_to_df(rows: list[dict]):
    import pandas as pd

    records = []
    for r in rows:
        records.append(
            {
                "Service Request": r.get("sr", ""),
                "LOCATION": r.get("location", ""),
                "Asset": r.get("asset", ""),
                "Summary": r.get("summary", ""),
                "Details": r.get("detail", ""),
                "Status": r.get("status", ""),
                "Reported Date": r.get("reported", ""),
                # REPORTED NAME = اسم المُبلِّغ العربي (zzrequestor); Reported By = اسم المستخدم
                "REPORTED NAME": r.get("reported_name", "") or r.get("reporter", ""),
                "Reported By": r.get("reporter", ""),
                # Kidana custom columns — detect() reads these by name
                "REQUESTOR NO.": r.get("requestor_no", "") or r.get("caller_phone", ""),
                "Contract": r.get("contract", ""),
                "Contractor": r.get("contractor", ""),
                "الجهة": r.get("party", ""),
                "زمن الاستجابه": r.get("resp_time", ""),
                "Response Esclation": r.get("resp_esc", ""),
                "Work Zone": r.get("workzone", ""),
                "Status Description": r.get("status_desc", ""),
                "Caller Name": r.get("caller_name", ""),
                "Caller Phone": r.get("caller_phone", ""),
                "Caller Email": r.get("caller_email", ""),
                "Caller Party": r.get("caller_party", ""),
                "Reporter Phone": r.get("reporter_phone", ""),
                "Reporter Email": r.get("reporter_email", ""),
                "Source": r.get("source", ""),
                "Source Description": r.get("source_desc", ""),
                "Owner Group": r.get("ownergroup", ""),
                "Assigned Owner Group": r.get("assigned_ownergroup", ""),
                "Latitude": r.get("lat", ""),
                "Longitude": r.get("lon", ""),
                "Site": r.get("siteid", ""),
                "Internal Priority": r.get("priority", ""),
                "Priority Desc": r.get("priority_desc", ""),
                "History": r.get("history", ""),
                "Act Start": r.get("actstart", ""),
                "Act Finish": r.get("actfinish", ""),
                "Status Date": r.get("statusdate", ""),
            }
        )
    return pd.DataFrame(records)


def _extract_zone_block(loc_ar: str) -> tuple[str, str]:
    """Extract (المنطقة, المربع) from an Arabic location description.

    Location descriptions look like:
        'مشعر منى-مخيم غير مطور-منطقة ج-مربع 79-شارع ...'
    Returns ('منطقة ج', 'مربع 79').
    """
    import re

    region = block = ""
    if not loc_ar:
        return region, block
    for seg in re.split(r"\s*[-–—]\s*", str(loc_ar)):
        seg = seg.strip()
        if not region:
            mm = re.match(r"^منطقة\s+(.+)$", seg)
            if mm:
                region = "منطقة " + mm.group(1).strip()
        if not block:
            mm = re.match(r"^مربع\s+(.+)$", seg)
            if mm:
                block = "مربع " + mm.group(1).strip()
    return region, block


def _enrich_member(m: dict, raw: dict, extra: tuple) -> None:
    """Re-attach EXTRA Maximo fields + derive المنطقة/المربع from loc_ar."""
    for k in extra:
        if not m.get(k):
            m[k] = raw.get(k, "")
    # Derive المنطقة (region) / المربع (block) from the location description
    if not m.get("region") or not m.get("block"):
        region, block = _extract_zone_block(m.get("loc_ar", ""))
        if region and not m.get("region"):
            m["region"] = region
        if block and not m.get("block"):
            m["block"] = block


def _enrich_result(result: dict, sr_to_raw: dict) -> None:
    """Re-attach extra Maximo fields that detect() strips."""
    EXTRA = (
        "caller_name",
        "caller_phone",
        "caller_email",
        "caller_party",
        "reporter_phone",
        "reporter_email",
        "source",
        "source_desc",
        "ownergroup",
        "assigned_ownergroup",
        "lat",
        "lon",
        "priority_desc",
        "siteid",
        "history",
        "actstart",
        "actfinish",
        "statusdate",
        # Kidana custom — Contract / Contractor / الجهة / REQUESTOR NO. / SLA
        "contract",
        "contractor",
        "party",
        "requestor_no",
        "reported_name",
        "resp_time",
        "resp_esc",
        "targetstart",
        "targetfinish",
    )
    for g in result.get("groups", []):
        for m in g.get("members", []):
            raw = sr_to_raw.get(m.get("sr", ""))
            if raw:
                _enrich_member(m, raw, EXTRA)
    for p in result.get("pairs", []):
        for side in ("r1", "r2"):
            m = p.get(side)
            if not m:
                continue
            raw = sr_to_raw.get(m.get("sr", ""))
            if raw:
                _enrich_member(m, raw, EXTRA)
    for m in result.get("all_rows", []):
        raw = sr_to_raw.get(m.get("sr", ""))
        if raw:
            _enrich_member(m, raw, EXTRA)


def _save_result(result: dict, all_rows: list[dict]) -> None:
    """Persist the scan result under :attr:`CFG.scan_pkl`."""
    CFG.scan_pkl.write_bytes(pickle.dumps(result))
    log.info("Scanner: saved -> %s", CFG.scan_pkl)


# ─── ScanError ────────────────────────────────────────────────────────────────


class ScanError(Exception):
    pass


# ─── Quick scan (runs every 15 s) ─────────────────────────────────────────────


def run_quick_scan(maximo_source) -> dict:
    """Fetch only the newest page (200 SRs), merge into cache, run detect()
    only when new SRs are found.  Returns immediately (no-op) if nothing changed.
    """
    global _rows_cache
    summary = {
        "sr_count": 0,
        "new_count": 0,
        "group_count": 0,
        "pair_count": 0,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source": "maximo_quick",
        "error": "",
        "unchanged": False,
    }
    try:
        _seed_cache_from_disk()

        # 1. Fetch one page (newest 200 SRs, no date filter)
        rows = maximo_source.fetch_latest(max_pages=1)
        if not rows:
            summary["error"] = "No SRs returned"
            return summary

        # Deduplicate within the fetched page
        page_map: dict[str, dict] = {}
        for r in rows:
            sr = str(r.get("sr", "")).strip()
            if sr and sr not in page_map:
                page_map[sr] = r

        # 2. Find truly new SRs
        new_srs = {sr: r for sr, r in page_map.items() if sr not in _rows_cache}
        summary["new_count"] = len(new_srs)
        changed_count = 0
        watch_fields = (
            "reported",
            "status",
            "status_desc",
            "location",
            "asset",
            "summary",
            "detail",
            "reporter",
            "workzone",
            "siteid",
            "priority",
            "priority_desc",
            "caller_phone",
            "caller_party",
            "source",
            "source_desc",
            "ownergroup",
            "assigned_ownergroup",
            "lat",
            "lon",
        )
        for sr, row in page_map.items():
            old = _rows_cache.get(sr)
            if not old:
                continue
            if any(str(old.get(k, "") or "") != str(row.get(k, "") or "") for k in watch_fields):
                _rows_cache[sr] = row
                changed_count += 1
        summary["changed_count"] = changed_count

        if not new_srs and not changed_count and _rows_cache:
            # Nothing new — skip detect() entirely
            log.debug("Quick scan: no new or changed SRs, skipping detect()")
            summary["sr_count"] = len(_rows_cache)
            summary["unchanged"] = True
            return summary

        # 3. Merge new SRs into cache
        _rows_cache.update(new_srs)
        if not _rows_cache:
            # First ever quick scan with empty cache — seed from page
            _rows_cache.update(page_map)

        all_rows = list(_rows_cache.values())
        summary["sr_count"] = len(all_rows)
        log.info(
            "Quick scan: +%d new, %d changed SRs → %d total in cache",
            len(new_srs),
            changed_count,
            len(all_rows),
        )

        # Enrich contractor names and reporter display names from Maximo
        _enrich_contractor(all_rows, maximo_source)
        _enrich_reporter(all_rows, maximo_source)

        # 4. Run detect()
        from duplicate_monitor.matching.legacy import detect

        df = _rows_to_df(all_rows)
        result = detect(df, min_score=CFG.min_score, max_days=_effective_max_days())

        # 5. Enrich with extra fields
        sr_to_raw = {r.get("sr", ""): r for r in all_rows if r.get("sr")}
        _enrich_result(result, sr_to_raw)

        groups = result.get("groups", [])
        pairs = result.get("pairs", [])
        summary["group_count"] = len(groups)
        summary["pair_count"] = len(pairs)

        result["scanned_at"] = summary["scanned_at"]
        result["sr_count"] = len(all_rows)
        result["source"] = "maximo_quick"
        result["raw_rows"] = all_rows
        result["max_days"] = _effective_max_days()

        # Detect truly-new groups (weren't in previous scan)
        new_groups = _compute_new_groups(groups)
        result["new_groups"] = new_groups
        summary["new_groups"] = new_groups
        if new_groups:
            log.info("Quick scan: %d NEW groups detected → notification triggered", len(new_groups))

        # Guard: quick scan must never overwrite a rich full-scan result.
        #
        # The ONLY reliable reference is _full_scan_row_count, which is set
        # exclusively by run_scan() (or seeded from a full-scan pkl at startup).
        # We deliberately do NOT fall back to the pkl's sr_count, because the
        # pkl might itself be a previous quick-scan result with a small row
        # count — using it as a reference is what caused the 8-vs-41 bug.
        #
        # If _full_scan_row_count == 0 it means no full scan has run in this
        # process yet (and no full-scan pkl exists on disk), so we refuse to
        # save at all.  The periodic full scan will establish the baseline.
        if _full_scan_row_count == 0:
            log.info(
                "Quick scan: skipping save — no full-scan baseline yet "
                "(run_scan() not called in this process; waiting for full scan)"
            )
            return summary

        if len(all_rows) < _full_scan_row_count * 0.70:
            log.info(
                "Quick scan: skipping save — %d rows < %.0f%% of full-scan baseline %d",
                len(all_rows),
                70.0,
                _full_scan_row_count,
            )
            result["sr_count"] = _full_scan_row_count
            return summary

        # Group-count guard: a quick scan must NEVER overwrite a saved result
        # that has more groups. detect() is window/parameter sensitive and a
        # degraded run (e.g. after a DNS hiccup or partial fetch) can drop the
        # group count from 13 → 4. Groups are the user's primary data and must
        # not silently vanish. Quick scans are always background → no force path.
        try:
            prev = pickle.loads(CFG.scan_pkl.read_bytes()) if CFG.scan_pkl.exists() else None
        except Exception:
            prev = None
        if prev and prev.get("source") in ("maximo", "file", "maximo_quick"):
            prev_groups = len(prev.get("groups", []))
            new_grp_cnt = len(groups)
            if prev_groups > 5 and new_grp_cnt < prev_groups * 0.85:
                log.warning(
                    "Quick scan: new result has %d groups vs previous %d "
                    "(dropped >15%%) — skipping overwrite to protect user data. "
                    "Use the manual scan button to force a refresh.",
                    new_grp_cnt,
                    prev_groups,
                )
                summary["skipped_overwrite"] = True
                return summary

        _save_result(result, all_rows)
        log.info("Quick scan: saved — %d groups, %d pairs", len(groups), len(pairs))

    except ScanError as e:
        summary["error"] = str(e)
        log.error("Quick scan error: %s", e)
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        log.error("Quick scan failed: %s\n%s", e, traceback.format_exc())

    return summary


# ─── Full scan (runs every LM_SCAN_SEC, default 300 s) ───────────────────────


def run_scan(maximo_source, force: bool = False, max_days: Optional[int] = None) -> dict:
    """Fetch the last N days of SRs, replace cache, run detect().

    Args:
        force: If True, bypass the safety guard and always save the result.
               Used when the user explicitly presses the "سحب من Maximo" button.
               Background/periodic scans should pass force=False (default).
        max_days: Comparison window in days chosen by the user at fetch time.
               When supplied it becomes the active window for ALL later quick
               scans too (see _effective_max_days).  None = keep current/default.

    Uses cutoff_hours = LM_FULL_SCAN_DAYS * 24 (default 7 days = 168 h), but is
    widened to at least max_days*24 so the chosen window actually has older SRs
    to compare against.
    """
    global _rows_cache, _full_scan_row_count
    summary = {
        "sr_count": 0,
        "group_count": 0,
        "pair_count": 0,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source": "maximo",
        "error": "",
    }
    try:
        # Window honoured exactly from config — NO hard 48h floor.
        # During Hajj season 48h can be 3000+ SRs, which overloads detect()
        # and the map. LM_SCAN_HOURS (if set) overrides; else full_scan_days*24.
        # detect() still filters pairs by max_days, so only SRs within that gap
        # are treated as potential duplicates.
        import os as _os

        try:
            _hours_env = int(_os.environ.get("LM_SCAN_HOURS", "0"))
        except ValueError:
            _hours_env = 0
        # Resolve the comparison window first; it also becomes the active window
        # for subsequent quick scans.
        md = _effective_max_days(max_days)
        cutoff_hours = _hours_env if _hours_env > 0 else (CFG.full_scan_days or 1) * 24
        # Widen the fetch window so the chosen comparison window has older SRs
        # to match against (no point comparing 7 days if we only fetched 2).
        cutoff_hours = max(cutoff_hours, md * 24)
        rows = maximo_source.fetch_latest(
            max_pages=CFG.full_scan_max_pages or 25,
            cutoff_hours=cutoff_hours,
        )
        log.info(
            "Full scan: fetched %d rows (cutoff=%dh, max_days=%d)", len(rows), cutoff_hours, md
        )

        if not rows:
            summary["error"] = "No SRs returned from Maximo"
            return summary

        # Deduplicate
        unique: dict[str, dict] = {}
        for r in rows:
            sr = str(r.get("sr", "")).strip()
            if sr and sr not in unique:
                unique[sr] = r
        rows = list(unique.values())
        summary["sr_count"] = len(rows)
        log.info("Full scan: %d unique SRs", len(rows))

        # Replace row cache — build new dict first, then assign atomically.
        # Assigning to the module-level name is a single bytecode STORE_GLOBAL
        # which the CPython GIL makes atomic, so quick_scan never sees an empty
        # or partially-filled cache.
        new_cache: dict[str, dict] = {}
        for r in rows:
            sr = str(r.get("sr", "")).strip()
            if sr:
                new_cache[sr] = r
        _rows_cache = new_cache  # atomic swap — quick_scan sees old OR new, never empty

        # Record authoritative row count so quick_scan guard has a solid baseline.
        _full_scan_row_count = len(rows)
        log.info(
            "Full scan: cache updated (%d rows, _full_scan_row_count=%d)",
            len(_rows_cache),
            _full_scan_row_count,
        )

        # ── Enrich contractor names and reporter display names ───────────
        _enrich_contractor(rows, maximo_source)
        _enrich_reporter(rows, maximo_source)

        # Run detect()
        try:
            from find_duplicates import detect  # type: ignore
        except ImportError as e:
            raise ScanError(f"Cannot import find_duplicates: {e}") from e

        df = _rows_to_df(rows)
        result = detect(df, min_score=CFG.min_score, max_days=md)

        # Enrich
        sr_to_raw = {r.get("sr", ""): r for r in rows if r.get("sr")}
        _enrich_result(result, sr_to_raw)

        groups = result.get("groups", [])
        pairs = result.get("pairs", [])
        summary["group_count"] = len(groups)
        summary["pair_count"] = len(pairs)
        log.info("Full scan: %d groups, %d pairs", len(groups), len(pairs))

        result["scanned_at"] = summary["scanned_at"]
        result["sr_count"] = len(rows)
        result["source"] = "maximo"
        result["raw_rows"] = rows
        result["max_days"] = md

        # Detect truly-new groups vs previous scan
        new_groups = _compute_new_groups(groups)
        result["new_groups"] = new_groups
        summary["new_groups"] = new_groups
        if new_groups:
            log.info("Full scan: %d NEW groups detected → notification triggered", len(new_groups))

        # Safety guard: never overwrite a richer result with fewer groups.
        # Groups are the user's primary data — they must never silently vanish.
        # We check group count independently of row count: even if Maximo
        # returns the same number of rows, a different detect() run can
        # produce far fewer groups (window shift, parameter sensitivity).
        # Only a MANUAL scan (force=True) bypasses this guard.
        try:
            prev = pickle.loads(CFG.scan_pkl.read_bytes()) if CFG.scan_pkl.exists() else None
        except Exception:
            prev = None
        if not force and prev and prev.get("source") in ("maximo", "file"):
            prev_groups = len(prev.get("groups", []))
            new_grp_cnt = len(groups)
            if prev_groups > 5 and new_grp_cnt < prev_groups * 0.85:
                log.warning(
                    "Full scan: new result has %d groups vs previous %d "
                    "(dropped >15%%) — skipping overwrite to protect user data. "
                    "Use the manual scan button to force an update.",
                    new_grp_cnt,
                    prev_groups,
                )
                summary["skipped_overwrite"] = True
                return summary

        _save_result(result, rows)

    except ScanError as e:
        summary["error"] = str(e)
        log.error("Full scan error: %s", e)
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        log.error("Full scan failed: %s\n%s", e, traceback.format_exc())

    return summary


# ─── Load cached scan (used by dashboard) ────────────────────────────────────


def load_scan() -> Optional[dict]:
    if not CFG.scan_pkl.exists():
        return None
    try:
        return pickle.loads(CFG.scan_pkl.read_bytes())
    except Exception as e:
        log.warning("Failed to load scan pkl: %s", e)
        return None
