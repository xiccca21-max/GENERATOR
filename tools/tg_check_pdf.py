"""
Отправляет PDF во внешний Telegram-бот чекер и печатает ответ.

Первый запуск попросит номер телефона и код из Telegram (один раз).
Дальше сессия сохраняется в tools/.tg_checker_session.session
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

_DIR = Path(__file__).resolve().parent
_ROOT = _DIR.parent
_ENV_PATH = _DIR / "tg_check.env"
_SESSION = str(_DIR / ".tg_checker_session")


def _load_env() -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    if not _ENV_PATH.exists():
        print(f"Нет файла настроек: {_ENV_PATH}")
        print(f"Скопируй пример: copy tg_check.env.example tg_check.env")
        sys.exit(1)
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        cfg[key.strip()] = val.strip()
    return cfg


def _need(cfg: Dict[str, str], key: str) -> str:
    val = cfg.get(key, "").strip()
    if not val:
        print(f"В tg_check.env не заполнено: {key}")
        sys.exit(1)
    return val


_LOADING_HINTS = ("загрузка", "loading", "подожд", "обработ")


def _is_loading(text: str) -> bool:
    t = text.lower()
    return any(h in t for h in _LOADING_HINTS) and len(text) < 120


def _is_final(text: str) -> bool:
    t = text.lower()
    return any(
        k in t
        for k in (
            "не прош", "нарушена структура", "прошёл", "прошел",
            "проверк", "детали платежа", "pdf не", "✅", "❌", "⛔",
        )
    )


async def _wait_bot_reply(client, bot: str, after_id: int, timeout: float) -> List[str]:
    """Ждём ответ Fraudex: бот часто редактирует «Загрузка…» в финальный вердикт."""
    deadline = time.monotonic() + timeout
    msg_map: Dict[int, str] = {}
    last_new_at = 0.0
    extended = False

    while time.monotonic() < deadline:
        got_new = False
        async for msg in client.iter_messages(bot, limit=15):
            if msg.id <= after_id or not msg.text:
                continue
            text = msg.text.strip()
            if msg.id not in msg_map:
                got_new = True
            msg_map[msg.id] = text
            last_new_at = time.monotonic()

        texts = [msg_map[k] for k in sorted(msg_map.keys())]

        if texts and _is_final(texts[-1]):
            break

        if texts and _is_loading(texts[-1]):
            if not extended:
                deadline += min(30.0, timeout * 0.5)
                extended = True
        elif texts and not _is_loading(texts[-1]):
            if not got_new and (time.monotonic() - last_new_at) >= 4:
                break

        await asyncio.sleep(1)

    return [msg_map[k] for k in sorted(msg_map.keys())]


async def check_one_pdf(pdf_path: Path, cfg: Dict[str, str]) -> str:
    try:
        from tg_client import ensure_login, make_client
    except ImportError:
        print("Установи telethon: pip install telethon")
        sys.exit(1)

    bot = _need(cfg, "TG_CHECKER_BOT")
    if not bot.startswith("@"):
        bot = "@" + bot
    timeout = float(cfg.get("TG_WAIT_SECONDS", "45"))

    if not pdf_path.is_file():
        return f"FAIL: файл не найден: {pdf_path}"

    client = make_client(cfg)
    try:
        await ensure_login(client, cfg)
        last_id = 0
        async for msg in client.iter_messages(bot, limit=1):
            last_id = msg.id

        await client.send_file(bot, str(pdf_path))
        replies = await _wait_bot_reply(client, bot, last_id, timeout)
    finally:
        await client.disconnect()

    if not replies:
        return "FAIL: бот не ответил за отведённое время (увеличь TG_WAIT_SECONDS)"

    body = "\n---\n".join(replies)
    if any(x in body.lower() for x in ("не прошёл", "не прошел", "нарушена структура", "⛔")):
        return "FAIL\n" + body
    if any(x in body.lower() for x in ("прошёл проверку", "прошел проверку", "✅")):
        return "PASS\n" + body
    return "UNKNOWN\n" + body


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Проверка PDF через Telegram-бот чекер")
    parser.add_argument("pdf", help="Путь к PDF")
    args = parser.parse_args()

    cfg = _load_env()
    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = (_ROOT / pdf_path).resolve()

    result = asyncio.run(check_one_pdf(pdf_path, cfg))
    print(result)


if __name__ == "__main__":
    main()
