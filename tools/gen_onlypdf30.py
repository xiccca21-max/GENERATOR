# -*- coding: utf-8 -*-
"""Generate N T-Bank SBP PDFs that each PASS @onlypdf_robot (retry + PASS×2).

Canonical gate lives in onlypdf_gate.py — keep this script as the SBP entrypoint.
Name pools: onlypdf_safe_names (no ё / ъ).

One fixed shell + glyph library — payloads do NOT clone corpus donor fields.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

logging.basicConfig(level=logging.ERROR)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from onlypdf_safe_names import (  # noqa: E402
    BANKS_MULTI,
    coverage_report,
    pick_hard_tbank_pair,
    pick_hard_tbank_recv,
)
from tbank_sbp_stealth import (  # noqa: E402
    create_tbank_sbp_stealth,
    _sbp_pdf_structure_ok,
)
import fitz  # noqa: E402

# Varied amounts — digits coverage, no donor amount cloning.
_AMOUNTS = [
    1200, 2500, 4500, 7800, 9470, 12000, 15500, 19999,
    25000, 33333, 38820, 42000, 55555, 67000, 88888, 99999,
]


def _payload(i: int, attempt: int = 0) -> dict:
    """Разные ФИО/суммы/даты — один shell в генераторе, буквы из library."""
    rng = random.Random(28072026 + i * 191 + attempt * 743 + int(time.time()) % 9973)
    first, last = pick_hard_tbank_pair(i, attempt)
    sender = f"{first} {last}"
    receiver = pick_hard_tbank_recv(i, attempt)
    amount = str(_AMOUNTS[(i + attempt * 3) % len(_AMOUNTS)])
    # Банки: чаще Сбер, иногда другие (как у живых пользователей).
    bank = "Сбербанк" if (i + attempt) % 4 else BANKS_MULTI[(i + attempt) % len(BANKS_MULTI)]
    # On/after 10.07.2026 → Keywords DOCS-2035 (Proton HARD if 991).
    base = datetime(2026, 8, 20, 18, 0, 0) - timedelta(hours=(i * 5 + attempt * 2) % 240)
    if base < datetime(2026, 7, 11, 0, 0, 0):
        base = datetime(2026, 7, 11, 12, 0, 0) + timedelta(minutes=i + attempt)
    base = base.replace(
        minute=rng.randint(0, 59),
        second=(rng.randint(0, 59) + attempt + i) % 60,
    )
    phone = (
        f"+7 ({rng.randint(900, 999)}) {rng.randint(100, 999)}-"
        f"{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    return {
        "date_time": base.strftime("%d.%m.%Y  %H:%M:%S"),
        "amount": amount,
        "sender": sender,
        "phone": phone,
        "receiver": receiver,
        "recipient_bank": bank,
        "sbp_id": "авто",
        "sbp_suffix": "авто",
        "receipt_num": "авто",
    }


def _gen(data: dict):
    pdf = create_tbank_sbp_stealth(data)
    if pdf:
        return pdf
    retry = dict(data)
    retry["sbp_id"] = "авто"
    retry["receipt_num"] = "авто"
    retry["sbp_suffix"] = "авто"
    return create_tbank_sbp_stealth(retry)


def _f1_ok(pdf: bytes) -> tuple[bool, str]:
    ok, reason = _sbp_pdf_structure_ok(pdf, require_f1_raw=True)
    if not ok:
        return False, f"structure:{reason}"
    try:
        import tbank_unlock_template as tut
        from tbank_dynamic import _tbank_ff2_is_corpus_twin, _load_corpus_f1_twin_for_glyf
        from tbank_sbp_stealth import _glyf_table_length
        doc = fitz.open(stream=pdf, filetype="pdf")
        fr = tut._find_font_objects(doc).get("TinkoffSans-Regular")
        ff1 = doc.xref_stream(fr["fontfile_xref"]) if fr else b""
        h = int(round(float(doc[0].mediabox.y1)))
        # Line-level value x1 (amount+₽) must not pass R=250.
        over = 0.0
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                if not spans:
                    continue
                boxes = [sp["bbox"] for sp in spans if sp.get("bbox")]
                if not boxes:
                    continue
                x0 = min(float(b[0]) for b in boxes)
                x1 = max(float(b[2]) for b in boxes)
                if x0 >= 100 and x1 >= 180:
                    over = max(over, x1 - 250.0)
        doc.close()
        g = int(_glyf_table_length(ff1)) if ff1 else 0
        if g and _load_corpus_f1_twin_for_glyf(g) is not None:
            if not _tbank_ff2_is_corpus_twin(ff1, height=h):
                return False, f"mutated-twin-glyf:{g}"
        if over > 0.01:
            return False, f"right-overshoot:+{over:.3f}"
    except Exception as exc:
        return False, f"ship-gate:{exc}"
    return True, "ok"


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args
    from onlypdf_safe_names import CYR_NO_YO_TVERD

    args = parse_batch_args(
        default_out=_ROOT / "_test30_tbank_sbp",
        description="T-Bank SBP — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_f1_ok,
        prefix="tbank_sbp",
        max_attempts=16,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("tbank_sbp_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(alphabet без ё/ъ: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
