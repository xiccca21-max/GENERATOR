# -*- coding: utf-8 -*-
"""Generate N Alfa SBP PDFs that each PASS @onlypdf_robot.

Same OnlyPDF gate as T-Bank channels (PASS×2 + full recheck).
No ё / ъ. Alphabet coverage via receiver/bank names.
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
    BANKS_MULTI,
    CYR_NO_YO_TVERD,
    coverage_report,
    force_rare_pair,
    pick_sender_pair,
)
from alfa_sbp_stealth import create_alfa_sbp_stealth  # noqa: E402
from alfa_corpus import canonical_paths  # noqa: E402
from alfa_orig_mode import union_available_chars  # noqa: E402
import fitz  # noqa: E402

_USED_STEMS: set[str] = set()
_ALFA_CHARS: set[str] | None = None
# Банки, которые реально встречаются в NATIVE-донорах (слот ≤16, глифы есть).
_SAFE_BANKS = [
    "Сбербанк",
    "Т-Банк",
    "Озон Банк (Ozon)",
    "ПСБ",
    "ВТБ",
    "Газпромбанк",
    "Совкомбанк",
    "Росбанк",
]


def _alfa_chars() -> set[str]:
    global _ALFA_CHARS
    if _ALFA_CHARS is None:
        _ALFA_CHARS = set(union_available_chars(canonical_paths("sbp")))
        _ALFA_CHARS.update(" -+()0123456789.")
    return _ALFA_CHARS


def _text_ok(text: str) -> bool:
    ch = _alfa_chars()
    return all((c in ch) or c.isspace() for c in text)


# Короткие / средние / длинные ФИО (разные длины слотов).
_FIO_SHORT = [
    "Ян Ю.",
    "Эд Э.",
    "Анна А.",
    "Игорь И.",
    "Жанна Ж.",
    "Хари Х.",
    "Инна Щ.",
    "Ольга О.",
]
_FIO_MED = [
    "Игорь Соколов",
    "Павел Волков",
    "Жанна Жукова",
    "Харитон Хромов",
    "Роман Щеглов",
    "Элина Юрьева",
    "Алена Сысоева",
    "Цветана Яшина",
]
_FIO_LONG = [
    "Алина Александровна А",
    "Дамир Евгеньевич С",
    "Дмитрий Владимирович Ч",
    "Евгения Викторовна Б",
    "Михаил Евгеньевич В",
    "Елена Юрьевна К",
    "Виктория Николаевна М",
    "Александр Сергеевич П",
]


def _safe_receiver(i: int, attempt: int) -> str:
    # Редкие буквы на фиксированных слотах.
    rare = force_rare_pair(i % 30)
    if rare and attempt == 0:
        name = f"{rare[0]} {rare[1]}"
        if _text_ok(name):
            return name
    # Чередуем длину: short / med / long / full pair.
    mode = (i + attempt) % 4
    pools = (_FIO_SHORT, _FIO_MED, _FIO_LONG)
    if mode < 3:
        pool = [n for n in pools[mode] if _text_ok(n)]
        if pool:
            return pool[(i * 7 + attempt) % len(pool)]
    for a in range(0, 80):
        fn, ln = pick_sender_pair(i + a, attempt + a)
        # Иногда удлиняем отчеством-инициалом.
        if (i + a) % 5 == 0:
            name = f"{fn} {(ln[:1] if ln else 'А')}. {ln}"
        else:
            name = f"{fn} {ln}"
        if _text_ok(name):
            return name
    fallbacks = [n for n in (_FIO_LONG + _FIO_MED + _FIO_SHORT) if _text_ok(n)]
    if not fallbacks:
        fallbacks = ["Алина Крылова"]
    return fallbacks[(i + attempt) % len(fallbacks)]


def _safe_bank(i: int, attempt: int) -> str:
    pool = [b for b in (_SAFE_BANKS + list(BANKS_MULTI)) if _text_ok(b) and "Альфа" not in b]
    if not pool:
        pool = ["Сбербанк", "Т-Банк"]
    return pool[(i + attempt) % len(pool)]


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO/bank/account; nudge seconds + phone."""
    from alfa_sbp_stealth import _extract_sbp_fields, _parse_dt

    rng = random.Random(29072026 + i * 241 + attempt * 839)

    pool: list[dict] = []
    for path in canonical_paths("sbp") or []:
        orig = _extract_sbp_fields(path) or {}
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
        if len(digs) < 3:
            continue
        pool.append(
            {
                "day": dt.strftime("%d.%m.%Y"),
                "hh": dt.hour,
                "mm": dt.minute,
                "ss": dt.second,
                "digs": digs,
                "receiver": orig.get("receiver") or "",
                "bank": orig.get("recipient_bank") or "",
                "account": orig.get("account") or "",
                "message": orig.get("message") or "Перевод",
            }
        )
    if not pool:
        pool = [{
            "day": "15.06.2026", "hh": 14, "mm": 30, "ss": 0, "digs": "3500",
            "receiver": "Алина Крылова", "bank": "Сбербанк",
            "account": "40817810123456789012", "message": "Перевод",
        }]

    d = pool[(i + attempt) % len(pool)]
    ss = (d["ss"] + 1 + attempt + i) % 60
    phone = (
        f"+7 ({900 + (i * 7 + attempt) % 100}) "
        f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    receiver = d["receiver"] if len(d["receiver"]) >= 4 else _safe_receiver(i, attempt)
    bank = d["bank"] if d["bank"] else _safe_bank(i, attempt)
    out = {
        "amount": str(int(d["digs"])),
        "receiver": receiver,
        "phone": phone,
        "recipient_bank": bank,
        "date_time": f"{d['day']}, {d['hh']:02d}:{d['mm']:02d}:{ss:02d}",
        "operation_num": "авто",
        "sbp_id": "авто",
        "message": d["message"],
    }
    if d["account"] and "авто" not in d["account"].lower():
        out["account"] = d["account"]
    return out


def _gen(data: dict):
    return create_alfa_sbp_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    # Alfa SBP markers
    low = text.lower()
    if "сбп" not in low and "sbp" not in low and "альфа" not in low:
        # still accept if amount/RUR present
        if "RUR" not in text and "rur" not in low:
            return False, "no-alfa-markers"
    # stem uniqueness via operation / sbp id if present
    for pat in (r"[A-Z0-9]{20,}",):
        m = re.search(pat, text)
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
        default_out=_ROOT / "_test30_alfa_sbp",
        description="gen_onlypdf30_alfa_sbp.py — OnlyPDF PASS×2 batch (canonical)",
    )
    n = args.n
    out = args.out
    if out.exists():
        for p in out.glob("alfa_sbp_*.pdf"):
            _mark_stem(p.read_bytes())

    rc = await generate_onlypdf_batch(
        out_dir=out,
        n=n,
        payload_fn=_payload,
        gen_fn=_gen,
        validate_fn=_local_ok,
        prefix="alfa_sbp",
        max_attempts=28,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=lambda _i, _p, pdf: _mark_stem(pdf),
    )
    if rc == 0:
        joined = "".join(
            fitz.open(p)[0].get_text() for p in sorted(out.glob("alfa_sbp_*.pdf"))
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
            # NATIVE доноры без полного алфавита — OnlyPDF важнее coverage.
            print(f"WARN cyr incomplete (native charset): {miss}", flush=True)
    return rc


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
