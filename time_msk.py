# -*- coding: utf-8 -*-
"""Moscow wall-clock for receipt timestamps (naive, MSK).

VPS often runs UTC; Alfa/Sber/T-Bank receipts label time as «мск».
Always UTC+3 — do not trust system TZ or tzdata (ZoneInfo can be missing
or, worse, datetime.now() on UTC host paints the face 3 hours early).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_MSK_OFFSET = timedelta(hours=3)


def now_msk() -> datetime:
    """Current Moscow time as naive datetime (no tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + _MSK_OFFSET
