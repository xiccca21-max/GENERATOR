# -*- coding: utf-8 -*-
"""Stress live bot generators: 50 weird payloads × each live method.

Does NOT stop night_proton. Prints summary JSON to output/stress_bot50/.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
for name in ("fontTools", "PIL", "urllib3"):
    logging.getLogger(name).setLevel(logging.ERROR)

_OUT = _ROOT / "output" / "stress_bot50"
_OUT.mkdir(parents=True, exist_ok=True)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4

# Full Cyrillic coverage stress (rare + common)
_RARE = list("ЁёЪъЫыЭэЩщЙйЖжЦцЮюФфХхЧчШшЬь")
_COMMON = list("АБВГДЕЗИКЛМНОПРСТУабвгдезиклмнопрсту")
_ALL_CYR = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
)
_DIG = list("0123456789")

# Long FIO templates — no hyphens (spaces / initials only)
_LONG_FIO_POOL = [
    "Артём Эдуард Щербаков Подъёмов",
    "Харитон Юрьевич Ъюдин Жуков",
    "Элина Фёдоровна Цветкова Шарова",
    "Владислав Ярославович Хромов Щукин",
    "Ксения Майя Григорьевна Рыбакова",
    "Ростислав Эдуардович Чёрный Юдин",
    "Инна Юлиана Щербакова Цветкова",
    "Глеб Фёдорович Подъёмов Харитонов",
    "Анастасия Эдуардовна Жукова Шашкова",
    "Павел Ярослав Щукин Цветков",
    "Фёдор Эмильевич Ъюдин Хромов",
    "Юлиана Ксения Фёдоровна Щербакова",
]
_SBER_FIO_POOL = [
    "Мария Александровна Ющенко",
    "Екатерина Владимировна Щукина",
    "Анастасия Эдуардовна Жукова",
    "Инна Юлиана Щербакова Цветкова",
]


def _word(rng: random.Random, n: int = 0) -> str:
    n = n or rng.randint(8, 18)
    chars = _RARE + _COMMON
    w = "".join(rng.choice(chars) for _ in range(n))
    return w[0].upper() + w[1:].lower() if w else "Иван"


def _word_cover(rng: random.Random, alphabet: str, n: int) -> str:
    """Build a word that pulls as many distinct letters as possible."""
    letters = list(alphabet)
    rng.shuffle(letters)
    base = "".join(letters[: max(n, min(len(letters), n))])
    while len(base) < n:
        base += rng.choice(letters)
    w = base[:n]
    return w[0].upper() + w[1:].lower()


def _fio(rng: random.Random) -> str:
    """Prefer long multi-token FIO with rare letters + wide charset. No hyphens."""
    kind = rng.randint(0, 9)
    if kind <= 3:
        return rng.choice(_LONG_FIO_POOL)
    if kind == 4:
        return " ".join(
            [
                _word_cover(rng, _ALL_CYR, rng.randint(10, 16)),
                _word_cover(rng, _ALL_CYR, rng.randint(10, 14)),
                _word_cover(rng, _ALL_CYR, rng.randint(12, 18)),
            ]
        )
    if kind == 5:
        return (
            f"{_word_cover(rng, _ALL_CYR, 12)} "
            f"{_word_cover(rng, _ALL_CYR, 10)} "
            f"{_word_cover(rng, _ALL_CYR, 14)}ов"
        )
    if kind == 6:
        return f"Артём Эдуард {_word_cover(rng, _ALL_CYR, 16)}"
    if kind == 7:
        return (
            f"{_word_cover(rng, _ALL_CYR, 11)} "
            f"{_word_cover(rng, _ALL_CYR, 13)} "
            f"{_word_cover(rng, _ALL_CYR, 12)}ич"
        )
    if kind == 8:
        return rng.choice(_LONG_FIO_POOL) + f" {_word_cover(rng, _ALL_CYR, 10)}"
    return (
        f"{_word_cover(rng, _ALL_CYR, 20)} "
        f"{_word_cover(rng, _ALL_CYR, 22)}"
    )


def _recv_init(rng: random.Random) -> str:
    # Longer first name + rare initial
    first = _word_cover(rng, _ALL_CYR, rng.randint(8, 14))
    init = rng.choice(list("ЁЪЫЭЩЙЖЦЮФХЧШЬЕАРТЁМ"))
    return f"{first} {init}."


def _phone(rng: random.Random) -> str:
    d = "".join(rng.choice(_DIG) for _ in range(10))
    return f"+7 ({d[:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"


def _amount(rng: random.Random) -> str:
    return str(rng.randint(1_000, 100_000))


def _bank(rng: random.Random) -> str:
    return rng.choice(
        [
            "Сбербанк",
            "Т-Банк",
            "ВТБ",
            "Альфа-Банк",
            "Райффайзенбанк",
            "Газпромбанк",
            "Озон Банк",
            "Совкомбанк",
            "Росбанк",
            "Банк СПБ",
            "ЮMoney",
            "QIWI",
        ]
    )


def _date(rng: random.Random) -> str:
    return f"{rng.randint(1,28):02d}.{rng.randint(1,12):02d}.2026, {rng.randint(0,23):02d}:{rng.randint(0,59):02d}"


def _card(rng: random.Random) -> str:
    return f"{rng.choice(['2202','4276','5536','5486'])}****{rng.randint(1000,9999)}"


def _load_gens() -> Dict[str, Callable[[Dict], Any]]:
    from tbank_sbp_stealth import create_tbank_sbp_stealth
    from tbank_phone_stealth import create_tbank_phone_stealth
    from tbank_stealth_v3 import create_tbank_stealth
    from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
    from tbank_nocomm_stealth import create_tbank_nocomm_stealth
    from sber_sbp_stealth import create_sber_sbp_stealth
    from sber_phone_stealth import create_sber_phone_stealth
    from alfa_sbp_stealth import create_alfa_sbp_stealth
    from alfa_card_stealth import create_alfa_card_stealth
    from alfa_phone_stealth import create_alfa_phone_stealth

    return {
        "tbank_sbp": create_tbank_sbp_stealth,
        "tbank_phone": create_tbank_phone_stealth,
        "tbank_card_sber": create_tbank_stealth,
        "tbank_card_tbank": create_tbank_card_tbank_stealth,
        "tbank_nocomm": create_tbank_nocomm_stealth,
        "sber_sbp": create_sber_sbp_stealth,
        "sber_phone": create_sber_phone_stealth,
        "alfa_sbp": create_alfa_sbp_stealth,
        "alfa_card": create_alfa_card_stealth,
        "alfa_phone": create_alfa_phone_stealth,
    }


def _payload(method: str, i: int) -> Dict:
    rng = random.Random(0xB07150 + i * 9973 + hash(method) % 100000)
    base = {
        "date_time": _date(rng),
        "date": f"{rng.randint(1,28):02d}.{rng.randint(1,12):02d}.2026",
        "time": f"{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}",
        "amount": _amount(rng),
        "sender": _fio(rng),
        "receiver": _recv_init(rng) if "tbank" in method else _fio(rng),
        "phone": _phone(rng),
        "recipient_bank": _bank(rng),
        "sbp_id": "авто",
        "sbp_suffix": "авто",
        "receipt_num": "авто",
        "spb_number": "авто",
        "document_num": "авто",
        "sender_name": _fio(rng),
        "receiver_name": _fio(rng),
        "bank_name": _bank(rng),
        "card": _card(rng),
        "sender_card": _card(rng),
        "receiver_card": _card(rng),
        "account": f"40817********{rng.randint(1000,9999)}",
        "operation_num": str(rng.randint(10**9, 10**10 - 1)),
        "message": rng.choice(["", "перевод", "за обед", "тест Ёжик"]),
    }
    if method.startswith("sber"):
        base["sender_name"] = rng.choice(_SBER_FIO_POOL)
        base["receiver_name"] = rng.choice(_SBER_FIO_POOL)
        if base["receiver_name"] == base["sender_name"]:
            base["receiver_name"] = _SBER_FIO_POOL[
                (_SBER_FIO_POOL.index(base["sender_name"]) + 1) % len(_SBER_FIO_POOL)
            ]
        base["sender"] = base["sender_name"]
        base["receiver"] = base["receiver_name"]
        base["bank_name"] = base["recipient_bank"]
    if method == "alfa_sbp":
        base["receiver"] = _fio(rng)
    if method == "alfa_phone":
        base["receiver"] = _fio(rng)
    if method == "tbank_card_sber":
        base["receiver"] = _fio(rng)
        base["recipient_bank"] = "Сбербанк"
    if method == "tbank_card_tbank":
        base["receiver"] = _recv_init(rng)
    if method == "tbank_nocomm":
        base["card"] = _card(rng)
    return base


def _one(method: str, gen: Callable, i: int) -> Dict:
    payload = _payload(method, i)
    t0 = time.monotonic()
    try:
        pdf = gen(payload)
        ok = bool(pdf) and isinstance(pdf, (bytes, bytearray)) and len(pdf) > 500
        return {
            "method": method,
            "i": i,
            "ok": ok,
            "bytes": len(pdf) if pdf else 0,
            "sec": round(time.monotonic() - t0, 2),
            "payload": {
                "sender": payload.get("sender") or payload.get("sender_name"),
                "receiver": payload.get("receiver") or payload.get("receiver_name"),
                "amount": payload.get("amount"),
                "bank": payload.get("recipient_bank") or payload.get("bank_name"),
            },
            "err": "" if ok else "NONE",
        }
    except Exception as exc:
        return {
            "method": method,
            "i": i,
            "ok": False,
            "bytes": 0,
            "sec": round(time.monotonic() - t0, 2),
            "payload": {
                "sender": payload.get("sender") or payload.get("sender_name"),
                "receiver": payload.get("receiver") or payload.get("receiver_name"),
                "amount": payload.get("amount"),
            },
            "err": f"{type(exc).__name__}:{exc}",
            "tb": traceback.format_exc()[-800:],
        }


def main() -> int:
    gens = _load_gens()
    methods = list(gens.keys())
    print(f"STRESS n={N} methods={len(methods)} workers={WORKERS}", flush=True)
    summary: Dict[str, Any] = {"ts": datetime.now().isoformat(), "n": N, "methods": {}}
    fails: List[Dict] = []

    for method in methods:
        gen = gens[method]
        rows: List[Dict] = []
        print(f"===== {method} ×{N} =====", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(_one, method, gen, i) for i in range(N)]
            for fut in as_completed(futs):
                row = fut.result()
                rows.append(row)
                mark = "OK" if row["ok"] else "FAIL"
                if not row["ok"]:
                    fails.append(row)
                    print(
                        f"  FAIL #{row['i']} {row['err']} | {row['payload']}",
                        flush=True,
                    )
                elif row["i"] % 10 == 0:
                    print(f"  {mark} #{row['i']} {row['bytes']}B {row['sec']}s", flush=True)
        ok_n = sum(1 for r in rows if r["ok"])
        summary["methods"][method] = {
            "ok": ok_n,
            "fail": N - ok_n,
            "fail_samples": [r for r in rows if not r["ok"]][:10],
        }
        (_OUT / f"{method}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"===== {method} DONE {ok_n}/{N} =====", flush=True)

    summary["total_ok"] = sum(m["ok"] for m in summary["methods"].values())
    summary["total_fail"] = sum(m["fail"] for m in summary["methods"].values())
    summary["fail_list"] = fails[:50]
    (_OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"TOTAL ok={summary['total_ok']} fail={summary['total_fail']}",
        flush=True,
    )
    return 0 if summary["total_fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
