# -*- coding: utf-8 -*-
"""Generate N Alfa card→card PDFs for OnlyPDF (PASS×2).

Same gate as other channels. No ё/ъ in any user text (card method has no FIO —
Cyrillic comes from receipt UI labels; digits forced via amount/card/date).
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

logging.basicConfig(level=logging.ERROR)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from onlypdf_safe_names import CYR_NO_YO_TVERD, coverage_report  # noqa: E402
from alfa_card_stealth import create_alfa_card_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_OPS: set[str] = set()


def _extract_alfa_card_fields(path: str) -> dict | None:
    from alfa_card_stealth import CARD_COORDS
    from alfa_orig_mode import AlfaOrigContext

    ctx = AlfaOrigContext()
    if not ctx.load(path):
        return None
    out: dict[str, str] = {}
    for k, (y, x) in CARD_COORDS.items():
        v = ctx.extract_at(y, x).replace("\xa0", " ").strip()
        if v:
            out[k] = ctx.extract_at(y, x)
    return out if len(out) >= 5 else None


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/card masks; nudge seconds only."""
    from alfa_sbp_stealth import _parse_dt
    from alfa_corpus import canonical_paths

    pool: list[dict] = []
    for path in canonical_paths("card") or []:
        orig = _extract_alfa_card_fields(path) or {}
        if len(orig) < 5:
            continue
        dt_raw = (orig.get("date_time") or "").replace("\xa0", " ").strip()
        if not re.match(r"\d{2}\.\d{2}\.\d{4}", dt_raw[:10]):
            continue
        try:
            dt = _parse_dt(dt_raw)
        except Exception:
            continue
        digs = "".join(c for c in (orig.get("amount") or "") if c.isdigit())
        if len(digs) < 3:
            continue
        sender_card = (orig.get("sender_card") or "").strip()
        receiver_card = (orig.get("receiver_card") or "").strip()
        if not sender_card or not receiver_card:
            continue
        pool.append(
            {
                "day": dt.strftime("%d.%m.%Y"),
                "hh": dt.hour,
                "mm": dt.minute,
                "ss": dt.second,
                "digs": digs,
                "sender_card": sender_card,
                "receiver_card": receiver_card,
            }
        )
    if not pool:
        pool = [{
            "day": "15.06.2026", "hh": 14, "mm": 30, "ss": 0, "digs": "3500",
            "sender_card": "220015******1234", "receiver_card": "427601******5678",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    return {
        "amount": str(int(d["digs"])),
        "sender_card": d["sender_card"],
        "receiver_card": d["receiver_card"],
        "date_time": f"{d['day']}, {d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
        "operation_num": "авто",
    }


def _gen(data: dict):
    return create_alfa_card_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    if "карт" not in text.lower() and "карту" not in text.lower():
        return False, "no-card-title"
    m = re.search(r"[A-Z]\d{14,}", text)
    if m and m.group(0) in _USED_OPS:
        return False, f"op-used:{m.group(0)}"
    return True, "ok"


def _mark(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        m = re.search(r"[A-Z]\d{14,}", text)
        if m:
            _USED_OPS.add(m.group(0))
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_alfa_card",
        description="gen_onlypdf30_alfa_card.py — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("alfa_card_*.pdf"):
            _mark(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="alfa_card",
        max_attempts=28,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("alfa_card_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(UI labels only — no FIO field; alphabet без ё/ъ: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
        if dig_miss:
            print(f"WARN digits incomplete: {dig_miss}", flush=True)
            # digits warn only under streak
        # Cyrillic gaps are expected (no name fields on Alfa card receipt)
        if miss:
            print(f"NOTE cyr from labels only, missing={''.join(miss)}", flush=True)
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
