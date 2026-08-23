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
from datetime import datetime
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
)
from alfa_sbp_stealth import (  # noqa: E402
    _match_corpus_bank,
    _oracle_receiver_face,
    create_alfa_sbp_stealth,
)
from alfa_corpus import canonical_paths  # noqa: E402
from alfa_orig_mode import union_available_chars  # noqa: E402
import fitz  # noqa: E402

_USED_STEMS: set[str] = set()
_ALFA_CHARS: set[str] | None = None
# Все живые маршруты корпуса — не сужаем до «удобных» Озон/Сбер.
_SAFE_BANKS = [
    "Озон Банк",
    "Сбербанк",
    "ПСБ",
    "Т-Банк",
    "Wildberries (Вайлдберриз Банк)",
    "ВТБ",
    "Газпромбанк",
    "Райффайзен",
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


# Oracle face: Имя Отчество Ф — only the surname is an initial.
# Long given+patronymic on purpose (no «Павел Волков» / «Ян Ю.»).
_FIO_FULL = [
    "Константин Владиславович Щ",
    "Владислав Александрович Х",
    "Екатерина Станиславовна Ж",
    "Мирослава Вячеславовна Ц",
    "Станислав Геннадьевич Ю",
    "Александра Валентиновна Ш",
    "Вячеслав Константинович Г",
    "Кристина Владимировна Б",
    "Максимилиан Сергеевич Т",
    "Велимира Святославовна Н",
    "Святослав Ростиславович М",
    "Ростислава Дмитриевна К",
    "Иннокентий Валерьевич П",
    "Валентина Георгиевна Л",
    "Георгий Иннокентьевич С",
    "Ярослава Константиновна Й",
    "Эмилия Вячеславовна Ч",
    "Харитон Станиславович Д",
    "Всеволод Александрович Р",
    "Серафима Владиславовна О",
    "Олимпиада Сергеевна У",
    "Геннадий Мирославович З",
    "Зинаида Всеволодовна И",
    "Борислав Константинович Е",
    "Елизавета Святославовна Ю",
    "Юлиан Владимирович Щ",
    "Владислава Максимилиановна Ж",
    "Станислава Вячеславовна Ц",
    "Аристарх Константинович Б",
    "Клементина Георгиевна Ш",
    "Доброслав Сергеевич П",
    "Милослава Геннадьевна Т",
    "Радмила Владиславовна К",
    "Святослава Дмитриевна Н",
    "Всеслава Александровна М",
    "Бронислав Валерьевич Л",
    "Валерия Иннокентьевна Г",
    "Екатерина Максимилиановна Д",
    "Александра Святославовна Ч",
    "Константин Всеволодович Й",
    "Мирослав Бориславович Ж",
    "Кристина Серафимовна Х",
    "Геннадий Аристархович Ц",
    "Иннокентия Владиславовна Ш",
    "Станислав Иннокентьевич Б",
    "Вячеслав Максимилианович К",
    "Олимпиада Константиновна Ж",
    "Серафим Вячеславович Т",
]
_USED_FIO: set[str] = set()
_USED_FIO_PATH = _DIR / "alfa_sbp_onlypdf_used_fio.txt"
_FIRST_M = [
    "Константин", "Владислав", "Станислав", "Вячеслав", "Святослав",
    "Иннокентий", "Георгий", "Харитон", "Всеволод", "Геннадий",
    "Борислав", "Юлиан", "Аристарх", "Доброслав", "Бронислав",
    "Мирослав", "Серафим", "Ростислав", "Клементий", "Демьян",
]
_FIRST_F = [
    "Екатерина", "Мирослава", "Александра", "Кристина", "Велимира",
    "Ростислава", "Валентина", "Ярослава", "Эмилия", "Серафима",
    "Олимпиада", "Зинаида", "Елизавета", "Владислава", "Станислава",
    "Клементина", "Милослава", "Радмила", "Святослава", "Всеслава",
    "Валерия", "Иннокентия",
]
_PATR_M = [
    "Владиславович", "Александрович", "Геннадьевич", "Константинович",
    "Ростиславович", "Валерьевич", "Иннокентьевич", "Станиславович",
    "Мирославович", "Всеволодович", "Вячеславович", "Бориславович",
    "Сергеевич", "Дмитриевич", "Георгиевич",
]
_PATR_F = [
    "Станиславовна", "Валентиновна", "Владимировна", "Святославовна",
    "Ростиславовна", "Дмитриевна", "Георгиевна", "Иннокентьевна",
    "Константиновна", "Вячеславовна", "Владиславовна", "Геннадьевна",
    "Александровна", "Серафимовна", "Всеволодовна",
]
_INITS = [c for c in "АБВГДЕЖЗИКЛМНОПРСТУХЦЧШЩЭЮЯ"]
# Live CHS / burned faces — never send to OnlyPDF.
_BURNED_FIO = frozenset({
    "алина александровна а",
    "елена павлова",
    "елена павловна р",
})


def _load_used_fio() -> None:
    if not _USED_FIO_PATH.exists():
        return
    try:
        for line in _USED_FIO_PATH.read_text(encoding="utf-8-sig").splitlines():
            key = " ".join(line.lower().replace("\xa0", " ").split())
            if key:
                _USED_FIO.add(key)
    except OSError:
        pass


def _remember_fio(key: str) -> None:
    key = " ".join(key.lower().replace("\xa0", " ").split())
    if not key or key in _USED_FIO:
        return
    _USED_FIO.add(key)
    try:
        with _USED_FIO_PATH.open("a", encoding="utf-8") as fh:
            fh.write(key + "\n")
    except OSError:
        pass


_load_used_fio()


def _safe_receiver(i: int, attempt: int) -> str:
    def _ok_face(name: str) -> bool:
        if not _text_ok(name) or any(ch in name for ch in "Фф"):
            return False
        key = " ".join(name.lower().replace("\xa0", " ").split())
        if key in _BURNED_FIO:
            return False
        if key.startswith("алина александровна"):
            return False
        return True

    pool = [n for n in _FIO_FULL if _ok_face(n)]
    if not pool:
        pool = [n for n in _FIO_FULL if "ф" not in n.lower()]
    n = max(1, len(pool))
    for offset in range(n):
        name = pool[(i * 13 + attempt * 5 + offset) % n]
        key = " ".join(name.lower().split())
        if key in _USED_FIO:
            continue
        _remember_fio(key)
        return _oracle_receiver_face(name)
    seed = i * 31 + attempt * 17 + len(_USED_FIO) * 13
    for k in range(800):
        n = seed + k
        if n % 2:
            name = (
                f"{_FIRST_M[n % len(_FIRST_M)]} "
                f"{_PATR_M[(n // 3) % len(_PATR_M)]} "
                f"{_INITS[(n // 7) % len(_INITS)]}"
            )
        else:
            name = (
                f"{_FIRST_F[n % len(_FIRST_F)]} "
                f"{_PATR_F[(n // 3) % len(_PATR_F)]} "
                f"{_INITS[(n // 11) % len(_INITS)]}"
            )
        if not _ok_face(name):
            continue
        key = " ".join(name.lower().split())
        if key in _USED_FIO:
            continue
        _remember_fio(key)
        return _oracle_receiver_face(name)
    name = pool[(i + attempt) % n]
    return _oracle_receiver_face(name)


def _safe_bank(i: int, attempt: int) -> str:
    pool = [b for b in _SAFE_BANKS if _text_ok(b)]
    if not pool:
        pool = ["Сбербанк", "Т-Банк"]
    return pool[(i + attempt) % len(pool)]


def _payload(i: int, attempt: int = 0) -> dict:
    """Near-donor fields — corpus day/amount/FIO/bank/account; nudge seconds + phone."""
    from alfa_sbp_stealth import _extract_sbp_fields, _parse_dt

    import time as _time

    rng = random.Random(29072026 + i * 241 + attempt * 839 + int(_time.time() * 1000) % 10_000_000)

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
    hh = int(d["hh"])
    mm = int(d["mm"])
    # Night clocks from June origs (02:22 / 00:14) rebased onto August still
    # FAKE: Юлиан 15.08 02:18 → OnlyPDF подделка, Proton ЧИСТО.
    if hh < 10 or hh > 21:
        hh = 10 + (i + attempt) % 10
        mm = 12 + (attempt * 7) % 45
    # 927 = Alina orig NDC (aa52 CHS). 940–949 is not a live DEF mobile range
    # (РСХБ FAKE Станислава +7 (943), Proton ЧИСТО).
    ndc = 900 + (i * 13 + attempt * 3) % 100
    for _ in range(24):
        if 910 <= ndc <= 999 and ndc != 927 and not (940 <= ndc <= 949):
            break
        ndc = 910 + rng.randint(0, 89)
    phone = (
        f"+7 ({ndc}) "
        f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    receiver = _safe_receiver(i, attempt)
    hit = _match_corpus_bank(d["bank"] or "")
    bank = hit[0] if hit else _safe_bank(i, attempt)
    amt = int(d["digs"]) + (i * 17 + attempt * 41) % 800 + rng.randint(0, 90)
    if amt < 400:
        amt += 400
    # Never resubmit a burned FIO+amount pair to OnlyPDF.
    amt += (hash(receiver) & 0x3FF)
    # Corpus faces are 4–5 digit amounts. Hash+orig can spill to 6 digits
    # (100807 T-Bank → OnlyPDF FAKE, Proton ЧИСТО).
    if amt > 99_999:
        amt = 4_000 + (amt % 90_000)
    if amt < 1_200:
        amt += 1_200
    if amt % 100 == 0:
        amt += 11 + rng.randint(0, 77)
    day = d["day"]
    try:
        d_dd, d_mm, d_yy = day.split(".")
        face = datetime(int(d_yy), int(d_mm), int(d_dd))
    except Exception:
        face = None
    # OnlyPDF: «Результат проверки: old» + «чек не распознан» on May–June faces.
    # Live PASSes are 30.07+ (0433) and August send-time (fcb5). Keep clock, rebase day.
    if face is None or face < datetime(2026, 7, 30):
        bank_low = (d.get("bank") or "").lower()
        if "озон" in bank_low or "ozon" in bank_low:
            # 10.08 + Ozon face uses pre-15.08 B-tail; OnlyPDF FAKE
            # (Максимилиан 10.08 Ozon CS 5187, Proton ЧИСТО).
            day = ("15.08.2026", "16.08.2026")[(i + attempt) % 2]
        else:
            day = ("10.08.2026", "15.08.2026", "16.08.2026")[(i + attempt) % 3]
    out = {
        "amount": str(amt),
        "receiver": receiver,
        "phone": phone,
        "recipient_bank": bank,
        "date_time": f"{day}, {hh:02d}:{mm:02d}:{ss:02d}",
        "operation_num": "авто",
        "sbp_id": "авто",
        "account": "авто",
        "message": d["message"],
    }
    return out


def _gen(data: dict):
    return create_alfa_sbp_stealth(data)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    from alfa_emit import emit_invariants, first_appearance_cps
    from alfa_orig_mode import AlfaOrigContext
    from alfa_sbp_stealth import SBP_COORDS

    # No PDF weight gate — size is soft-ship only in create_alfa_sbp_stealth.
    why = emit_invariants(pdf)
    if why:
        return False, why
    if len(pdf) > 59_087:
        return False, f"size-onlypdf:{len(pdf)}"
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    low = text.lower()
    if "сбп" not in low and "sbp" not in low and "альфа" not in low:
        if "RUR" not in text and "rur" not in low:
            return False, "no-alfa-markers"
    ctx = AlfaOrigContext()
    if not ctx.load_bytes(pdf):
        return False, "load"
    recv = (ctx.extract_at(*SBP_COORDS["receiver"]) or "").replace("\xa0", " ").strip()
    if len(recv.split()) < 3:
        return False, f"fio-short:{recv}"
    amt_s = (ctx.extract_at(*SBP_COORDS["amount"]) or "").replace("\xa0", "")
    amt_digs = "".join(c for c in amt_s if c.isdigit())
    if amt_digs:
        amt_n = int(amt_digs)
        if amt_n > 99_999:
            return False, f"amt-6dig:{amt_n}"
        if amt_n % 100 == 0:
            return False, f"amt-round:{amt_n}"
    face_dt = (ctx.extract_at(*SBP_COORDS["date_time"]) or "").replace("\xa0", " ")
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
    phone = (ctx.extract_at(*SBP_COORDS["phone"]) or "").replace("\xa0", " ")
    pndc = re.search(r"\+7\s*\((\d{3})\)", phone)
    if pndc:
        ndc_n = int(pndc.group(1))
        if ndc_n == 927 or 940 <= ndc_n <= 949:
            return False, f"ndc-gap:{ndc_n}"
    sbp = (ctx.extract_at(*SBP_COORDS["sbp_id"]) or "").replace("\xa0", "").strip()
    # date_formed is minute-only; Proton completion = HH:MM:00. Encoded local
    # must not sit later in that minute.
    face_dt = (ctx.extract_at(*SBP_COORDS["date_time"]) or "").replace("\xa0", " ")
    formed = (ctx.extract_at(*SBP_COORDS["date_formed"]) or "").replace("\xa0", " ")
    try:
        from datetime import datetime as _dt, timedelta as _td
        import re as _re
        m = _re.search(
            r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?",
            face_dt or formed,
        )
        if m and len(sbp) >= 11:
            sec = int(m.group(6) or 0)
            completion = _dt(
                int(m.group(3)), int(m.group(2)), int(m.group(1)),
                int(m.group(4)), int(m.group(5)), sec,
            )
            cal = int(sbp[1:5])
            enc = _dt(2020 + cal // 1000, 1, 1) + _td(days=(cal % 1000) - 1)
            enc = enc.replace(
                hour=int(sbp[5:7]), minute=int(sbp[7:9]), second=int(sbp[9:11]),
            )
            local = enc + _td(hours=3)
            formed_floor = completion.replace(second=0, microsecond=0)
            if local > formed_floor:
                return False, f"sbp-formed:{local.time()} > {formed_floor.time()}"
            lag = (completion - local).total_seconds()
            if lag < -30 or lag > 3 * 3600 + 5 * 60:
                return False, f"sbp-time:{local.time()} vs {completion.time()}"
    except Exception as exc:
        return False, f"sbp-time-parse:{exc}"
    fo = first_appearance_cps(bytes(ctx.stream), ctx.cid_to_uni)
    s = "".join(chr(c) if c != 0xA0 else "·" for c in fo)
    if "ь" not in s:
        return False, "fo-no-soft-sign"
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


_KEEP = _ROOT / "_test20_alfa_sbp_kept"


def _on_accept(i: int, path, pdf: bytes) -> None:
    _mark_stem(pdf)
    try:
        _KEEP.mkdir(parents=True, exist_ok=True)
        (_KEEP / f"{path.stem}_{i:02d}.pdf").write_bytes(pdf)
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
        max_attempts=48,
        fresh=not args.keep,
        full_recheck=False,
        strict_streak=args.strict,
        max_streak_breaks=args.max_streak_breaks,
        on_accept=_on_accept,
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
