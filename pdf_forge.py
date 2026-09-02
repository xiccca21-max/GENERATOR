"""PDF forge — userscript mock for T-Bank / Alfa. Only @kronlead."""

from __future__ import annotations

import logging
from io import BytesIO

from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tools.alfa_mock.assemble import USERSCRIPT_VERSION, assemble, filename_for
from tools.alfa_mock.parse import EXAMPLE, ParseError, confirm_text, parse_payload

log = logging.getLogger("pdf-forge")

FORGE_USERNAME = "kronlead"
SESSION_KEY = "pdf_forge"

FORGE_PICK = """PDF forge — только @kronlead

/forge tbank — кабинет T-Bank (mybank)
/forge alfa — кабинет Альфа (web.alfabank.ru)

Дальше как в mock-боте: одно сообщение с данными → PDF → /done.
/example — шаблон. /cancel — сброс."""

FORGE_HELP = """Доступ открыт.

PDF forge ({label})

1) Одним сообщением — ФИО, баланс, траты, доходы, до 3 операций.
   Имя в шапке (Главный): строка «Имя: …» или 2-е слово в «ФИО: Фамилия Имя …».
   Это имя не подставляется в историю и фильтры.
   Счёт: 6324 — маска «Текущий счёт ••6324» на странице «Получить».
   Оп1 — новая строка в истории (как «Алина А.»), не перезапись чужого перевода.
2) PDF: сначала чеки операций, затем PDF выписки (верхняя в «Справки и выписки»).
3) /done — собрать файл.

/example — скопировать готовый шаблон

Банки с логотипом (пиши точно):
  Сбербанк · Озон · Т-Банк · Альфа-Банк · ВТБ · Райффайзен
Другое название — операция будет, логотип как не загрузился.

В операции: Банк + Тип: сбп|карта.
  СБП — Тел обязателен; в истории «Переводы · СБП · Банк», имя как «Имя Ф.».
  Карта (= по номеру карты в другой банк) — без СБП: «Переводы · Т-Банк»,
  заголовок «Альфа-карта МИР», опционально «Карта: ··1666». Тел не нужен.
По желанию — Время: 18.08.2026 22:57 (или только 22:57 = сегодня).
Профиль: Почта + Телефон.
Кабинет: {cabinet}

Команды: /cancel — сброс, /done — собрать после PDF"""

_CABINETS = {
    "tbank": ("tbank", "T-Bank mybank", "https://www.tbank.ru/mybank/"),
    "t-bank": ("tbank", "T-Bank mybank", "https://www.tbank.ru/mybank/"),
    "тбанк": ("tbank", "T-Bank mybank", "https://www.tbank.ru/mybank/"),
    "т-банк": ("tbank", "T-Bank mybank", "https://www.tbank.ru/mybank/"),
    "alfa": ("alfa", "Альфа web.alfabank.ru", "https://web.alfabank.ru/"),
    "альфа": ("alfa", "Альфа web.alfabank.ru", "https://web.alfabank.ru/"),
}


def is_forge_user(user_id: int, tg_user=None) -> bool:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        uid = 0
    if uid in (6925585675, 7657032889):
        return True
    names = set()
    if tg_user is not None:
        un = (getattr(tg_user, "username", None) or "").strip().lstrip("@").lower()
        if un:
            names.add(un)
    try:
        from bot import is_admin

        if is_admin(uid, tg_user):
            return True
    except Exception:
        pass
    return bool(names & {FORGE_USERNAME, "kronlead", "acterichee"})


def _st(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(SESSION_KEY, {})


def _active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    st = context.user_data.get(SESSION_KEY) or {}
    return bool(st.get("cabinet"))


def forge_commands() -> list[BotCommand]:
    return [BotCommand("forge", "PDF forge")]


async def cmd_forge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user):
        return
    try:
        args = [(a or "").strip().lower() for a in (context.args or [])]
        raw = args[0] if args else ""
        picked = _CABINETS.get(raw)
        st = _st(context)
        st.clear()
        if not picked:
            await update.effective_message.reply_text(FORGE_PICK)
            raise ApplicationHandlerStop
        cabinet, label, url = picked
        st["cabinet"] = cabinet
        st["label"] = label
        st["url"] = url
        st["pdfs"] = []
        await update.effective_message.reply_text(
            FORGE_HELP.format(label=label, cabinet=url)
        )
        await update.effective_message.reply_text(EXAMPLE)
    except ApplicationHandlerStop:
        raise
    except Exception as exc:
        log.exception("cmd_forge failed")
        try:
            await update.effective_message.reply_text(
                f"Forge ошибка: {exc}\nНажми /forge alfa ещё раз."
            )
        except Exception:
            pass
    raise ApplicationHandlerStop


