# -*- coding: utf-8 -*-
"""OnlyPDF gate: fast Telegram acceptance for live generators.

Canonical QA (Proton OFF):
  1) generate PDF (create_*_stealth)
  2) optional local validate_fn (lightweight)
  3) send to @onlypdf_robot
  4) OnlyPDF PASS×1 — budget ~30s per check

Session: tools/.tg_checker_session + tools/tg_check.env
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

from tg_check_pdf import _load_env
from tg_client import connect_client, ensure_login, make_client

DEFAULT_ONLYPDF = "@onlypdf_robot"
DEFAULT_PROTON = "@proton_pdf_bot"


def _gate_is_proton() -> bool:
    import os

    return (os.environ.get("TG_GATE") or "").strip().lower() in (
        "proton",
        "proton_pdf",
        "proton_pdf_bot",
    )


def _norm_bot(name: str, default: str) -> str:
    bot = (name or default).strip() or default
    if not bot.startswith("@"):
        bot = "@" + bot
    return bot


def _is_service_error(text: str) -> bool:
    """Сбой бота/бэка — хуже FAKE: не оригинал и не вердикт."""
    low = (text or "").lower()
    needles = (
        "ошибка сервиса",
        "ошибка сервера",
        "ошибка во время проверки",
        "сервис временно",
        "временная ошибка",
        "не удалось проверить",
        "попробуйте позже",
        "service error",
        "internal error",
        "try again later",
    )
    return any(n in low for n in needles)


def _parse_onlypdf(text: str) -> str:
    """Только явный оригинал. Ошибка сервиса / нераспознан / мусор — не PASS."""
    t = text or ""
    if _is_service_error(t):
        return "SERVICE_ERROR"
    if "Результат проверки" not in t:
        return "?"
    low = t.lower()
    # Нераспознанный документ — не PASS (раньше ложно матчили «подделка: нет»).
    if "чек не распознан: да" in low:
        return "UNKNOWN"
    # Заголовок вида «❌ чек не распознан» / «не распознан».
    for line in t.splitlines():
        s = line.strip().lower()
        if "результат проверки" in s and "не распознан" in s:
            return "UNKNOWN"
        if s.startswith("❌") and "не распознан" in s:
            return "UNKNOWN"
    if "вероятная подделка: да" in low:
        return "FAKE"
    if "подделан" in low and "вероятная подделка: нет" not in low:
        return "FAKE"
    # Строго: явный «не подделка» + документ распознан.
    if "вероятная подделка: нет" in low:
        if "чек не распознан: нет" in low or "✅" in t:
            return "PASS"
        # Короткий ответ без чеклиста — не доверяем.
        return "?"
    return "?"


def _parse_proton(text: str) -> str:
    """Явное ЧИСТО/ОРИГИНАЛ. Ошибка сервиса / ФЕЙК / UNKNOWN — не PASS.

    Live @proton_pdf_bot для @kronlead (verbose) пишет «✅ ОРИГИНАЛ»,
    для обычных — «✅ Оригинал»; слово «ЧИСТО» часто только в meta Alfa.
    """
    import re

    t = text or ""
    # HTML / markdown из parse_mode — снимаем разметку.
    plain = re.sub(r"<[^>]+>", "", t)
    plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    plain = re.sub(r"[*_`]+", "", plain)
    low = plain.lower()
    if _is_service_error(plain):
        return "SERVICE_ERROR"
    # Промежуточный статус — ещё ждём финал.
    if "проверяю" in low and "оригинал" not in low and "фейк" not in low and "чисто" not in low:
        return "?"
    if "1970" in plain and ("дата" in low or "платеж" in low):
        return "FAKE"
    if (
        "verdict=фейк" in low
        or "final_verdict: фейк" in low
        or "❌ фейк" in low
        or "❌ подделка" in low
    ):
        return "FAKE"
    if (
        "verdict=чисто" in low
        or "final_verdict: чисто" in low
        or "✅ чисто" in low
        or "✅ оригинал" in low
    ):
        return "PASS"
    for line in plain.splitlines():
        s = line.strip().lower()
        if s in ("✅ чисто", "чисто", "чисто.", "результат: чисто"):
            return "PASS"
        if s.startswith("✅ оригинал") or s in ("оригинал", "оригинал.", "результат: оригинал"):
            return "PASS"
        if s in ("❌ фейк", "фейк", "фейк.", "результат: фейк", "❌ подделка", "подделка"):
            return "FAKE"
        if "неизвестный документ" in s:
            return "UNKNOWN"
    if "неизвестный документ" in low:
        return "UNKNOWN"
    return "?"


_FINAL_VERDICTS = frozenset({"PASS", "FAKE", "UNKNOWN", "SERVICE_ERROR"})

# @onlypdf_robot: быстрее ~2 файла/мин → UNKNOWN/flake. Держим паузу между send.
_ONLYPDF_MIN_INTERVAL_SEC = 30.0
_onlypdf_last_send_mono = 0.0


async def _pace_onlypdf_send() -> None:
    global _onlypdf_last_send_mono
    now = time.monotonic()
    wait = _ONLYPDF_MIN_INTERVAL_SEC - (now - _onlypdf_last_send_mono)
    if wait > 0:
        await asyncio.sleep(wait)
    _onlypdf_last_send_mono = time.monotonic()


async def _wait_verdict(
    client,
    bot: str,
    after_id: int,
    cfg: Dict[str, str],
    *,
    parser,
    timeout: Optional[float] = None,
) -> str:
    poll = float(cfg.get("TG_POLL_SECONDS", "0.7"))
    deadline = time.monotonic() + float(
        timeout if timeout is not None else cfg.get("TG_WAIT_SECONDS", "25")
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        async for msg in client.iter_messages(bot, limit=12):
            if msg.id <= after_id:
                continue
            body = (msg.text or msg.message or "").strip()
            if not body:
                continue
            v = parser(body)
            if v in _FINAL_VERDICTS:
                if v != "PASS":
                    snippet = " | ".join(
                        ln.strip() for ln in body.splitlines() if ln.strip()
                    )[:400]
                    tag = "proton_raw" if parser is _parse_proton else "onlypdf_raw"
                    print(f"  {tag}={snippet}", flush=True)
                return v
        if not client.is_connected():
            try:
                await connect_client(client, cfg)
            except Exception:
                pass
    return "TIMEOUT"


async def onlypdf_verdict(
    client,
    path: Path,
    cfg: Dict[str, str],
    *,
    bot: Optional[str] = None,
) -> str:
    bot = _norm_bot(bot or cfg.get("TG_ONLYPDF_BOT", DEFAULT_ONLYPDF), DEFAULT_ONLYPDF)
    for reconnect in range(4):
        try:
            if not client.is_connected():
                await connect_client(client, cfg)
            msgs = await client.get_messages(bot, limit=1)
            after = msgs[0].id if msgs else 0
            await _pace_onlypdf_send()
            await client.send_file(bot, str(path))
            return await _wait_verdict(client, bot, after, cfg, parser=_parse_onlypdf)
        except (ConnectionError, OSError, sqlite3.OperationalError) as exc:
            print(f"tg reconnect after {type(exc).__name__}: {exc}", flush=True)
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(1 + reconnect)
            await connect_client(client, cfg)
    return "TIMEOUT"


async def proton_verdict(
    client,
    path: Path,
    cfg: Dict[str, str],
    *,
    bot: Optional[str] = None,
) -> str:
    bot = _norm_bot(bot or cfg.get("TG_PROTON_BOT", DEFAULT_PROTON), DEFAULT_PROTON)
    for reconnect in range(4):
        try:
            if not client.is_connected():
                await connect_client(client, cfg)
            msgs = await client.get_messages(bot, limit=1)
            after = msgs[0].id if msgs else 0
            await _pace_onlypdf_send()
            await client.send_file(bot, str(path))
            wait = float(cfg.get("TG_WAIT_SECONDS", "90"))
            return await _wait_verdict(
                client, bot, after, cfg, parser=_parse_proton, timeout=max(wait, 90.0),
            )
        except (ConnectionError, OSError, sqlite3.OperationalError) as exc:
            print(f"tg reconnect proton after {type(exc).__name__}: {exc}", flush=True)
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(1 + reconnect)
            await connect_client(client, cfg)
    return "TIMEOUT"


async def dual_verdict_parallel(
    client,
    path: Path,
    cfg: Dict[str, str],
) -> Tuple[str, str]:
    """OnlyPDF-only. Second value always SKIP (Proton disabled)."""
    v = await onlypdf_verdict(client, path, cfg)
    return v, "SKIP"


async def dual_accept_verdict(
    client,
    path: Path,
    cfg: Dict[str, str],
    *,
    pause: float = 0.2,
    onlypdf_double: bool = False,
    reject_dir: Optional[Path] = None,
) -> str:
    """OnlyPDF PASS×1. TIMEOUT flake-retry; FAKE/UNKNOWN are final."""
    _ = pause
    v1 = "TIMEOUT"
    tries = 5
    use_proton = _gate_is_proton()
    checker = "proton" if use_proton else "onlypdf"
    for try_i in range(1, tries + 1):
        if use_proton:
            v1 = await proton_verdict(client, path, cfg)
        else:
            v1 = await onlypdf_verdict(client, path, cfg)
        print(
            f"  {checker}={v1}"
            + (f" (try {try_i}/{tries})" if try_i > 1 and v1 != "PASS" else ""),
            flush=True,
        )
        if v1 == "PASS":
            break
        if v1 in ("FAKE", "UNKNOWN"):
            break
        if v1 == "TIMEOUT" and try_i < tries:
            await asyncio.sleep(2.0 + try_i)
            continue
        if v1 == "SERVICE_ERROR":
            await asyncio.sleep(3)
        break
    if v1 != "PASS":
        try:
            rej_dir = reject_dir or (path.parent / "_rejects")
            rej_dir.mkdir(parents=True, exist_ok=True)
            rej = rej_dir / f"{path.stem}_rej_{v1}.pdf"
            pdf_b = path.read_bytes()
            rej.write_bytes(pdf_b)
            print(f"  saved reject → {rej}", flush=True)
            # Local Proton — so FAKE is never ignored without a reason string.
            try:
                import sys as _sys
                from pathlib import Path as _P

                checker = _P(r"C:\Users\fanis\OneDrive\Desktop\pdf-checker-bot")
                if checker.is_dir() and str(checker) not in _sys.path:
                    _sys.path.insert(0, str(checker))
                from detector import route as _route  # type: ignore

                _bank, res, _rec = _route(pdf_b)
                flags = res.get("flags") or []
                print(
                    f"  local_proton={res.get('verdict')} "
                    f"flags={flags[:3] if flags else '[]'}",
                    flush=True,
                )
            except Exception as pe:
                print(f"  local_proton_err={type(pe).__name__}:{pe}", flush=True)
        except Exception as exc:
            print(f"  reject-save err: {exc}", flush=True)
        return v1
    if not onlypdf_double:
        return "PASS"
    await asyncio.sleep(0.3)
    v2 = await onlypdf_verdict(client, path, cfg)
    print(f"  onlypdf2={v2}", flush=True)
    if v2 == "SERVICE_ERROR":
        await asyncio.sleep(3)
        return v2
    return v2 if v2 == "PASS" else v2


async def dual_accept(
    client,
    path: Path,
    cfg: Dict[str, str],
    *,
    pause: float = 0.2,
    onlypdf_double: bool = False,
) -> bool:
    """OnlyPDF PASS×1 (быстро). onlypdf_double=True → PASS×2 с короткой паузой."""
    return (
        await dual_accept_verdict(
            client, path, cfg, pause=pause, onlypdf_double=onlypdf_double
        )
        == "PASS"
    )


# Back-compat aliases used by older scripts
async def onlypdf_accept_double(
    client,
    path: Path,
    cfg: Dict[str, str],
    *,
    bot: Optional[str] = None,
    pause: float = 0.3,
) -> bool:
    """Legacy name — OnlyPDF PASS×1."""
    _ = bot
    return await dual_accept(client, path, cfg, pause=pause, onlypdf_double=False)


async def open_onlypdf_client(cfg: Optional[Dict[str, str]] = None):
    cfg = cfg or _load_env()
    if _gate_is_proton():
        try:
            wait = float(cfg.get("TG_WAIT_SECONDS") or 90)
        except (TypeError, ValueError):
            wait = 90.0
        cfg["TG_WAIT_SECONDS"] = str(max(wait, 90.0))
        cfg.setdefault("TG_POLL_SECONDS", "0.7")
    else:
        cfg.setdefault("TG_WAIT_SECONDS", "25")
        cfg.setdefault("TG_POLL_SECONDS", "0.5")
    client = make_client(cfg)
    await ensure_login(client, cfg)
    return client, cfg


GenFn = Callable[[dict], Optional[bytes]]
PayloadFn = Callable[[int, int], dict]
ValidateFn = Callable[[bytes], tuple[bool, str]]
AcceptFn = Callable[[int, Path, bytes], None]


async def generate_onlypdf_batch(
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
    """Generate n PDFs that PASS OnlyPDF×1.

    strict_streak=True: любой FAKE/UNKNOWN **сбрасывает серию на 0** (wipe
    принятых PDF) — нужны n зелёных подряд без косяков. GEN_FAIL / local-reject
    / TIMEOUT / SERVICE_ERROR серию не ломают (ретрай того же слота).
    Успех только RESULT STREAK n/n FAKE=0 resets=K (в принятой серии FAKE=0).
    max_streak_breaks = лимит полных сбросов серии; превышение → rc=5.

    TG_GATE=fraudex → delegate to fraudex_gate (parallel second TG account).
    """
    import os
    import shutil

    if (os.environ.get("TG_GATE") or "").strip().lower() in (
        "fraudex",
        "fx",
        "safecheck",
    ):
        from fraudex_gate import generate_fraudex_batch

        return await generate_fraudex_batch(
            out_dir=out_dir,
            n=n,
            payload_fn=payload_fn,
            gen_fn=gen_fn,
            validate_fn=validate_fn,
            prefix=prefix,
            max_attempts=max_attempts,
            fresh=fresh,
            full_recheck=full_recheck,
            on_accept=on_accept,
            start_at=start_at,
            strict_streak=strict_streak,
            max_streak_breaks=max_streak_breaks,
        )

    if fresh and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Rejects вне out_dir — переживают wipe серии.
    reject_root = out_dir.parent / f"{out_dir.name}_rejects"
    reject_root.mkdir(parents=True, exist_ok=True)

    client, cfg = await open_onlypdf_client()
    use_proton = _gate_is_proton()
    gate_bot = (
        _norm_bot(cfg.get("TG_PROTON_BOT", DEFAULT_PROTON), DEFAULT_PROTON)
        if use_proton
        else cfg.get("TG_ONLYPDF_BOT", DEFAULT_ONLYPDF)
    )
    gate_name = "Proton" if use_proton else "OnlyPDF"
    mode = "STRICT_STREAK_WIPE" if strict_streak else "soft-fill"
    print(
        f"{gate_name} gate → {gate_bot} | {mode} target {n} (from {start_at}) → {out_dir}",
        flush=True,
    )

    streak_resets = 0  # сколько раз FAKE/UNKNOWN обнулил серию (i>1)

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
            # После wipe смещаем attempt, иначе слот 1 крутит тот же PDF.
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
                    f"{i:02d} a{attempt}+{bias} check… "
                    f"{data.get('sender') or data.get('sender_name') or data.get('receiver') or data.get('receiver_name')} "
                    f"{data.get('amount')} "
                    f"{data.get('recipient_bank') or data.get('bank_name') or data.get('bank') or ''}",
                    flush=True,
                )
                verdict = await dual_accept_verdict(
                    client, path, cfg, reject_dir=reject_root
                )
                if verdict == "PASS":
                    dt = time.monotonic() - t_item
                    print(
                        f"{i:02d} PASS {gate_name.lower()} streak={i}/{n} ({dt:.1f}s)",
                        flush=True,
                    )
                    if on_accept:
                        on_accept(i, path, pdf)
                    ok = True
                    break

                print(f"{i:02d} a{attempt}+{bias} rejected ({verdict})", flush=True)
                try:
                    dump = reject_root / f"{prefix}_{i:02d}_a{attempt}_payload.json"
                    dump.write_text(
                        __import__("json").dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"  saved payload → {dump}", flush=True)
                except Exception as _pe:
                    print(f"  payload-save err: {_pe}", flush=True)
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

                if strict_streak and verdict in ("FAKE", "UNKNOWN"):
                    if (os.environ.get("PROTON_HOLD_FAIL") or "").strip() == "1":
                        print(
                            "HOLD_FAIL same payload — fix generator, "
                            f"then retry this data. verdict={verdict}",
                            flush=True,
                        )
                        return 4
                    streak_resets += 1
                    if streak_resets > max_streak_breaks:
                        print(
                            f"FATAL too many STREAK_BREAK ({streak_resets})",
                            flush=True,
                        )
                        return 5
                    print(
                        f"STREAK_BREAK at {i}/{n} verdict={verdict} "
                        f"reset→0 (resets={streak_resets}) wipe accepted",
                        flush=True,
                    )
                    _wipe_accepted()
                    i = start_at
                    wiped = True
                    await asyncio.sleep(2)
                    break

                if strict_streak and verdict in ("TIMEOUT", "SERVICE_ERROR", "?"):
                    await asyncio.sleep(2)
                    continue
                # soft-fill: ретрай того же i

            if wiped:
                continue
            if not ok:
                print(f"FATAL item {i}", flush=True)
                return 2
            i += 1

        if full_recheck:
            print("=== FULL RECHECK (OnlyPDF) ===", flush=True)
            fails = []
            paths = [
                out_dir / f"{prefix}_{j:02d}.pdf"
                for j in range(start_at, n + 1)
                if (out_dir / f"{prefix}_{j:02d}.pdf").is_file()
            ]
            for p in paths:
                vo, _vp = await dual_verdict_parallel(client, p, cfg)
                print(f"recheck {p.name} onlypdf={vo}", flush=True)
                if vo != "PASS":
                    fails.append(f"{p.name}:{vo}")
            if fails:
                print(f"RECHECK FAIL {fails}", flush=True)
                return 3

        if strict_streak:
            print(
                f"RESULT STREAK {n}/{n} FAKE=0 resets={streak_resets} → {out_dir}",
                flush=True,
            )
        else:
            print(f"RESULT {n}/{n} PASS {gate_name.lower()} → {out_dir}", flush=True)
        return 0
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            print(f"disconnect warn: {exc}", flush=True)
