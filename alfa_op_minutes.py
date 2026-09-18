# -*- coding: utf-8 -*-
"""Global registry of used Alfa operation minutes.

bankpdf cuts a receipt before FIO/bank/account checks if the face minute
was already shown. One PDF → one unique (date, HH:MM). Seconds may differ;
the minute must not repeat.

JSON: alfa_sent_op_minutes.json next to the other sent-* files.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from time_msk import now_msk

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
_JSON_PATH = os.path.join(_DIR, "alfa_sent_op_minutes.json")
_LOCK = threading.Lock()

# Minutes already shown to @bankpdfbot (A/B cluster). Always blocked,
# even if the JSON file is missing on a fresh VPS.
_SEED_MINUTES = frozenset({
    "2026-05-22T12:31",  # bp_25 / ab08 PASS → burned slot
    "2026-05-23T12:31",  # ab07
    "2026-05-25T16:08",  # ab14 PASS
    "2026-08-14T14:26",  # pass_middle_nul
    "2026-05-21T12:31",
})

_FACE_DT_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})[^\d]{1,4}(\d{2}:\d{2})(?::(\d{2}))?"
)


def minute_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_face_dt(raw: str) -> Optional[datetime]:
    s = (raw or "").replace("\xa0", " ").replace(",", " ")
    s = s.split("мск")[0].strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _load_json() -> Dict[str, dict]:
    if not os.path.isfile(_JSON_PATH):
        return {}
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    minutes = data.get("minutes")
    if isinstance(minutes, dict):
        return minutes
    # Flat {key: meta} form.
    if data and all(isinstance(k, str) and "T" in k for k in data.keys()):
        return data
    return {}


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _save_json(minutes: Dict[str, dict]) -> None:
    payload = {
        "version": 1,
        "minutes": dict(sorted(minutes.items())),
    }
    _atomic_write(
        _JSON_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def used_minutes() -> set:
    with _LOCK:
        return set(_SEED_MINUTES) | set(_load_json().keys())


def is_used(dt: datetime) -> bool:
    return minute_key(dt) in used_minutes()


def _with_new_seconds(dt: datetime) -> datetime:
    return dt.replace(second=secrets.randbelow(54) + 5, microsecond=0)


def _select_free(
    dt: datetime,
    used: set,
    *,
    allow_future: bool = False,
    hour_lo: int = 0,
    hour_hi: int = 23,
    day_floor: Optional[date] = None,
) -> datetime:
    """Pick a free minute. Live receipts must not be dated in the future vs send.

    Requested minute is kept even at night. Collision walk for Alfa SBP stays
    in 10:00–21:59 on/after 30.07: OnlyPDF FAKEs 02:18 / 09:53 (Proton ЧИСТО)
    when claim_free walks backward into morning after a busy day.
    """
    cand = dt.replace(microsecond=0)

    def ok(x: datetime) -> bool:
        if minute_key(x) in used:
            return False
        if not (hour_lo <= x.hour <= hour_hi):
            return False
        if day_floor is not None and x.date() < day_floor:
            return False
        return True

    if minute_key(cand) not in used:
        return cand
    day = cand.date()
    # Collision: later minutes first (orig print is later), then earlier.
    if allow_future:
        for delta in range(1, 24 * 60):
            nxt = cand + timedelta(minutes=delta)
            if nxt.date() != day:
                break
            if ok(nxt):
                return _with_new_seconds(nxt)
    for delta in range(1, 24 * 60):
        nxt = cand - timedelta(minutes=delta)
        if nxt.date() != day:
            break
        if ok(nxt):
            return _with_new_seconds(nxt)
    for delta in range(1, 24 * 60 * 14):
        nxt = cand - timedelta(minutes=delta)
        if ok(nxt):
            return _with_new_seconds(nxt)
    if allow_future:
        for delta in range(1, 24 * 60 * 14):
            nxt = cand + timedelta(minutes=delta)
            if ok(nxt):
                return _with_new_seconds(nxt)
    logger.warning("no free operation minute in 14 days — keep requested clock")
    return _with_new_seconds(cand)


def _select_near_now(dt: datetime, used: set) -> datetime:
    """«сейчас»: keep wall-clock. Do not walk hours back into a burned evening."""
    cand = dt.replace(microsecond=0)
    if minute_key(cand) not in used:
        return cand
    for delta in range(1, 31):
        nxt = cand + timedelta(minutes=delta)
        if minute_key(nxt) not in used:
            return _with_new_seconds(nxt)
        prv = cand - timedelta(minutes=delta)
        if minute_key(prv) not in used:
            return _with_new_seconds(prv)
    logger.warning("no free minute within ±30 of now — keep requested clock")
    return _with_new_seconds(cand)


def pick_free(dt: datetime) -> datetime:
    """Return a free minute without persisting. Prefer claim_free() before emit."""
    return _select_free(dt, used_minutes())


def claim_free(
    dt: datetime,
    *,
    channel: str = "",
    source: str = "claim",
    near_now: bool = False,
) -> datetime:
    """Pick a free minute and persist it before generation (no race)."""
    with _LOCK:
        minutes = _load_json()
        used = set(_SEED_MINUTES) | set(minutes.keys())
        if near_now:
            chosen = _select_near_now(dt, used)
        else:
            daytime = channel in ("alfa_sbp", "alfa_card")
            chosen = _select_free(
                dt, used, allow_future=(channel == "alfa_card"),
                hour_lo=10 if daytime else 0,
                hour_hi=21 if daytime else 23,
                day_floor=(
                    None
                    if (daytime and dt.date() < date(2026, 7, 30))
                    else (date(2026, 7, 30) if daytime else None)
                ),
            )
        key = minute_key(chosen)
        if key not in minutes:
            minutes[key] = {
                "datetime": chosen.strftime("%d.%m.%Y %H:%M:%S"),
                "channel": channel,
                "source": source,
                "recorded_at": now_msk().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            _save_json(minutes)
            if minute_key(dt) != key:
                logger.info(
                    "op-minute %s taken — claimed %s",
                    minute_key(dt), key,
                )
        return chosen


def remember(dt: datetime, *, channel: str = "", source: str = "emit") -> None:
    key = minute_key(dt)
    meta = {
        "datetime": dt.strftime("%d.%m.%Y %H:%M:%S"),
        "channel": channel,
        "source": source,
        "recorded_at": now_msk().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with _LOCK:
        minutes = _load_json()
        if key in minutes:
            return
        minutes[key] = meta
        _save_json(minutes)


def remember_prepared(prepared: Dict, *, channel: str = "") -> None:
    raw = str(prepared.get("date_time") or prepared.get("date_formed") or "")
    dt = parse_face_dt(raw)
    if dt is None:
        return
    remember(dt, channel=channel, source="emit")


def stamp_unique_minute(
    data: Dict,
    parse_dt,
    *,
    force_auto_ids: bool = True,
    channel: str = "",
) -> datetime:
    """Rewrite data['date_time'] to a claimed unique minute. Regen op/SBP if bumped."""
    raw = str(data.get("date_time") or data.get("date") or "сейчас")
    requested = parse_dt(raw)
    auto = str(raw or "").replace("\xa0", " ").strip().lower() in (
        "сейчас", "now", "авто", "auto", "-", "",
    )
    op_dt = claim_free(requested, channel=channel, source="claim", near_now=auto)
    data["date_time"] = op_dt.strftime("%d.%m.%Y %H:%M:%S")
    if minute_key(op_dt) != minute_key(requested) and force_auto_ids:
        data["operation_num"] = "авто"
        data["sbp_id"] = "авто"
        data.pop("operation_number", None)
        data.pop("spb_number", None)
    return op_dt


def seed_from_pdf_bytes(pdf: bytes, *, source: str = "scan") -> Optional[str]:
    """Record the face minute from an already-sent Alfa PDF. Best-effort."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            text = doc[0].get_text() if doc.page_count else ""
        finally:
            doc.close()
    except Exception:
        return None
    keys = []
    for m in _FACE_DT_RE.finditer(text.replace("\xa0", " ")):
        day, hm, sec = m.group(1), m.group(2), m.group(3) or "00"
        dt = parse_face_dt(f"{day} {hm}:{sec}")
        if dt is not None:
            keys.append(minute_key(dt))
            remember(dt, channel="seed", source=source)
    return keys[0] if keys else None


def bootstrap_seed_file() -> None:
    """Ensure JSON exists and contains frozen burned minutes."""
    with _LOCK:
        minutes = _load_json()
        changed = False
        for key in _SEED_MINUTES:
            if key in minutes:
                continue
            day, hm = key.split("T")
            minutes[key] = {
                "datetime": f"{day[8:10]}.{day[5:7]}.{day[:4]} {hm}:00",
                "channel": "seed",
                "source": "burned",
                "recorded_at": now_msk().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            changed = True
        if changed or not os.path.isfile(_JSON_PATH):
            _save_json(minutes)


bootstrap_seed_file()
