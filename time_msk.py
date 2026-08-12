# -*- coding: utf-8 -*-
"""Moscow wall-clock for receipt timestamps (naive, MSK).

VPS often runs UTC; Alfa/Sber/T-Bank receipts label time as «мск».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover
    _MSK = None

# Fixed offset fallback if tz database is missing
_MSK_FIXED = timezone(timedelta(hours=3), name="MSK")


def now_msk() -> datetime:
    """Current Moscow time as naive datetime (no tzinfo)."""
    if _MSK is not None:
        return datetime.now(_MSK).replace(tzinfo=None)
    return datetime.now(_MSK_FIXED).replace(tzinfo=None)