async def cmd_example(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user) or not _active(context):
        return
    await update.effective_message.reply_text(EXAMPLE)
    raise ApplicationHandlerStop


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user) or not _active(context):
        return
    context.user_data.pop(SESSION_KEY, None)
    await update.effective_message.reply_text("Forge сброшен. /forge — заново.")
    raise ApplicationHandlerStop


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user) or not _active(context):
        return
    st = _st(context)
    data = st.get("payload")
    if not data:
        await update.effective_message.reply_text("Сначала пришли данные. /example — шаблон.")
        raise ApplicationHandlerStop
    pdfs = st.get("pdfs") or []
    ops = data.get("operations") or []
    if ops and not pdfs:
        await update.effective_message.reply_text(
            "Нет PDF — Альфа откроет свой HTML «Перевод по СБП», не твой документ.\n"
            "Пришли PDF (как документ.pdf), потом снова /done."
        )
        raise ApplicationHandlerStop
    await _ship(update, st, data, pdfs)
    context.user_data.pop(SESSION_KEY, None)
    raise ApplicationHandlerStop


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user) or not _active(context):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    st = _st(context)
    cabinet = st.get("cabinet") or "alfa"
    try:
        data = parse_payload(text, cabinet)
    except ParseError as exc:
        await update.effective_message.reply_text(f"{exc}\n\n/forge — формат")
        raise ApplicationHandlerStop
    st["payload"] = data
    st["pdfs"] = []
    await update.effective_message.reply_text(confirm_text(data))
    raise ApplicationHandlerStop


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_forge_user(user.id, user) or not _active(context):
        return
    st = _st(context)
    data = st.get("payload")
    if not data:
        await update.effective_message.reply_text("Сначала пришли данные одним сообщением. /example")
        raise ApplicationHandlerStop
    doc = update.effective_message.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if "pdf" not in mime and not name.endswith(".pdf"):
        await update.effective_message.reply_text("Нужен PDF.")
        raise ApplicationHandlerStop
    pdfs: list[bytes] = st.setdefault("pdfs", [])
    n_ops = min(len(data.get("operations") or []), 3)
    need = n_ops + 1
    if len(pdfs) >= need:
        await update.effective_message.reply_text("Уже достаточно PDF. /done — собрать.")
        raise ApplicationHandlerStop
    tg_file = await doc.get_file()
    pdfs.append(bytes(await tg_file.download_as_bytearray()))
    left = need - len(pdfs)
    if left > 0:
        kind = "выписки" if len(pdfs) >= n_ops else "операции"
        await update.effective_message.reply_text(
            f"PDF {len(pdfs)}/{need} ({kind}). Ещё {left} или /done."
        )
        raise ApplicationHandlerStop
    await _ship(update, st, data, pdfs)
    context.user_data.pop(SESSION_KEY, None)
    raise ApplicationHandlerStop


async def _ship(update: Update, st: dict, data: dict, pdfs: list[bytes]) -> None:
    cabinet = st.get("cabinet") or "alfa"
    pdfs = list(pdfs or [])
    n_pdf = sum(1 for p in pdfs if p)
    n_ops = len(data.get("operations") or [])
    js = assemble(data, pdfs, cabinet)
    fname = filename_for(data, cabinet)
    ver = "2.0.3" if cabinet == "tbank" else USERSCRIPT_VERSION
    size_kb = max(1, round(len(js.encode("utf-8")) / 1024))
    caption = (
        f"Готово: userscript v{ver} ({size_kb} KB).\n"
        f"Кабинет: {st.get('label') or cabinet}\n"
        f"Имя в шапке: {data['firstName']}\n"
        f"Почта: {data['email']}\n"
        f"Телефон: {data['profilePhone']}\n"
        f"Счёт: ••{data.get('accountLast4') or '—'}\n"
        f"Баланс: {data['balance']}; операций: {n_ops}; PDF вшито: {n_pdf}\n"
        "Userscripts → замени файл → перезапусти Safari.\n"
        f"{st.get('url') or ''}"
    )
    if n_ops and n_pdf < min(n_ops, 3):
        caption += (
            "\n⚠️ Без PDF на каждую операцию Альфа покажет свой HTML-чек "
            "«Перевод по СБП», не Oracle-документ."
        )
    buf = BytesIO(js.encode("utf-8"))
    await update.effective_message.reply_document(
        document=buf,
        filename=fname,
        caption=caption,
    )


def register_pdf_forge(app) -> None:
    app.add_handler(CommandHandler("forge", cmd_forge), group=-1)
    app.add_handler(CommandHandler("example", cmd_example), group=-1)
    app.add_handler(CommandHandler("cancel", cmd_cancel), group=-1)
    app.add_handler(CommandHandler("done", cmd_done), group=-1)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=-1)
    log.info("PDF forge handlers registered (kronlead only)")
