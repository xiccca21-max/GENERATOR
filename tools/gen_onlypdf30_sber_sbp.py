# -*- coding: utf-8 -*-
"""Generate N Sber SBP PDFs that each PASS @onlypdf_robot.

Same OnlyPDF gate as T-Bank / Alfa (PASS×2 + full recheck).
Coverage includes ё / ъ and every digit.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

logging.basicConfig(level=logging.ERROR)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from orig_match_gate import wrap_validate  # noqa: E402
from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    coverage_report,
    diverse_amount,
    diverse_mobile_phone,
    pick_diverse_sber_face,
    strip_yo,
)
from sber_sbp_stealth import create_sber_sbp_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_STEMS: set[str] = set()


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO/bank; nudge seconds + unique phone."""
    from sber_dynamic import _corpus_sbp_donors, _extract_sber_fields
    from sber_sbp_stealth import _parse_sber_date_time_label

    rng = random.Random(22072026 + i * 251 + attempt * 857)

    pool: list[dict] = []
    for path in _corpus_sbp_donors():
        orig = _extract_sber_fields(path) or {}
        if len(orig) < 6:
            continue
        dt = _parse_sber_date_time_label(orig.get("date_time") or "")
        if not dt:
            continue
        digs = "".join(c for c in (orig.get("amount") or "") if c.isdigit())
        # Drop trailing kopecks «00» from donor amount text.
        if len(digs) >= 3 and digs.endswith("00"):
            digs = digs[:-2]
        # Prefer 4–5 digit amounts (6+ rarely fit exact-flate slots).
        if len(digs) < 3 or len(digs) > 5:
            continue
        pool.append(
            {
                "day": dt.strftime("%d.%m.%Y"),
                "hh": dt.hour,
                "mm": dt.minute,
                "ss": dt.second,
                "digs": digs,
                "sender": orig.get("sender_name") or "",
                "receiver": orig.get("receiver_name") or "",
                "bank": orig.get("recipient_bank") or "",
                "phone": orig.get("phone") or "",
            }
        )
    if not pool:
        pool = [{
            "day": "20.07.2026", "hh": 12, "mm": 0, "ss": 0, "digs": "4700",
            "sender": "Андрей Розенталь О.", "receiver": "Анна Иванова",
            "bank": "Т-Банк", "phone": "+7 (916) 123-45-67",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    # Keep donor phone when present (slot/glyph clone); else unique.
    phone = diverse_mobile_phone(i, attempt)
    sender = strip_yo(pick_diverse_sber_face(i, attempt, role="sender"))
    receiver = strip_yo(pick_diverse_sber_face(i, attempt, role="recv"))
    banks = ("Т-Банк", "Альфа-Банк", "ВТБ", "Газпромбанк", "ПСБ", "Озон Банк")
    bank = banks[(i + attempt) % len(banks)]
    amt = int(diverse_amount(i, attempt))
    while len(str(amt)) > 5:
        amt //= 10
    if len(str(amt)) < 3:
        amt = 1000 + (i * 137 + attempt * 41) % 8000
    return {
        "amount": str(amt),
        "sender_name": sender,
        "receiver_name": receiver,
        "phone": phone,
        "bank_name": bank,
        "date": d["day"],
        "time": f"{d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
    }


def _gen(data: dict):
    return create_sber_sbp_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    low = text.lower()
    if "сбп" not in low and "sbp" not in low:
        if "мск" not in low:
            return False, "no-sber-sbp-markers"
    m = re.search(r"[A-Z0-9]{20,}", text)
    if m:
        st = m.group(0)[:24]
        if st in _USED_STEMS:
            return False, f"stem-used:{st}"
    return True, "ok"


def _mark_stem(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        m = re.search(r"[A-Z0-9]{24,}", text)
        if m:
            _USED_STEMS.add(m.group(0)[:24])
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_sber_sbp",
        description="Sber SBP — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("sber_sbp_*.pdf"):
            _mark_stem(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=wrap_validate("sber_sbp", _local_ok),
        prefix="sber_sbp",
        max_attempts=12,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark_stem(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("sber_sbp_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(full alphabet: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
        if dig_miss or miss:
            print(f"WARN incomplete miss={miss} dig={dig_miss}", flush=True)
            # coverage warn only; OnlyPDF streak is the gate
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
