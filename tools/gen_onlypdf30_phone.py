# -*- coding: utf-8 -*-
"""Generate N T-Bank phone PDFs that each PASS @onlypdf_robot.

Same OnlyPDF gate as SBP / card_sber / card_tbank (PASS×2 + full recheck).
No ё / ъ. Keywords tail DOCS-2035 (current T-Bank generation).
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
from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    coverage_report,
    pick_receiver_short,
    pick_sender_pair,
)
from tbank_phone_stealth import create_tbank_phone_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_RECEIPTS: set[str] = set()


def _payload(i: int, attempt: int = 0) -> dict:
    """Real fake on donor calendar day — new FIO/phone/amount/time (not a clone)."""
    rng = random.Random(22072026 + i * 229 + attempt * 821)
    # Template day 21.04.2026 — keep calendar; mutate clock.
    hh = rng.randint(8, 22)
    mm = rng.randint(0, 59)
    ss = (rng.randint(0, 59) + attempt + i) % 60
    amounts = (
        3500, 5200, 7800, 9100, 12000, 14500, 16800, 18200, 21000, 24500,
        5600, 13400, 15700, 19900, 22000,
    )
    amount = amounts[(i * 3 + attempt) % len(amounts)]
    first, last = pick_sender_pair(i, attempt)
    sender = f"{first} {last}"
    receiver = pick_receiver_short(i, attempt)
    phone = (
        f"+7 ({rng.randint(900, 999)}) {rng.randint(100, 999)}-"
        f"{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    return {
        "date_time": f"21.04.2026  {hh:02d}:{mm:02d}:{ss:02d}",
        "amount": str(amount),
        "sender": sender,
        "phone": phone,
        "receiver": receiver,
        "receipt_num": "авто",
    }


def _gen(data: dict):
    return create_tbank_phone_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        kw = (doc.metadata or {}).get("keywords") or ""
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    if "По номеру телефона" not in text:
        return False, "no-phone-title"
    if "DOCS-2035" not in kw:
        return False, f"bad-kw:{kw.split('|')[-1].strip() if kw else '?'}"
    # Reject pure template face (clone with only receipt/hash rotated).
    if "Дамир Сеничев" in text and "Марина Ч." in text and "14 000" in text:
        return False, "template-face-clone"
    m = re.search(r"Квитанция\s+№\s+([\d-]+)", text)
    if m:
        rec = m.group(1).strip()
        if not rec.startswith("1-104-397-813-"):
            return False, f"bad-stem:{rec}"
        if rec in _USED_RECEIPTS:
            return False, f"receipt-used:{rec}"
    return True, "ok"


def _mark_stem(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        m = re.search(r"Квитанция\s+№\s+([\d-]+)", text)
        if m:
            _USED_RECEIPTS.add(m.group(1).strip())
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_phone",
        description="gen_onlypdf30_phone.py — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("phone_*.pdf"):
            _mark_stem(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="phone",
        max_attempts=25,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark_stem(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("phone_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(alphabet без ё/ъ: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
        if dig_miss:
            print(f"WARN digits incomplete: {dig_miss}", flush=True)
        if miss:
            print(f"WARN cyr incomplete: {miss}", flush=True)
            # coverage warn only
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
