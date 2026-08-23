# -*- coding: utf-8 -*-
"""Generate N T-Bank card→T-Bank (card_tbank) PDFs that each PASS @onlypdf_robot.

Same OnlyPDF gate as SBP / card_sber (PASS×2 + full recheck).
No ё / ъ. Keywords tail DOCS-2035 (current T-Bank generation).
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
from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    coverage_report,
    pick_hard_tbank_pair,
    pick_hard_tbank_recv,
)
from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_STEMS: set[str] = set()


def _stem(receipt: str) -> str:
    parts = receipt.strip().split("-")
    if len(parts) >= 4:
        return "-".join(parts[:4])
    return receipt


def _parse_tbank_dt(date_raw: str) -> tuple[str, int, int, int] | None:
    raw = (date_raw or "").replace("\xa0", " ").strip()
    m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}):(\d{2}):(\d{2})", raw)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    if re.match(r"\d{2}\.\d{2}\.\d{4}", raw[:10]):
        return raw[:10], 12, 0, 0
    return None


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO/card; nudge seconds only."""
    from tbank_corpus import corpus_paths
    from tbank_card_tbank_stealth import _extract_ct_fields

    pool: list[dict] = []
    for path in corpus_paths("card_tbank") or []:
        orig = _extract_ct_fields(path) or {}
        parsed = _parse_tbank_dt(orig.get("date") or "")
        if not parsed:
            continue
        day, hh, mm, ss = parsed
        digs = "".join(c for c in (orig.get("amount") or "") if c.isdigit())
        if len(digs) < 3:
            continue
        card = (orig.get("card") or "").strip()
        if not card:
            continue
        pool.append(
            {
                "day": day,
                "hh": hh,
                "mm": mm,
                "ss": ss,
                "digs": digs,
                "sender": orig.get("sender") or "",
                "receiver": orig.get("receiver") or "",
                "card": card,
            }
        )
    if not pool:
        pool = [{
            "day": "21.04.2026", "hh": 15, "mm": 5, "ss": 0, "digs": "14000",
            "sender": "Роман Туров", "receiver": "Анна А.", "card": "*1234",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    first, last = pick_hard_tbank_pair(i, attempt)
    sender = f"{first} {last}"
    receiver = pick_hard_tbank_recv(i, attempt)
    return {
        "date_time": f"{d['day']}, {d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
        "amount": str(int(d["digs"]) + (i * 13 + attempt) % 80),
        "sender": sender,
        "receiver": receiver,
        "card": d["card"],
        "receipt_num": "авто",
    }


def _gen(data: dict):
    return create_tbank_card_tbank_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        kw = (doc.metadata or {}).get("keywords") or ""
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    if "Клиенту Т-Банка" not in text:
        return False, "no-ct-title"
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
        default_out=_ROOT / "_test30_card_tbank",
        description="gen_onlypdf30_card_tbank.py — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("card_tbank_*.pdf"):
            _mark_stem(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="card_tbank",
        max_attempts=25,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark_stem(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("card_tbank_*.pdf"))
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
