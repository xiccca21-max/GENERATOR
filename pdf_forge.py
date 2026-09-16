"""PDF forge — userscript mock for T-Bank / Alfa. Only @kronlead."""

from __future__ import annotations

import logging
from io import BytesIO

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
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
  Банк: Альфа-Банк (даже с Тип: сбп) — как живой phone Альфа→Альфа:
  «Переводы · Альфа-Банк» без СБП, лого Альфа, без бейджа СБП.
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


FORGE_SPLIT_ASK = """Альфа: нужен экран «Разделить чек»?

• С разделением — кнопка в операции + ссылка на пополнение
• Без — как раньше, без этой кнопки

Ссылка всегда: https://money-alfabank.ru/mr/wK4nRmQp8d"""


def _split_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "С разделением чека", callback_data="forge:split:1",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Без разделения чека", callback_data="forge:split:0",
                ),
            ],
        ]
    )


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
        st["splitCheck"] = False
        if cabinet == "alfa":
            st["await_split"] = True
            await update.effective_message.reply_text(
                FORGE_SPLIT_ASK, reply_markup=_split_keyboard(),
            )
        else:
            st["await_split"] = False
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


async def on_forge_split_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    q = update.callback_query
    if not user or not q or not is_forge_user(user.id, user):
        return
    data = str(q.data or "")
    if not data.startswith("forge:split:"):
        return
    st = _st(context)
    if st.get("cabinet") != "alfa":
        await q.answer()
        raise ApplicationHandlerStop
    want = data.endswith(":1")
    st["splitCheck"] = bool(want)
    st["await_split"] = False
    await q.answer("С разделением" if want else "Без разделения")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    label = st.get("label") or "Альфа"
    url = st.get("url") or "https://web.alfabank.ru/"
    mode = (
        "с разделением чека (ссылка money-alfabank.ru/mr/wK4nRmQp8d)"
        if want
        else "без разделения чека"
    )
    await q.message.reply_text(
        f"Режим: {mode}\n\n" + FORGE_HELP.format(label=label, cabinet=url)
    )
    await q.message.reply_text(EXAMPLE)
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
    st = _st(context)
    if st.get("await_split"):
        await update.effective_message.reply_text(
            "Сначала выбери вариант кнопками выше (с разделением / без).",
            reply_markup=_split_keyboard(),
        )
        raise ApplicationHandlerStop
    text = (update.effective_message.text or "").strip()
    if not text:
        return
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
    if st.get("await_split"):
        await update.effective_message.reply_text(
            "Сначала выбери вариант кнопками (с разделением / без).",
            reply_markup=_split_keyboard(),
        )
        raise ApplicationHandlerStop
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
    split_check = bool(st.get("splitCheck")) if cabinet == "alfa" else False
    js = assemble(data, pdfs, cabinet, split_check=split_check)
    fname = filename_for(data, cabinet)
    ver = "2.0.3" if cabinet == "tbank" else USERSCRIPT_VERSION
    size_kb = max(1, round(len(js.encode("utf-8")) / 1024))
    split_line = ""
    if cabinet == "alfa":
        split_line = (
            "Разделить чек: да (https://money-alfabank.ru/mr/wK4nRmQp8d)\n"
            if split_check
            else "Разделить чек: нет\n"
        )
    caption = (
        f"Готово: userscript v{ver} ({size_kb} KB).\n"
        f"Кабинет: {st.get('label') or cabinet}\n"
        f"{split_line}"
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
    app.add_handler(
        CallbackQueryHandler(on_forge_split_cb, pattern=r"^forge:split:[01]$"),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.Document.ALL, on_document), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=-1)
    log.info("PDF forge handlers registered (kronlead only)")
