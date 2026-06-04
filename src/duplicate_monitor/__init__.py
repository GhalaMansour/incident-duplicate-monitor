"""Duplicate Monitor.

24/7 live duplicate detection service for Kidana Maximo service requests.

The package layout is:

* ``core``     — configuration and logging.
* ``matching`` — Arabic normalization, similarity scorer, and the
                 legacy bulk detector. Public matching API.
* ``sources``  — Maximo OSLC source and Excel file source.
* ``storage``  — SQLite schema and queries.
* ``poller``   — Background polling loop.
* ``scanner``  — Full historical scan.
* ``web``      — FastAPI dashboard.

The Maximo source implements an in-house six-way endpoint and
authentication fallback methodology developed for the Kidana
deployment. See ``docs/maximo_oslc_methodology.md`` for the full
write-up.
"""

__version__ = "1.0.0"
