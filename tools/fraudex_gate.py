# -*- coding: utf-8 -*-
"""Fraudex gate: Telegram acceptance via @FraudexBot on a *separate* session.

Parallel to OnlyPDF (tools/.tg_checker_session):
  - Session: tools/.tg_fraudex_session  (TG_SESSION / TG_FRAUDEX_SESSION)
  - Bot:     @FraudexBot               (TG_FRAUDEX_BOT / TG_CHECKER_BOT)

Canonical QA still goes through gen_onlypdf30_*.py; set TG_GATE=fraudex
(or --gate fraudex) so the batch uses this gate instead of OnlyPDF.

Strict streak: FAKE/UNKNOWN wipe → need n consecutive PASS (RESULT STREAK).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from tg_check_pdf import _load_env
from tg_client import connect_client, ensure_login, make_client

DEFAULT_FRAUDEX = "@FraudexBot"

GenFn = Callable[[dict], Optional[bytes]]
PayloadFn = Callable[[int, int], dict]
ValidateFn = Callable[[bytes], tuple[bool, str]]
AcceptFn = Callable[[int, Path, bytes], None]


def _norm_bot(name: str, default: str) -> str:
    bot = (name or default).strip() or default
    if not bot.startswith("@"):
        bot = "@" + bot
    return bot


def _is_service_error(text: str) -> bool:
    low = (text or "").lower()
    needles = (
        "ошибка сервиса",
        "ошибка сервера",
        "сервис временно",
        "не удалось проверить",
        "попробуйте позже",
        "service error",
        "internal error",
        "try again later",
        "слишком много запросов",
        "подождите",
    )
    return any(n in low for n in needles)


def _parse_fraudex(text: str) -> str:
    """PASS only on explicit green.

    Both red modes are reject (still returned as FAKE for streak wipe):
      - structure: «Нарушена структура…»
      - scammer:  «скамерский чек» / подделка / не прошёл
    """
    t = text or ""
    low = t.lower()
    if _is_service_error(t):
        return "SERVICE_ERROR"
    # Intermediate «Загрузка…»
    if ("загрузка" in low or "loading" in low or "обработ" in low) and len(t) < 160:
        return "?"
    # Structure first (distinct message from scammer flag).
    if "нарушена структура" in low or ("структур" in low and "❌" in t):
        return "FAKE"
    if any(
        x in low
        for x in (
            "скамерск",
            "скамер",
            "поддел",
            "подозрительн",
            "встречался в другом",
            "не прошёл",
            "не прошел",
            "не прошла проверку",
            "не прошел проверку",
            "pdf не",
            "⛔️",
            "⛔",
        )
    ):
        return "FAKE"
    if "❌" in t and ("проверк" in low or "поддел" in low or "скамер" in low):
        return "FAKE"
    if any(
        x in low
        for x in (
            "прошёл проверку",
            "прошел проверку",
            "прошла проверку",
            "проверка пройдена",
            "полностью прошёл",
            "полностью прошел",
        )
    ):
        return "PASS"
    if "✅" in t and "структур" in low and "не " not in low.split("✅", 1)[-1][:40]:
        return "PASS"
    if "✅" in t and any(x in low for x in ("прошёл", "прошел", "оригинал", "чист")):
        return "PASS"
    return "?"


def _fraudex_fail_kind(text: str) -> str:
    """Fine-grained red reason for logs: structure | scammer | other | ok."""
    low = (text or "").lower()
    if _parse_fraudex(text) == "PASS":
        return "ok"
    if "нарушена структура" in low or ("структур" in low and ("❌" in (text or "") or "нарушен" in low)):
        return "structure"
    if any(x in low for x in ("скамерск", "скамер", "поддел", "подозрительн", "встречался в другом")):
        return "scammer"
    if _parse_fraudex(text) == "FAKE":
        return "other"
    return "unknown"


_FINAL = frozenset({"PASS", "FAKE", "UNKNOWN", "SERVICE_ERROR"})


def _fraudex_cfg(cfg: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    cfg = dict(cfg or _load_env())
    # Dedicated session — never share SQLite with OnlyPDF runner.
    sess = (
        cfg.get("TG_FRAUDEX_SESSION")
        or cfg.get("TG_SESSION")
        or ""
    ).strip()
    if not sess:
        sess = str(Path(__file__).resolve().parent / ".tg_fraudex_session")
    cfg["TG_SESSION"] = sess
    cfg.setdefault("TG_WAIT_SECONDS", "60")
    cfg.setdefault("TG_POLL_SECONDS", "0.7")
    bot = (
        cfg.get("TG_FRAUDEX_BOT")
        or cfg.get("TG_CHECKER_BOT")
        or DEFAULT_FRAUDEX
    )
    cfg["TG_FRAUDEX_BOT"] = _norm_bot(bot, DEFAULT_FRAUDEX)
    return cfg


async def _wait_verdict(client, bot: str, after_id: int, cfg: Dict[str, str]) -> str:
    poll = float(cfg.get("TG_POLL_SECONDS", "0.7"))
    deadline = time.monotonic() + float(cfg.get("TG_WAIT_SECONDS", "60"))
    best = "?"
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        async for msg in client.iter_messages(bot, limit=15):
            if msg.id <= after_id:
                continue
            body = (msg.text or msg.message or "").strip()
            if not body:
                continue
            v = _parse_fraudex(body)
            if v in _FINAL:
                return v
            if v == "?":
                best = "?"
        if not client.is_connected():
            try:
                await connect_client(client, cfg)
            except Exception:
                pass
    return "TIMEOUT" if best == "?" else best


async def fraudex_verdict(client, path: Path, cfg: Dict[str, str]) -> str:
    bot = _norm_bot(cfg.get("TG_FRAUDEX_BOT", DEFAULT_FRAUDEX), DEFAULT_FRAUDEX)
    for _ in range(4):
        try:
            if not client.is_connected():
                await connect_client(client, cfg)
            msgs = await client.get_messages(bot, limit=1)
            after = msgs[0].id if msgs else 0
            await client.send_file(bot, str(path))
            return await _wait_verdict(client, bot, after, cfg)
        except Exception as exc:
            print(f"  fraudex send/wait err: {exc}", flush=True)
            await asyncio.sleep(2)
            try:
                await connect_client(client, cfg)
            except Exception:
                pass
    return "TIMEOUT"


async def open_fraudex_client(cfg: Optional[Dict[str, str]] = None):
    cfg = _fraudex_cfg(cfg)
    client = make_client(cfg)
    await ensure_login(client, cfg)
    return client, cfg


async def generate_fraudex_batch(
    *,
    out_dir: Path,
    n: int,
    payload_fn: PayloadFn,
    gen_fn: GenFn,
    validate_fn: Optional[ValidateFn] = None,
    prefix: str = "pass",
    max_attempts: int = 12,
    fresh: bool = True,
    full_recheck: bool = False,
    on_accept: Optional[AcceptFn] = None,
    start_at: int = 1,
    strict_streak: bool = False,
    max_streak_breaks: int = 30,
) -> int:
    """Same streak contract as onlypdf_gate, but verdicts from @FraudexBot."""
    _ = full_recheck  # reserved; Fraudex recheck optional later
    if fresh and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reject_root = out_dir.parent / f"{out_dir.name}_rejects"
    reject_root.mkdir(parents=True, exist_ok=True)

    client, cfg = await open_fraudex_client()
    bot = cfg["TG_FRAUDEX_BOT"]
    mode = "STRICT_STREAK_WIPE" if strict_streak else "soft-fill"
    print(
        f"Fraudex gate → {bot} | session={cfg['TG_SESSION']} | "
        f"{mode} target {n} (from {start_at}) → {out_dir}",
        flush=True,
    )

    streak_resets = 0

    def _wipe_accepted() -> None:
        for p in out_dir.glob(f"{prefix}_*.pdf"):
            try:
                p.unlink()
            except OSError:
                pass

    try:
        i = start_at
        while i <= n:
            t_item = time.monotonic()
            ok = False
            wiped = False
            bias = streak_resets * 17
            for attempt in range(max_attempts):
                data = payload_fn(i, attempt + bias)
                pdf = gen_fn(data)
                if not pdf:
                    print(f"{i:02d} a{attempt}+{bias} GEN_FAIL", flush=True)
                    continue
                if validate_fn:
                    good, reason = validate_fn(pdf)
                    if not good:
                        print(
                            f"{i:02d} a{attempt}+{bias} reject-local {reason}",
                            flush=True,
                        )
                        continue
                path = out_dir / f"{prefix}_{i:02d}.pdf"
                path.write_bytes(pdf)
                print(
                    f"{i:02d} a{attempt}+{bias} fraudex… "
                    f"{data.get('sender') or data.get('sender_name') or data.get('receiver') or ''} "
                    f"{data.get('amount')} "
                    f"{data.get('recipient_bank') or data.get('bank_name') or data.get('bank') or ''}",
                    flush=True,
                )
                verdict = await fraudex_verdict(client, path, cfg)
                if verdict == "PASS":
                    dt = time.monotonic() - t_item
                    print(
                        f"{i:02d} PASS fraudex streak={i}/{n} ({dt:.1f}s)",
                        flush=True,
                    )
                    if on_accept:
                        on_accept(i, path, pdf)
                    ok = True
                    break

                print(f"{i:02d} a{attempt}+{bias} rejected ({verdict})", flush=True)
                try:
                    # Keep rejects for analysis
                    rej = reject_root / f"{prefix}_{i:02d}_a{attempt}_{verdict}.pdf"
                    if path.is_file():
                        shutil.copy2(path, rej)
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

                if strict_streak and verdict in ("FAKE", "UNKNOWN"):
                    if i > start_at:
                        streak_resets += 1
                        _wipe_accepted()
                        print(
                            f"STREAK_WIPE at {i}/{n} verdict={verdict} "
                            f"resets={streak_resets}/{max_streak_breaks} "
                            f"→ restart from {start_at} reject→{reject_root}",
                            flush=True,
                        )
                        if streak_resets > max_streak_breaks:
                            print(
                                f"FATAL too many streak wipes ({streak_resets})",
                                flush=True,
                            )
                            return 5
                        i = start_at
                        wiped = True
                        break
                    print(
                        f"STREAK_RETRY_FIRST at 1/{n} verdict={verdict} "
                        f"(no wipe yet) reject→{reject_root}",
                        flush=True,
                    )
                    continue

                if strict_streak and verdict in ("TIMEOUT", "SERVICE_ERROR", "?"):
                    await asyncio.sleep(2)
                    continue

            if wiped:
                continue
            if not ok:
                print(f"FATAL item {i}", flush=True)
                return 2
            i += 1

        if strict_streak:
            print(
                f"RESULT STREAK {n}/{n} FAKE=0 resets={streak_resets} → {out_dir}",
                flush=True,
            )
        else:
            print(f"RESULT {n}/{n} PASS fraudex → {out_dir}", flush=True)
        return 0
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            print(f"disconnect warn: {exc}", flush=True)


def gate_requested() -> bool:
    """True when caller wants Fraudex instead of OnlyPDF."""
    g = (os.environ.get("TG_GATE") or "").strip().lower()
    return g in ("fraudex", "fx", "safecheck")
