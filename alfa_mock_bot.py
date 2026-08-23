"""
Alfa-Bank mock bot — тот же сценарий, что T-Bank mock:

1) Одним сообщением — ФИО, баланс, траты, доходы, до 3 операций.
2) PDF 1–3 (первый = самая свежая операция).
3) /done — собрать userscript.

Токен: ALFA_MOCK_BOT_TOKEN в .env (отдельный бот, не receipt-bot).
Доступ: ADMIN_USERNAMES из config_bot + ALFA_MOCK_ALLOWED_USERNAMES.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tools.alfa_mock.assemble import USERSCRIPT_VERSION, assemble, filename_for
from tools.alfa_mock.parse import EXAMPLE, ParseError, confirm_text, parse_payload

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("alfa-mock-bot")

START_TEXT = """Доступ открыт.

Alfa-Bank mock bot

1) Одним сообщением — ФИО, баланс, траты, доходы, до 3 операций.
   Имя в шапке: строка «Имя: …» или 2-е слово в «ФИО: Фамилия Имя …».
   Имя в операции (Оп1) — только для чека, на профиль не влияет.
2) Бот попросит PDF — пришли 1–3 файла (первый = самая свежая операция).
3) /done — собрать файл.

/example — скопировать готовый шаблон

В операции: Банк + Тел (чек). Время: 18.08.2026 22:57 (можно только 22:57 = сегодня).
Профиль: Почта + Телефон.
Кабинет: web.alfabank.ru (Userscripts → замени файл → перезапусти Safari).

Команды: /cancel — сброс, /done — собрать после PDF"""


def _usernames() -> set[str]:
    names = set()
    try:
        from config_bot import ADMIN_USERNAMES

        names |= {str(x).lstrip("@").lower() for x in (ADMIN_USERNAMES or set())}
    except Exception:
        pass
    extra = os.getenv("ALFA_MOCK_ALLOWED_USERNAMES", "")
    names |= {x.strip().lstrip("@").lower() for x in extra.split(",") if x.strip()}
    return names


def _allowed_ids() -> set[int]:
    ids: set[int] = set()
    try:
        from config_bot import ADMIN_IDS

        ids |= {int(x) for x in (ADMIN_IDS or []) if x}
    except Exception:
        pass
    extra = os.getenv("ALFA_MOCK_ALLOWED_IDS", "")
    for part in extra.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if user.id in _allowed_ids():
        return True
    uname = (user.username or "").lstrip("@").lower()
    return bool(uname and uname in _usernames())


def session(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("alfa_mock", {})


async def deny_if_needed(update: Update) -> bool:
    if is_allowed(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("Нет доступа.")
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    session(context).clear()
    await update.effective_message.reply_text(START_TEXT)
    await update.effective_message.reply_text(EXAMPLE)


async def cmd_example(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    await update.effective_message.reply_text(EXAMPLE)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    session(context).clear()
    await update.effective_message.reply_text("Сброс. Пришли данные заново или /example.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    st = session(context)
    data = st.get("payload")
    if not data:
        await update.effective_message.reply_text("Сначала пришли данные. /example — шаблон.")
        return
    await _ship(update, data, st.get("pdfs") or [])
    st.clear()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    try:
        data = parse_payload(text)
    except ParseError as exc:
        await update.effective_message.reply_text(f"{exc}\n\n/start — формат")
        return
    st = session(context)
    st.clear()
    st["payload"] = data
    st["pdfs"] = []
    await update.effective_message.reply_text(confirm_text(data))


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    st = session(context)
    data = st.get("payload")
    if not data:
        await update.effective_message.reply_text("Сначала пришли данные одним сообщением. /example")
        return
    doc = update.effective_message.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if "pdf" not in mime and not name.endswith(".pdf"):
        await update.effective_message.reply_text("Нужен PDF.")
        return
    pdfs: list[bytes] = st.setdefault("pdfs", [])
    need = min(len(data.get("operations") or []), 3)
    if len(pdfs) >= max(need, 1):
        await update.effective_message.reply_text("Уже достаточно PDF. /done — собрать.")
        return
    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    pdfs.append(raw)
    left = max(need, 1) - len(pdfs)
    if left > 0:
        await update.effective_message.reply_text(
            f"PDF {len(pdfs)}/{max(need, 1)}. Ещё {left} или /done."
        )
        return
    await _ship(update, data, pdfs)
    st.clear()


async def _ship(update: Update, data: dict, pdfs: list[bytes]) -> None:
    js = assemble(data, pdfs)
    fname = filename_for(data)
    tmp = Path(os.getenv("TEMP") or "/tmp") / fname
    tmp.write_text(js, encoding="utf-8")
    size_kb = max(1, round(tmp.stat().st_size / 1024))
    caption = (
        f"Готово: userscript v{USERSCRIPT_VERSION} ({size_kb} KB).\n"
        f"Имя в шапке: {data['firstName']}\n"
        f"Почта: {data['email']}\n"
        f"Телефон: {data['profilePhone']}\n"
        f"Баланс: {data['balance']}; операций: {len(data.get('operations') or [])}\n"
        "Userscripts → замени файл → перезапусти Safari.\n"
        "Кабинет: https://web.alfabank.ru/"
    )
    try:
        with tmp.open("rb") as fh:
            await update.effective_message.reply_document(
                document=fh,
                filename=fname,
                caption=caption,
            )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    token = (os.getenv("ALFA_MOCK_BOT_TOKEN") or "").strip()
    if not token:
        print("⚠️ Укажите ALFA_MOCK_BOT_TOKEN в .env")
        return
    proxy = (
        os.getenv("TELEGRAM_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or ""
    ).strip()
    builder = (
        Application.builder()
        .token(token)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(60)
    )
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
        print(f"🌐 Telegram proxy: {proxy}")
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("example", cmd_example))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Alfa mock bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
