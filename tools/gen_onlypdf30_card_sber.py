# -*- coding: utf-8 -*-
"""Generate N T-Bank card→Sber (card_sber) PDFs that each PASS @onlypdf_robot.

Uses the saved OnlyPDF gate (PASS×2 + full recheck) from onlypdf_gate.py.
No ё / ъ in name pools — alphabet target without those letters.

Hard gates:
  - Keywords tail DOCS-2035 (current T-Bank generation)
  - unique receipt stems across the batch
  - retry with jitter until PASS
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
    pick_hard_tbank_pair,
    pick_hard_tbank_recv,
)
from tbank_stealth_v3 import create_tbank_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_STEMS: set[str] = set()


def _stem(receipt: str) -> str:
    parts = receipt.strip().split("-")
    if len(parts) >= 4:
        return "-".join(parts[:4])
    return receipt


def _payload(i: int, attempt: int = 0) -> dict:
    rng = random.Random(22072026 + i * 211 + attempt * 809)
    fn, ln = pick_hard_tbank_pair(i, attempt)
    hh, mm = rng.randint(8, 22), rng.randint(0, 59)
    day = 10 + ((i * 3 + attempt * 5) % 19)  # 10..28
    # Prefer amount lengths that fit corpus zero-comm slots (≈7 chars with spaces)
    amt = rng.choice(
        [
            rng.randint(3000, 9999),
            rng.randint(10000, 19999),
            rng.randint(20000, 49999),
            rng.randint(50000, 89999),
        ]
    )
    last4 = f"{(i * 137 + attempt * 41) % 10000:04d}"
    bin3 = rng.choice(["220220", "427612", "553691", "220070", "546938"])
    card = f"{bin3}******{last4}"
    recv = pick_hard_tbank_recv(i, attempt)
    return {
        "date_time": f"{day:02d}.07.2026, {hh:02d}:{mm:02d}",
        "amount": str(amt),
        "sender": f"{fn} {ln}",
        "receiver": recv,
        "recipient_bank": "Сбербанк",
        "card": card,
        "commission": "0",
        "receipt_num": "авто",
    }


def _gen(data: dict):
    return create_tbank_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        kw = (doc.metadata or {}).get("keywords") or ""
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    if "По номеру карты" not in text:
        return False, "no-card-title"
    if "Сбербанк" not in text:
        return False, "no-sber"
    if "DOCS-2035" not in kw and "| 991" not in kw and not kw.strip().endswith("991"):
        return False, f"bad-kw:{kw.split('|')[-1].strip() if kw else '?'}"
    m = re.search(r"Квитанция\s+№\s+([\d-]+)", text)
    if m:
        st = _stem(m.group(1))
        if st in _USED_STEMS:
            return False, f"stem-used:{st}"
    return True, "ok"


def _mark_stem(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        m = re.search(r"Квитанция\s+№\s+([\d-]+)", text)
        if m:
            _USED_STEMS.add(_stem(m.group(1)))
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_card_sber",
        description="gen_onlypdf30_card_sber.py — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    # seed used stems from any already-accepted files (resume-friendly)
    if out.exists():
        for p in out.glob("card_sber_*.pdf"):
            _mark_stem(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="card_sber",
        max_attempts=25,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark_stem(pdf),
    )
    if rc == 0:
        for p in sorted(out.glob("card_sber_*.pdf")):
            _mark_stem(p.read_bytes())
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("card_sber_*.pdf"))
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
