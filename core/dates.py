"""NNTP-Datumshilfen.

Eigenes Modul, damit sowohl `core.nntp_client` als auch `core.header_cache`
es nutzen können, ohne zirkulär aufeinander zu zeigen.
"""
from __future__ import annotations

import email.utils
from datetime import datetime, timezone


def parse_nntp_date(s: str) -> datetime | None:
    """RFC-2822-Datum aus dem Date-Header → tz-aware datetime, oder None."""
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_nntp_date_unix(s: str) -> int:
    """Wie parse_nntp_date, aber liefert Unix-Sekunden (0 bei Fehler).

    0 dient als 'nicht parsbar / nicht gesetzt'-Sentinel. Reale NNTP-Posts
    aus den 70ern existieren nicht, also kollidiert das nicht mit
    legitimen Daten.
    """
    dt = parse_nntp_date(s)
    if dt is None:
        return 0
    try:
        return int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return 0
