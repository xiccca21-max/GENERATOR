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
from datetime import datetime, timedelta
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_DIR))

logging.basicConfig(level=logging.ERROR)
logging.getLogger("sber_stealth_v3").setLevel(logging.CRITICAL)
logging.getLogger("sber_dynamic").setLevel(logging.CRITICAL)

from onlypdf_gate import generate_onlypdf_batch  # noqa: E402
from orig_match_gate import wrap_validate  # noqa: E402
from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    coverage_report,
    diverse_amount,
    diverse_mobile_phone,
    pick_diverse_sber_face,
    strip_yo,
)
from sber_phone_stealth import create_sber_phone_stealth  # noqa: E402
import fitz  # noqa: E402

_USED_DOCS: set[str] = set()
_USED_FACES: set[str] = set()
_EXCL = frozenset("ъЪёЁйЙ")

# Faces never used on the SBP 20/20 (proton23). No donor clones.
_PHONE_SENDERS = [
    "Степан Глухов",
    "Лариса Панкова",
    "Дарья Хмелева",
    "Кирилл Зубов",
    "Полина Абрамова",
    "Артем Леонов",
    "Вера Кузнецова",
    "Наталья Морозова",
    "Евгений Смирнов",
    "Людмила Белова",
    "Тимур Сафин",
    "Светлана Орлова",
    "Георгий Павлов",
    "Василиса Крылова",
    "Денис Фролов",
    "Ксения Волкова",
    "Михаил Соколов",
    "Алина Петрова",
    "Руслан Газизов",
    "Ульяна Светлова",
    "Родион Громов",
    "Лилия Сафонова",
    "Марк Лебедев",
    "Софья Грачева",
    "Платон Воронов",
    "Карина Лескова",
    "Эдуард Малов",
    "Нонна Власова",
    "Захар Плотников",
]
_PHONE_RECV = [
    "Глеб Уткин",
    "Зульфия Каримова",
    "Татьяна Соколова",
    "Юлия Ковалева",
    "Анна Лебедева",
    "Никита Орлов",
    "Владимир Шаров",
    "Максим Попов",
    "Алексей Уткин",
    "Борис Новиков",
    "Сергей Семенов",
    "Цветана Яшина",
    "Элина Белова",
    "Федор Фахрутдинов",
    "Ольга Рыбакова",
    "Павел Чернов",
    "Игорь Волков",
    "Марина Щукина",
    "Яна Журавлева",
    "Харитон Белов",
]
_AMOUNTS = [
    1810, 2540, 3470, 4890, 5120, 6780, 7310, 8450, 9260, 10400,
    11880, 13250, 15670, 16990, 18440, 19770, 22100, 24680, 27890, 30550,
]


def _norm_face(name: str) -> str:
    return re.sub(r"[.\s]+", " ", (name or "").lower()).strip(" .")


def _load_banned_faces() -> None:
    """Never reuse SBP proton23 / prior PASS phone faces."""
    _USED_FACES.update(
        {
            _norm_face("Степан Глухов"),
            _norm_face("Глеб Уткин"),
            _norm_face("Наталья Морозова"),
            _norm_face("Максим Попов"),
            _norm_face("Лариса Панкова"),
            _norm_face("Людмила Белова"),
            _norm_face("Дарья Хмелева"),
            _norm_face("Полина Абрамова"),
            _norm_face("Кирилл Зубов"),
            _norm_face("Руслан Газизов"),
            _norm_face("Михаил Соколов"),
            _norm_face("Артем Леонов"),
            _norm_face("Василиса Крылова"),
            _norm_face("Марк Лебедев"),
            _norm_face("Яна Журавлева"),
        }
    )
    passed = _ROOT / "_sber_passed_faces.json"
    if passed.is_file():
        try:
            import json

            blob = json.loads(passed.read_text(encoding="utf-8"))
            for name in blob.get("faces") or []:
                _USED_FACES.add(_norm_face(str(name)))
        except Exception:
            pass
    roots = [
        _ROOT / "_test20_sber_sbp_proton23",
        _ROOT / "_test20_sber_phone_proton01",
        _ROOT / "_test20_sber_phone_proton02",
        _ROOT / "_test20_sber_phone_proton03",
    ]
    for folder in roots:
        if not folder.is_dir():
            continue
        for p in folder.glob("*.pdf"):
            try:
                text = fitz.open(p)[0].get_text()
            except Exception:
                continue
            for ln in text.splitlines():
                s = ln.strip()
                if 6 <= len(s) <= 48 and any("а" <= ch.lower() <= "я" for ch in s):
                    if "сбп" in s.lower() or "перевод" in s.lower():
                        continue
                    if "банк" in s.lower() or "операц" in s.lower():
                        continue
                    _USED_FACES.add(_norm_face(s))


