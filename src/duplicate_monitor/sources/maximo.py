"""Maximo OSLC source — fetch new SRs.

Tries multiple endpoint/auth strategies because we don't know what
permissions or Maximo version the user has:

  1. /oslc/os/mxsr               (standard OSLC SR object — most common)
  2. /oslc/os/mxapisr             (newer JSON API alias)
  3. /maxrest/rest/os/mxsr        (legacy REST endpoint)

For each endpoint we try authentication as:
  a) MAXAUTH header (base64) — works on most modern Maximo
  b) Basic Auth                  — fallback
  c) j_security_check session    — last resort (form-based login)

The first combination that returns 200 with a non-empty member list wins
and is cached for subsequent calls.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx

from duplicate_monitor.core.config import CFG

log = logging.getLogger("duplicate_monitor.maximo")


# Fields we ask Maximo for. Mirrors find_duplicates expected columns.
# longdescription / description_longdescription is the Details field —
# critical for text-similarity scoring in find_duplicates.
_SELECT_FIELDS = ",".join(
    [
        "ticketid",
        "siteid",
        "location",
        "description",
        "assetnum",
        "internalpriority",
        "internalpriority_description",
        "status",
        "status_description",
        "reportdate",
        "statusdate",
        "classstructureid",
        "workzone",
        "reportedby",
        "reportedphone",
        "reportedemail",
        # History flag (Y/N) — shows in "History" column
        "historyflag",
        # Actual start/finish dates — تاريخ المباشره / تاريخ المعالجة
        "actstart",
        "actfinish",
        # Caller / requestor — what the user calls "رقم المبلّغ"
        "affectedperson",
        "affectedphone",
        "affectedemail",
        # Requestor (Kidana custom fields) — REQUESTOR NO. / REPORTED NAME
        "zzrequestorno",
        "zzrequestor",
        # Contract & external party (Kidana custom) — Contract / Contractor / الجهة
        "zzpcontract",
        "zzpcontract_description",
        "zzextparty",
        "zzextparty_description",
        # SLA / escalation (Kidana custom) — زمن الاستجابه / Response Escalation
        "zzbreachedtime",
        "zzesclation",
        # Target start/finish dates
        "targetstart",
        "targetfinish",
        # Actual finish (correct field name is actualfinish, not actfinish)
        "actualfinish",
        # Source channel (Call / Scada / etc.)
        "source",
        "source_description",
        # Contractor / assigned group (+ description = name of the person group)
        "ownergroup",
        "ownergroup_description",
        "assignedownergroup",
        "assignedownergroup_description",
        # GPS coords for map view
        "latitudey",
        "longitudex",
        "autolocate",
        # Caller party (extra context field)
        "zzcallerparty",
        # Long description — field name varies by Maximo version/config:
        "longdescription",
        "description_longdescription",
    ]
)


class MaximoSourceError(Exception):
    """Raised when no auth/endpoint combination works."""


# ─────────────────────────────────────────────────────────────────────────────


def _basic_auth_header(user: str, pw: str) -> str:
    raw = f"{user}:{pw}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _maxauth_header(user: str, pw: str) -> str:
    raw = f"{user}:{pw}".encode()
    return base64.b64encode(raw).decode()


# Each strategy = (endpoint path, auth header builder, header name)
_STRATEGIES = [
    ("/oslc/os/mxsr", _maxauth_header, "maxauth"),
    ("/oslc/os/mxsr", _basic_auth_header, "Authorization"),
    ("/oslc/os/mxapisr", _maxauth_header, "maxauth"),
    ("/oslc/os/mxapisr", _basic_auth_header, "Authorization"),
    ("/maxrest/rest/os/mxsr", _maxauth_header, "maxauth"),
    ("/maxrest/rest/os/mxsr", _basic_auth_header, "Authorization"),
]


def validate_credentials(base_url: str, user: str, password: str, timeout: int = 10) -> bool:
    """Probe Maximo with the given credentials to decide if they grant
    access to the OSLC SR collection.

    Tries each of the six (endpoint, auth-header) strategies the main
    source supports and stops at the first one that returns 200.
    Returns ``False`` if every strategy gets rejected (401/403) or the
    host is unreachable. Used by the login flow to authenticate the
    dashboard user against the live Maximo deployment rather than a
    local password store.
    """
    if not (base_url and user and password):
        return False

    base = base_url.rstrip("/")
    params = {"oslc.pageSize": "1", "oslc.select": "ticketid"}
    for endpoint, auth_fn, header_name in _STRATEGIES:
        url = f"{base}{endpoint}"
        headers = {header_name: auth_fn(user, password), "Accept": "application/json"}
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        except (httpx.HTTPError, OSError):
            continue
        if r.status_code == 200:
            return True
        # 401/403 → wrong creds; keep trying other strategies in case
        # this endpoint specifically rejects this auth header style.
        if r.status_code not in (401, 403, 404):
            continue
    return False


class MaximoSource:
    """Stateful client — caches the first working strategy and reuses the HTTP session."""

    def __init__(self):
        self.base = (CFG.maximo_base_url or "").rstrip("/")
        self.user = CFG.maximo_user
        self.pw = CFG.maximo_pass
        self.timeout = CFG.request_timeout
        self._best_strategy: Optional[tuple] = None
        # Persistent HTTP client so Maximo session cookies survive across pages
        self._client: Optional[httpx.Client] = None

    def __del__(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

    # ── public API ─────────────────────────────────────────────────────

    def configured(self) -> bool:
        return bool(self.base and self.user and self.pw)

    def fetch_all_open(self) -> list[dict]:
        """Return ALL open SRs (paginated). Used by the full-scan scanner."""
        if not self.configured():
            raise MaximoSourceError("Maximo credentials not configured")

        statuses = [s.strip() for s in CFG.open_statuses.split(",") if s.strip()]
        if statuses:
            status_list = '","'.join(statuses)
            where = f'status in ["{status_list}"]'
        else:
            # Exclude only fully closed tickets
            where = 'status!="CLOSE" and status!="CLOSED"'

        if CFG.maximo_site_id:
            where = f'{where} and siteid="{CFG.maximo_site_id}"'

        return self._fetch_paginated(where, label="fetch_all_open")

    def fetch_all(self) -> list[dict]:
        """Return SRs for the configured season window using early-stop pagination.

        Strategy (handles Maximo instances that ignore OSLC WHERE date filters):
          1. Request pages sorted newest-first (oslc.orderBy=-reportdate)
          2. After each page, check the oldest date seen
          3. Once a page contains records older than scan_start_date → keep
             only the in-range rows from that page and STOP immediately
          This caps API calls to ~(season_records / page_size) instead of
          fetching the entire Maximo history.
        """
        if not self.configured():
            raise MaximoSourceError("Maximo credentials not configured")

        # Parse date bounds
        _start_dt: Optional[datetime] = None
        _end_dt: Optional[datetime] = None

        # Rolling window: use the LATER of scan_start_date and (today - full_scan_days).
        # This prevents scanning thousands of pages when Maximo ignores WHERE filters.
        start = (CFG.scan_start_date or "").strip()
        if CFG.full_scan_days > 0:
            from datetime import date
            from datetime import timedelta as _td

            rolling = (date.today() - _td(days=CFG.full_scan_days)).strftime("%Y-%m-%d")
            if not start or rolling > start:
                start = rolling
                log.info(
                    "fetch_all: using rolling window — start=%s (%d days back)",
                    start,
                    CFG.full_scan_days,
                )

        if start:
            try:
                _start_dt = datetime.strptime(start, "%Y-%m-%d")
            except ValueError:
                log.warning("fetch_all: invalid scan_start_date '%s'", start)

        end = (CFG.scan_end_date or "").strip()
        if end:
            try:
                _end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                log.warning("fetch_all: invalid scan_end_date '%s'", end)

        # Build WHERE (best-effort — Maximo may ignore it, Python filter is the fallback)
        clauses: list[str] = []
        if _start_dt:
            clauses.append(f'reportdate>="{start}T00:00:00+00:00"')
        if _end_dt:
            day_after = (_end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
            clauses.append(f'reportdate<"{day_after}T00:00:00+00:00"')
        if CFG.maximo_site_id:
            clauses.append(f'siteid="{CFG.maximo_site_id}"')

        where = " and ".join(clauses)
        log.info("fetch_all WHERE: %s | orderBy: -reportdate", where or "(none)")

        # ── Paginate newest-first with early stop + hard page cap ────────────
        all_rows: list[dict] = []
        page_size = min(CFG.page_size, 200)
        offset = 0
        page_count = 0
        max_pages = CFG.full_scan_max_pages if CFG.full_scan_max_pages > 0 else 999999

        def _parse_dt(raw: str) -> Optional[datetime]:
            try:
                return datetime.strptime(str(raw)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

        while True:
            params: dict = {
                "oslc.select": _SELECT_FIELDS,
                "oslc.pageSize": str(page_size),
                "oslc.offset": str(offset),
                "oslc.orderBy": "-reportdate",  # newest first → early stop works
                "lean": "1",
            }
            if where:
                params["oslc.where"] = where

            data = self._call(params)
            members = data.get("member") or data.get("rdfs:member") or []
            if not members:
                break

            page_rows = [_normalize(m) for m in members]
            early_stop = False

            if _start_dt or _end_dt:
                filtered = []
                for r in page_rows:
                    dt = _parse_dt(r.get("reported", ""))
                    if dt is None:
                        filtered.append(r)
                        continue
                    if _start_dt and dt < _start_dt:
                        early_stop = True  # found a record older than window → stop after this page
                        continue  # drop this row
                    if _end_dt and dt > _end_dt:
                        continue  # drop future records (shouldn't happen with orderBy desc)
                    filtered.append(r)
                all_rows.extend(filtered)
            else:
                all_rows.extend(page_rows)

            page_count += 1
            cap_hit = page_count >= max_pages

            log.info(
                "fetch_all: page offset=%d → %d rows (total so far: %d)%s",
                offset,
                len(members),
                len(all_rows),
                " — EARLY STOP" if early_stop else f" — PAGE CAP ({max_pages})" if cap_hit else "",
            )

            if early_stop or cap_hit or len(members) < page_size:
                break
            offset += page_size

        log.info(
            "fetch_all: fetched %d SRs total (start=%s end=%s)",
            len(all_rows),
            start or "any",
            end or "any",
        )
        return all_rows

    def _fetch_paginated(self, where: str, *, label: str = "fetch", on_page=None) -> list[dict]:
        """Shared pagination loop used by fetch_all_open / fetch_all.

        Args:
            where:   OSLC WHERE clause string (may be empty for no filter).
            label:   Log-prefix for diagnostic messages.
            on_page: Optional callable(total_so_far: int) called after each
                     page so callers can stream progress to the UI.
        """
        all_rows: list[dict] = []
        page_size = min(CFG.page_size, 200)
        offset = 0
        next_url: Optional[str] = None

        while True:
            if next_url:
                data = self._call({}, direct_url=next_url)
            else:
                params: dict = {
                    "oslc.select": _SELECT_FIELDS,
                    "oslc.pageSize": str(page_size),
                    "oslc.offset": str(offset),
                    "lean": "1",
                }
                if where:
                    params["oslc.where"] = where
                data = self._call(params)

            members = data.get("member") or data.get("rdfs:member") or []
            if not members:
                break
            nxt = _extract_next_page(data)
            next_url = self._rewrite_host(nxt) if nxt else None
            all_rows.extend([_normalize(m) for m in members])
            log.info(
                "%s: page offset=%d → %d rows (total so far: %d)%s",
                label,
                offset,
                len(members),
                len(all_rows),
                " [nextPage]" if next_url else "",
            )
            if on_page is not None:
                try:
                    on_page(len(all_rows))
                except Exception:
                    pass
            if len(members) < page_size:
                break  # last page
            if not next_url:
                offset += page_size

        log.info("%s: fetched %d SRs total", label, len(all_rows))
        return all_rows

    def fetch_latest(self, max_pages: int = 200, cutoff_hours: int = 48) -> list[dict]:
        """Fetch ALL SRs reported in the last `cutoff_hours` hours.

        Reliability strategy (definitive 48h coverage):
          1. PRIMARY — server-side date filter:  ``reportdate >= <cutoff>``
             built in Maximo's local timezone (+03:00 KSA). Maximo returns only
             in-window SRs, so we get the full two days regardless of volume.
          2. Paginate via ``oslc:nextPage`` cursor (preferred) or ``oslc.offset``
             (fallback) until exhausted — session cookies persist on self._client.
          3. SAFETY — a client-side cutoff re-check drops anything that slips
             past the server filter (e.g. if Maximo ignores the WHERE clause).
        `max_pages` is a hard safety cap.
        """
        if not self.configured():
            raise MaximoSourceError("Maximo credentials not configured")

        import re as _re
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        # KSA timezone (+03:00) — Maximo stores/returns dates in this zone.
        ksa = _tz(_td(hours=3))
        cutoff_dt = _dt.now(ksa) - _td(hours=cutoff_hours)
        cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")
        # Naive comparison value (KSA local) for the client-side safety re-check.
        cutoff_naive = cutoff_dt.replace(tzinfo=None)

        def _parse_dt(v: str) -> Optional[datetime]:
            """Parse a Maximo date → naive KSA-local datetime for comparison."""
            if not v:
                return None
            s = str(v).strip()
            try:
                dt = _dt.fromisoformat(s)
                if dt.tzinfo is not None:
                    return dt.astimezone(ksa).replace(tzinfo=None)
                return dt
            except Exception:
                pass
            s2 = _re.sub(r"[+-]\d{2}:\d{2}$", "", s.replace("T", " ")).strip()[:19]
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return _dt.strptime(s2[: len(fmt)], fmt)
                except Exception:
                    pass
            return None

        # ── Build WHERE: date filter + optional site filter ──────────────────
        clauses = [f'reportdate>="{cutoff_iso}"']
        if CFG.maximo_site_id:
            clauses.append(f'siteid="{CFG.maximo_site_id}"')
        where = " and ".join(clauses)
        log.info("fetch_latest: WHERE %s", where)

        page_size = min(CFG.page_size, 200)
        all_rows: list[dict] = []
        offset = 0
        next_url: Optional[str] = None  # OSLC nextPage cursor
        server_filter_ok = True  # becomes False if Maximo ignores WHERE

        for page_num in range(max_pages):
            if next_url:
                data = self._call({}, direct_url=next_url)
            else:
                params: dict = {
                    "oslc.select": _SELECT_FIELDS,
                    "oslc.pageSize": str(page_size),
                    "oslc.offset": str(offset),
                    "oslc.orderBy": "-reportdate",
                    "lean": "1",
                    "oslc.where": where,
                }
                data = self._call(params)

            members = data.get("member") or data.get("rdfs:member") or []
            if not members:
                break

            nxt = _extract_next_page(data)
            next_url = self._rewrite_host(nxt) if nxt else None

            page_rows = [_normalize(m) for m in members]
            in_window = 0
            out_window = 0
            for r in page_rows:
                dt = _parse_dt(r.get("reported", ""))
                if dt and dt < cutoff_naive:
                    out_window += 1  # older than 48h → drop (safety net)
                else:
                    all_rows.append(r)
                    in_window += 1

            # If a whole page came back out-of-window, Maximo ignored the WHERE
            # filter → switch to early-stop mode (newest-first guarantees we can stop).
            if out_window > 0 and in_window == 0:
                server_filter_ok = False

            log.info(
                "fetch_latest: page %d offset=%d → %d rows (in:%d out:%d, total:%d)%s",
                page_num + 1,
                offset,
                len(members),
                in_window,
                out_window,
                len(all_rows),
                " [nextPage]" if next_url else "",
            )

            # Stop conditions:
            #  • short page  → no more data
            #  • server filter ignored AND this page was all out-of-window → done
            if len(members) < page_size:
                break
            if not server_filter_ok and out_window > 0 and in_window == 0:
                log.info("fetch_latest: reached cutoff (server ignored WHERE) — stopping")
                break
            if not next_url:
                offset += page_size

        log.info(
            "fetch_latest: done — %d SRs covering last %d hours (since %s)",
            len(all_rows),
            cutoff_hours,
            cutoff_iso,
        )
        return all_rows

    def fetch_recent(
        self, lookback_minutes: Optional[int] = None, max_pages: int = 1
    ) -> list[dict]:
        """Return SRs reported within the lookback window.

        Args:
            lookback_minutes: minutes to look back from now. Defaults to CFG.lookback_minutes.
            max_pages: number of 200-row pages to fetch. 1 = ~newest 200 only (fast,
                       used by the 30 s poller). For 24 h scans pass higher (e.g. 10).
        """
        if not self.configured():
            raise MaximoSourceError("Maximo credentials not configured")

        lookback = lookback_minutes if lookback_minutes is not None else CFG.lookback_minutes
        since = datetime.now(UTC) - timedelta(minutes=lookback)
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        since_dt_naive = since.replace(tzinfo=None)

        where = f'reportdate>="{since_iso}"'
        if CFG.maximo_site_id:
            where = f'{where} and siteid="{CFG.maximo_site_id}"'

        page_size = min(CFG.page_size, 200)
        all_rows: list[dict] = []
        offset = 0

        def _parse_dt(raw: str) -> Optional[datetime]:
            try:
                return datetime.strptime(str(raw)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None

        for page_idx in range(max_pages):
            params = {
                "oslc.where": where,
                "oslc.select": _SELECT_FIELDS,
                "oslc.pageSize": str(page_size),
                "oslc.offset": str(offset),
                "oslc.orderBy": "-reportdate",
                "lean": "1",
            }
            data = self._call(params)
            members = data.get("member") or data.get("rdfs:member") or []
            if not members:
                break

            page_rows = [_normalize(m) for m in members]
            early_stop = False
            filtered = []
            for r in page_rows:
                dt = _parse_dt(r.get("reported", ""))
                if dt is None:
                    filtered.append(r)
                    continue
                if dt < since_dt_naive:
                    early_stop = True
                    continue
                filtered.append(r)
            all_rows.extend(filtered)

            if max_pages > 1:
                log.info(
                    "fetch_recent: page offset=%d → %d rows (kept %d, total: %d)%s",
                    offset,
                    len(members),
                    len(filtered),
                    len(all_rows),
                    " — EARLY STOP" if early_stop else "",
                )

            if early_stop or len(members) < page_size:
                break
            offset += page_size

        return all_rows

    # ── internals ──────────────────────────────────────────────────────

    def _rewrite_host(self, url: str) -> str:
        """Rewrite a nextPage URL's scheme+host to match our public base URL.

        Maximo returns nextPage links pointing at the internal app-server host
        (e.g. http://10.13.0.99/maximo/...). We swap that for the configured
        public base (e.g. https://maximo.kidana.com.sa/maximo) so the request
        is routable and uses the correct scheme.
        """
        try:
            from urllib.parse import urlsplit, urlunsplit

            base = urlsplit(self.base)  # https, maximo.kidana.com.sa, /maximo
            nxt = urlsplit(url)  # http, 10.13.0.99, /maximo/oslc/...
            # Keep nextPage's path + query, but use base's scheme + netloc.
            return urlunsplit((base.scheme, base.netloc, nxt.path, nxt.query, nxt.fragment))
        except Exception:
            return url

    def _call(self, params: dict, direct_url: Optional[str] = None) -> dict:
        """Make one OSLC request.

        If `direct_url` is given (e.g. a nextPage link from a previous response)
        it is used as-is with the cached strategy — no strategy discovery needed.

        Otherwise tries strategies in order and caches the winner.
        The underlying httpx.Client is kept alive on `self._client` so that
        Maximo session cookies (JSESSIONID / LtpaToken2) survive across paginated
        requests — without this the server rejects page 2+ with 401/403.
        """
        attempts: list[str] = []
        last_err: Optional[Exception] = None

        # ── Fast path: reuse cached session for subsequent paginated requests ──
        if self._best_strategy and self._client:
            endpoint, auth_fn, header_name = self._best_strategy
            url = direct_url or f"{self.base}{endpoint}"
            headers = {
                header_name: auth_fn(self.user, self.pw),
                "Accept": "application/json",
                "User-Agent": "duplicate_monitor/1.0",
            }
            try:
                r = self._client.get(url, params=(None if direct_url else params), headers=headers)
                if r.status_code == 200:
                    try:
                        data = r.json()
                        log.debug("Maximo page OK (session reuse, %s)", endpoint)
                        return data
                    except Exception:
                        pass
                # Session expired or rejected — fall through to re-auth
                log.warning("Cached session returned %d — will re-authenticate", r.status_code)
            except httpx.RequestError as e:
                log.warning("Cached session network error: %s — will retry", e)
            # Invalidate dead session
            self._best_strategy = None
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        # ── Strategy discovery: try each auth method with a fresh client ──────
        for strat in _STRATEGIES:
            endpoint, auth_fn, header_name = strat
            url = direct_url or f"{self.base}{endpoint}"
            headers = {
                header_name: auth_fn(self.user, self.pw),
                "Accept": "application/json",
                "User-Agent": "duplicate_monitor/1.0",
            }
            attempts.append(f"{endpoint} via {header_name}")
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
            try:
                r = client.get(url, params=(None if direct_url else params), headers=headers)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        last_err = MaximoSourceError(f"Non-JSON response from {endpoint}")
                        client.close()
                        continue
                    # Strategy worked — persist this client + strategy
                    self._best_strategy = strat
                    if self._client:
                        try:
                            self._client.close()
                        except Exception:
                            pass
                    self._client = client  # keep alive for session cookies
                    log.info("Maximo source OK via %s + %s", endpoint, header_name)
                    return data
                if r.status_code in (401, 403):
                    last_err = MaximoSourceError(f"{r.status_code} on {endpoint} ({header_name})")
                elif r.status_code == 404:
                    last_err = MaximoSourceError(f"404 on {endpoint}")
                else:
                    last_err = MaximoSourceError(
                        f"HTTP {r.status_code} on {endpoint}: {r.text[:200]}"
                    )
                client.close()
            except httpx.RequestError as e:
                last_err = MaximoSourceError(f"Network error: {e}")
                client.close()

        # Nothing worked
        self._best_strategy = None
        self._client = None
        raise MaximoSourceError(
            "All Maximo strategies failed. Tried: "
            + " | ".join(attempts)
            + (f" — last error: {last_err}" if last_err else "")
        )

    def fetch_ownergroup_names(self, codes: list[str]) -> dict[str, str]:
        """Fetch Arabic contractor names for ownergroup codes from Maximo.

        Tries multiple endpoints/fields to maximise coverage:
          1. mxpersongroup  → persongroup.description
          2. mxsr ownergroup_description (already in SELECT, re-aggregate here)
        Returns {ownergroup_code: arabic_name}.
        """
        return self._fetch_code_names(
            codes=codes,
            endpoints=["/oslc/os/mxpersongroup", "/oslc/os/mxapipersngrp"],
            code_field="persongroup",
            desc_fields=["description", "name"],
            where_tpl='persongroup in ["{codes}"]',
        )

    def fetch_person_names(self, usernames: list[str]) -> dict[str, str]:
        """Fetch display names for Maximo usernames (reportedby → full name).

        Queries mxperson / mxapiperson → {username: displayname}.
        Falls back to an empty dict if the endpoint is unavailable.
        """
        result = self._fetch_code_names(
            codes=usernames,
            endpoints=["/oslc/os/mxperson", "/oslc/os/mxapiperson"],
            code_field="personid",
            desc_fields=["displayname", "firstname", "lastname"],
            where_tpl='personid in ["{codes}"]',
        )
        # If displayname is absent, build "firstname lastname" from parts
        if not result:
            return {}
        for uid, name in list(result.items()):
            if not name:
                result.pop(uid, None)
        return result

    def fetch_contract_names(self, codes: list[str]) -> dict[str, str]:
        """Fetch Arabic contractor names for contract codes from Maximo.

        Tries:
          1. mxcontract   → contractnum / vendor / description
          2. mxpo         → ponum / vendor / description
        Returns {contract_code: arabic_name}.
        """
        result = self._fetch_code_names(
            codes=codes,
            endpoints=["/oslc/os/mxcontract", "/oslc/os/mxapicontract"],
            code_field="contractnum",
            desc_fields=["description", "vendor", "vendorname"],
            where_tpl='contractnum in ["{codes}"]',
        )
        if not result:
            result = self._fetch_code_names(
                codes=codes,
                endpoints=["/oslc/os/mxpo", "/oslc/os/mxapipo"],
                code_field="ponum",
                desc_fields=["description", "vendor", "vendorname"],
                where_tpl='ponum in ["{codes}"]',
            )
        return result

    def _fetch_code_names(
        self,
        codes: list[str],
        endpoints: list[str],
        code_field: str,
        desc_fields: list[str],
        where_tpl: str,
    ) -> dict[str, str]:
        """Generic helper: query an OSLC endpoint and return {code: description}."""
        if not codes or not self.configured():
            return {}
        results: dict[str, str] = {}
        quoted = '","'.join(c.replace('"', "") for c in codes)
        where = where_tpl.replace("{codes}", quoted)
        select = ",".join([code_field] + desc_fields)
        params = {
            "oslc.where": where,
            "oslc.select": select,
            "lean": "1",
            "oslc.pageSize": "500",
        }
        for ep in endpoints:
            try:
                data = self._call(params, direct_url=f"{self.base}{ep}")
                members = data.get("member") or data.get("rdfs:member") or data.get("Members") or []
                if not members:
                    continue
                for m in members:
                    code = ""
                    for k in (code_field, f"spi:{code_field}"):
                        v = m.get(k, "")
                        if v:
                            code = str(v).strip()
                            break
                    if not code:
                        continue
                    for df in desc_fields:
                        for k in (df, f"spi:{df}"):
                            v = m.get(k, "")
                            if v and str(v).strip():
                                results[code] = str(v).strip()
                                break
                        if code in results:
                            break
                log.info("%s lookup: %d names fetched via %s", code_field, len(results), ep)
                return results
            except Exception as e:
                log.debug("%s fetch via %s failed: %s", code_field, ep, e)
        return results


# ─── OSLC nextPage extractor ─────────────────────────────────────────────────


def _extract_next_page(data: dict) -> Optional[str]:
    """Extract the OSLC nextPage URL from a Maximo response, if present.

    Maximo can return the cursor in several different shapes depending on
    version and lean mode. The Kidana instance uses (lean=1):
        data["responseInfo"]["nextPage"]["href"]
    Note: the href often points at an INTERNAL host (e.g. 10.13.0.99) — callers
    must rewrite the scheme+host to the public base URL before using it.
    """
    # Lean=1 (Kidana): lowercase responseInfo wrapper
    for ri_key in ("responseInfo", "ResponseInfo", "oslc:ResponseInfo"):
        ri = data.get(ri_key)
        if isinstance(ri, dict):
            for key in ("nextPage", "oslc:nextPage"):
                v = ri.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    return v
                if isinstance(v, dict):
                    href = v.get("href") or v.get("@id") or v.get("rdf:resource")
                    if href and isinstance(href, str):
                        return href
    # Flat variants (non-lean / other Maximo versions)
    for key in ("oslc:nextPage", "spi:nextPage", "rdfs:nextPage"):
        v = data.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            href = v.get("@id") or v.get("rdf:resource") or v.get("href")
            if href and isinstance(href, str):
                return href
    return None


# ─── Normalize a Maximo member into our DB row shape ─────────────────────────


def _g(m: dict, *keys: str) -> str:
    """Get the first non-empty value from any of the given keys."""
    for k in keys:
        v = m.get(k)
        if v not in (None, ""):
            return str(v)
        # Try spi: prefix variants
        v = m.get(f"spi:{k}")
        if v not in (None, ""):
            return str(v)
    return ""


def _normalize(m: dict) -> dict:
    """Flatten an OSLC member dict to the columns our pipeline expects."""
    return {
        "sr": _g(m, "ticketid"),
        "siteid": _g(m, "siteid"),
        "location": _g(m, "location"),
        "asset": _g(m, "assetnum"),
        "summary": _g(m, "description"),
        "status": _g(m, "status"),
        "priority": _g(m, "internalpriority"),
        "priority_desc": _g(m, "internalpriority_description"),
        "reported": _g(m, "reportdate"),
        "class_id": _g(m, "classstructureid"),
        "workzone": _g(m, "workzone"),
        # مُدخِل البلاغ (الموظف الذي أدخل البلاغ في النظام)
        "reporter": _g(m, "reportedby"),
        "reporter_phone": _g(m, "reportedphone"),
        "reporter_email": _g(m, "reportedemail"),
        # رقم المبلّغ (الشخص المتصل / المتأثر)
        "caller_name": _g(m, "affectedperson"),
        "caller_phone": _g(m, "affectedphone"),
        "caller_email": _g(m, "affectedemail"),
        "caller_party": _g(m, "zzcallerparty"),
        # المصدر (Call / Scada / Web …)
        "source": _g(m, "source"),
        "source_desc": _g(m, "source_description"),
        # العقد / مجموعة المقاول
        "ownergroup": _g(m, "ownergroup"),
        "assigned_ownergroup": _g(m, "assignedownergroup"),
        # الإحداثيات للخريطة
        "lat": _g(m, "latitudey"),
        "lon": _g(m, "longitudex"),
        "geom": _g(m, "autolocate"),
        # وصف الحالة بالإنجليزي
        "status_desc": _g(m, "status_description"),
        # detail isn't always present in mxsr; if your tenant exposes
        # a long-description field, add it here.
        "detail": _g(m, "description_longdescription", "longdescription"),
        # History flag & actual dates
        "history": _g(m, "historyflag"),
        "actstart": _g(m, "actstart", "targetstart"),
        # تاريخ المعالجة — Maximo field is actualfinish (actfinish is a fallback)
        "actfinish": _g(m, "actualfinish", "actfinish"),
        "targetstart": _g(m, "targetstart"),
        "targetfinish": _g(m, "targetfinish"),
        "statusdate": _g(m, "statusdate"),
        # Requestor (Kidana custom) — REQUESTOR NO. / REPORTED NAME (Arabic)
        "requestor_no": _g(m, "zzrequestorno"),
        "reported_name": _g(m, "zzrequestor"),
        # Contract & external party — Contract / Contractor / الجهة
        "contract": _g(m, "zzpcontract"),
        # اسم الشركة: وصف العقد أولاً، ثم الجهة الخارجية
        "contractor": _g(m, "zzpcontract_description", "zzextparty_description", "zzextparty"),
        "party": _g(m, "zzextparty_description", "zzextparty"),
        # SLA — زمن الاستجابه / Response Escalation
        "resp_time": _g(m, "zzbreachedtime"),
        "resp_esc": _g(m, "zzesclation"),
        "_raw": m,
    }
