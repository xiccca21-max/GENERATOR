# -*- coding: utf-8 -*-
"""Overnight dual loop — ALWAYS send every PDF to @proton_pdf_bot (Telegram).

Local detector is only a side-log so flags are recorded; the source of truth
you see in chat is Proton.

Stop: create output/night_dual/STOP
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_CHECKER = Path(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot")
_ORIG = Path(r"C:\Users\fanis\OneDrive\Desktop\чеки")
_OUT = _ROOT / "output" / "night_dual"
_STOP = _OUT / "STOP"

# pdf-checker-bot MUST win over zapaska vendor/detector
sys.path = [p for p in sys.path if "vendor" not in p.replace("\\", "/").lower()]
if _CHECKER.is_dir():
    sys.path.insert(0, str(_CHECKER))
sys.path.insert(0, str(_DIR))
sys.path.insert(0, str(_ROOT))
# re-assert checker first after root inserts
if _CHECKER.is_dir() and sys.path[0] != str(_CHECKER):
    sys.path.insert(0, str(_CHECKER))

from onlypdf_safe_names import (  # noqa: E402
    CYR_NO_YO_TVERD,
    pick_receiver_short,
    pick_sender_pair,
)
from onlypdf_gate import (  # noqa: E402
    open_onlypdf_client,
    proton_verdict,
    _parse_proton,
)

METHODS = [
    # Alfa first — user must see Proton verdicts on this bank.
    "alfa_sbp",
    "alfa_card",
    "alfa_phone",
    "tbank_sbp",
    "tbank_card_sber",
    "tbank_card_tbank",
    "tbank_phone",
    "tbank_nocomm",
    "sber_sbp",
    "sber_phone",
]


def _load_gens():
    from tbank_sbp_stealth import create_tbank_sbp_stealth
    from tbank_stealth_v3 import create_tbank_stealth
    from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
    from tbank_phone_stealth import create_tbank_phone_stealth
    from tbank_nocomm_stealth import create_tbank_nocomm_stealth
    from alfa_sbp_stealth import create_alfa_sbp_stealth
    from alfa_card_stealth import create_alfa_card_stealth
    from alfa_phone_stealth import create_alfa_phone_stealth
    from sber_sbp_stealth import create_sber_sbp_stealth
    from sber_phone_stealth import create_sber_phone_stealth

    return {
        "tbank_sbp": create_tbank_sbp_stealth,
        "tbank_card_sber": create_tbank_stealth,
        "tbank_card_tbank": create_tbank_card_tbank_stealth,
        "tbank_phone": create_tbank_phone_stealth,
        "tbank_nocomm": create_tbank_nocomm_stealth,
        "alfa_sbp": create_alfa_sbp_stealth,
        "alfa_card": create_alfa_card_stealth,
        "alfa_phone": create_alfa_phone_stealth,
        "sber_sbp": create_sber_sbp_stealth,
        "sber_phone": create_sber_phone_stealth,
    }


def _log(msg: str) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (_OUT / "live.log").open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def _write_status(status: dict) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_row(name: str, row: dict) -> None:
    with (_OUT / name).open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stress_payload(method: str, i: int, attempt: int = 0) -> dict:
    rng = random.Random(31072026 + hash(method) % 10_000 + i * 997 + attempt * 131)
    alphabet = CYR_NO_YO_TVERD
    first, last = pick_sender_pair(i, attempt)
    # phone lean shell: keep donor-compatible FIO until full unlock restored
    if method == "tbank_phone":
        phone_faces = [
            ("Дамир", "Сеничев"),
            ("Дамир", "Семенов"),
            ("Марина", "Чернова"),
            ("Сергей", "Сеничев"),
            ("Анна", "Семенова"),
        ]
        first, last = phone_faces[(i + attempt) % len(phone_faces)]
        amounts = [300, 1000, 3500, 4000, 5010, 9500, 14000, 41000]
    else:
        if i % 3 == 0 and not method.startswith("alfa"):
            extra = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 10)))
            last = (last + extra)[:26]
        amounts = [
            1000, 3500, 5010, 9500, 14000, 21000, 41000,
            56000, 100000, 10001, 77777,
        ]
        if method.startswith("alfa"):
            amounts = [1000, 3500, 4000, 5000, 7800, 9500, 12000, 14000]
            # Keep short bank-style FIO — long stress tails break Alfa slot fit.
            first, last = pick_sender_pair(i, attempt)
    receiver = pick_receiver_short(i, attempt)
    if method == "tbank_phone":
        receiver = ["Марина Ч.", "Ольга М.", "Анна С.", "Дарья Б."][(i) % 4]
    # Sber exact-profile: dotted initial «Имя X.» → SBER_RECIPIENT_INITIAL_PUNCTUATION
    if method.startswith("sber"):
        receiver = [
            "Анна Иванова",
            "Мария Смирнова",
            "Ольга Морозова",
            "Елена Козлова",
            "Павел Волков",
        ][(i + attempt) % 5]
        amounts = [1000, 1500, 2500, 3500, 4000, 5000, 7800, 9500]
    amount = amounts[(i + attempt) % len(amounts)]
    hh, mm = rng.randint(8, 22), rng.randint(0, 59)
    ss = 1 + (rng.randint(0, 58) + i + attempt) % 59
    phone = (
        f"+7 ({rng.randint(900, 999)}) {rng.randint(100, 999)}-"
        f"{rng.randint(10, 99)}-{rng.randint(10, 99)}"
    )
    date = f"21.04.2026  {hh:02d}:{mm:02d}:{ss:02d}"
    if method.startswith("alfa"):
        date = f"15.05.2026 {hh:02d}:{mm:02d}:{ss:02d}"
    if method.startswith("sber"):
        date = f"12.06.2026 {hh:02d}:{mm:02d}:{ss:02d}"
    base = {
        "date_time": date,
        "amount": str(amount),
        "sender": f"{first} {last}",
        "receiver": receiver,
        "phone": phone,
        "receipt_num": "авто",
    }
    if method.startswith("alfa"):
        base["recipient_bank"] = rng.choice(("Сбербанк", "Т-Банк", "ВТБ"))
    elif method == "tbank_card_sber":
        base["recipient_bank"] = "Сбербанк"
    elif method == "tbank_card_tbank":
        base["recipient_bank"] = "Т-Банк"
    return base


def _visual_ok(pdf: bytes, expect_bits: List[str]) -> Tuple[bool, str]:
    import fitz

    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        nblocks = len(doc[0].get_text("blocks"))
        doc.close()
        for bit in expect_bits:
            if bit and bit not in text and bit.replace(" ", "") not in text.replace(" ", ""):
                return False, f"missing-text:{bit[:40]}"
        if nblocks < 3:
            return False, "too-few-blocks"
        return True, "ok"
    except Exception as exc:
        return False, f"visual:{exc}"


def _local_side(pdf: bytes) -> Tuple[str, List[str]]:
    try:
        from detector import route  # type: ignore

        _bank, res, _rec = route(pdf)
        return str(res.get("verdict") or "?"), [str(f) for f in (res.get("flags") or [])]
    except Exception as exc:
        return f"ERR:{type(exc).__name__}", [str(exc)[:120]]


async def _send_proton(client, cfg, path: Path, caption: str) -> Tuple[str, str]:
    """Send file to Proton with caption; return (verdict, raw_tail)."""
    from onlypdf_gate import _norm_bot, DEFAULT_PROTON, _wait_verdict

    bot = _norm_bot(cfg.get("TG_PROTON_BOT", DEFAULT_PROTON), DEFAULT_PROTON)
    if not client.is_connected():
        from tg_client import connect_client

        await connect_client(client, cfg)
    msgs = await client.get_messages(bot, limit=1)
    after = msgs[0].id if msgs else 0
    await client.send_file(bot, str(path), caption=caption[:900])
    # Prefer full wait with parser; also keep last text for explain
    v = await _wait_verdict(client, bot, after, cfg, parser=_parse_proton)
    raw = ""
    try:
        async for msg in client.iter_messages(bot, limit=6):
            if msg.id <= after:
                continue
            t = (msg.text or "").strip()
            if t and ("оригинал" in t.lower() or "фейк" in t.lower() or "чисто" in t.lower() or "вердикт" in t.lower()):
                raw = t[:1500]
                break
    except Exception:
        pass
    return v, raw


async def run_round_async(
    *,
    per_method: int = 2,
    methods: Optional[List[str]] = None,
    pause: float = 5.0,
) -> dict:
    gens = _load_gens()
    methods = methods or METHODS
    status: Dict[str, Any] = {
        "started": datetime.now().isoformat(),
        "methods": {},
        "round": int(time.time()),
        "mode": "tg_proton_always",
    }
    client, cfg = await open_onlypdf_client()
    cfg.setdefault("TG_WAIT_SECONDS", "55")
    cfg.setdefault("TG_POLL_SECONDS", "0.7")
    try:
        for method in methods:
            if _STOP.exists():
                _log("STOP — abort")
                break
            gen = gens[method]
            mdir = _OUT / method
            mdir.mkdir(parents=True, exist_ok=True)
            stats = {
                "gen_ok": 0,
                "gen_fail": 0,
                "visual_fail": 0,
                "tg_pass": 0,
                "tg_fake": 0,
                "tg_other": 0,
                "samples": [],
            }
            _log(f"=== TG {method} n={per_method} ===")
            for i in range(per_method):
                if _STOP.exists():
                    break
                payload = _stress_payload(method, i + int(time.time()) % 900, 0)
                try:
                    pdf = gen(payload)
                except Exception as exc:
                    stats["gen_fail"] += 1
                    _log(f"{method} GEN_EXC {exc}")
                    continue
                if not pdf:
                    stats["gen_fail"] += 1
                    _log(f"{method} GEN_NONE {payload.get('sender')}")
                    continue
                stats["gen_ok"] += 1
                expect = [payload.get("sender", "").split()[0]]
                vok, vdetail = _visual_ok(pdf, expect)
                if not vok:
                    stats["visual_fail"] += 1
                    _log(f"{method} VISUAL_FAIL {vdetail}")
                path = mdir / f"tg_{status['round']}_{i:02d}.pdf"
                path.write_bytes(pdf)
                local_v, local_flags = _local_side(pdf)
                caption = (
                    f"NIGHT {method} #{i+1}\n"
                    f"{payload.get('sender')} → {payload.get('receiver')}\n"
                    f"amt={payload.get('amount')} phone={payload.get('phone')}\n"
                    f"local={local_v}"
                )
                try:
                    tv, raw = await asyncio.wait_for(
                        _send_proton(client, cfg, path, caption),
                        timeout=100.0,
                    )
                except Exception as exc:
                    stats["tg_other"] += 1
                    _log(f"{method} TG_ERR {type(exc).__name__}:{exc}")
                    await asyncio.sleep(2)
                    continue
                row = {
                    "ts": datetime.now().isoformat(),
                    "method": method,
                    "path": str(path),
                    "tg": tv,
                    "local": local_v,
                    "local_flags": local_flags[:8],
                    "payload": payload,
                    "raw": raw[:800],
                }
                _append_row("queue_tg.jsonl", row)
                stats["samples"].append({"file": path.name, "tg": tv, "local": local_v})
                if tv == "PASS":
                    stats["tg_pass"] += 1
                    _log(f"{method} TG=PASS local={local_v}")
                elif tv == "FAKE":
                    stats["tg_fake"] += 1
                    _append_row("queue_fake.jsonl", row)
                    _log(f"{method} TG=FAKE local={local_v} flags={local_flags[:2]}")
                    if raw:
                        _log(f"{method} EXPLAIN: {raw[:400].replace(chr(10), ' | ')}")
                else:
                    stats["tg_other"] += 1
                    _log(f"{method} TG={tv} local={local_v}")
                await asyncio.sleep(max(3.0, pause))

            status["methods"][method] = stats
            _write_status(status)
            _log(
                f"{method} done tg_pass={stats['tg_pass']} tg_fake={stats['tg_fake']} "
                f"other={stats['tg_other']} gen_fail={stats['gen_fail']}"
            )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    status["finished"] = datetime.now().isoformat()
    _write_status(status)
    return status


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-n", type=int, default=5, help="PDFs per method")
    ap.add_argument("--sleep", type=int, default=25)
    ap.add_argument("--only", default="", help="comma methods subset")
    ap.add_argument("--pause", type=float, default=8.0, help="pause between TG sends")
    args = ap.parse_args()
    methods = [m.strip() for m in args.only.split(",") if m.strip()] or None

    _OUT.mkdir(parents=True, exist_ok=True)
    if _STOP.exists():
        _STOP.unlink()
    _log("NIGHT TG-PROTON LOOP START (every PDF → @proton_pdf_bot)")
    round_i = 0
    while True:
        if _STOP.exists():
            _log("STOP — exit")
            return 0
        round_i += 1
        _log(f"==== ROUND {round_i} ====")
        try:
            asyncio.run(
                run_round_async(
                    per_method=args.n, methods=methods, pause=args.pause
                )
            )
        except Exception:
            _log("ROUND_CRASH\n" + traceback.format_exc())
        if args.once:
            return 0
        time.sleep(max(5, args.sleep))


if __name__ == "__main__":
    raise SystemExit(main())