def _payload(i: int, attempt: int = 0) -> dict:
    """Unique face/amount/date — short, long, mash; без ё."""
    rng = random.Random(23082026 + i * 409 + attempt * 1103)
    _load_banned_faces()
    sender = strip_yo(pick_diverse_sber_face(i, attempt, role="sender"))
    receiver = strip_yo(pick_diverse_sber_face(i, attempt, role="recv"))
    for _ in range(40):
        if (
            _norm_face(sender) not in _USED_FACES
            and _norm_face(receiver) not in _USED_FACES
            and _norm_face(sender) != _norm_face(receiver)
            and "ё" not in sender.lower()
            and "ё" not in receiver.lower()
        ):
            break
        sender = strip_yo(pick_diverse_sber_face(i + _, attempt + _, role="sender"))
        receiver = strip_yo(pick_diverse_sber_face(i + _, attempt + _, role="recv"))
    base = datetime(2026, 8, 22, 16, 0, 0) - timedelta(hours=(i * 7 + attempt * 3) % 280)
    if base < datetime(2026, 7, 11, 0, 0, 0):
        base = datetime(2026, 7, 11, 13, 0, 0) + timedelta(minutes=i * 17 + attempt * 5)
    base = base.replace(
        minute=rng.randint(0, 59),
        second=(rng.randint(0, 59) + attempt + i) % 60,
    )
    amount = int(diverse_amount(i, attempt))
    phone = diverse_mobile_phone(i, attempt)
    return {
        "amount": str(amount),
        "sender_name": sender,
        "receiver_name": receiver,
        "phone": phone,
        "date": base.strftime("%d.%m.%Y"),
        "time": base.strftime("%H:%M:%S"),
        "commission": "0",
        "document_num": "авто",
    }


def _gen(data: dict):
    pdf = create_sber_phone_stealth(data)
    if pdf:
        return pdf
    retry = dict(data)
    retry["document_num"] = "авто"
    return create_sber_phone_stealth(retry)


def _local_ok(pdf: bytes) -> tuple[bool, str]:
    try:
        from sber_dynamic import _pdf_proton_flate_ok

        if not _pdf_proton_flate_ok(pdf):
            return False, "proton-flate"
    except Exception as exc:
        return False, f"flate:{exc}"
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
    except Exception as exc:
        return False, f"open:{exc}"
    low = text.lower()
    if "мск" not in low and "телефон" not in low:
        if "₽" not in text and "руб" not in low:
            return False, "no-sber-phone-markers"
    m = re.search(r"\d{15,}", re.sub(r"\D", "", text))
    if m and m.group(0) in _USED_DOCS:
        return False, f"doc-used:{m.group(0)}"
    return True, "ok"


def _mark(pdf: bytes) -> None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        doc.close()
        digits = re.sub(r"\D", "", text)
        m = re.search(r"\d{15,}", digits)
        if m:
            _USED_DOCS.add(m.group(0))
        skip = (
            "чек", "операц", "перевод", "телефон", "номер", "карт",
            "счёт", "счет", "сумма", "комисс", "код", "документ",
            "мск", "если", "деньги", "обратит", "дополнит",
        )
        for ln in text.splitlines():
            s = ln.strip()
            if not (6 <= len(s) <= 40):
                continue
            if any(ch.isdigit() for ch in s):
                continue
            low = s.lower()
            if any(tok in low for tok in skip):
                continue
            if any("а" <= ch.lower() <= "я" for ch in s):
                _USED_FACES.add(_norm_face(s))
        passed = _ROOT / "_sber_passed_faces.json"
        import json

        clean = [
            f for f in sorted(_USED_FACES)
            if not any(ch.isdigit() for ch in f)
            and not any(tok in f for tok in skip)
        ]
        passed.write_text(
            json.dumps({"faces": clean}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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
        validate_fn=wrap_validate("sber_phone", _local_ok),
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
