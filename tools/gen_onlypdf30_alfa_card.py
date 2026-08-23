# -*- coding: utf-8 -*-
"""Generate N Alfa card→card PDFs for OnlyPDF (PASS×2).

Same gate as other channels. No ё/ъ in any user text (card method has no FIO —
Cyrillic comes from receipt UI labels; digits forced via amount/card/date).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from datetime import datetime
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
_BAD_FACE: set[tuple[str, str, str]] = set()
_BAD_AMOUNTS: set[str] = set()
_BAD_FACE_LOADED = False


def _load_bad_face_once() -> set[tuple[str, str, str]]:
    """Remember rejected day+hh:mm+amount to avoid repeating FAKE faces."""
    global _BAD_FACE_LOADED
    if _BAD_FACE_LOADED:
        return _BAD_FACE
    rej = _ROOT / "_test20_alfa_card_rejects"
    for p in sorted(rej.glob("alfa_card_*_rej_FAKE.pdf")):
        got = _extract_alfa_card_fields(str(p)) or {}
        dt = (got.get("date_time") or "").replace("\xa0", " ").strip()
        amt = "".join(c for c in (got.get("amount") or "") if c.isdigit())
        m = re.search(r"(\d{2}\.\d{2}\.\d{4}).*?(\d{2}:\d{2})", dt)
        if m and amt:
            _BAD_FACE.add((m.group(1), m.group(2), amt))
            _BAD_AMOUNTS.add(amt)
    _BAD_FACE_LOADED = True
    return _BAD_FACE


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
    from alfa_op_minutes import _select_free, used_minutes
    from datetime import date

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
            "sender_card": "220015******1234", "receiver_card": "220220******8275",
        }]

    d = pool[(i + attempt) % len(pool)]
    rng = random.Random(19082026 + i * 211 + attempt * 647)
    # Sole usable orig is 31.03.2025 21:07:45 — do not clone that clock.
    hh = 10 + (i * 3 + attempt) % 10
    mm = 11 + (i * 7 + attempt * 5) % 46
    ss = 8 + (i * 11 + attempt * 3) % 50
    day = d["day"]
    try:
        d_dd, d_mm, d_yy = day.split(".")
        face = datetime(int(d_yy), int(d_mm), int(d_dd))
    except Exception:
        face = None
    # Same OnlyPDF «old» cutoff as Alfa SBP: May–June / 2025 origs unrecognized.
    if face is None or face < datetime(2026, 7, 30):
        day = ("10.08.2026", "15.08.2026", "16.08.2026")[(i + attempt) % 3]
    bad = _load_bad_face_once()
    # Tighten amount jitter to stay closer to corpus amounts.
    base_amt = int(d["digs"])
    amt = base_amt + rng.randint(-120, 120) + ((i * 13 + attempt * 29) % 81) - 40
    if amt > 99_999:
        amt = 4_000 + (amt % 90_000)
    if amt < 1_200:
        amt += 1_200
    if amt % 100 == 0 or amt % 1000 == 999 or amt % 1000 >= 990:
        amt += 13 + rng.randint(0, 70)
    if str(amt) in _BAD_AMOUNTS:
        amt += 71 + rng.randint(0, 60)
    # stamp_unique_minute() changes the actual minute in the PDF.
    # Filter must use the *predicted claimed* minute, otherwise we keep
    # hitting already-known FAKE tuples.
    try:
        d_dd, d_mm, d_yy = day.split(".")
        requested_dt = datetime(
            int(d_yy), int(d_mm), int(d_dd),
            hh, mm, ss,
            0,
        )
    except Exception:
        requested_dt = datetime(2026, 8, 15, hh, mm, ss)
    # Predict the exact claimed minute selection logic used by
    # stamp_unique_minute(channel="alfa_card") → claim_free(...).
    claimed_dt = _select_free(
        requested_dt,
        used_minutes(),
        allow_future=False,
        hour_lo=10,
        hour_hi=21,
        day_floor=date(2026, 7, 30),
    )
    claimed_day = claimed_dt.strftime("%d.%m.%Y")
    claimed_hhmm = claimed_dt.strftime("%H:%M")

    for j in range(32):
        if (claimed_day, claimed_hhmm, str(amt)) not in bad and str(amt) not in _BAD_AMOUNTS:
            break
        amt += 11 + ((i + attempt + j) % 37)
        if amt > 99_999:
            amt = 4_000 + (amt % 90_000)
        if amt % 100 == 0 or amt % 1000 >= 990:
            amt += 17
    return {
        "amount": str(amt),
        # Keep donor PAN masks from the corpus.
        # OnlyPDF seems to validate the card identity more than the skeleton.
        "sender_card": d["sender_card"],
        "receiver_card": d["receiver_card"],
        "date_time": f"{day}, {hh:02d}:{mm:02d}:{ss:02d}",
        "operation_num": "авто",
        "receiver": f"{day} {hh:02d}:{mm:02d}",
    }


def _gen(data: dict):
    return create_alfa_card_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    from alfa_emit import emit_invariants
    from alfa_orig_mode import AlfaOrigContext
    from alfa_card_stealth import (
        CARD_COORDS,
        _ALFA_SENDER_BIN,
        _MIR_RECV_BINS,
        _last4_ok,
    )

    why = emit_invariants(pdf, channel="card")
    if why:
        return False, why
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    if "карт" not in text.lower() and "карту" not in text.lower():
        return False, "no-card-title"
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        return False, "load"
    face_dt = (ctx.extract_at(*CARD_COORDS["date_time"]) or "").replace("\xa0", " ")
    mday = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", face_dt)
    if mday:
        face = datetime(int(mday.group(3)), int(mday.group(2)), int(mday.group(1)))
        if face < datetime(2026, 7, 30):
            return False, f"date-old:{face_dt[:10]}"
    hm = re.search(r"(\d{2}):(\d{2})", face_dt or "")
    if hm:
        hour = int(hm.group(1))
        if hour < 10 or hour > 21:
            return False, f"hour-night:{hour:02d}"
    m = re.search(r"[A-Z]\d{14,}", text.replace("\xa0", ""))
    if m and m.group(0) in _USED_OPS:
        return False, f"op-used:{m.group(0)}"
    sender = (ctx.extract_at(*CARD_COORDS["sender_card"]) or "").replace("\xa0", "")
    recv = (ctx.extract_at(*CARD_COORDS["receiver_card"]) or "").replace("\xa0", "")
    sm = re.fullmatch(r"(\d{6})\*{4,8}(\d{4})", sender)
    rm = re.fullmatch(r"(\d{6})\*{4,8}(\d{4})", recv)
    if not sm or sm.group(1) != _ALFA_SENDER_BIN:
        return False, f"sender-bin:{sender}"
    if not rm or rm.group(1) not in _MIR_RECV_BINS:
        return False, f"recv-bin:{recv}"
    if not _last4_ok(sm.group(2)) or not _last4_ok(rm.group(2)):
        return False, f"last4:{sm.group(2)}/{rm.group(2)}"
    formed = (ctx.extract_at(*CARD_COORDS["date_formed"]) or "").replace("\xa0", " ")

    def _hm(s: str):
        mm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})", s)
        if not mm:
            return None
        return datetime(
            int(mm.group(3)), int(mm.group(2)), int(mm.group(1)),
            int(mm.group(4)), int(mm.group(5)),
        )

    ft, ot = _hm(formed), _hm(face_dt)
    if ft and ot:
        if ft.date() <= ot.date():
            return False, f"formed-same-day:{formed}/{face_dt}"
        if ft <= ot:
            return False, f"formed-not-after:{formed}/{face_dt}"
    digs = "".join(c for c in (ctx.extract_at(*CARD_COORDS["amount"]) or "") if c.isdigit())
    if digs.isdigit() and (int(digs) % 100 == 0 or int(digs) % 1000 >= 990):
        return False, f"amt-round:{digs}"
    return True, "ok"


_KEEP = _ROOT / "_test20_alfa_card_kept"


def _mark(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        m = re.search(r"[A-Z]\d{14,}", text.replace("\xa0", ""))
        if m:
            _USED_OPS.add(m.group(0))
    except Exception:
        pass


def _on_accept(i: int, path, pdf: bytes) -> None:
    _mark(pdf)
    try:
        _KEEP.mkdir(parents=True, exist_ok=True)
        (_KEEP / f"{path.stem}_{i:02d}.pdf").write_bytes(pdf)
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
        on_accept=_on_accept,
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
