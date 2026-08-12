# -*- coding: utf-8 -*-
"""Generate N Alfa phone PDFs that each PASS @onlypdf_robot.

NATIVE donors only — FIO must fit donor charset (no font inject).
OnlyPDF PASS×1. Amounts keep full RUR (never truncated to «R»).
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
    force_rare_pair,
    pick_sender_pair,
)
from alfa_phone_stealth import create_alfa_phone_stealth  # noqa: E402
from alfa_corpus import canonical_paths  # noqa: E402
from alfa_phone_orig_mode import AlfaPhoneOrigContext  # noqa: E402
import fitz  # noqa: E402

_USED_OPS: set[str] = set()
_PHONE_CHARS: set[str] | None = None

# Короткие / средние / длинные — только буквы из NATIVE phone charset.
_FIO_SHORT = [
    "Павлов К",
    "Сидоров А",
    "Волкова Н",
    "Соколов И",
    "Попова А",
    "Лебедев С",
]
_FIO_MED = [
    "Кузнецов В",
    "Новикова Е",
    "Морозов Д",
    "Козлова П",
    "Андреев Б",
    "Николаев К",
]
_FIO_LONG = [
    "Алексеева Виктория",
    "Дмитриев Николай",
    "Сергеева Лариса",
    "Александров Павел",
    "Владимиров Сергей",
    "Николаева Марина",
]
_SAFE_FALLBACKS = _FIO_SHORT + _FIO_MED + _FIO_LONG


def _phone_chars() -> set[str]:
    global _PHONE_CHARS
    if _PHONE_CHARS is None:
        chars: set[str] = set()
        for path in canonical_paths("phone") or []:
            ctx = AlfaPhoneOrigContext()
            if ctx.load(path):
                chars |= set(ctx.available_chars)
        chars.update(" -+()0123456789.*")
        _PHONE_CHARS = chars
    return _PHONE_CHARS


def _text_ok(text: str) -> bool:
    ch = _phone_chars()
    return all((c in ch) or c.isspace() for c in (text or ""))


def _safe_receiver(i: int, attempt: int) -> str:
    rare = force_rare_pair(i % 30)
    if rare and attempt == 0:
        name = f"{rare[0]} {rare[1]}"
        if _text_ok(name):
            return name
    mode = (i + attempt) % 4
    pools = (_FIO_SHORT, _FIO_MED, _FIO_LONG)
    if mode < 3:
        pool = [n for n in pools[mode] if _text_ok(n)]
        if pool:
            return pool[(i * 7 + attempt) % len(pool)]
    for a in range(0, 120):
        fn, ln = pick_sender_pair(i + a, attempt + a)
        if mode == 0:
            name = f"{ln} {fn[:1]}"
        elif mode == 1:
            name = f"{fn} {ln}"
        else:
            name = f"{ln} {fn}"
        if _text_ok(name):
            # Укорачиваем / удлиняем фамилию на ретраях.
            if attempt % 3 == 1 and " " in name:
                parts = name.split()
                if len(parts[0]) > 5:
                    parts[0] = parts[0][: max(4, len(parts[0]) - 2)]
                    name = " ".join(parts)
            return name
    pool = [n for n in _SAFE_FALLBACKS if _text_ok(n)] or _SAFE_FALLBACKS
    return pool[(i + attempt) % len(pool)]


def _extract_alfa_phone_fields(path: str) -> dict | None:
    from alfa_phone_stealth import PHONE_COORDS
    from alfa_phone_orig_mode import AlfaPhoneOrigContext

    ctx = AlfaPhoneOrigContext()
    if not ctx.load(path):
        return None
    out: dict[str, str] = {}
    for k, (y, x) in PHONE_COORDS.items():
        v = ctx.extract_at(y, x).replace("\xa0", " ").strip()
        if v:
            out[k] = ctx.extract_at(y, x)
    return out if len(out) >= 6 else None


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO/account; nudge seconds + phone."""
    from alfa_sbp_stealth import _parse_dt

    rng = random.Random(29072026 + i * 257 + attempt * 881)

    pool: list[dict] = []
    for path in canonical_paths("phone") or []:
        orig = _extract_alfa_phone_fields(path) or {}
        if len(orig) < 6:
            continue
        dt_raw = (orig.get("date_time") or "").replace("\xa0", " ").strip()
        if not re.match(r"\d{2}\.\d{2}\.\d{4}", dt_raw[:10]):
            continue
        try:
            dt = _parse_dt(dt_raw)
        except Exception:
            continue
        digs = "".join(c for c in (orig.get("amount") or "") if c.isdigit())
        if len(digs) < 2:
            continue
        pool.append(
            {
                "day": dt.strftime("%d.%m.%Y"),
                "hh": dt.hour,
                "mm": dt.minute,
                "ss": dt.second,
                "digs": digs,
                "receiver": orig.get("receiver") or "",
                "account": orig.get("account") or "",
                "message": orig.get("message") or "Перевод",
            }
        )
    if not pool:
        pool = [{
            "day": "15.06.2026", "hh": 14, "mm": 30, "ss": 0, "digs": "3500",
            "receiver": "Кузнецов В", "account": "40817810123456789012",
            "message": "Перевод",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    phone = (
        f"+7 (9{(i * 7 + attempt) % 10}{rng.randint(0, 9)}) "
        f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    receiver = d["receiver"] if len(d["receiver"]) >= 4 else _safe_receiver(i, attempt)
    out = {
        "amount": str(int(d["digs"])),
        "receiver": receiver,
        "phone": phone,
        "date_time": f"{d['day']}, {d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
        "operation_num": "авто",
        "message": d["message"],
    }
    if d["account"] and "авто" not in d["account"].lower():
        out["account"] = d["account"]
    return out


def _gen(data: dict):
    return create_alfa_phone_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    low = text.lower()
    if "телефон" not in low and "клиенту" not in low:
        if "RUR" not in text:
            return False, "no-phone-markers"
    flat = text.replace("\xa0", " ")
    if re.search(r"\d\s+R(?:\s|$)", flat) and "RUR" not in flat:
        return False, "amount-truncated-R"
    if "RUR" not in flat:
        return False, "no-RUR"
    m = re.search(r"C07\d{13}", text.replace("\xa0", ""))
    if m and m.group(0) in _USED_OPS:
        return False, f"op-used:{m.group(0)}"
    return True, "ok"


def _mark(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text().replace("\xa0", "")
        doc.close()
        m = re.search(r"C07\d{13}", text)
        if m:
            _USED_OPS.add(m.group(0))
    except Exception:
        pass


async def main() -> int:
    from onlypdf_batch_cli import parse_batch_args

    args = parse_batch_args(
        default_out=_ROOT / "_test30_alfa_phone",
        description="gen_onlypdf30_alfa_phone.py — OnlyPDF PASS×1 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("alfa_phone_*.pdf"):
            _mark(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="alfa_phone",
        max_attempts=28,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("alfa_phone_*.pdf"))
        )
        miss, dig_miss = coverage_report(joined)
        print(
            f"coverage cyr_missing={''.join(miss) or 'нет'} "
            f"(alphabet без ё/ъ: {CYR_NO_YO_TVERD}) "
            f"dig_missing={''.join(dig_miss) or 'нет'}",
            flush=True,
        )
        if dig_miss or miss:
            print(f"WARN incomplete miss={miss} dig={dig_miss}", flush=True)
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
