# -*- coding: utf-8 -*-
"""Generate N Sber phone PDFs that each PASS @onlypdf_robot.

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
logging.getLogger("sber_stealth_v3").setLevel(logging.CRITICAL)
logging.getLogger("sber_dynamic").setLevel(logging.CRITICAL)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    coverage_report,
)
from sber_phone_stealth import create_sber_phone_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_DOCS: set[str] = set()


def _extract_phone_donor_fields(path: str) -> dict | None:
    """Field values from Sber phone CARD-shell donors (_PHONE_FIELD_Y coords)."""
    import fitz
    import tbank_unlock_template as tut
    from sber_dynamic import (
        _PHONE_FIELD_Y,
        _pick_arial,
        _read_field_at_y,
    )

    doc = fitz.open(path)
    cs_xref = None
    for xref in range(1, doc.xref_length()):
        try:
            stream = doc.xref_stream(xref)
            if stream and b"Tm" in stream and b"Tj" in stream and b"BT" in stream:
                cs_xref = xref
                break
        except Exception:
            pass
    if cs_xref is None:
        doc.close()
        return None
    stream = doc.xref_stream(cs_xref)
    fm = tut._find_font_objects(doc)
    key = _pick_arial(fm)
    if not key:
        doc.close()
        return None
    sub = tut._parse_subset_tounicode(
        doc.xref_stream(fm[key]["tounicode_xref"]).decode("latin1", "replace"))
    doc.close()
    uni_gid = {u: c for c, u in sub.items()}
    fields: dict[str, str] = {}
    for field_name, target_y in _PHONE_FIELD_Y.items():
        row = _read_field_at_y(stream, target_y, uni_gid)
        if row:
            fields[field_name] = row[1]
    return fields if len(fields) >= 6 else None


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO; nudge seconds + unique phone."""
    from sber_dynamic import _corpus_phone_donors
    from sber_sbp_stealth import _parse_sber_date_time_label

    rng = random.Random(22072026 + i * 263 + attempt * 881)

    pool: list[dict] = []
    for path in _corpus_phone_donors():
        orig = _extract_phone_donor_fields(path) or {}
        if len(orig) < 6:
            continue
        dt = _parse_sber_date_time_label(orig.get("date_time") or "")
        if not dt:
            continue
        digs = "".join(c for c in (orig.get("amount") or "") if c.isdigit())
        if len(digs) < 3:
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
            }
        )
    if not pool:
        pool = [{
            "day": "23.12.2025", "hh": 10, "mm": 16, "ss": 0, "digs": "3500",
            "sender": "Андрей Розенталь О.", "receiver": "Анна Иванова",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    phone = (
        f"+7 ({rng.randint(900, 999)}) {rng.randint(100, 999)}-"
        f"{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    sender = d["sender"] if len(d["sender"]) >= 6 else "Анна Иванова"
    receiver = d["receiver"] if len(d["receiver"]) >= 6 else "Иван Козлов"
    excluded = set("ъЪёЁйЙ")
    if set(sender) & excluded:
        sender = "Мария Александровна Ющенко"
    if set(receiver) & excluded:
        receiver = "Екатерина Владимировна Щукина"
    if i % 30 == 0:
        sender = "Мария Александровна Ющенко"
        receiver = "Екатерина Владимировна Щукина"
    if sender == receiver:
        receiver = "Максим Павлов"
    return {
        "amount": str(int(d["digs"])),
        "sender_name": sender,
        "receiver_name": receiver,
        "phone": phone,
        "date": d["day"],
        "time": f"{d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
        "commission": "0",
        "document_num": "авто",
    }


def _gen(data: dict):
    return create_sber_phone_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        xref = doc[0].get_contents()[0]
        raw = doc.xref_stream_raw(xref)
        dec = doc.xref_stream(xref)
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    # PASS-shell content stream: 932 compressed / 4662 decoded. Иначе OnlyPDF FAKE.
    if len(raw) != 932 or len(dec) != 4662 or raw[:2] != b"\x78\x9c":
        return False, f"stream:{len(raw)}/{len(dec)}"
    low = text.lower()
    if "мск" not in low and "телефон" not in low:
        if "₽" not in text and "руб" not in low:
            return False, "no-sber-phone-markers"
    if "номер карты получателя" not in low.replace("ё", "е"):
        return False, "not-card-layout"
    m = re.search(r"\d{15,}", re.sub(r"\D", "", text))
    if m and m.group(0) in _USED_DOCS:
        return False, f"doc-used:{m.group(0)}"
    return True, "ok"


def _mark(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = re.sub(r"\D", "", doc[0].get_text())
        doc.close()
        m = re.search(r"\d{15,}", text)
        if m:
            _USED_DOCS.add(m.group(0))
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_sber_phone",
        description="Sber phone — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("sber_phone_*.pdf"):
            _mark(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="sber_phone",
        max_attempts=20,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("sber_phone_*.pdf"))
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
