"""
Telegram бот UMBRA SYNDICATE
Версия 6.0 - Система валюты и пакетов
"""
import os
import json
import logging
import re
import random
import secrets
import sys
import asyncio
import threading
import html as html_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time_msk import now_msk
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ApplicationHandlerStop,
    filters,
    ContextTypes
)
from telegram import BotCommand, Message, Bot
try:
    from telegram import MenuButtonCommands
except ImportError:
    MenuButtonCommands = None  # type: ignore
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
import urllib.request  # noqa: F401 - used via cryptopay

from pdf_forge import forge_commands, register_pdf_forge

try:
    import cryptopay
except ImportError:
    cryptopay = None  # type: ignore


# ---------- Жирный текст: только заголовки и суммы (кнопки без изменений) ----------

_HTML_READY_PREFIX = "\x01HTML\x01"

# Суммы: 960 $, 20 USDT, 8$
_AMOUNT_RE = re.compile(
    r"(?<![\w<])(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(\s*)(\$|USDT|USD|₽)",
    re.IGNORECASE,
)

# Строки-заголовки (эмодзи / типичные шапки)
_HEADING_RE = re.compile(
    r"^(?:"
    r"👤|📦|💰|🟢|🟡|🔴|🟣|🏛|🔵|🛠|🔐|✅|❌|🧾|⚠️|🎉|🤝|🔗|📝|📋|💵|💸|🆕|⚫|💳|📄|📲|📱|📌|⚡️|⏳|💼|"
    r"Мой профиль|Приобрести|Пополнение|Выберите|Админ|Счёт|Оплата|Куплен|"
    r"Недостаточно|Доступ|Для доступа|Главн|Заявка|Собираю"
    r")"
)


def _strip_md(s: str) -> str:
    for ch in "*_`":
        s = s.replace(ch, "")
    return s.replace("\\_", "_")


def _bold_amounts_escaped(escaped_line: str) -> str:
    return _AMOUNT_RE.sub(r"<b>\1\2\3</b>", escaped_line)


def _is_heading_line(line: str, *, is_first: bool) -> bool:
    t = line.strip()
    if not t or len(t) > 100:
        return False
    if ":" in t:
        _left, right = t.split(":", 1)
        # «Баланс: 960 $» - не заголовок, жирной будет сумма
        if right.strip() and re.search(r"\d", right):
            return False
        # «📝 Отправьте данные:» - заголовок
        if not right.strip():
            return True
    if is_first:
        return True
    return bool(_HEADING_RE.match(t))


def to_bold_html(text) -> str:
    """HTML: жирные только заголовки и суммы. Остальное обычным шрифтом."""
    if text is None:
        return text
    s = str(text)
    if not s:
        return s
    if s.startswith(_HTML_READY_PREFIX):
        return s[len(_HTML_READY_PREFIX):]
    # Готовый code/pre - не трогать (копируемые примеры)
    stripped = s.strip()
    if ("<pre>" in s and "</pre>" in s) or (
        stripped.startswith("<code>") and "</code>" in s
    ):
        return s
    s = _strip_md(s)
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    lines = s.split("\n")
    out = []
    seen_content = False
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        is_first = not seen_content
        seen_content = True
        esc = html_lib.escape(line)
        if _is_heading_line(line, is_first=is_first):
            out.append(f"<b>{esc}</b>")
        else:
            out.append(_bold_amounts_escaped(esc))
    return "\n".join(out)


def as_html(html: str) -> str:
    """Пометить строку как готовый HTML (обход автоформатирования)."""
    return _HTML_READY_PREFIX + html


_FORM_HINTS: dict[int, dict] = {}


async def send_data_entry_prompt(
    update: Update,
    title: str,
    fields: str,
    example: str,
    notes: str | None = None,
    *,
    reply_markup=None,
) -> None:
    """
    Подсказка + пример отдельным сообщением в <pre>
    (весь блок копируется целиком, как ссылка в <code>).
    """
    title = _strip_md(title or "").strip().replace("\u2014", "-").replace("\u2013", "-")
    fields = _strip_md(fields or "").strip().replace("\u2014", "-").replace("\u2013", "-")
    example = (example or "").strip("\n").replace("\u2014", "-").replace("\u2013", "-")
    notes = _strip_md(notes).strip().replace("\u2014", "-").replace("\u2013", "-") if notes else ""
    parts = [
        f"<b>{html_lib.escape(title)}</b>",
        "",
        f"<b>{html_lib.escape('📝 Отправьте данные:')}</b>",
        "",
        html_lib.escape(fields),
        "",
        f"<b>{html_lib.escape('📋 пример — ниже')}</b>",
        "",
        "Кнопка «📋 Пример» — образец.",
    ]
    if notes:
        n_lines = notes.split("\n")
        parts.append("")
        parts.append(f"<b>{html_lib.escape(n_lines[0])}</b>")
        if len(n_lines) > 1:
            parts.append(html_lib.escape("\n".join(n_lines[1:])))
    uid = update.effective_user.id if update.effective_user else 0
    if uid:
        _FORM_HINTS[uid] = {"title": title, "fields": fields, "example": example}
    msg = update.effective_message
    await msg.reply_text(as_html("\n".join(parts)), reply_markup=form_keyboard())
    # <pre> = весь многострочный пример одним блоком (у <code> в Telegram
    # часто жирным/моно только первая строка)
    await msg.reply_text(as_html(f"<pre>{html_lib.escape(example)}</pre>"))


_BOLD_OUTGOING_INSTALLED = False


def install_bold_outgoing() -> None:
    """Патч исходящих сообщений - заголовки и суммы жирным. Кнопки без изменений."""
    global _BOLD_OUTGOING_INSTALLED
    if _BOLD_OUTGOING_INSTALLED:
        return
    _BOLD_OUTGOING_INSTALLED = True

    _bot_send = Bot.send_message

    async def _bold_send_message(self, chat_id, text, *args, parse_mode=None, **kwargs):
        if text is not None:
            text = to_bold_html(text)
            parse_mode = "HTML"
        return await _bot_send(self, chat_id, text, *args, parse_mode=parse_mode, **kwargs)

    Bot.send_message = _bold_send_message  # type: ignore[method-assign]

    _bot_doc = Bot.send_document

    async def _bold_send_document(self, chat_id, document, *args, caption=None, parse_mode=None, **kwargs):
        if caption is not None:
            caption = to_bold_html(caption)
            parse_mode = "HTML"
        return await _bot_doc(
            self, chat_id, document, *args, caption=caption, parse_mode=parse_mode, **kwargs
        )

    Bot.send_document = _bold_send_document  # type: ignore[method-assign]

    _bot_edit = Bot.edit_message_text

    async def _bold_edit_message_text(self, text=None, *args, parse_mode=None, **kwargs):
        if text is None:
            text = kwargs.pop("text", None)
        if text is not None:
            text = to_bold_html(text)
            parse_mode = "HTML"
        return await _bot_edit(self, text, *args, parse_mode=parse_mode, **kwargs)

    Bot.edit_message_text = _bold_edit_message_text  # type: ignore[method-assign]
    # Кнопки под чатом - обычный шрифт.

# На случай старых «жирных» цифр/латиницы на кнопках у клиента.
_BOLD_DIGIT = {chr(0x1D7EC + i): chr(0x30 + i) for i in range(10)}
_BOLD_LATIN = {
    **{chr(0x1D5D4 + i): chr(0x41 + i) for i in range(26)},
    **{chr(0x1D5EE + i): chr(0x61 + i) for i in range(26)},
}


def normalize_btn_text(text: str) -> str:
    if not text:
        return text
    return "".join(_BOLD_DIGIT.get(c, _BOLD_LATIN.get(c, c)) for c in text)

# Импортируем генераторы чеков
try:
    from vtb_stealth_v3 import create_vtb_stealth
    from template_manager import find_template_for_data, get_missing_chars_for_data
    VTB_AVAILABLE = True
except ImportError:
    VTB_AVAILABLE = False

    def create_vtb_stealth(*_a, **_k):
        return None

    def find_template_for_data(*_a, **_k):
        return None

    def get_missing_chars_for_data(*_a, **_k):
        return []

# OnlyPDF-verified banks shown in UI (see tools/ONLYPDF_METHODS.md).
# VTB / OTP / Ozon stay in code but are hidden until gated 30/30.
LIVE_BANKS = frozenset({"alfa", "sber"})
# Public: banks in LIVE_BANKS. Owners also get T-Bank and Uralsib.
ADMIN_LIVE_BANKS = frozenset({"alfa", "sber", "tbank", "uralsib"})

try:
    from sber_stealth_v3 import create_sber_stealth, check_text as sber_check_text, MISSING_UPPER as SBER_MISSING_UPPER, MISSING_LOWER as SBER_MISSING_LOWER
    SBER_AVAILABLE = True
except ImportError:
    SBER_AVAILABLE = False
    def sber_check_text(text): return []
    SBER_MISSING_UPPER = ""
    SBER_MISSING_LOWER = ""

try:
    from sber_sbp_stealth import create_sber_sbp_stealth
    SBER_SBP_AVAILABLE = True
except ImportError:
    SBER_SBP_AVAILABLE = False
    def create_sber_sbp_stealth(*_a, **_k):
        return None

try:
    from sber_phone_stealth import create_sber_phone_stealth
    SBER_PHONE_AVAILABLE = True
except ImportError:
    SBER_PHONE_AVAILABLE = False
    def create_sber_phone_stealth(*_a, **_k):
        return None

try:
    from uralsib_sbp_stealth import create_uralsib_sbp_stealth
    URALSIB_SBP_AVAILABLE = True
except ImportError:
    URALSIB_SBP_AVAILABLE = False
    def create_uralsib_sbp_stealth(*_a, **_k):
        return None

try:
    from sber_card_stealth import create_sber_card_stealth
    SBER_CARD_AVAILABLE = True
except ImportError:
    SBER_CARD_AVAILABLE = False
    def create_sber_card_stealth(*_a, **_k):
        return None

# Альфа-Банк
try:
    from alfa_sbp_stealth import (
        create_alfa_sbp_stealth,
        check_text as alfa_check_text,
        alfa_sbp_reject_reason,
    )
    ALFA_SBP_AVAILABLE = True
except ImportError:
    ALFA_SBP_AVAILABLE = False
    def create_alfa_sbp_stealth(*_a, **_k):
        return None
    def alfa_check_text(text): return []
    def alfa_sbp_reject_reason(*_a, **_k):
        return None

try:
    from alfa_card_stealth import create_alfa_card_stealth
    ALFA_CARD_AVAILABLE = True
except ImportError:
    ALFA_CARD_AVAILABLE = False
    def create_alfa_card_stealth(*_a, **_k):
        return None

try:
    from alfa_phone_stealth import create_alfa_phone_stealth, check_text as alfa_phone_check_text
    ALFA_PHONE_AVAILABLE = True
except ImportError:
    ALFA_PHONE_AVAILABLE = False
    def create_alfa_phone_stealth(*_a, **_k):
        return None
    def alfa_phone_check_text(text): return []

try:
    from alfa_statement_stealth import (
        create_alfa_statement_stealth,
        check_text as alfa_statement_check_text,
    )
    ALFA_STATEMENT_AVAILABLE = True
except ImportError:
    ALFA_STATEMENT_AVAILABLE = False
    def create_alfa_statement_stealth(*_a, **_k):
        return None
    def alfa_statement_check_text(text): return []

ALFA_AVAILABLE = (
    ALFA_SBP_AVAILABLE or ALFA_CARD_AVAILABLE
    or ALFA_PHONE_AVAILABLE or ALFA_STATEMENT_AVAILABLE
)
ALFA_CHARS = set()  # legacy; проверка через alfa_check_text

# Озон Банк
try:
    from ozon_stealth_v3 import create_ozon_stealth, check_text as ozon_check_text
    from template_manager_ozon import get_missing_chars_for_data as ozon_get_missing
    OZON_AVAILABLE = True
except ImportError:
    OZON_AVAILABLE = False
    def create_ozon_stealth(*_a, **_k):
        return None
    def ozon_check_text(text, cid=None): return []
    def ozon_get_missing(data): return []

# Т-Банк
try:
    from tbank_stealth_v3 import create_tbank_stealth, check_text as tbank_check_text
    TBANK_AVAILABLE = True
except ImportError:
    TBANK_AVAILABLE = False
    def create_tbank_stealth(*_a, **_k):
        return None
    def tbank_check_text(text): return []

# Т-Банк СБП
try:
    from tbank_sbp_stealth import (
        create_tbank_sbp_stealth,
        decode_sbp_operation_id,
        _parse_tbank_sbp_rest,
        _is_date_now_token,
    )
    TBANK_SBP_AVAILABLE = True
except ImportError:
    TBANK_SBP_AVAILABLE = False
    def create_tbank_sbp_stealth(*_a, **_k):
        return None
    def decode_sbp_operation_id(_): return None
    def _parse_tbank_sbp_rest(rest):
        return "Сбербанк", "сейчас", list(rest or [])
    def _is_date_now_token(text):
        return (text or "").strip().lower() in (
            "сейчас", "now", "-", "", "авто", "auto",
        )

# Т-Банк По номеру телефона
try:
    from tbank_phone_stealth import create_tbank_phone_stealth
    TBANK_PHONE_AVAILABLE = True
except ImportError:
    TBANK_PHONE_AVAILABLE = False
    def create_tbank_phone_stealth(*_a, **_k):
        return None

# Т-Банк Клиенту Т-Банка (карта внутри банка)
try:
    from tbank_card_tbank_stealth import create_tbank_card_tbank_stealth
    TBANK_CARD_TBANK_AVAILABLE = True
except ImportError:
    TBANK_CARD_TBANK_AVAILABLE = False
    def create_tbank_card_tbank_stealth(*_a, **_k):
        return None

# ОТП Банк СБП
try:
    from otp_sbp_stealth import create_otp_sbp_stealth, OtpStealthError
    OTP_SBP_AVAILABLE = True
except ImportError:
    OTP_SBP_AVAILABLE = False
    def create_otp_sbp_stealth(*_a, **_k):
        return None
    class OtpStealthError(Exception):
        pass

# ОТП Банк - карта в другой банк
try:
    from otp_card_stealth import create_otp_card_stealth
    OTP_CARD_AVAILABLE = True
except ImportError:
    OTP_CARD_AVAILABLE = False
    def create_otp_card_stealth(*_a, **_k):
        return None

# Ozon Банк СБП
try:
    from ozon_sbp_stealth import create_ozon_sbp_stealth
    OZON_SBP_AVAILABLE = True
except ImportError:
    OZON_SBP_AVAILABLE = False
    def create_ozon_sbp_stealth(*_a, **_k):
        return None

# Настройка логирования
logging.basicConfig(format='%(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния
MAIN_MENU, TOOLS_MENU, CHECKS_MENU, ENTERING_DATA, BALANCE_MENU, PACKAGES_MENU = range(6)
TBANK_SUBMENU = 6  # подтип перевода Т-Банка (СБП / карта в другой банк / карта в Т-Банк)
OTP_SUBMENU = 7    # подтип перевода ОТП Банка (СБП)
OZON_SUBMENU = 8   # подтип перевода Ozon Банка (СБП)
SBER_SUBMENU = 9   # подтип перевода Сбербанка (СБП / телефон / карта)
ALFA_SUBMENU = 10  # подтип перевода Альфа-Банка (СБП / карта)
WAITING_PIN = 11   # новый пользователь вводит PIN до доступа
WAITING_PROMO = 12
WAITING_TOPUP_AMOUNT = 13
SHOP_MENU = 14  # Витрина чеков / аренды
WAITING_SHOP_PAY = 15  # Выбор CryptoBot / TRC-20 после тарифа

# PIN только для новых пользователей (ещё нет записи в payments.json)
# PIN для входа
ACCESS_PIN = "155115"
# Старый PIN «только Альфа» больше не открывает бота.
ALFA_ONLY_PIN = ""
SCOPE_ALFA = "alfa"
SCOPE_FULL = "full"  # все банки, без админки, без оплаты (бета)
LOCK_NOTICE = (
    "🔒 Доступ к боту закрыт.\n\n"
    "По вопросам пишите @kronlead"
)

# ========== ЗАГРУЗКА КОНФИГА ==========
try:
    from config_bot import (
        BOT_TOKEN, ADMIN_IDS, CURRENCY_NAME, COINS_PER_USDT, MIN_DEPOSIT,
        TOOL_PRICES, PACKAGES, USDT_ADDRESS, PAYMENTS_FILE,
        TRIAL_ENABLED, TRIAL_USES,
        CHECK_PACKAGES, TOPUP_PRESETS_USDT, LARGE_DEPOSIT_USDT, CRYPTO_PAY_TOKEN,
        ADMIN_USERNAMES, SHOP_OFFERS, SHOP_PREVIEW_ADMINS_ONLY,
    )
except ImportError:
    BOT_TOKEN = ""
    ADMIN_IDS = []
    ADMIN_USERNAMES = {"acterichee", "kronlead"}
    CURRENCY_NAME = "💎"
    COINS_PER_USDT = 1
    MIN_DEPOSIT = 3
    TOOL_PRICES = {}
    PACKAGES = {}
    CHECK_PACKAGES = {
        "pack1": {"name": "1 чек", "checks": 1, "price_usdt": 5, "btn": "📦 1 чек - 5$"},
        "pack10": {"name": "10 чеков", "checks": 10, "price_usdt": 15, "btn": "📦 10 чеков - 15$"},
        "pack50": {"name": "50 чеков", "checks": 50, "price_usdt": 60, "btn": "📦 50 чеков - 60$"},
    }
    TOPUP_PRESETS_USDT = (5, 10, 25, 50)
    LARGE_DEPOSIT_USDT = 50
    CRYPTO_PAY_TOKEN = ""
    USDT_ADDRESS = ""
    PAYMENTS_FILE = "payments.json"
    TRIAL_ENABLED = False
    TRIAL_USES = {}
    SHOP_PREVIEW_ADMINS_ONLY = True
    SHOP_OFFERS = {}

if CRYPTO_PAY_TOKEN and not os.getenv("CRYPTO_PAY_TOKEN"):
    os.environ["CRYPTO_PAY_TOKEN"] = CRYPTO_PAY_TOKEN

# Кэш ID админов (по username / getChat)
_ADMIN_ID_CACHE: set[int] = set(int(x) for x in (ADMIN_IDS or []) if str(x).isdigit() or isinstance(x, int))
_KRONLEAD_ID: int | None = None
ADMIN_USERNAMES = {str(u).lower().lstrip("@") for u in (ADMIN_USERNAMES or {"acterichee", "kronlead"})}
NEWS_SKIP_USERNAMES = {"poahuevaly"}

# ========== ПРИМЕРЫ ДАННЫХ ==========
VTB_EXAMPLE = """55555
Иван Иванович И.
Петр Петрович П.
+7 (999) 123-45-67
Сбербанк
22.01.2026, 15:30"""

SBER_EXAMPLE = """55555
Иван Иванович И.
Петр Петрович П.
+7 999 123-45-67
Т-Банк
сейчас"""

SBER_SBP_EXAMPLE = """5500
Оюмаа Орлановна К.
Алексей Александрович П.
+7 911 826-78-06
Т-Банк
сейчас"""

SBER_PHONE_EXAMPLE = """10000
Дмитрий Евгеньевич С.
Татиана Игоревна Р.
+7 928 969-80-08
Сбербанк
сейчас"""

URALSIB_SBP_EXAMPLE = """5500
0
сейчас
авто
БЕЛОВ ИВАН ПЕТРОВИЧ
Алексей Сергеевич Б.
+7 (999) 123-45-67
Сбербанк"""

TBANK_EXAMPLE = """55000
Иван Иванов
Петр П.
220220******8333
Сбербанк
сейчас
Без комиссии
55000
авто"""

TBANK_NOCOMM_EXAMPLE = """15000
Иван Иванов
220220******8333
сейчас
авто"""

TBANK_CARD_TBANK_EXAMPLE = """23500
Никита Васильев
Андрей Т.
*6993
сейчас
авто"""

# Выписка Т-Банка - формат:
# ── шапка ──
#   1: основная дата (или 'сейчас')
#   2: ФИО полностью
#   3: адрес одной строкой (без переводов)
#   4: период_с (DD.MM.YYYY)
#   5: период_по (DD.MM.YYYY)
#   6: пополнения (сумма за период, число)
#   7: расходы   (сумма за период, число)
# ── операция 1 ──
#   8 : дата операции   (DD.MM.YYYY)
#   9 : время операции  (HH:MM)
#  10 : дата списания    (DD.MM.YYYY)   [можно поставить = операции]
#  11 : время списания   (HH:MM)
#  12 : сумма            (число, со знаком - для расходов)
#  13 : описание строка1
#  14 : описание строка2
#  15 : последние 4 цифры карты
# ── операция 2 (строки 16-23, тот же формат) ──
TBANK_STATEMENT_EXAMPLE = """сейчас
Иванов Иван Иванович
117303, г Москва, Севастопольский проспект, д. 28, кв. 12
01.05.2026
12.05.2026
12500
35780
05.05.2026
10:15
05.05.2026
14:30
-12500
Оплата в OZON.RU
MOSCOW RU
1234
07.05.2026
19:45
07.05.2026
20:01
-23280
Перевод по СБП
Сбербанк
1234"""

TBANK_SBP_EXAMPLE = """55000
Алексей Морозов
+7 (916) 123-45-67
Мария К.
ВТБ
сейчас
авто или номер
авто"""

TBANK_PHONE_EXAMPLE = """55000
Алексей Морозов
+7 (916) 123-45-67
Мария К.
сейчас
авто"""

OTP_SBP_EXAMPLE = """6500
Андрей Андреевич А.
45** ***678
+7 949 417-04-68
Надежда Игоревна П.
Сбербанк
044525225
B6127120848620170B10130011750703
12.05.2026
0
авто"""

# Примечание про защиту OTP SBP валидатором:
#   ❗ Дата: можно менять ТОЛЬКО число (день 1-31). Месяц/год/время в чеке
#      жёстко = шаблон (05.2026 20:15:01) - иначе валидатор бросит «подделка»
#      (поля закодированы в SBP_ID).
#   ❗ Банк: должен быть из списка: Сбербанк, Газпромбанк, Банк ВТБ,
#      Райффайзенбанк, Яндекс, Т-Банк. БИК подставляется автоматически.
#   ❗ SBP_ID авто-синхронизируется с днём (pos 4-5 = day+20).
OTP_SBP_NOTE = (
    "ℹ️ <b>Особенности валидации ОТП Банка (СБП):</b>\n"
    "• <b>Дата:</b> можно менять только <b>число</b> (1-31). Месяц/год/время в чеке "
    "форсятся = шаблон (<code>05.2026 20:15:01</code>) - иначе «подделка».\n"
    "• <b>Банк</b> - только из списка: <code>Сбербанк</code> / <code>Газпромбанк</code> / "
    "<code>Банк ВТБ</code> / <code>Райффайзенбанк</code> / <code>Яндекс</code> / <code>Т-Банк</code>. "
    "БИК подставляется автоматически (поле БИК в примере можно оставить пустым).\n"
    "• <b>SBP_ID</b> - авто-синхронизируется с днём (поле в примере можно оставить как есть).\n"
    "• Остальные поля (имена, телефон, паспорт, сумма, № квитанции) меняются свободно."
)

OTP_CARD_EXAMPLE = """2670
АНТОН ИГОРЕВИЧ Ш******
5*** *****1
2204310306568331
03.05.2026 16:33:49
150
авто"""

OZON_SBP_EXAMPLE = """10000
Владимир Иванович Н.
+7 (983) 625-36-76
Евгений Константинович Т.
Сбербанк
05.05.2026 23:40
Без комиссии
авто"""

ALFA_SBP_EXAMPLE = """10755
Диана Камильевна П
79163407825
Т-Банк
сейчас
авто
авто
авто
Перевод"""

ALFA_CARD_EXAMPLE = """100
2200158946123456
4377721666123456
сейчас
авто"""

ALFA_PHONE_EXAMPLE = """12500
ВОЛКОВ Д. В.
79131234511
сейчас
40817810123456780922
авто
авто"""

ALFA_STATEMENT_EXAMPLE = """40817810404219876543
16.08.2026
Смирнова Анна Петровна
авто
15.08.2026
15.08.2026
10000
18000
8000
10000
8000
15.08.2026
авто
Перевод через Систему быстрых платежей. Без НДС."""

ALFA_EXAMPLE = ALFA_SBP_EXAMPLE

# ========== ТЕКСТЫ КНОПОК ==========
BTN_SELECT_BANK = "🏦 Выбрать банк"
BTN_SELECT_BANK_OLD = "Банк"
BTN_PROFILE = "💼 Мой профиль"
BTN_PROFILE_OLD = "Профиль"
BTN_BALANCE_CHECKS = "🏪 Магазин"
BTN_BALANCE_CHECKS_OLD = "💵 Баланс и проверки"
BTN_PACKAGES = "📦 Приобрести пакеты"
BTN_BALANCE = "💰 Пополнить"
BTN_SUPPORT = "💀 Поддержка"
BTN_SUPPORT_OLD = "💬 Поддержка"
BTN_CLEAR = "🗑 Очистить следы"
BTN_HELP = "📖 Инфо"
BTN_PROMO = "🎟 Промокод"
BTN_TOPUP_CUSTOM = "💵 Ввести сумму USDT"
BTN_CRYPTOBOT = "🤖 CryptoBot"
BTN_CRYPTOBOT_OLD = "CryptoBot"
BTN_USDT_TRC = "💵 USDT TRC-20"
BTN_USDT_TRC_OLD = "TRC-20"
BTN_USDT_TRC_OLD2 = "💵 USDT (trc-20)"
BTN_SHOP_REFRESH = "🔄 Обновить"
BTN_SHOP_RECHECK = "🔄 Проверить повторно"
BTN_FORM_EXAMPLE = "📋 Пример"
BTN_ADMIN_HELP = "🛠 /help"
BTN_ADMIN_USERS = "👥 Юзеры"
BTN_ADMIN_STATS = "📊 Статы"

BTN_CHECKS = "💳 Генератор чеков"
BTN_SPOOF = "📧 SPOOFING mail.ru"
BTN_TELEGRAM = "📱 РФ ТГ аккаунты"
BTN_VOICE = "🎤 Копирование голоса"
BTN_CHAT_DRAW = "💬 Отрисовка переписок"
BTN_DOC_DRAW = "📄 Отрисовка документов"
BTN_AI = "🤖 ИИ без цензуры"

BTN_SBER = "🟢 Сбер (в доработке)"
BTN_SBER_OLD = "🟢 Сбер"
BTN_VTB = "🔵 ВТБ (в доработке)"
BTN_VTB_OLD = "🔵 ВТБ"
BTN_OZON = "🟣 ОЗОН (в доработке)"
BTN_OZON_OLD = "🟣 ОЗОН"
BTN_ALFA = "🔴 Альфа"
BTN_TBANK = "🟡 Т-Банк (в доработке)"
BTN_TBANK_OLD = "🟡 Т-Банк"
BTN_OTP = "🏛 ОТП Банк (в доработке)"
BTN_OTP_OLD = "🏛 ОТП Банк"
BTN_URALSIB = "🟣 Уралсиб (в доработке)"
BTN_URALSIB_OLD = "🟠 Уралсиб"
BTN_URALSIB_PLAIN = "🟣 Уралсиб"
BANK_PICK_BUTTONS = frozenset({
    BTN_SBER, BTN_SBER_OLD, BTN_VTB, BTN_VTB_OLD, BTN_OZON, BTN_OZON_OLD,
    BTN_ALFA, BTN_TBANK, BTN_TBANK_OLD, BTN_OTP, BTN_OTP_OLD,
    BTN_URALSIB, BTN_URALSIB_OLD, BTN_URALSIB_PLAIN,
})

# Подтипы перевода Т-Банка (после выбора банка)
BTN_TBANK_SBP = "📲 СБП"
BTN_TBANK_PHONE = "📱 По номеру телефона"
BTN_TBANK_CARD_OTHER = "💳 По карте на Сбербанк"
BTN_TBANK_CARD_NOCOMM = "💳 По карте в другой банк(без к-и)"
BTN_TBANK_CARD_TBANK = "💳 По карте в Т-Банк"
BTN_TBANK_STATEMENT  = "📄 Выписка Т-Банка"

# Подтипы перевода ОТП Банка
BTN_OTP_SBP  = "📲 СБП"
BTN_OTP_CARD = "💳 По карте в другой банк"

# Подтипы перевода Ozon Банка
BTN_OZON_SBP = "📲 СБП"

# Подтипы перевода Сбербанка
BTN_SBER_SBP = "📲 СБП"
BTN_SBER_PHONE = "📱 По номеру телефона"
BTN_SBER_CARD_OTHER = "💳 По карте в другой банк"

# Подтипы перевода Альфа-Банка
BTN_ALFA_SBP = "📲 СБП"
BTN_ALFA_CARD = "💳 По номеру карты в другой банк"
BTN_ALFA_PHONE = "📱 По телефону (Альфа→Альфа)"
BTN_ALFA_PHONE_OLD = "📱 По телефону (Альфа→Альфа)"
BTN_ALFA_STATEMENT = "📄 Выписка"

BTN_BACK = "◀️ Назад"
BTN_HOME = "🏠 На главную"

MAIN_MENU_TEXT = "📌 Выберите желаемую функцию"

# ========== РАБОТА С ДАННЫМИ ПОЛЬЗОВАТЕЛЕЙ ==========

def load_data():
    """Загрузка всех данных"""
    try:
        with open(PAYMENTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"users": {}}


def save_data(data):
    """Сохранение данных"""
    with open(PAYMENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def user_exists(user_id: int) -> bool:
    """Есть ли пользователь в базе (уже регистрировался)."""
    data = load_data()
    return str(user_id) in data.get("users", {})


def is_banned(user_id: int) -> bool:
    """Бот закрыт админом. Админа так не закрыть. PIN этот запрет не снимает."""
    if is_admin(user_id):
        return False
    user = load_data().get("users", {}).get(str(user_id))
    return bool(user and user.get("banned"))


def _normalize_pin(text: str) -> str:
    return re.sub(r"[\s\-]", "", (text or "").strip())


def has_bot_access(user_id: int, tg_user=None) -> bool:
    """Бот открыт всем, кроме бана. PIN не нужен."""
    if is_admin(user_id, tg_user):
        return True
    return not is_banned(user_id)


def revoke_all_non_admin_access() -> int:
    """Старое имя: больше не раздаёт Альфу. Только публичный платный режим."""
    return open_public_bot()


def _shop_paid_user_ids(data: dict) -> set[int]:
    paid: set[int] = set()
    for rec in (data.get("invoices") or {}).values():
        if not isinstance(rec, dict) or not rec.get("credited"):
            continue
        try:
            uid = int(rec.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid:
            paid.add(uid)
    return paid


def _seed_admin_ids_from_store() -> None:
    """Подтянуть ID админов из базы, если Telegram getChat не резолвит @username."""
    global _KRONLEAD_ID
    for key, user in (load_data().get("users") or {}).items():
        if not isinstance(user, dict):
            continue
        un = str(user.get("username") or "").strip().lstrip("@").lower()
        if un not in ADMIN_USERNAMES:
            continue
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        _ADMIN_ID_CACHE.add(uid)
        if un == "kronlead":
            _KRONLEAD_ID = uid


def open_public_bot() -> int:
    """Снять баны, PIN, Альфа-only и бесплатные чеки. Квота только у тех, кто оплатил."""
    _seed_admin_ids_from_store()
    data = load_data()
    users = data.setdefault("users", {})
    paid = _shop_paid_user_ids(data)
    admin_ids = set(_ADMIN_ID_CACHE)
    n = 0
    for key, user in users.items():
        if not isinstance(user, dict):
            continue
        try:
            uid = int(key)
        except (TypeError, ValueError):
            uid = 0
        stored_un = str(user.get("username") or "").strip().lstrip("@").lower()
        is_adm = uid in admin_ids or stored_un in ADMIN_USERNAMES
        changed = False
        if user.pop("banned", None) is not None:
            changed = True
        if user.pop("scope", None) is not None:
            changed = True
        user["pin_ok"] = True
        if user.pop("assigned_pin", None) is not None:
            changed = True
        if not is_adm and uid not in paid:
            if int(user.get("check_credits") or 0) != 0:
                user["check_credits"] = 0
                changed = True
            if user.pop("rent_until", None) is not None:
                changed = True
            user.pop("rent_ended_notified", None)
            if user.get("packages"):
                user["packages"] = []
                changed = True
            if int(user.get("balance") or 0) != 0:
                user["balance"] = 0
                changed = True
        if changed:
            n += 1
    data["personal_pins"] = {}
    data["open_usernames"] = []
    save_data(data)
    return n


def _personal_pin_store() -> dict:
    data = load_data()
    store = data.setdefault("personal_pins", {})
    if not isinstance(store, dict):
        store = {}
        data["personal_pins"] = store
    save_data(data)
    return data


def _gen_unique_pin(taken: set[str]) -> str:
    blocked = set(taken)
    blocked.add(ACCESS_PIN)
    if ALFA_ONLY_PIN:
        blocked.add(ALFA_ONLY_PIN)
    for _ in range(80):
        pin = f"{secrets.randbelow(1_000_000):06d}"
        if pin not in blocked and pin != "000000":
            return pin
    return f"{secrets.randbelow(900000) + 100000:06d}"


def list_personal_pins() -> list[dict]:
    data = load_data()
    store = data.get("personal_pins") or {}
    rows = []
    for pin, rec in store.items():
        if not isinstance(rec, dict):
            continue
        rows.append({
            "pin": str(pin),
            "user_id": int(rec.get("user_id") or 0),
            "username": str(rec.get("username") or "").strip().lstrip("@").lower(),
            "created": rec.get("created") or "",
        })
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    return rows


def revoke_personal_pin(user_id: int | None = None, username: str = "") -> str | None:
    """Снять именной PIN. Вернуть снятый код или None."""
    data = load_data()
    store = data.setdefault("personal_pins", {})
    uname = (username or "").strip().lstrip("@").lower()
    found = None
    drop = []
    drop_uids: set[int] = set()
    for pin, rec in list(store.items()):
        if not isinstance(rec, dict):
            continue
        rid = int(rec.get("user_id") or 0)
        run = str(rec.get("username") or "").strip().lstrip("@").lower()
        if (user_id and rid == int(user_id)) or (uname and run == uname):
            drop.append(pin)
            found = pin
            if rid:
                drop_uids.add(rid)
    for pin in drop:
        store.pop(pin, None)
    if user_id:
        drop_uids.add(int(user_id))
    users = data.get("users", {})
    for uid in drop_uids:
        user = users.get(str(int(uid)))
        if isinstance(user, dict):
            user.pop("assigned_pin", None)
            user["pin_ok"] = False
            user.pop("scope", None)
    data["personal_pins"] = store
    save_data(data)
    return found


def assign_personal_pin(
    user_id: int | None,
    username: str = "",
    pin: str | None = None,
) -> str:
    """Выдать 6-значный PIN, который откроет бота только этому человеку."""
    uname = (username or "").strip().lstrip("@").lower()
    pin_n = _normalize_pin(pin or "")
    data = _personal_pin_store()
    store = data.setdefault("personal_pins", {})
    taken = {str(k) for k in store.keys()}
    if pin_n:
        if not re.fullmatch(r"\d{6}", pin_n):
            raise ValueError("PIN должен быть 6 цифр")
        if pin_n == ACCESS_PIN:
            raise ValueError("этот PIN служебный")
        owner = store.get(pin_n)
        if isinstance(owner, dict):
            oid = int(owner.get("user_id") or 0)
            oun = str(owner.get("username") or "").strip().lstrip("@").lower()
            same = (user_id and oid == int(user_id)) or (uname and oun == uname)
            if not same:
                raise ValueError("этот PIN уже выдан другому")
        assigned = pin_n
    else:
        assigned = _gen_unique_pin(taken)
    revoke_personal_pin(user_id, uname)
    data = load_data()
    store = data.setdefault("personal_pins", {})
    store[assigned] = {
        "user_id": int(user_id or 0),
        "username": uname,
        "created": now_msk().isoformat(),
    }
    if user_id:
        users = data.setdefault("users", {})
        key = str(int(user_id))
        if key not in users:
            users[key] = {
                "balance": 0,
                "packages": [],
                "trial_uses": {},
                "total_spent": 0,
                "checks_created": 0,
                "created": now_msk().isoformat(),
                "pin_ok": False,
            }
        if uname:
            users[key]["username"] = uname
        users[key]["assigned_pin"] = assigned
        users[key]["pin_ok"] = False
    data["personal_pins"] = store
    save_data(data)
    return assigned


def personal_pin_owner(pin: str) -> dict | None:
    pin_n = _normalize_pin(pin)
    if not pin_n:
        return None
    rec = (load_data().get("personal_pins") or {}).get(pin_n)
    return rec if isinstance(rec, dict) else None


def personal_pin_matches(pin: str, user_id: int, tg_user=None) -> bool:
    rec = personal_pin_owner(pin)
    if not rec:
        return False
    oid = int(rec.get("user_id") or 0)
    oun = str(rec.get("username") or "").strip().lstrip("@").lower()
    me_un = ""
    if tg_user is not None:
        me_un = (getattr(tg_user, "username", None) or "").strip().lstrip("@").lower()
    if not me_un:
        me_un = (stored_username(user_id) or "").lower()
    if oid and oid == int(user_id):
        return True
    if oun and me_un and oun == me_un:
        return True
    return False


def bind_personal_pin_user(pin: str, user_id: int, username: str = "") -> None:
    """После первого входа дописать ID, если PIN выдавали только по @username."""
    pin_n = _normalize_pin(pin)
    data = load_data()
    rec = (data.get("personal_pins") or {}).get(pin_n)
    if not isinstance(rec, dict):
        return
    rec["user_id"] = int(user_id)
    uname = (username or rec.get("username") or "").strip().lstrip("@").lower()
    if uname:
        rec["username"] = uname
    users = data.setdefault("users", {})
    key = str(int(user_id))
    if key in users:
        users[key]["assigned_pin"] = pin_n
        if uname:
            users[key]["username"] = uname
    data.setdefault("personal_pins", {})[pin_n] = rec
    save_data(data)


def set_user_banned(user_id: int, banned: bool) -> None:
    """Закрыть бота целиком или вернуть полный доступ."""
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
            "pin_ok": False,
        }
    if banned:
        users[key]["banned"] = True
        users[key]["pin_ok"] = False
    else:
        users[key]["banned"] = False
        users[key]["pin_ok"] = True
    save_data(data)


def remember_tg_user(user_id: int, tg_user) -> None:
    """Сохранить @username / имя для админов и отображения."""
    if not tg_user:
        return
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
            "pin_ok": False,
        }
    uname = (getattr(tg_user, "username", None) or "").strip().lstrip("@")
    if uname:
        users[key]["username"] = uname
    users[key]["first_name"] = (getattr(tg_user, "first_name", None) or "").strip()
    users[key]["full_name"] = (getattr(tg_user, "full_name", None) or "").strip()
    save_data(data)
    if uname and uname.lower() in ADMIN_USERNAMES:
        try:
            _ADMIN_ID_CACHE.add(int(user_id))
        except (TypeError, ValueError):
            pass


def stored_username(user_id: int) -> str:
    data = load_data()
    u = data.get("users", {}).get(str(user_id)) or {}
    return (u.get("username") or "").strip().lstrip("@")


def _username_lookup_key(username: str) -> str:
    """Буквы+цифры в lower - Naval_pay_manager == Navalpaymanager."""
    return "".join(ch for ch in (username or "").strip().lstrip("@").lower() if ch.isalnum())


def find_user_id_by_username(username: str) -> int | None:
    """Ищем ID в payments.json по сохранённому @username (точно или без _/-)."""
    un = (username or "").strip().lstrip("@").lower()
    if not un:
        return None
    norm_q = _username_lookup_key(un)
    data = load_data()
    fuzzy_uid: int | None = None
    for uid, u in (data.get("users") or {}).items():
        stored = (u.get("username") or "").strip().lstrip("@")
        if not stored:
            continue
        if stored.lower() == un:
            try:
                return int(uid)
            except (TypeError, ValueError):
                continue
        if fuzzy_uid is None and norm_q and _username_lookup_key(stored) == norm_q:
            try:
                fuzzy_uid = int(uid)
            except (TypeError, ValueError):
                continue
    return fuzzy_uid


def suggest_usernames(username: str, limit: int = 3) -> list[tuple[int, str]]:
    """Подсказки при промахе: частичное совпадение alnum-ключа."""
    q = _username_lookup_key(username)
    if len(q) < 4:
        return []
    data = load_data()
    hits: list[tuple[int, str, int]] = []
    for uid, u in (data.get("users") or {}).items():
        stored = (u.get("username") or "").strip().lstrip("@")
        if not stored:
            continue
        key = _username_lookup_key(stored)
        if q in key or key in q:
            score = min(len(q), len(key))
            hits.append((score, int(uid), stored))
    hits.sort(key=lambda x: (-x[0], x[2].lower()))
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for _, uid, stored in hits:
        if uid in seen:
            continue
        seen.add(uid)
        out.append((uid, stored))
        if len(out) >= limit:
            break
    return out


def format_user_not_found(arg: str) -> str:
    text = "❌ Пользователь не найден (@username или числовой ID)"
    hints = suggest_usernames(arg)
    if hints:
        lines = [f"• @{uname} (`{uid}`)" for uid, uname in hints]
        text += "\n\nПохожие в базе:\n" + "\n".join(lines)
    text += "\n\nСписок: /users"
    return text


async def resolve_tg_user_arg(bot, arg: str) -> tuple[int | None, str]:
    """
    USER_ID или @username → (id, label).
    Сначала payments.json, потом Telegram getChat(@username).
    """
    raw = (arg or "").strip()
    if not raw:
        return None, ""
    # Чистый ID
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        try:
            uid = int(raw)
        except ValueError:
            return None, raw
        un = stored_username(uid)
        return uid, f"@{un}" if un else str(uid)

    uname = raw.lstrip("@")
    uid = find_user_id_by_username(uname)
    if uid is not None:
        return uid, f"@{uname}"

    try:
        chat = await bot.get_chat(f"@{uname}")
        if chat and chat.id:
            uid = int(chat.id)
            # запомнить username в базе, если юзер уже есть / создать минимальную запись
            data = load_data()
            users = data.setdefault("users", {})
            key = str(uid)
            if key not in users:
                users[key] = {
                    "balance": 0,
                    "packages": [],
                    "trial_uses": {},
                    "total_spent": 0,
                    "checks_created": 0,
                    "created": now_msk().isoformat(),
                    "pin_ok": False,
                }
            users[key]["username"] = uname
            save_data(data)
            return uid, f"@{uname}"
    except Exception as e:
        logger.warning("resolve_tg_user_arg @%s failed: %s", uname, e)
    return None, f"@{uname}"


def tg_user_label(tg_user=None, user_id: int | None = None) -> str:
    """@username, иначе ID."""
    if tg_user is not None:
        uname = (getattr(tg_user, "username", None) or "").strip().lstrip("@")
        if uname:
            return f"@{uname}"
        uid = getattr(tg_user, "id", None)
        if uid is not None:
            return str(uid)
    if user_id is not None:
        uname = stored_username(int(user_id))
        if uname:
            return f"@{uname}"
        return str(int(user_id))
    return "?"


def mark_pin_ok(user_id: int, scope: str | None = None) -> None:
    """После верного PIN - открыть доступ."""
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
        }
    if is_banned(user_id):
        save_data(data)
        return
    users[key]["pin_ok"] = True
    if scope == SCOPE_ALFA and not is_admin(user_id):
        users[key]["scope"] = SCOPE_ALFA
    else:
        users[key].pop("scope", None)
    save_data(data)


def get_user_data(user_id: int) -> dict:
    """Получение данных пользователя"""
    data = load_data()
    user_str = str(user_id)
    if user_str not in data.get("users", {}):
        data["users"] = data.get("users", {})
        data["users"][user_str] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
            "pin_ok": True,
        }
        save_data(data)
    return data["users"][user_str]


def update_user_data(user_id: int, user_data: dict):
    """Обновление данных пользователя"""
    data = load_data()
    data["users"][str(user_id)] = user_data
    save_data(data)


def is_alfa_only(user_id: int) -> bool:
    """Режим только Альфа снят: всем полная витрина."""
    return False


def has_gifted_checks(user_id: int) -> bool:
    """Бесплатные чеки по PIN сняты - только покупка."""
    return False


def has_full_access(user_id: int) -> bool:
    """Бесплатная бета снята - чеки только после оплаты."""
    return False


def grant_full_access(user_id: int, username: str = "") -> bool:
    """Полный бот без админки: все банки, без оплаты. Админом не делает."""
    if is_admin(user_id):
        return False
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
        }
    users[key]["pin_ok"] = True
    users[key].pop("scope", None)
    users[key].pop("banned", None)
    uname = (username or "").strip().lstrip("@").lower() or str(users[key].get("username") or "").strip().lstrip("@").lower()
    if uname:
        users[key]["username"] = uname
        names = [str(x).strip().lstrip("@").lower() for x in (data.get("open_usernames") or []) if str(x).strip()]
        data["open_usernames"] = [n for n in names if n != uname]
    save_data(data)
    return True


def grant_alfa_only(user_id: int, username: str = "") -> bool:
    """Открыть пользователю только Альфу. Админу не ставится."""
    if is_admin(user_id):
        return False
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        users[key] = {
            "balance": 0,
            "packages": [],
            "trial_uses": {},
            "total_spent": 0,
            "checks_created": 0,
            "created": now_msk().isoformat(),
        }
    users[key]["pin_ok"] = True
    users[key].pop("scope", None)
    users[key].pop("banned", None)
    uname = (username or "").strip().lstrip("@").lower()
    if uname:
        users[key]["username"] = uname
    save_data(data)
    return True


def _open_username_set(data: dict | None = None) -> set[str]:
    if data is None:
        data = load_data()
    raw = data.get("open_usernames") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().lstrip("@").lower() for x in raw if str(x).strip()}


def silent_open_access(user_id: int | None, username: str = "") -> None:
    """Тихий бесплатный доступ снят. Чеки только после оплаты."""
    return


def revoke_silent_open(user_id: int | None = None, username: str = "") -> None:
    uname = (username or "").strip().lstrip("@").lower()
    if user_id and not uname:
        uname = (stored_username(int(user_id)) or "").strip().lstrip("@").lower()
    data = load_data()
    names = [str(x).strip().lstrip("@").lower() for x in (data.get("open_usernames") or []) if str(x).strip()]
    if uname:
        names = [n for n in names if n != uname]
    data["open_usernames"] = names
    save_data(data)


def bind_silent_open(user_id: int, tg_user=None) -> None:
    """Тихий бесплатный доступ снят."""
    return


def revoke_bot_access(user_id: int) -> None:
    """Закрыть вход: снова нужен PIN."""
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    if key not in users:
        return
    users[key]["pin_ok"] = False
    users[key].pop("scope", None)
    save_data(data)


def is_admin(user_id: int, tg_user=None) -> bool:
    """Админ только по резолвленному ID или живому @acterichee / @kronlead."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _ADMIN_ID_CACHE:
        return True
    if tg_user is not None:
        un = (getattr(tg_user, "username", None) or "").strip().lstrip("@").lower()
        if un in ADMIN_USERNAMES:
            _ADMIN_ID_CACHE.add(uid)
            return True
    return False


def note_admin_user(user_id: int, tg_user=None) -> None:
    """Если username из whitelist - запомнить ID."""
    is_admin(user_id, tg_user)


_KNOWN_ADMIN_IDS = (6925585675, 7657032889)


def admin_recipient_ids() -> list[int]:
    """Всегда оба админа: кэш + база + запасные ID."""
    _seed_admin_ids_from_store()
    ids = set(_ADMIN_ID_CACHE)
    ids.update(int(x) for x in _KNOWN_ADMIN_IDS)
    return sorted(ids)


def get_balance(user_id: int) -> int:
    """Реальный баланс (для отображения). Админы всё равно безлимитны при списании."""
    return int(get_user_data(user_id).get("balance", 0) or 0)


def add_balance(user_id: int, amount: int):
    """Добавление монет (для админ-выдач)."""
    user_data = get_user_data(user_id)
    user_data["balance"] = user_data.get("balance", 0) + amount
    update_user_data(user_id, user_data)


def credit_deposit(user_id: int, amount: int) -> None:
    """Пополнение баланса (CryptoBot и т.п.)."""
    if amount <= 0:
        return
    user_data = get_user_data(user_id)
    user_data["balance"] = user_data.get("balance", 0) + amount
    user_data["total_deposited"] = user_data.get("total_deposited", 0) + amount
    update_user_data(user_id, user_data)


def can_see_shop(user_id: int, tg_user=None) -> bool:
    """Витрина открыта всем, кто в боте."""
    return has_bot_access(user_id, tg_user)


def get_check_credits(user_id: int) -> int:
    return int(get_user_data(user_id).get("check_credits", 0) or 0)


def get_rent_until(user_id: int) -> datetime | None:
    raw = get_user_data(user_id).get("rent_until")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if now_msk() >= dt:
        return None
    return dt


def has_active_rent(user_id: int) -> bool:
    return get_rent_until(user_id) is not None


def format_checks_left(user_id: int) -> str:
    """Остаток чеков. При активной аренде - безлимит, не знак ∞."""
    if has_active_rent(user_id):
        return "безлимит"
    return str(get_check_credits(user_id))


def format_rent_until_clock(user_id: int) -> str:
    until = get_rent_until(user_id)
    if not until:
        return ""
    return until.strftime("%d.%m %H:%M")


def live_banks_line() -> str:
    labels = {"alfa": "Альфа", "sber": "Сбер", "tbank": "Т-Банк", "uralsib": "Уралсиб"}
    names = [labels.get(b, b) for b in sorted(LIVE_BANKS)]
    if not names:
        return "Банки в доработке"
    if len(names) == 1:
        return f"Сейчас работает {names[0]}"
    return "Сейчас работают " + ", ".join(names)


def format_rent_left(user_id: int) -> str:
    until = get_rent_until(user_id)
    if not until:
        return ""
    sec = max(0, int((until - now_msk()).total_seconds()))
    hours, rem = divmod(sec, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


_QUOTA_LOCK = threading.Lock()


def extend_rent(user_id: int, hours: int) -> datetime:
    now = now_msk()
    with _QUOTA_LOCK:
        data = load_data()
        users = data.setdefault("users", {})
        key = str(user_id)
        user = users.get(key)
        if not isinstance(user, dict):
            get_user_data(user_id)
            data = load_data()
            users = data.setdefault("users", {})
            user = users.get(key)
        if not isinstance(user, dict):
            user = {}
            users[key] = user
        raw = user.get("rent_until")
        cur = None
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                if now < dt:
                    cur = dt
            except (TypeError, ValueError):
                cur = None
        base = cur if cur and cur > now else now
        until = base + timedelta(hours=int(hours))
        user["rent_until"] = until.isoformat()
        user.pop("rent_ended_notified", None)
        user.pop("rent_soon_notified", None)
        users[key] = user
        save_data(data)
        return until


def grant_shop_offer(user_id: int, offer_id: str) -> str:
    cfg = SHOP_OFFERS.get(offer_id) or {}
    kind = cfg.get("kind") or ""
    if kind == "checks":
        n = int(cfg.get("checks") or 0)
        add_check_credits(user_id, n)
        left = format_checks_left(user_id)
        return f"✅ +{n} чек\nОстаток чеков - {left}"
    if kind == "rent":
        hours = int(cfg.get("hours") or 0)
        extend_rent(user_id, hours)
        title = cfg.get("title") or "аренда"
        return f"✅ {title}\n{rent_checks_text(user_id)}"
    return "✅ Оплата получена."


def rent_checks_text(
    user_id: int,
    *,
    html: bool = False,
    with_until: bool = True,
    sep: str | None = None,
) -> str:
    """Остаток чеков и время аренды, даже если оба 0."""
    left = format_rent_left(user_id) or "0"
    credits = format_checks_left(user_id)
    if html:
        cred = f"<b>{html_lib.escape(credits)}</b>"
        left_s = f"<b>{html_lib.escape(left)}</b>"
    else:
        cred = credits
        left_s = left
    lines = [f"Остаток чеков - {cred}"]
    if sep:
        lines.append(sep)
    lines.append(f"Время аренды - {left_s}")
    return "\n".join(lines)


def status_bar(user_id: int) -> str:
    return rent_checks_text(user_id, with_until=False)


def with_status(user_id: int, text: str) -> str:
    t = (text or "").rstrip()
    bar = status_bar(user_id)
    if not bar:
        return t
    if not t:
        return bar
    return f"{t}\n\n{bar}"


def add_check_credits(user_id: int, n: int) -> int:
    if n <= 0:
        return get_check_credits(user_id)
    with _QUOTA_LOCK:
        data = load_data()
        users = data.setdefault("users", {})
        key = str(user_id)
        user = users.get(key)
        if not isinstance(user, dict):
            get_user_data(user_id)
            data = load_data()
            users = data.setdefault("users", {})
            user = users.get(key)
        if not isinstance(user, dict):
            return 0
        user["check_credits"] = int(user.get("check_credits", 0) or 0) + int(n)
        users[key] = user
        save_data(data)
        return int(user["check_credits"])


def spend_check_credit(user_id: int) -> bool:
    with _QUOTA_LOCK:
        data = load_data()
        user = (data.get("users") or {}).get(str(user_id))
        if not isinstance(user, dict):
            return False
        credits = int(user.get("check_credits", 0) or 0)
        if credits <= 0:
            return False
        user["check_credits"] = credits - 1
        data.setdefault("users", {})[str(user_id)] = user
        save_data(data)
        return True


def save_invoice_record(invoice_id: int | str, record: dict) -> None:
    data = load_data()
    inv = data.setdefault("invoices", {})
    inv[str(invoice_id)] = record
    save_data(data)


def get_invoice_record(invoice_id: int | str) -> dict | None:
    data = load_data()
    return (data.get("invoices") or {}).get(str(invoice_id))


def latest_open_shop_order(user_id: int, *, pay: str | None = None) -> str | None:
    best_id = None
    best_t = ""
    want = (pay or "").strip().lower()
    for iid, rec in (load_data().get("invoices") or {}).items():
        if not isinstance(rec, dict) or rec.get("credited"):
            continue
        rec_pay = str(rec.get("pay") or "cryptobot").lower()
        if want and rec_pay != want:
            continue
        try:
            if int(rec.get("user_id") or 0) != int(user_id):
                continue
        except (TypeError, ValueError):
            continue
        created = str(rec.get("created") or "")
        if created >= best_t:
            best_t = created
            best_id = str(iid)
    return best_id


def latest_open_trc_order(user_id: int) -> str | None:
    return latest_open_shop_order(user_id, pay="trc20")


def mark_invoice_credited(invoice_id: int | str) -> bool:
    """True если только что пометили (ещё не было credited)."""
    data = load_data()
    inv = data.setdefault("invoices", {})
    rec = inv.get(str(invoice_id))
    if not rec or rec.get("credited"):
        return False
    rec["credited"] = True
    rec["credited_at"] = now_msk().isoformat()
    save_data(data)
    return True


def redeem_promo(user_id: int, code: str) -> tuple[bool, str]:
    """Промокоды больше не открывают чеки. Только оплата в магазине."""
    if not (code or "").strip():
        return False, "Введите промокод."
    if not is_admin(user_id):
        return False, "Промокоды отключены. Оплата только в магазине."
    code_key = (code or "").strip().upper()
    if not code_key:
        return False, "Введите промокод."
    data = load_data()
    promos = data.setdefault("promo_codes", {})
    promo = promos.get(code_key)
    if not promo:
        return False, "Промокод не найден."
    used_by = promo.setdefault("used_by", [])
    if str(user_id) in used_by or user_id in used_by:
        return False, "Вы уже использовали этот промокод."
    max_uses = int(promo.get("max_uses", 0) or 0)
    if max_uses > 0 and len(used_by) >= max_uses:
        return False, "Промокод больше не действует."
    ptype = (promo.get("type") or "balance").lower()
    amount = int(promo.get("amount", 0) or 0)
    if amount <= 0:
        return False, "Промокод настроен неверно."
    used_by.append(str(user_id))
    save_data(data)
    if ptype in ("checks", "check", "credit"):
        add_check_credits(user_id, amount)
        return True, f"✅ Промокод принят: +{amount} чек(ов) в пакет."
    add_balance(user_id, amount)
    return True, f"✅ Промокод принят: +{amount} $ на баланс."


def upsert_promo(code: str, ptype: str, amount: int, max_uses: int = 0) -> None:
    data = load_data()
    promos = data.setdefault("promo_codes", {})
    key = code.strip().upper()
    promos[key] = {
        "type": "checks" if ptype.lower() in ("checks", "check", "credit") else "balance",
        "amount": int(amount),
        "max_uses": int(max_uses),
        "used_by": list((promos.get(key) or {}).get("used_by") or []),
    }
    save_data(data)


async def notify_admins(bot, text: str, *, skip: int | None = None, reply_markup=None) -> None:
    ids = admin_recipient_ids()
    if not ids:
        logger.warning("notify_admins: empty admin ids")
    for admin_id in ids:
        if skip is not None and int(admin_id) == int(skip):
            continue
        try:
            await bot.send_message(int(admin_id), text, reply_markup=reply_markup)
        except Exception:
            logger.exception("notify_admins fail id=%s", admin_id)


def _shop_kind_title(kind: str) -> str:
    cfg = SHOP_OFFERS.get(kind) or {}
    return str(cfg.get("title") or kind or "тариф")


async def notify_admins_payment(
    bot,
    *,
    method: str,
    user_id: int,
    kind: str,
    amount: object,
    detail: str,
    extra: str = "",
) -> None:
    who = tg_user_label(user_id=user_id)
    title = _shop_kind_title(kind)
    lines = [
        "Оплата прошла",
        f"Способ: {method}",
        f"Кто: {who} ({user_id})",
        f"Тариф: {title}",
        f"Сумма: {amount} USDT",
        str(detail or "").strip(),
    ]
    if extra:
        lines.append(extra)
    uname = (stored_username(user_id) or "").strip().lstrip("@")
    kb = None
    if uname:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✉️ Написать", url=f"https://t.me/{uname}")]]
        )
    await notify_admins(bot, "\n".join(x for x in lines if x), reply_markup=kb)


def kronlead_chat_id() -> int | None:
    if _KRONLEAD_ID:
        return int(_KRONLEAD_ID)
    for admin_id in admin_recipient_ids():
        uname = (stored_username(admin_id) or "").strip().lstrip("@").lower()
        if uname == "kronlead":
            return int(admin_id)
    return None


def is_kronlead(user_id: int | None = None, tg_user=None) -> bool:
    """True only for @kronlead (live username or stored/admin id)."""
    if tg_user is not None:
        un = str(getattr(tg_user, "username", "") or "").strip().lstrip("@").lower()
        if un == "kronlead":
            return True
    if user_id is None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if _KRONLEAD_ID and uid == int(_KRONLEAD_ID):
        return True
    un = (stored_username(uid) or "").strip().lstrip("@").lower()
    return un == "kronlead"


def live_banks_for(user_id: int | None = None, tg_user=None) -> frozenset:
    """Public LIVE_BANKS; admins get Sber / T-Bank / Uralsib for QA."""
    if is_admin(user_id, tg_user) or is_kronlead(user_id, tg_user):
        return ADMIN_LIVE_BANKS
    return LIVE_BANKS


async def notify_kronlead_receipt(bot, pdf_bytes: bytes, filename: str, caption: str) -> None:
    """@kronlead получает сам PDF созданного чека."""
    chat_id = kronlead_chat_id()
    if not chat_id or not pdf_bytes:
        logger.warning("kronlead receipt report skipped: id=%s bytes=%s", chat_id, bool(pdf_bytes))
        return
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=bytes(pdf_bytes),
            filename=filename or "check.pdf",
            caption=(caption or "")[:1024],
        )
    except Exception:
        logger.exception("kronlead receipt report failed")


def spend_balance(user_id: int, amount: int) -> bool:
    """Списание монет"""
    if amount <= 0:
        return False

    user_data = get_user_data(user_id)
    if user_data.get("balance", 0) >= amount:
        user_data["balance"] -= amount
        user_data["total_spent"] = user_data.get("total_spent", 0) + amount
        update_user_data(user_id, user_data)
        return True
    return False


def has_unlimited(user_id: int, tool_id: str) -> bool:
    """Безлимит только по оплаченной аренде."""
    return has_active_rent(user_id) and str(tool_id).endswith("_check")


def get_tool_price(tool_id: str) -> int:
    """Цена инструмента"""
    return TOOL_PRICES.get(tool_id, 2)


def reserve_generation(user_id: int, tool_id: str, tg_user=None) -> str | None:
    """Списать квоту до сборки PDF. None = нельзя."""
    if is_admin(user_id, tg_user):
        return "admin"
    if is_banned(user_id):
        return None
    with _QUOTA_LOCK:
        data = load_data()
        user = (data.get("users") or {}).get(str(user_id))
        if not isinstance(user, dict):
            return None
        raw = user.get("rent_until")
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                if now_msk() < dt and str(tool_id).endswith("_check"):
                    return "unlimited"
            except (TypeError, ValueError):
                pass
        credits = int(user.get("check_credits", 0) or 0)
        if credits <= 0:
            return None
        user["check_credits"] = credits - 1
        data.setdefault("users", {})[str(user_id)] = user
        save_data(data)
        return "credit"


def refund_generation(user_id: int, reason: str) -> None:
    if reason != "credit":
        return
    with _QUOTA_LOCK:
        data = load_data()
        users = data.setdefault("users", {})
        user = users.get(str(user_id))
        if not isinstance(user, dict):
            return
        user["check_credits"] = int(user.get("check_credits", 0) or 0) + 1
        users[str(user_id)] = user
        save_data(data)


def bump_checks_created(user_id: int) -> None:
    with _QUOTA_LOCK:
        data = load_data()
        users = data.setdefault("users", {})
        user = users.get(str(user_id))
        if not isinstance(user, dict):
            return
        user["checks_created"] = int(user.get("checks_created", 0) or 0) + 1
        users[str(user_id)] = user
        save_data(data)


def can_create_check(user_id: int, tg_user=None) -> bool:
    """Можно собирать чек: админ, аренда или купленные чеки."""
    if is_admin(user_id, tg_user):
        return True
    if is_banned(user_id):
        return False
    if has_active_rent(user_id):
        return True
    if get_check_credits(user_id) > 0:
        return True
    return False


def can_use_tool(user_id: int, tool_id: str, tg_user=None) -> tuple:
    """
    Проверка возможности использования инструмента
    Возвращает: (can_use: bool, reason: str, cost: int)
    """
    if is_admin(user_id, tg_user):
        return True, "admin", 0
    if is_banned(user_id):
        return False, "banned", 0

    if has_active_rent(user_id) and str(tool_id).endswith("_check"):
        return True, "unlimited", 0

    if get_check_credits(user_id) > 0:
        return True, "credit", 0

    return False, "no_quota", 0


def use_tool(user_id: int, tool_id: str) -> bool:
    """Списать оплату за инструмент. False = не списывалось / не хватило."""
    _can, reason, cost = can_use_tool(user_id, tool_id)
    if not _can:
        return False

    if reason in ("admin", "full", "unlimited", "alfa_only", "gifted"):
        return True

    if reason == "trial":
        user_data = get_user_data(user_id)
        trial_uses = user_data.get("trial_uses", {})
        trial_uses[tool_id] = trial_uses.get(tool_id, 0) + 1
        user_data["trial_uses"] = trial_uses
        update_user_data(user_id, user_data)
        return True

    if reason == "credit":
        return spend_check_credit(user_id)

    return False


def commit_generated_check(user_id: int, tool_id: str) -> bool:
    """Charge and increment checks_created in one persisted update after delivery."""
    data = load_data()
    users = data.setdefault("users", {})
    user_data = users.get(str(user_id))
    if not isinstance(user_data, dict):
        return False

    if is_admin(user_id):
        reason = "admin"
    else:
        reason = ""
        rent_raw = user_data.get("rent_until")
        if rent_raw:
            try:
                if now_msk() < datetime.fromisoformat(str(rent_raw)) and str(tool_id).endswith("_check"):
                    reason = "unlimited"
            except (TypeError, ValueError):
                pass
        if not reason and int(user_data.get("check_credits", 0) or 0) > 0:
            reason = "credit"
        if not reason:
            return False

    if reason == "credit":
        user_data["check_credits"] = int(user_data.get("check_credits", 0) or 0) - 1
    elif reason == "trial":
        trial_uses = dict(user_data.get("trial_uses", {}))
        trial_uses[tool_id] = int(trial_uses.get(tool_id, 0) or 0) + 1
        user_data["trial_uses"] = trial_uses
    elif reason == "paid":
        cost = get_tool_price(tool_id)
        user_data["balance"] = int(user_data.get("balance", 0) or 0) - cost
        user_data["total_spent"] = int(user_data.get("total_spent", 0) or 0) + cost

    user_data["checks_created"] = int(user_data.get("checks_created", 0) or 0) + 1
    users[str(user_id)] = user_data
    save_data(data)
    return True


_NEED_PACKAGE = (
    "Чтобы создавать чеки, купите пакет.\n"
    "\n"
    "1 чек, день, неделя или месяц."
)
_RENT_ENDED = (
    "Аренда закончилась.\n"
    "\n"
    "Чтобы продолжить, купите пакет."
)


async def send_need_package(update: Update, *, expired: bool = False) -> int:
    """Отказ без примера. Магазин, если витрина открыта."""
    user = update.effective_user
    uid = user.id if user else 0
    text = _RENT_ENDED if expired else _NEED_PACKAGE
    if uid and can_see_shop(uid, user):
        await update.effective_message.reply_text(
            with_status(uid, text),
            reply_markup=shop_menu_keyboard(),
        )
        return SHOP_MENU
    extra = "Напишите @kronlead"
    await update.effective_message.reply_text(
        with_status(uid, f"{text}\n{extra}") if uid else f"{text}\n{extra}",
        reply_markup=main_menu_keyboard(uid, user) if uid else None,
    )
    return MAIN_MENU


def _parse_rent_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (TypeError, ValueError):
        return None


def _claim_rent_soon_notice(user_id: int) -> bool:
    """True один раз, когда до конца аренды меньше часа."""
    data = load_data()
    user = (data.get("users") or {}).get(str(user_id))
    if not isinstance(user, dict) or user.get("rent_soon_notified") or user.get("rent_ended_notified"):
        return False
    dt = _parse_rent_dt(user.get("rent_until"))
    if not dt:
        return False
    left = (dt - now_msk()).total_seconds()
    if left <= 0 or left > 3600:
        return False
    user["rent_soon_notified"] = True
    save_data(data)
    return True


async def _notify_rent_soon(bot, user_id: int) -> None:
    if not _claim_rent_soon_notice(user_id):
        return
    left = format_rent_left(user_id) or "меньше часа"
    try:
        await bot.send_message(
            user_id,
            f"Аренда кончится через {left}.\nЧтобы не прерываться — продлите в магазине.",
            reply_markup=shop_menu_keyboard(),
        )
    except Exception:
        logger.exception("rent soon notify uid=%s", user_id)


def _claim_rent_ended_notice(user_id: int) -> bool:
    """True один раз, когда аренда уже вышла."""
    data = load_data()
    user = (data.get("users") or {}).get(str(user_id))
    if not isinstance(user, dict) or user.get("rent_ended_notified"):
        return False
    dt = _parse_rent_dt(user.get("rent_until"))
    if not dt or now_msk() < dt:
        return False
    user["rent_ended_notified"] = True
    save_data(data)
    return True


async def _notify_rent_ended(bot, user_id: int) -> None:
    if not _claim_rent_ended_notice(user_id):
        return
    leftover = get_check_credits(user_id)
    text = _RENT_ENDED if leftover <= 0 else f"{_RENT_ENDED}\nОстаток чеков - {leftover}"
    try:
        kb = shop_menu_keyboard() if can_see_shop(user_id) else main_menu_keyboard(user_id)
        await bot.send_message(user_id, text, reply_markup=kb)
    except Exception:
        logger.exception("rent ended notify uid=%s", user_id)


async def _sweep_expired_rent(bot) -> None:
    now = now_msk()
    users = (load_data().get("users") or {})
    due = []
    soon = []
    for key, user in users.items():
        if not isinstance(user, dict):
            continue
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        dt = _parse_rent_dt(user.get("rent_until"))
        if not dt:
            continue
        if now >= dt:
            due.append(uid)
        elif (dt - now).total_seconds() <= 3600:
            soon.append(uid)
    for uid in soon:
        await _notify_rent_soon(bot, uid)
    for uid in due:
        await _notify_rent_ended(bot, uid)


async def send_need_package_if_unpaid(update: Update) -> int | None:
    user = update.effective_user
    if user and can_create_check(user.id, user):
        return None
    return await send_need_package(update)


async def prompt_check_form(update: Update, *args, **kwargs) -> int:
    """Пример формы только если аренда или чеки есть."""
    blocked = await send_need_package_if_unpaid(update)
    if blocked is not None:
        return blocked
    await send_data_entry_prompt(update, *args, **kwargs)
    return ENTERING_DATA


async def ensure_can_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, tool_id: str) -> bool:
    """Перед генерацией PDF - есть ли аренда или чеки."""
    user_id = update.effective_user.id
    ok, _reason, _price = can_use_tool(user_id, tool_id, update.effective_user)
    if ok:
        return True
    await send_need_package(update)
    return False


def give_package(user_id: int, package_id: str) -> bool:
    """Legacy unlimited-пакеты отключены. Чеки только из магазина."""
    return False


def get_active_packages(user_id: int) -> list:
    """Активные пакеты пользователя"""
    user_data = get_user_data(user_id)
    packages = user_data.get("packages", [])
    active = []
    
    for pkg in packages:
        if pkg.get("expires"):
            expires = datetime.fromisoformat(pkg["expires"])
            if now_msk() > expires:
                continue
            days_left = (expires - now_msk()).days
            pkg["days_left"] = max(0, days_left)
        else:
            pkg["days_left"] = -1  # Бессрочно
        active.append(pkg)
    
    return active


def get_trial_left(user_id: int, tool_id: str) -> int:
    """Сколько триальных использований осталось"""
    if not TRIAL_ENABLED:
        return 0
    
    user_data = get_user_data(user_id)
    trial_uses = user_data.get("trial_uses", {})
    max_trial = TRIAL_USES.get(tool_id, 0)
    used = trial_uses.get(tool_id, 0)
    return max(0, max_trial - used)


# ========== КЛАВИАТУРЫ (только reply - под полем ввода) ==========

ADMIN_HELP_TEXT = (
    "🛠 Админ-панель\n\n"
    "Команды (вместо ID можно @username):\n"
    "/give @user AMOUNT - монеты на баланс\n"
    "/givechecks @user N - чеки в пакет\n"
    "/bal @user - баланс и остаток чеков\n"
    "/promo CODE balance|checks AMOUNT [max_uses]\n"
    "/users - список юзеров\n"
    "/stats - статистика\n"
    "/approve @user - +100$\n"
    "/alfaonly @user - (снято: бот открыт всем)\n"
    "/full @user - (снято: чеки только после оплаты)\n"
    "/alfaoff @user - (снято: PIN больше нет)\n"
    "/ban @user - закрыть бота для человека\n"
    "/unban @user - вернуть доступ\n"
    "/open @user - открыть бота тихо (без PIN и без сообщения ему)\n"
    "/pin @user - выдать именной PIN (только Альфа, только этот человек)\n"
    "/pin @user 123456 - задать свой 6-значный PIN\n"
    "/anpin @user - забрать доступ и PIN\n"
    "/unpin @user - то же, что /anpin\n"
    "/pins - список именных PIN\n"
    "/giverent @user day|week|month - выдать аренду (тест таймера)\n"
    "/news текст - новость всем, кто жал /start\n\n"
    "Магазин: 1 чек 5$ · день 50$ · неделя 200$ · месяц 500$.\n"
    "Оплата: CryptoBot или USDT TRC-20, зачисление само после оплаты.\n\n"
    "Админы только @acterichee / @kronlead. /full - бета: все банки, без оплаты, без админки."
)


def main_menu_keyboard(user_id: int | None = None, tg_user=None):
    """Два столбика: банк/магазин, поддержка/профиль."""
    if user_id is not None and can_see_shop(user_id, tg_user):
        rows = [
            [BTN_SELECT_BANK, BTN_BALANCE_CHECKS],
            [BTN_SUPPORT, BTN_PROFILE],
        ]
    else:
        rows = [
            [BTN_SELECT_BANK],
            [BTN_SUPPORT, BTN_PROFILE],
        ]
    if user_id is not None and is_admin(user_id, tg_user):
        rows.append([BTN_ADMIN_HELP])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def menu_kb(update: Update):
    u = update.effective_user
    return main_menu_keyboard(u.id if u else None, u)


def shop_menu_keyboard():
    """Витрина тарифов: 2 колонки."""
    btns = [(cfg.get("btn") or "").strip() for cfg in SHOP_OFFERS.values() if (cfg.get("btn") or "").strip()]
    rows = []
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    rows.append([BTN_SHOP_REFRESH, BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def shop_method_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_CRYPTOBOT, BTN_USDT_TRC],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def shop_pay_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_SHOP_RECHECK, BTN_BACK],
        ],
        resize_keyboard=True,
    )


def offer_id_by_btn(text: str) -> str | None:
    t = (text or "").strip()
    aliases = {
        "📄 1 чек · 2$": "check1",
        "🧾 1 чек · 2$": "check1",
        "📄 1 чек · 5$": "check1",
        "🧾 1 чек · 5$": "check1",
        "1 чек · 5$": "check1",
        "1 чек · 2$": "check1",
        "День · 50$": "rent_day",
        "Неделя · 200$": "rent_week",
        "Месяц · 500$": "rent_month",
        "☀️ День · 50$": "rent_day",
        "📅 Неделя · 150$": "rent_week",
        "📅 Неделя · 200$": "rent_week",
        "📦 Неделя · 200$": "rent_week",
        "🌙 Месяц · 500$": "rent_month",
        "📦 День · 50$": "rent_day",
        "📦 Неделя · 150$": "rent_week",
        "📦 Месяц · 500$": "rent_month",
    }
    if t in aliases:
        return aliases[t]
    for oid, cfg in SHOP_OFFERS.items():
        btn = (cfg.get("btn") or "").strip()
        title = (cfg.get("title") or "").strip()
        if t == btn or t == title:
            return oid
    return None


def rent_checks_html(user_id: int, *, sep: str | None = None) -> str:
    return rent_checks_text(user_id, html=True, sep=sep)


def shop_catalog_html(user_id: int) -> str:
    status = rent_checks_html(user_id)
    return as_html(
        "<b>Магазин</b>\n"
        "\n"
        f"{status}\n"
        "\n"
        "1 чек — <b>5$</b>\n"
        "День — <b>50$</b>\n"
        "Неделя — <b>200$</b>\n"
        "Месяц — <b>500$</b>\n"
        "\n"
        "Оплата: CryptoBot или TRC-20"
    )


_SHOP_LIVE_GEN: dict[int, int] = {}
_USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
_PAY_LOCK = asyncio.Lock()
_CRYPTOPAY_UPDATES_OK = True
_PAYMENT_WATCH_STARTED = False


def profile_html(user_id: int, full_name: str | None = None) -> str:
    name = html_lib.escape((full_name or "").strip() or "-")
    until = format_rent_until_clock(user_id)
    until_line = f"до {html_lib.escape(until)}\n" if until else ""
    return as_html(
        f"<b>Профиль</b>\n"
        f"\n"
        f"{name}\n"
        f"<code>{user_id}</code>\n"
        f"\n"
        f"{rent_checks_html(user_id)}\n"
        f"{until_line}"
        f"{html_lib.escape(live_banks_line())}"
    )


async def _start_shop_live(bot, chat_id: int, message_id: int, user_id: int, render=None) -> None:
    if not get_rent_until(user_id):
        return
    gen = _SHOP_LIVE_GEN.get(user_id, 0) + 1
    _SHOP_LIVE_GEN[user_id] = gen
    paint = render or shop_catalog_html
    asyncio.create_task(_shop_live_tick(bot, chat_id, message_id, user_id, gen, paint))


async def _edit_live_text(bot, chat_id: int, message_id: int, text: str) -> bool:
    """True = можно тикать дальше. False = сообщение уже нельзя править."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        return True
    except BadRequest as exc:
        err = str(exc).lower()
        if "not modified" in err:
            return True
        return False
    except Forbidden:
        return False
    except Exception:
        logger.exception("live clock edit")
        return True


async def _shop_live_tick(bot, chat_id: int, message_id: int, user_id: int, gen: int, render) -> None:
    while _SHOP_LIVE_GEN.get(user_id) == gen:
        if not get_rent_until(user_id):
            await _edit_live_text(bot, chat_id, message_id, render(user_id))
            await _notify_rent_ended(bot, user_id)
            return
        await _notify_rent_soon(bot, user_id)
        await asyncio.sleep(60)
        if _SHOP_LIVE_GEN.get(user_id) != gen:
            return
        if not await _edit_live_text(bot, chat_id, message_id, render(user_id)):
            return


def unique_trc_sun(price_usdt: float) -> tuple[float, int]:
    """Цена + уникальные микродоли, чтобы два перевода не склеились."""
    base = int(round(float(price_usdt) * 1_000_000))
    pending: set[int] = set()
    for rec in (load_data().get("invoices") or {}).values():
        if not rec or rec.get("pay") != "trc20" or rec.get("credited"):
            continue
        sun = rec.get("amount_sun")
        try:
            pending.add(int(sun if sun is not None else round(float(rec.get("amount_usdt") or 0) * 1_000_000)))
        except (TypeError, ValueError):
            continue
    sun = base + secrets.randbelow(9900) + 100
    for _ in range(80):
        if sun not in pending:
            break
        sun = base + secrets.randbelow(9900) + 100
    return round(sun / 1_000_000, 6), sun


def unique_trc_amount(price_usdt: float) -> float:
    amount, _sun = unique_trc_sun(price_usdt)
    return amount


def consume_trc_txid(txid: str) -> bool:
    if not txid:
        return False
    data = load_data()
    used = data.setdefault("trc_used", [])
    if txid in used:
        return False
    used.append(txid)
    if len(used) > 400:
        data["trc_used"] = used[-300:]
    save_data(data)
    return True


def _trc_want_sun(rec: dict) -> int:
    try:
        sun = rec.get("amount_sun")
        if sun is not None:
            return int(sun)
    except (TypeError, ValueError):
        pass
    try:
        return int(round(float(rec.get("amount_usdt") or 0) * 1_000_000))
    except (TypeError, ValueError):
        return 0


def _trc_created_min_ms(created: str, fallback_sec: int = 3600) -> int:
    try:
        created_dt = datetime.fromisoformat(str(created))
        utc_naive = created_dt - timedelta(hours=3)
        return int((utc_naive.replace(tzinfo=timezone.utc).timestamp() - 120) * 1000)
    except (TypeError, ValueError, OSError):
        return int((datetime.now(timezone.utc).timestamp() - fallback_sec) * 1000)


def _trc20_incoming(address: str, min_ts_ms: int) -> list[dict]:
    addr = (address or "").strip()
    if not addr:
        return []
    url = (
        f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20"
        f"?only_to=true&only_confirmed=true&limit=80&min_timestamp={int(min_ts_ms)}"
        f"&contract_address={_USDT_TRC20_CONTRACT}"
    )
    headers = {"Accept": "application/json", "User-Agent": "ReceiptBot/1.0"}
    api_key = (os.getenv("TRONGRID_API_KEY") or os.getenv("TRON_PRO_API_KEY") or "").strip()
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.exception("trongrid fetch failed")
        return []
    return list(raw.get("data") or [])


def match_trc_txid_from_txs(rec: dict, txs: list[dict], used: set[str]) -> str | None:
    addr = (USDT_ADDRESS or "").strip()
    want = _trc_want_sun(rec)
    if not addr or want <= 0:
        return None
    for tx in txs:
        if tx.get("confirmed") is False:
            continue
        to_addr = str(tx.get("to") or "").strip()
        if to_addr and to_addr != addr:
            continue
        try:
            val = int(tx.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if val != want:
            continue
        txid = str(tx.get("transaction_id") or tx.get("transactionId") or "").strip()
        if not txid or txid in used:
            continue
        return txid
    return None


def find_matching_trc_txid(rec: dict) -> str | None:
    addr = (USDT_ADDRESS or "").strip()
    if not addr or _trc_want_sun(rec) <= 0:
        return None
    min_ms = _trc_created_min_ms(rec.get("created") or "")
    used = set(load_data().get("trc_used") or [])
    return match_trc_txid_from_txs(rec, _trc20_incoming(addr, min_ms), used)


def iter_unpaid_invoices(*, max_age_hours: float = 8.0):
    now = now_msk()
    max_age = timedelta(hours=max_age_hours)
    for iid, rec in (load_data().get("invoices") or {}).items():
        if not rec or rec.get("credited"):
            continue
        created = rec.get("created") or ""
        try:
            dt = datetime.fromisoformat(str(created))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            if now - dt > max_age:
                continue
        except (TypeError, ValueError, OSError):
            pass
        yield str(iid), rec


def admin_help_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ADMIN_USERS, BTN_ADMIN_STATS],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def tools_keyboard(user_id: int | None = None, tg_user=None):
    """legacy - сразу выбор банка"""
    return checks_keyboard(user_id, tg_user)


def checks_keyboard(user_id: int | None = None, tg_user=None):
    """Публично — Альфа и Сбер. Админам ещё Т-Банк и Уралсиб."""
    if is_admin(user_id, tg_user) or is_kronlead(user_id, tg_user):
        return ReplyKeyboardMarkup(
            [
                [BTN_SBER_OLD, BTN_TBANK_OLD],
                [BTN_ALFA, BTN_URALSIB_PLAIN],
                [BTN_BACK],
            ],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup(
        [
            [BTN_SBER_OLD, BTN_TBANK],
            [BTN_ALFA, BTN_URALSIB],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def checks_kb(update: Update | None = None, user_id: int | None = None, tg_user=None):
    if update is not None and update.effective_user is not None:
        u = update.effective_user
        return checks_keyboard(u.id, u)
    return checks_keyboard(user_id, tg_user)


async def send_bank_wip(update: Update, bank_name: str = "") -> int:
    name = (bank_name or "Этот банк").strip()
    user = update.effective_user
    await update.effective_message.reply_text(
        f"{name} скоро.\nОсталось чуть-чуть — напишем, когда откроем.",
        reply_markup=checks_kb(update),
    )
    return CHECKS_MENU


def form_keyboard():
    return ReplyKeyboardMarkup([[BTN_FORM_EXAMPLE, BTN_BACK]], resize_keyboard=True)


def back_keyboard():
    """Кнопка назад"""
    return ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True)


def home_keyboard():
    """После успешной оплаты - на главную."""
    return ReplyKeyboardMarkup([[BTN_HOME]], resize_keyboard=True)


def tbank_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_TBANK_SBP, BTN_TBANK_PHONE],
            [BTN_TBANK_CARD_OTHER, BTN_TBANK_CARD_NOCOMM],
            [BTN_TBANK_CARD_TBANK],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def otp_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_OTP_SBP, BTN_OTP_CARD],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def ozon_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_OZON_SBP],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def sber_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_SBER_SBP, BTN_SBER_PHONE],
            [BTN_SBER_CARD_OTHER],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


ALFA_METHOD_BUTTONS = (
    BTN_ALFA_SBP,
    BTN_ALFA_CARD,
    BTN_ALFA_PHONE,
    BTN_ALFA_PHONE_OLD,
    BTN_ALFA_STATEMENT,
)


def alfa_only_keyboard():
    """Только способы генерации Альфа-Банка."""
    return ReplyKeyboardMarkup(
        [
            [BTN_ALFA_SBP, BTN_ALFA_CARD],
            [BTN_ALFA_PHONE, BTN_ALFA_STATEMENT],
        ],
        resize_keyboard=True,
    )


def alfa_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ALFA_SBP, BTN_ALFA_CARD],
            [BTN_ALFA_PHONE_OLD, BTN_ALFA_STATEMENT],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


ALFA_ONLY_MENU_TEXT = "🔴 Альфа-Банк\n\n📌 Выберите способ перевода"


async def show_alfa_only_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_main_menu(update, context)
    return MAIN_MENU


async def alfa_only_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Чужое меню для alfa-only закрыто. Кнопки Альфа уходят в её обработчик."""
    user = update.effective_user
    if not user or not is_alfa_only(user.id):
        return None
    text = pressed_text(update)
    if text in ALFA_METHOD_BUTTONS:
        return await alfa_submenu_handler(update, context)
    return await show_alfa_only_menu(update, context)


_ALFA_ONLY_FOREIGN = None


def _alfa_only_foreign_buttons() -> frozenset[str]:
    global _ALFA_ONLY_FOREIGN
    if _ALFA_ONLY_FOREIGN is None:
        _ALFA_ONLY_FOREIGN = frozenset({
            BTN_SBER, BTN_VTB, BTN_OZON, BTN_TBANK, BTN_OTP, BTN_URALSIB, BTN_URALSIB_OLD, BTN_ALFA,
            BTN_SELECT_BANK, BTN_BALANCE_CHECKS, BTN_BALANCE_CHECKS_OLD, BTN_BALANCE, BTN_PACKAGES,
            BTN_PROFILE, BTN_HOME, BTN_CHECKS, BTN_BACK,
            BTN_SBER_PHONE, BTN_SBER_CARD_OTHER,
            BTN_TBANK_PHONE, BTN_TBANK_CARD_OTHER, BTN_TBANK_CARD_NOCOMM,
            BTN_TBANK_CARD_TBANK, BTN_TBANK_STATEMENT,
            BTN_OTP_SBP, BTN_OTP_CARD, BTN_OZON_SBP,
            BTN_ADMIN_HELP, BTN_SUPPORT, BTN_SUPPORT_OLD, BTN_HELP, BTN_CLEAR,
            BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW,
            BTN_DOC_DRAW, BTN_AI, BTN_PROMO, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_CRYPTOBOT,
            BTN_USDT_TRC,
            BTN_TBANK_CARD_OTHER,
        })
    return _ALFA_ONLY_FOREIGN


async def reject_alfa_only_foreign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Старые кнопки Сбера/Т-Банка и главное меню у alfa-only не работают."""
    user = update.effective_user
    if user is None or is_admin(user.id, user) or not is_alfa_only(user.id):
        return
    text = pressed_text(update)
    if not text or text in ALFA_METHOD_BUTTONS:
        return
    if text not in _alfa_only_foreign_buttons():
        return
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    await show_alfa_only_menu(update, context)
    raise ApplicationHandlerStop


def packages_keyboard():
    """Только пакеты чеков (без промокода)."""
    rows = []
    for cfg in CHECK_PACKAGES.values():
        rows.append([cfg.get("btn") or f"📦 {cfg['name']} - {cfg['price_usdt']}$"])
    rows.append([BTN_BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def packages_need_topup_keyboard():
    """Не хватает баланса - предложить пополнить."""
    return ReplyKeyboardMarkup(
        [
            [BTN_BALANCE],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def balance_keyboard():
    """Способ пополнения: Cryptobot / USDT TRC-20."""
    return ReplyKeyboardMarkup(
        [
            [BTN_USDT_TRC, BTN_CRYPTOBOT],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def profile_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_BACK]],
        resize_keyboard=True,
    )


def pack_id_by_btn(text: str) -> str | None:
    t = (text or "").strip().replace("\u2014", "-").replace("\u2013", "-")
    for pid, cfg in CHECK_PACKAGES.items():
        btn = (cfg.get("btn") or "").strip()
        name = (cfg.get("name") or "").strip()
        if t == btn or t == name or t == f"📦 {name}":
            return pid
        if t == f"📦 {name} - {cfg['price_usdt']}$":
            return pid
        if t == f"{name} - {cfg['price_usdt']}$":
            return pid
    return None


def parse_topup_preset(text: str) -> int | None:
    m = re.match(r"^💵 \+(\d+) USDT$", (text or "").strip())
    if not m:
        return None
    return int(m.group(1))


_BTN_ALIASES = {
    "Банк": "🏦 Выбрать банк",
    "Профиль": "💼 Мой профиль",
    "Магазин": "🏪 Магазин",
    "Поддержка": "💀 Поддержка",
    "💬 Поддержка": "💀 Поддержка",
    "CryptoBot": "🤖 CryptoBot",
    "Cryptobot": "🤖 CryptoBot",
    "🤖 Cryptobot": "🤖 CryptoBot",
    "TRC-20": "💵 USDT TRC-20",
    "💵 USDT (trc-20)": "💵 USDT TRC-20",
    "Обновить": "🔄 Обновить",
    "Проверить повторно": "🔄 Проверить повторно",
    "Пример": "📋 Пример",
}


def pressed_text(update: Update) -> str:
    """Текст нажатой кнопки / сообщения."""
    raw = ""
    if update.callback_query and update.callback_query.data:
        raw = update.callback_query.data
    elif update.message and update.message.text:
        raw = update.message.text
    text = normalize_btn_text(raw)
    return _BTN_ALIASES.get(text, text)


async def ack_press(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = MAIN_MENU_TEXT):
    uid = update.effective_user.id if update.effective_user else None
    if uid and is_alfa_only(uid):
        await show_alfa_only_menu(update, context)
        return
    msg = with_status(uid, text) if uid else text
    await update.effective_message.reply_text(
        msg,
        reply_markup=main_menu_keyboard(uid, update.effective_user),
    )


async def send_with_back(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Вторичный экран: только «Назад» под клавиатурой."""
    await update.effective_message.reply_text(
        text,
        reply_markup=back_keyboard(),
        **kwargs,
    )


# ========== ОБРАБОТЧИКИ ==========

async def ask_for_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """PIN снят - сразу главное меню. Бан остаётся закрытым."""
    user = update.effective_user
    if user:
        remember_tg_user(user.id, user)
        if is_banned(user.id) and not is_admin(user.id, user):
            await update.effective_message.reply_text(
                LOCK_NOTICE,
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END
    await send_main_menu(update, context)
    return MAIN_MENU


async def reject_if_banned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрет админа: бот молча недоступен, PIN не помогает."""
    user = update.effective_user
    if user is None or is_admin(user.id, user) or not is_banned(user.id):
        return
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    if update.effective_message:
        await update.effective_message.reply_text(
            LOCK_NOTICE,
            reply_markup=ReplyKeyboardRemove(),
        )
    raise ApplicationHandlerStop


async def pin_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """PIN снят."""
    return await start(update, context)


async def deny_without_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if has_bot_access(update.effective_user.id, update.effective_user):
        return
    await update.effective_message.reply_text(
        LOCK_NOTICE,
        reply_markup=ReplyKeyboardRemove(),
    )


async def callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline-кнопка без активной сессии (после рестарта и т.п.)."""
    await ack_press(update)
    user_id = update.effective_user.id
    if not has_bot_access(user_id, update.effective_user):
        await update.effective_message.reply_text(
            LOCK_NOTICE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    text = pressed_text(update)
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    if text in (
        BTN_SELECT_BANK, BTN_PROFILE,
        BTN_SUPPORT, BTN_HELP, BTN_CLEAR, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES,
        BTN_SHOP_REFRESH, BTN_SHOP_RECHECK,
    ):
        return await main_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)
    if text in (
        BTN_TBANK_SBP, BTN_TBANK_PHONE, BTN_TBANK_CARD_OTHER,
        BTN_TBANK_CARD_NOCOMM, BTN_TBANK_CARD_TBANK, BTN_TBANK_STATEMENT,
    ):
        return await tbank_submenu_handler(update, context)
    if text in (BTN_OTP_SBP, BTN_OTP_CARD):
        return await otp_submenu_handler(update, context)
    if text in (BTN_OZON_SBP,):
        return await ozon_submenu_handler(update, context)
    if text in (BTN_SBER_SBP, BTN_SBER_PHONE, BTN_SBER_CARD_OTHER):
        return await sber_submenu_handler(update, context)
    if text in ALFA_METHOD_BUTTONS:
        return await alfa_submenu_handler(update, context)
    if text == BTN_BACK:
        await update.effective_message.reply_text(
            MAIN_MENU_TEXT,
            reply_markup=menu_kb(update),
        )
        return MAIN_MENU

    await update.effective_message.reply_text(
        MAIN_MENU_TEXT,
        reply_markup=menu_kb(update),
    )
    return MAIN_MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню / PIN."""
    context.user_data.clear()
    user_id = update.effective_user.id
    if update.effective_user:
        remember_tg_user(user_id, update.effective_user)

    if is_banned(user_id) and not is_admin(user_id, update.effective_user):
        await update.effective_message.reply_text(
            LOCK_NOTICE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    get_user_data(user_id)
    await send_main_menu(update, context)
    return MAIN_MENU


async def resume_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start(update, context)


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню"""
    await ack_press(update)
    text = pressed_text(update)
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated

    if text == BTN_BACK or text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU

    if text == BTN_ADMIN_HELP:
        if not is_admin(update.effective_user.id, update.effective_user):
            await send_main_menu(update, context)
            return MAIN_MENU
        await update.effective_message.reply_text(
            ADMIN_HELP_TEXT,
            reply_markup=admin_help_keyboard(),
        )
        return MAIN_MENU

    if text == BTN_ADMIN_USERS:
        if not is_admin(update.effective_user.id, update.effective_user):
            await send_main_menu(update, context)
            return MAIN_MENU
        return await admin_users(update, context) or MAIN_MENU

    if text == BTN_ADMIN_STATS:
        if not is_admin(update.effective_user.id, update.effective_user):
            await send_main_menu(update, context)
            return MAIN_MENU
        return await admin_stats(update, context) or MAIN_MENU
    
    if text == BTN_SELECT_BANK:
        u = update.effective_user
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
        )
        return CHECKS_MENU

    elif text == BTN_PROFILE:
        return await show_profile(update, context)

    elif text in (BTN_BALANCE_CHECKS, BTN_BALANCE_CHECKS_OLD, BTN_SHOP_REFRESH, BTN_PACKAGES, BTN_BALANCE):
        if not can_see_shop(update.effective_user.id, update.effective_user):
            await send_main_menu(update, context)
            return MAIN_MENU
        return await show_shop_menu(update, context)
    elif text == BTN_SHOP_RECHECK:
        return await recheck_shop_trc(update, context)

    elif text in (BTN_SUPPORT, BTN_SUPPORT_OLD):
        await update.effective_message.reply_text(
            as_html(
                "<b>Поддержка</b>\n"
                "\n"
                "Вопросы и косяки - <a href=\"https://t.me/kronlead\">@kronlead</a>"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✉️ Написать", url="https://t.me/kronlead")]]
            ),
        )
        return MAIN_MENU
    
    elif text == BTN_HELP:
        await send_with_back(
            update,
            context,
            "📖 *Уведомление*\n\n"
            "Данный бот создан исключительно в развлекательных целях.\n\n"
            "⚠️ *Запрещено использование в целях мошенничества!*\n\n"
            "Поддержка: @kronlead",
            parse_mode="Markdown",
        )
        return MAIN_MENU
    
    elif text == BTN_CLEAR:
        return await clear_chat(update, context)

    await send_main_menu(update, context)
    return MAIN_MENU


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Профиль пользователя"""
    await ack_press(update)
    user_id = update.effective_user.id
    if not has_bot_access(user_id, update.effective_user):
        await deny_without_access(update, context)
        return ConversationHandler.END
    if is_alfa_only(user_id):
        return await show_alfa_only_menu(update, context)
    user = update.effective_user
    if update.effective_user:
        remember_tg_user(user_id, update.effective_user)

    full_name = user.full_name or ""
    sent = await update.effective_message.reply_text(
        profile_html(user_id, full_name),
        reply_markup=profile_keyboard(),
    )
    await _start_shop_live(
        context.bot,
        sent.chat_id,
        sent.message_id,
        user_id,
        lambda uid, _n=full_name: profile_html(uid, _n),
    )
    return MAIN_MENU


async def show_shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Витрина: 1 чек / аренда. Пока только админы."""
    user = update.effective_user
    if not has_bot_access(user.id, user):
        await deny_without_access(update, context)
        return ConversationHandler.END
    if not can_see_shop(user.id, user):
        await send_main_menu(update, context)
        return MAIN_MENU
    context.user_data.pop("shop_offer", None)
    sent = await update.effective_message.reply_text(
        shop_catalog_html(user.id),
        reply_markup=shop_menu_keyboard(),
    )
    await _start_shop_live(context.bot, sent.chat_id, sent.message_id, user.id)
    return SHOP_MENU


async def shop_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    await ack_press(update)
    user = update.effective_user
    if not can_see_shop(user.id, user):
        await send_main_menu(update, context)
        return MAIN_MENU
    text = pressed_text(update)
    if text == BTN_BACK or text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text in (BTN_SHOP_REFRESH, BTN_BALANCE_CHECKS, BTN_BALANCE_CHECKS_OLD):
        return await show_shop_menu(update, context)
    if text == BTN_SHOP_RECHECK:
        return await recheck_shop_trc(update, context)
    if text in (BTN_SELECT_BANK, BTN_PROFILE, BTN_ADMIN_HELP):
        return await main_menu_handler(update, context)
    offer_id = offer_id_by_btn(text)
    if offer_id:
        return await show_shop_method(update, context, offer_id)
    await show_shop_menu(update, context)
    return SHOP_MENU


async def show_shop_method(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: str) -> int:
    if offer_id not in SHOP_OFFERS:
        return await show_shop_menu(update, context)
    context.user_data["shop_offer"] = offer_id
    context.user_data.pop("shop_invoice_shown", None)
    cfg = SHOP_OFFERS[offer_id]
    title = html_lib.escape(str(cfg.get("title") or offer_id))
    price = int(cfg.get("price_usdt") or 0)
    await update.effective_message.reply_text(
        as_html(
            f"{title}\n"
            f"Сумма - <b>{price}</b> USDT\n"
            f"\n"
            f"Выберите способ оплаты"
        ),
        reply_markup=shop_method_keyboard(),
    )
    return WAITING_SHOP_PAY


async def show_shop_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: str) -> int:
    return await show_shop_method(update, context, offer_id)


async def shop_pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    await ack_press(update)
    user = update.effective_user
    if not can_see_shop(user.id, user):
        await send_main_menu(update, context)
        return MAIN_MENU
    text = pressed_text(update)
    if text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text == BTN_BACK:
        if context.user_data.get("shop_invoice_shown") and context.user_data.get("shop_offer"):
            return await show_shop_method(update, context, str(context.user_data.get("shop_offer")))
        return await show_shop_menu(update, context)
    if text == BTN_SHOP_RECHECK:
        return await recheck_shop_trc(update, context)
    picked = offer_id_by_btn(text)
    if picked:
        return await show_shop_method(update, context, picked)
    offer_id = context.user_data.get("shop_offer")
    if not offer_id or offer_id not in SHOP_OFFERS:
        return await show_shop_menu(update, context)
    if text in (BTN_CRYPTOBOT, BTN_CRYPTOBOT_OLD):
        return await start_shop_cryptobot(update, context, offer_id)
    if text in (BTN_USDT_TRC, BTN_USDT_TRC_OLD, BTN_USDT_TRC_OLD2):
        return await start_shop_trc(update, context, offer_id)
    return await show_shop_method(update, context, offer_id)


async def start_shop_cryptobot(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: str) -> int:
    context.user_data["shop_offer"] = offer_id
    cfg = SHOP_OFFERS[offer_id]
    price = float(cfg["price_usdt"])
    title = cfg.get("title") or offer_id
    return await create_and_send_invoice(
        update,
        context,
        kind=offer_id,
        amount_usdt=price,
        description=f"{title} · {int(price)} USDT",
    )


async def start_shop_trc(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: str) -> int:
    context.user_data["shop_offer"] = offer_id
    cfg = SHOP_OFFERS[offer_id]
    addr = (USDT_ADDRESS or "").strip()
    if not addr:
        await update.effective_message.reply_text(
            "⚠️ USDT TRC-20 адрес не задан.",
            reply_markup=shop_pay_keyboard(),
        )
        return WAITING_SHOP_PAY
    price = float(cfg["price_usdt"])
    amount, sun = unique_trc_sun(price)
    order_id = "trc" + secrets.token_hex(8)
    user_id = update.effective_user.id
    context.user_data["trc_order_id"] = order_id
    context.user_data["shop_invoice_shown"] = True
    save_invoice_record(
        order_id,
        {
            "user_id": user_id,
            "kind": offer_id,
            "amount_usdt": amount,
            "amount_sun": sun,
            "list_price": price,
            "pay": "trc20",
            "credited": False,
            "created": now_msk().isoformat(),
        },
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Я оплатил", callback_data=f"trcpaid:{order_id}")]]
    )
    title = cfg.get("title") or offer_id
    await update.effective_message.reply_text(
        as_html(
            f"{html_lib.escape(title)}\n"
            f"\n"
            f"Адрес - <code>{html_lib.escape(addr)}</code>\n"
            f"Сумма - <code>{amount:.6f}</code> USDT\n"
            f"\n"
            f"В случае какой-либо проблемы напишите <a href=\"https://t.me/kronlead\">@kronlead</a>"
        ),
        reply_markup=kb,
    )
    await update.effective_message.reply_text(
        "Ожидаю оплату.",
        reply_markup=shop_pay_keyboard(),
    )
    chat_id = update.effective_chat.id
    asyncio.create_task(_poll_trc_paid(context.bot, chat_id, order_id, user_id))
    return WAITING_SHOP_PAY


def after_pay_state(user_id: int, tg_user=None) -> int:
    return CHECKS_MENU if can_create_check(user_id, tg_user) else SHOP_MENU


async def _announce_shop_paid(bot, chat_id: int, user_id: int, msg: str) -> None:
    try:
        can = can_create_check(user_id)
        until = format_rent_until_clock(user_id)
        extra = "Дальше — выбрать банк и собрать чек." if can else "Чтобы собирать чеки, купите пакет."
        bits = [msg]
        if until:
            bits.append(f"до {until}")
        bits.append(extra)
        kb = checks_keyboard(user_id) if can else shop_menu_keyboard()
        await bot.send_message(chat_id, "\n\n".join(bits), reply_markup=kb)
    except Exception:
        logger.exception("announce shop paid")


async def _credit_and_announce(bot, invoice_id: str, rec: dict | None = None, invoice: dict | None = None) -> bool:
    rec = rec or get_invoice_record(invoice_id)
    if not rec:
        return False
    already = bool(rec.get("credited"))
    if already:
        return False
    pay = (rec.get("pay") or "cryptobot").lower()
    if pay == "trc20":
        ok, msg = await fulfill_trc_invoice(bot, invoice_id, force=False)
    else:
        ok, msg = await fulfill_paid_invoice(bot, invoice_id, invoice)
    if not ok:
        return False
    if already or msg == "✅ Уже зачислено.":
        return False
    uid = int(rec.get("user_id") or 0)
    if uid:
        await _announce_shop_paid(bot, uid, uid, msg)
    logger.info("auto-credit id=%s pay=%s user=%s", invoice_id, pay, uid)
    return True


async def _poll_cryptobot_paid(bot, chat_id: int, invoice_id: str, user_id: int) -> None:
    if not (cryptopay and cryptopay.is_configured()):
        return
    try:
        for _ in range(240):
            await asyncio.sleep(8)
            rec = get_invoice_record(invoice_id)
            if not rec or rec.get("credited"):
                return
            try:
                inv = await asyncio.to_thread(cryptopay.get_invoice, invoice_id)
            except Exception:
                continue
            if (inv or {}).get("status", "").lower() != "paid":
                continue
            await _credit_and_announce(bot, invoice_id, rec, inv)
            return
    except Exception:
        logger.exception("cryptobot poll")


async def _poll_trc_paid(bot, chat_id: int, order_id: str, user_id: int) -> None:
    try:
        for _ in range(240):
            await asyncio.sleep(8)
            rec = get_invoice_record(order_id)
            if not rec or rec.get("credited"):
                return
            if await _credit_and_announce(bot, order_id, rec):
                return
    except Exception:
        logger.exception("trc poll")


def _cryptopay_offset() -> int:
    try:
        return int(load_data().get("cryptopay_offset") or 0)
    except (TypeError, ValueError):
        return 0


def _save_cryptopay_offset(offset: int) -> None:
    data = load_data()
    data["cryptopay_offset"] = int(offset)
    save_data(data)


async def _drain_cryptopay_updates(bot) -> None:
    global _CRYPTOPAY_UPDATES_OK
    if not _CRYPTOPAY_UPDATES_OK or not (cryptopay and cryptopay.is_configured()):
        return
    try:
        offset = _cryptopay_offset()
        updates = await asyncio.to_thread(
            lambda: cryptopay.get_updates(offset=offset or None, timeout=0, limit=50)
        )
    except Exception as exc:
        logger.warning("cryptopay getUpdates skip: %s", exc)
        return
    max_id = offset
    for upd in updates or []:
        try:
            uid = int(upd.get("update_id") or 0)
        except (TypeError, ValueError):
            uid = 0
        if uid:
            max_id = max(max_id, uid)
        ut = str(upd.get("update_type") or upd.get("type") or "").lower()
        if ut != "invoice_paid":
            continue
        inv = upd.get("payload") if isinstance(upd.get("payload"), dict) else {}
        iid = inv.get("invoice_id")
        if iid:
            await _credit_and_announce(bot, str(iid), invoice=inv)
    if max_id and max_id != offset:
        _save_cryptopay_offset(max_id + 1)


async def _sweep_cryptobot_unpaid(bot) -> None:
    if not (cryptopay and cryptopay.is_configured()):
        return
    crypto_ids = [
        iid for iid, rec in iter_unpaid_invoices()
        if (rec.get("pay") or "cryptobot").lower() != "trc20"
    ]
    if not crypto_ids:
        return
    try:
        items = await asyncio.to_thread(
            lambda: cryptopay.get_invoices(invoice_ids=",".join(crypto_ids[:100]), count=100)
        )
    except Exception:
        logger.exception("cryptobot sweep getInvoices")
        return
    paid = {}
    for inv in items or []:
        if str((inv or {}).get("status") or "").lower() == "paid":
            paid[str(inv.get("invoice_id"))] = inv
    for iid in crypto_ids:
        inv = paid.get(str(iid))
        if inv:
            await _credit_and_announce(bot, iid, invoice=inv)


async def _sweep_trc_unpaid(bot) -> None:
    addr = (USDT_ADDRESS or "").strip()
    pending = [
        (iid, rec) for iid, rec in iter_unpaid_invoices()
        if rec.get("pay") == "trc20"
    ]
    if not addr or not pending:
        return
    min_ms = min(_trc_created_min_ms(rec.get("created") or "") for _iid, rec in pending)
    txs = await asyncio.to_thread(_trc20_incoming, addr, min_ms)
    if not txs:
        return
    used = set(load_data().get("trc_used") or [])
    for iid, rec in pending:
        txid = match_trc_txid_from_txs(rec, txs, used)
        if not txid:
            continue
        ok, msg = await fulfill_trc_invoice(bot, iid, force=False, txid=txid)
        if ok:
            used.add(txid)
            uid = int(rec.get("user_id") or 0)
            if uid:
                await _announce_shop_paid(bot, uid, uid, msg)
            logger.info("auto-credit id=%s pay=trc20 user=%s tx=%s", iid, uid, txid[:16])


async def _payment_watch_loop(bot) -> None:
    await asyncio.sleep(4)
    logger.info(
        "payment watch start cryptobot=%s trc=%s",
        bool(cryptopay and cryptopay.is_configured()),
        bool((USDT_ADDRESS or "").strip()),
    )
    while True:
        try:
            await _drain_cryptopay_updates(bot)
            await _sweep_cryptobot_unpaid(bot)
            await _sweep_trc_unpaid(bot)
            await _sweep_expired_rent(bot)
        except Exception:
            logger.exception("payment watch")
        await asyncio.sleep(8)


def start_payment_watch(application) -> None:
    global _PAYMENT_WATCH_STARTED
    if _PAYMENT_WATCH_STARTED:
        return
    _PAYMENT_WATCH_STARTED = True
    try:
        application.create_task(_payment_watch_loop(application.bot))
    except Exception:
        asyncio.create_task(_payment_watch_loop(application.bot))


async def _notify_trc_pending(bot, order_id: str) -> None:
    rec = get_invoice_record(order_id) or {}
    uid = int(rec.get("user_id") or 0)
    who = tg_user_label(user_id=uid)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Зачислить", callback_data=f"trcok:{order_id}")]]
    )
    text = (
        f"💵 TRC-20 заявка\n"
        f"Юзер: {who} (`{uid}`)\n"
        f"Сумма: {rec.get('amount_usdt')} USDT\n"
        f"{rec.get('kind')}\n"
        f"Зачисление само, когда перевод появится в сети."
    )
    for admin_id in admin_recipient_ids():
        try:
            await bot.send_message(int(admin_id), text, reply_markup=kb)
        except Exception:
            pass


def _credit_shop_kind(user_id: int, kind: str, coins: int) -> tuple[str, str]:
    if kind in SHOP_OFFERS:
        msg = grant_shop_offer(user_id, kind)
        return msg, f"shop {kind}"
    if kind in CHECK_PACKAGES:
        checks = int(CHECK_PACKAGES[kind]["checks"])
        add_check_credits(user_id, checks)
        msg = f"✅ Оплата получена: +{checks} чек(ов) в пакет."
        return msg, f"pack {kind} +{checks} checks"
    credit_deposit(user_id, coins)
    msg = f"✅ Оплата получена: +{coins} $ на баланс."
    return msg, f"balance +{coins}"


async def show_packages_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Старое меню пакетов - сразу в витрину."""
    return await show_shop_menu(update, context)


async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пополнение кошелька не нужно: оплата тарифа в магазине."""
    return await show_shop_menu(update, context)


async def ask_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """После Cryptobot - ввод суммы."""
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        with_status(uid, "📄 Введите сумму пополнения в USDT"),
        reply_markup=back_keyboard(),
    )
    return WAITING_TOPUP_AMOUNT


def _shop_keyboard_for_kind(kind: str):
    if kind in SHOP_OFFERS:
        return shop_pay_keyboard()
    if kind in CHECK_PACKAGES:
        return packages_keyboard()
    return balance_keyboard()


def _shop_state_for_kind(kind: str) -> int:
    if kind in SHOP_OFFERS:
        return WAITING_SHOP_PAY
    if kind in CHECK_PACKAGES:
        return PACKAGES_MENU
    return BALANCE_MENU


async def create_and_send_invoice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    amount_usdt: float,
    description: str,
) -> int:
    user_id = update.effective_user.id
    if kind not in SHOP_OFFERS:
        return await show_shop_menu(update, context)
    cfg = SHOP_OFFERS[kind]
    list_price = float(cfg.get("price_usdt") or 0)
    amount_usdt = float(list_price)
    title = str(cfg.get("title") or kind)
    shop_kb = shop_method_keyboard()
    shop_state = WAITING_SHOP_PAY
    if not (cryptopay and cryptopay.is_configured()):
        await update.effective_message.reply_text(
            "CryptoBot сейчас недоступен. Оплатите USDT TRC-20 или напишите @kronlead",
            reply_markup=shop_kb,
        )
        return shop_state
    payload = json.dumps(
        {"uid": int(user_id), "kind": kind, "usdt": amount_usdt},
        ensure_ascii=False,
    )
    try:
        inv = await asyncio.to_thread(
            lambda: cryptopay.create_invoice(
                amount_usdt=amount_usdt,
                description=description or f"{title} · {int(amount_usdt)} USDT",
                payload=payload,
            )
        )
    except Exception:
        logger.exception("create_invoice failed")
        await update.effective_message.reply_text(
            "Не удалось создать счёт CryptoBot. Попробуйте ещё раз или USDT TRC-20.",
            reply_markup=shop_kb,
        )
        return shop_state

    invoice_id = inv.get("invoice_id")
    pay_url = cryptopay.invoice_pay_url(inv) if cryptopay else ""
    if not invoice_id or not pay_url:
        await update.effective_message.reply_text(
            "Не удалось создать счёт CryptoBot. Попробуйте USDT TRC-20.",
            reply_markup=shop_kb,
        )
        return shop_state
    save_invoice_record(
        invoice_id,
        {
            "user_id": user_id,
            "kind": kind,
            "amount_usdt": amount_usdt,
            "list_price": list_price,
            "pay": "cryptobot",
            "credited": False,
            "created": now_msk().isoformat(),
        },
    )
    context.user_data["crypto_order_id"] = str(invoice_id)
    context.user_data["shop_invoice_shown"] = True
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Оплатить", url=pay_url)],
            [InlineKeyboardButton("🔄 Проверить повторно", callback_data=f"paycheck:{invoice_id}")],
        ]
    )
    await update.effective_message.reply_text(
        as_html(
            f"{html_lib.escape(title)}\n"
            f"\n"
            f"CryptoBot\n"
            f"Сумма - <code>{amount_usdt:.2f}</code> USDT\n"
            f"\n"
            f"Оплата зачислится сама. Если нет — нажмите «Проверить повторно».\n"
            f"\n"
            f"В случае какой-либо проблемы напишите <a href=\"https://t.me/kronlead\">@kronlead</a>"
        ),
        reply_markup=kb,
    )
    await update.effective_message.reply_text(
        "Ожидаю оплату.",
        reply_markup=shop_pay_keyboard(),
    )
    chat_id = update.effective_chat.id
    asyncio.create_task(_poll_cryptobot_paid(context.bot, chat_id, str(invoice_id), user_id))
    return shop_state


def _cryptobot_payload_ok(invoice: dict, rec: dict) -> bool:
    raw = invoice.get("payload")
    if not raw:
        return True
    try:
        pl = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if not isinstance(pl, dict):
        return True
    try:
        puid = int(pl.get("uid") or 0)
    except (TypeError, ValueError):
        puid = 0
    try:
        ruid = int(rec.get("user_id") or 0)
    except (TypeError, ValueError):
        ruid = 0
    if puid and ruid and puid != ruid:
        return False
    pkind = str(pl.get("kind") or "")
    rkind = str(rec.get("kind") or "")
    if pkind and rkind and pkind != rkind:
        return False
    return True


async def fulfill_paid_invoice(bot, invoice_id: str, invoice: dict | None = None) -> tuple[bool, str]:
    """Зачислить оплату по invoice_id. (ok, message)."""
    rec = get_invoice_record(invoice_id)
    if not rec:
        return False, "Счёт не найден."
    if rec.get("credited"):
        return True, "✅ Уже зачислено."
    if (rec.get("pay") or "") == "trc20":
        return await fulfill_trc_invoice(bot, invoice_id, force=False)
    if not (cryptopay and cryptopay.is_configured()):
        return False, "CryptoBot сейчас недоступен."
    try:
        live = await asyncio.to_thread(cryptopay.get_invoice, invoice_id)
    except Exception:
        logger.exception("cryptobot get_invoice")
        return False, "Не удалось проверить оплату. Подождите минуту."
    if not live:
        return False, "Счёт не найден в CryptoBot."
    status = str(live.get("status") or "").lower()
    if status != "paid":
        return False, "Оплата ещё не прошла."
    asset = str(live.get("paid_asset") or live.get("asset") or "USDT").upper()
    if asset and asset != "USDT":
        return False, "Оплата должна быть в USDT."
    if not _cryptobot_payload_ok(live, rec):
        logger.warning("cryptobot payload mismatch id=%s", invoice_id)
        return False, "Счёт не совпадает. Напишите @kronlead"
    try:
        paid_amt = float(live.get("paid_amount") or live.get("amount") or 0)
    except (TypeError, ValueError):
        paid_amt = 0.0
    kind = rec.get("kind") or ""
    try:
        need = float(rec.get("list_price") or rec.get("amount_usdt") or 0)
    except (TypeError, ValueError):
        need = 0.0
    if kind in SHOP_OFFERS:
        try:
            need = max(need, float(SHOP_OFFERS[kind].get("price_usdt") or 0))
        except (TypeError, ValueError):
            pass
    if need <= 0 or paid_amt + 0.02 < need:
        return False, "Сумма оплаты меньше тарифа."
    if kind not in SHOP_OFFERS and kind not in CHECK_PACKAGES:
        return False, "Этот счёт больше не принимается."

    user_id = int(rec["user_id"])
    async with _PAY_LOCK:
        rec = get_invoice_record(invoice_id)
        if not rec:
            return False, "Счёт не найден."
        if rec.get("credited"):
            return True, "✅ Уже зачислено."
        if not mark_invoice_credited(invoice_id):
            return True, "✅ Уже зачислено."
        list_price = float(rec.get("list_price") or need)
        coins = max(1, int(round(list_price * COINS_PER_USDT)))
        msg, detail = _credit_shop_kind(user_id, kind, coins)

    await notify_admins_payment(
        bot,
        method="CryptoBot",
        user_id=user_id,
        kind=kind,
        amount=paid_amt,
        detail=detail,
    )
    return True, msg


async def fulfill_trc_invoice(bot, order_id: str, *, force: bool = False, txid: str | None = None) -> tuple[bool, str]:
    rec = get_invoice_record(order_id)
    if not rec:
        return False, "Счёт не найден."
    if rec.get("credited"):
        return False, "Уже зачислено ранее."
    if not force:
        found = txid or await asyncio.to_thread(find_matching_trc_txid, rec)
        if not found:
            return False, "Перевод ещё не виден в сети TRC-20. Обычно 1-2 минуты."
        txid = found

    async with _PAY_LOCK:
        rec = get_invoice_record(order_id)
        if not rec:
            return False, "Счёт не найден."
        if rec.get("credited"):
            return False, "Уже зачислено ранее."
        if not mark_invoice_credited(order_id):
            return False, "Уже зачислено ранее."
        if txid:
            consume_trc_txid(str(txid))
        user_id = int(rec["user_id"])
        kind = rec.get("kind") or "balance"
        list_price = float(rec.get("list_price") or rec.get("amount_usdt") or 0)
        coins = max(1, int(round(list_price * COINS_PER_USDT)))
        msg, detail = _credit_shop_kind(user_id, kind, coins)

    how = "USDT TRC-20, вручную" if force else "USDT TRC-20"
    await notify_admins_payment(
        bot,
        method=how,
        user_id=user_id,
        kind=kind,
        amount=rec.get("amount_usdt"),
        detail=detail,
    )
    return True, msg


async def packages_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Покупка пакетов с баланса профиля."""
    await ack_press(update)
    text = pressed_text(update)
    if text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text == BTN_BACK or text == BTN_BALANCE_CHECKS:
        return await show_shop_menu(update, context)
    if text == BTN_BALANCE:
        return await show_balance_menu(update, context)

    pack_id = pack_id_by_btn(text)
    if pack_id:
        await update.effective_message.reply_text(
            "Покупка за внутренний баланс отключена.\nОплата только в магазине.",
            reply_markup=shop_menu_keyboard(),
        )
        return await show_shop_menu(update, context)

    await show_packages_menu(update, context)
    return PACKAGES_MENU


async def balance_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Выбор способа пополнения."""
    await ack_press(update)
    text = pressed_text(update)
    if text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text == BTN_BACK or text == BTN_BALANCE_CHECKS:
        return await show_shop_menu(update, context)
    if text == BTN_PACKAGES:
        return await show_packages_menu(update, context)
    if text in (BTN_CRYPTOBOT, BTN_CRYPTOBOT_OLD):
        return await ask_topup_amount(update, context)
    if text in (BTN_USDT_TRC, BTN_USDT_TRC_OLD, BTN_USDT_TRC_OLD2):
        await update.effective_message.reply_text(
            "USDT TRC-20 доступен в магазине: тариф → USDT TRC-20.",
            reply_markup=balance_keyboard(),
        )
        return BALANCE_MENU
    await show_balance_menu(update, context)
    return BALANCE_MENU


async def promo_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_btn_text((update.message.text or "").strip())
    if text == BTN_BACK:
        return await show_balance_menu(update, context)
    ok, msg = redeem_promo(update.effective_user.id, text)
    await update.effective_message.reply_text(msg, reply_markup=back_keyboard())
    return await show_balance_menu(update, context)


async def topup_amount_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = normalize_btn_text((update.message.text or "").strip()).replace(",", ".")
    if text == BTN_BACK or text == BTN_HOME:
        return await show_balance_menu(update, context)
    if text in (BTN_CRYPTOBOT, BTN_CRYPTOBOT_OLD):
        return await ask_topup_amount(update, context)
    if text in (BTN_USDT_TRC, BTN_USDT_TRC_OLD, BTN_USDT_TRC_OLD2):
        await update.effective_message.reply_text(
            "USDT TRC-20 доступен в магазине: тариф → USDT TRC-20.",
            reply_markup=balance_keyboard(),
        )
        return BALANCE_MENU
    try:
        amount = float(text)
    except ValueError:
        await update.effective_message.reply_text(
            f"📄 Введите сумму пополнения в USDT\n"
            f"(минимум {MIN_DEPOSIT})",
            reply_markup=back_keyboard(),
        )
        return WAITING_TOPUP_AMOUNT
    if amount < float(MIN_DEPOSIT):
        await update.effective_message.reply_text(
            f"Минимум {MIN_DEPOSIT} USDT.\n"
            f"📄 Введите сумму пополнения в USDT",
            reply_markup=back_keyboard(),
        )
        return WAITING_TOPUP_AMOUNT
    return await create_and_send_invoice(
        update,
        context,
        kind="balance",
        amount_usdt=amount,
        description=f"Пополнение баланса +{amount} USDT",
    )


async def paycheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_press(update)
    data = update.callback_query.data or ""
    invoice_id = data.split(":", 1)[-1]
    rec = get_invoice_record(invoice_id) or {}
    clicker = update.effective_user.id if update.effective_user else 0
    owner = int(rec.get("user_id") or 0)
    if not rec:
        await update.effective_message.reply_text("Счёт не найден.")
        return WAITING_SHOP_PAY
    if owner and clicker and owner != clicker and not is_admin(clicker, update.effective_user):
        await update.effective_message.reply_text("❌ Это чужой счёт.")
        return MAIN_MENU
    if str(rec.get("pay") or "").lower() == "trc20":
        return await _credit_or_wait_trc(update, context, invoice_id)
    return await _credit_or_wait_crypto(update, context, invoice_id)


async def trcpaid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_press(update)
    data = update.callback_query.data or ""
    order_id = data.split(":", 1)[-1]
    if order_id:
        context.user_data["trc_order_id"] = order_id
    return await _credit_or_wait_trc(update, context, order_id)


async def recheck_shop_trc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    order_id = (
        context.user_data.get("crypto_order_id")
        or context.user_data.get("trc_order_id")
        or latest_open_shop_order(uid)
    )
    if not order_id:
        await update.effective_message.reply_text(
            "Счёт не найден. Выберите тариф ещё раз.",
            reply_markup=shop_menu_keyboard(),
        )
        return await show_shop_menu(update, context)
    rec = get_invoice_record(order_id) or {}
    if str(rec.get("pay") or "").lower() == "trc20":
        return await _credit_or_wait_trc(update, context, str(order_id))
    return await _credit_or_wait_crypto(update, context, str(order_id))


async def _credit_or_wait_trc(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> int:
    rec = get_invoice_record(order_id) or {}
    clicker = update.effective_user.id if update.effective_user else 0
    owner = int(rec.get("user_id") or 0)
    if owner and clicker and owner != clicker and not is_admin(clicker, update.effective_user):
        await update.effective_message.reply_text("❌ Это чужой счёт.")
        return WAITING_SHOP_PAY
    already = bool(rec.get("credited"))
    ok, msg = (True, "✅ Уже зачислено.") if already else await fulfill_trc_invoice(context.bot, order_id, force=False)
    uid = update.effective_user.id
    if ok:
        if not already:
            await _announce_shop_paid(context.bot, update.effective_chat.id, uid, msg)
        return after_pay_state(uid, update.effective_user)
    await update.effective_message.reply_text(msg, reply_markup=shop_pay_keyboard())
    return WAITING_SHOP_PAY


async def _credit_or_wait_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> int:
    rec = get_invoice_record(order_id) or {}
    clicker = update.effective_user.id if update.effective_user else 0
    owner = int(rec.get("user_id") or 0)
    if owner and clicker and owner != clicker and not is_admin(clicker, update.effective_user):
        await update.effective_message.reply_text("❌ Это чужой счёт.")
        return WAITING_SHOP_PAY
    already = bool(rec.get("credited"))
    ok, msg = (True, "✅ Уже зачислено.") if already else await fulfill_paid_invoice(context.bot, order_id)
    uid = update.effective_user.id
    if ok:
        if not already:
            await _announce_shop_paid(context.bot, update.effective_chat.id, uid, msg)
        return after_pay_state(uid, update.effective_user)
    await update.effective_message.reply_text(msg, reply_markup=shop_pay_keyboard())
    return WAITING_SHOP_PAY


async def trcok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_press(update)
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return MAIN_MENU
    data = update.callback_query.data or ""
    order_id = data.split(":", 1)[-1]
    rec = get_invoice_record(order_id) or {}
    ok, msg = await fulfill_trc_invoice(context.bot, order_id, force=True)
    await update.effective_message.reply_text(msg)
    if ok:
        uid = int(rec.get("user_id") or 0)
        if uid:
            try:
                await _announce_shop_paid(context.bot, uid, uid, msg)
            except Exception:
                logger.exception("trcok notify user")
    return MAIN_MENU


async def show_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await show_packages_menu(update, context)


async def tools_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """legacy: сразу в выбор банка"""
    await ack_press(update)
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    text = pressed_text(update)
    if text == BTN_BACK:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text in (
        BTN_CHECKS, BTN_SELECT_BANK, BTN_SPOOF, BTN_TELEGRAM,
        BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI,
    ):
        u = update.effective_user
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
        )
        return CHECKS_MENU
    return await checks_menu_handler(update, context)


async def checks_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню чеков"""
    await ack_press(update)
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    text = pressed_text(update)
    user_id = update.effective_user.id
    
    if text == BTN_BACK:
        await send_main_menu(update, context)
        return MAIN_MENU
    
    bank_map = {
        BTN_VTB: ('vtb', 'vtb_check', '🔵 ВТБ', VTB_EXAMPLE),
        BTN_VTB_OLD: ('vtb', 'vtb_check', '🔵 ВТБ', VTB_EXAMPLE),
        BTN_SBER: ('sber', 'sber_check', '🟢 Сбербанк', SBER_EXAMPLE),
        BTN_SBER_OLD: ('sber', 'sber_check', '🟢 Сбербанк', SBER_EXAMPLE),
        BTN_OZON: ('ozon', 'ozon_check', '🟣 ОЗОН', None),
        BTN_OZON_OLD: ('ozon', 'ozon_check', '🟣 ОЗОН', None),
        BTN_ALFA: ('alfa', 'alfa_check', '🔴 Альфа-Банк', ALFA_EXAMPLE),
        BTN_TBANK: ('tbank', 'tbank_check', '🟡 Т-Банк', TBANK_EXAMPLE),
        BTN_TBANK_OLD: ('tbank', 'tbank_check', '🟡 Т-Банк', TBANK_EXAMPLE),
        BTN_OTP: ('otp', 'otp_check', '🏛 ОТП Банк', OTP_SBP_EXAMPLE),
        BTN_OTP_OLD: ('otp', 'otp_check', '🏛 ОТП Банк', OTP_SBP_EXAMPLE),
        BTN_URALSIB: ('uralsib', 'uralsib_check', '🟣 Уралсиб', URALSIB_SBP_EXAMPLE),
        BTN_URALSIB_OLD: ('uralsib', 'uralsib_check', '🟣 Уралсиб', URALSIB_SBP_EXAMPLE),
        BTN_URALSIB_PLAIN: ('uralsib', 'uralsib_check', '🟣 Уралсиб', URALSIB_SBP_EXAMPLE),
    }
    
    if text in bank_map:
        bank_id, tool_id, bank_name, example = bank_map[text]
        u = update.effective_user
        allowed = live_banks_for(u.id if u else None, u)
        kb = checks_keyboard(u.id if u else None, u)
        if bank_id not in allowed:
            return await send_bank_wip(update, bank_name)
        # Без аренды/чеков всё равно показываем список методов банка;
        # paywall — в prompt_check_form / ensure_can_generate.
        
        # Проверяем доступность
        # ОЗОН
        if bank_id == 'ozon' and not (OZON_AVAILABLE or OZON_SBP_AVAILABLE):
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Озон Банка не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU
        
        # Сбербанк
        if bank_id == 'sber' and not (SBER_SBP_AVAILABLE or SBER_PHONE_AVAILABLE or SBER_AVAILABLE):
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Сбербанка не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU
        
        # Альфа-Банк
        if bank_id == 'alfa' and not ALFA_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Альфа-Банка не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        # Уралсиб
        if bank_id == 'uralsib' and not URALSIB_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Уралсиба не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        if bank_id == 'tbank' and not (
            TBANK_AVAILABLE or TBANK_SBP_AVAILABLE or TBANK_PHONE_AVAILABLE
            or TBANK_CARD_TBANK_AVAILABLE
        ):
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Т-Банка не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        if bank_id == 'vtb' and not VTB_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль ВТБ не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        # ОТП Банк
        if bank_id == 'otp' and not (OTP_SBP_AVAILABLE or OTP_CARD_AVAILABLE):
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль ОТП Банка не загружен.",
                reply_markup=kb,
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        context.user_data['bank'] = bank_id
        context.user_data['tool_id'] = tool_id
        
        # Информация о стоимости
        # Т-Банк: сначала выбор способа перевода
        if bank_id == 'tbank':
            context.user_data.pop('tbank_submethod', None)
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                                f"📌 Выберите способ перевода",
                reply_markup=tbank_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return TBANK_SUBMENU

        # ОТП Банк: выбор способа перевода
        if bank_id == 'otp':
            context.user_data.pop('otp_submethod', None)
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                                f"📌 Выберите способ перевода",
                reply_markup=otp_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return OTP_SUBMENU

        # Ozon Банк: выбор способа перевода
        if bank_id == 'ozon':
            context.user_data.pop('ozon_submethod', None)
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                                f"📌 Выберите способ перевода",
                reply_markup=ozon_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return OZON_SUBMENU

        # Сбербанк: выбор способа перевода
        if bank_id == 'sber':
            context.user_data.pop('sber_submethod', None)
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                                f"📌 Выберите способ перевода",
                reply_markup=sber_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return SBER_SUBMENU

        # Альфа-Банк: выбор способа перевода
        if bank_id == 'alfa':
            context.user_data.pop('alfa_submethod', None)
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                                f"📌 Выберите способ перевода",
                reply_markup=alfa_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return ALFA_SUBMENU
        
        # Разные подсказки для разных банков
        if bank_id == 'alfa':
            fields_text = (
                f"Сумма\n"
                f"Получатель\n"
                f"Телефон\n"
                f"Банк получателя\n"
                f"Дата (или 'сейчас')\n"
                f"Счёт списания (20 цифр)\n"
                f"Номер операции\n"
                f"Номер СБП (32 символа)\n"
                f"Сообщение"
            )
        elif bank_id == 'tbank':
            fields_text = (
                f"Сумма\n"
                f"Отправитель\n"
                f"Получатель\n"
                f"Карта получателя\n"
                f"Банк получателя\n"
                f"Дата (или 'сейчас')\n"
                f"Комиссия (или 'Без комиссии')\n"
                f"Номер квитанции (или 'авто')"
            )
        elif bank_id == 'uralsib':
            fields_text = (
                f"Сумма\n"
                f"Комиссия (или 0)\n"
                f"Дата и время (или 'сейчас')\n"
                f"Номер квитанции (или 'авто')\n"
                f"Отправитель\n"
                f"Получатель\n"
                f"Телефон получателя\n"
                f"Банк получателя"
            )
        else:
            fields_text = (
                f"Сумма\n"
                f"Отправитель\n"
                f"Получатель\n"
                f"Телефон\n"
                f"Банк получателя\n"
                f"Дата (или 'сейчас')"
            )
        
        return await prompt_check_form(
            update,
            bank_name, fields_text, example or VTB_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA
    
    return CHECKS_MENU


async def tbank_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Подменю Т-Банка: СБП / карта в другой банк / карта в Т-Банк"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('tbank_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    u = update.effective_user
    if "tbank" not in live_banks_for(u.id if u else None, u):
        return await send_bank_wip(update, "🟡 Т-Банк")

    if text == BTN_TBANK_SBP:
        if not TBANK_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Т-Банк СБП не загружен.",
                reply_markup=tbank_submethod_keyboard(),
            )
            return TBANK_SUBMENU
        tool_id = context.user_data.get('tool_id', 'tbank_check')
        context.user_data['tbank_submethod'] = 'sbp'
        fields_text = (
            "Сумма\n"
            "Отправитель (Имя Фамилия)\n"
            "Телефон получателя\n"
            "Получатель (Имя X.)\n"
            "Банк получателя\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "ID операции СБП (авто или номер)\n"
            "Номер квитанции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🟡 Т-Банк - СБП", fields_text, TBANK_SBP_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_TBANK_PHONE:
        if not TBANK_PHONE_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Т-Банк (телефон) не загружен.",
                reply_markup=tbank_submethod_keyboard(),
            )
            return TBANK_SUBMENU
        tool_id = context.user_data.get('tool_id', 'tbank_check')
        context.user_data['tbank_submethod'] = 'phone'
        fields_text = (
            "Сумма\n"
            "Отправитель\n"
            "Телефон получателя\n"
            "Получатель\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Номер квитанции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🟡 Т-Банк - По номеру телефона", fields_text, TBANK_PHONE_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_TBANK_CARD_TBANK:
        if not TBANK_CARD_TBANK_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Т-Банк (карта внутри) не загружен.",
                reply_markup=tbank_submethod_keyboard(),
            )
            return TBANK_SUBMENU
        tool_id = context.user_data.get('tool_id', 'tbank_check')
        context.user_data['tbank_submethod'] = 'card_tbank'
        return await prompt_check_form(
            update,
            "🟡 Т-Банк - Клиенту Т-Банка",
                "Сумма\n"
                "Отправитель\n"
                "Получатель\n"
                "Карта (последние 4 цифры)\n"
                "Дата (или 'сейчас' / 'авто')\n"
                "Номер квитанции (или 'авто')",
                TBANK_CARD_TBANK_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_TBANK_CARD_OTHER:
        if not TBANK_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Т-Банк (карта→другой) не загружен.",
                reply_markup=tbank_submethod_keyboard(),
            )
            return TBANK_SUBMENU
        tool_id = context.user_data.get('tool_id', 'tbank_check')
        context.user_data['tbank_submethod'] = 'card_other'
        bank_name = '🟡 Т-Банк'
        fields_text = (
            f"Сумма\n"
            f"Отправитель\n"
            f"Получатель\n"
            f"Карта получателя\n"
            f"Банк получателя\n"
            f"Дата (или 'сейчас')\n"
            f"Комиссия (или 'Без комиссии')\n"
            f"Итого (или пусто = как Сумма)\n"
            f"Номер квитанции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            f"{bank_name} - по карте на Сбербанк", fields_text, TBANK_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_TBANK_CARD_NOCOMM:
        try:
            from tbank_nocomm_stealth import create_tbank_nocomm_stealth  # noqa: F401
        except ImportError:
            await update.effective_message.reply_text(
                "⚠️ Модуль Т-Банк (без к-и) не загружен.",
                reply_markup=tbank_submethod_keyboard(),
            )
            return TBANK_SUBMENU
        tool_id = context.user_data.get('tool_id', 'tbank_check')
        context.user_data['tbank_submethod'] = 'card_nocomm'
        fields_text = (
            f"Сумма\n"
            f"Отправитель\n"
            f"Карта получателя\n"
            f"Дата (или 'сейчас')\n"
            f"Номер квитанции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🟡 Т-Банк - по карте в другой банк (без к-и)",
                fields_text,
                TBANK_NOCOMM_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_TBANK_STATEMENT:
        await update.effective_message.reply_text(
            "⚠️ Выписка временно отключена (нет OnlyPDF 30/30).",
            reply_markup=tbank_submethod_keyboard(),
        )
        return TBANK_SUBMENU

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=tbank_submethod_keyboard(),
    )
    return TBANK_SUBMENU


async def otp_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Подменю ОТП Банка: СБП"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('otp_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    return await send_bank_wip(update)

    if text == BTN_OTP_SBP:
        tool_id = context.user_data.get('tool_id', 'otp_check')
        context.user_data['otp_submethod'] = 'sbp'
        fields_text = (
            "Сумма\n"
            "Отправитель\n"
            "Данные паспорта (маска)\n"
            "Телефон получателя\n"
            "Получатель\n"
            "Банк получателя\n"
            "БИК (можно оставить - подставится сам)\n"
            "ID операции СБП (можно оставить - синхр. сам)\n"
            "Дата (меняется ТОЛЬКО день)\n"
            "Комиссия (или 0)\n"
            "Номер квитанции (или 'авто')"
        )
        notes_text = (
            "⚠️ Особенности валидации:\n"
            "• Дата: меняется только число (1-31). Месяц/год/время "
            "форсятся = 05.2026 20:15:01.\n"
            "• Сумма и номер квитанции: используйте только цифры "
            "0, 1, 2, 4, 5, 6, 8 (3, 7, 9 не пройдут - нет в шрифте).\n"
            "• Банк: только из списка - Сбербанк, Газпромбанк, "
            "Банк ВТБ, Райффайзенбанк, Яндекс, Т-Банк. "
            "БИК подставится автоматически.\n"
            "• SBP_ID: pos 4-5 авто-синхр. с днём (день+20)."
        )
        return await prompt_check_form(
            update,
            "🏛 ОТП Банк - СБП", fields_text, OTP_SBP_EXAMPLE, notes_text,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_OTP_CARD:
        if not OTP_CARD_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль ОТП (карта) не загружен.",
                reply_markup=otp_submethod_keyboard(),
            )
            return OTP_SUBMENU
        tool_id = context.user_data.get('tool_id', 'otp_check')
        context.user_data['otp_submethod'] = 'card'
        fields_text_c = (
            "Сумма перевода (с комиссией)\n"
            "ФИО плательщика (напр. ИВАН ИВАНОВИЧ И.)\n"
            "Данные паспорта (маска, напр. 5*** *****1)\n"
            "Номер карты получателя (16 цифр)\n"
            "Дата (ДД.ММ.ГГГГ ЧЧ:ММ:СС или 'сейчас')\n"
            "Комиссия (число или 0)\n"
            "Номер квитанции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🏛 ОТП Банк - По карте в другой банк",
                fields_text_c,
                OTP_CARD_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=otp_submethod_keyboard(),
    )
    return OTP_SUBMENU


async def ozon_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Подменю Ozon Банка: СБП"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('ozon_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    return await send_bank_wip(update)

    if text == BTN_OZON_SBP:
        tool_id = context.user_data.get('tool_id', 'ozon_check')
        context.user_data['ozon_submethod'] = 'sbp'
        fields_text = (
            "Сумма\n"
            "Отправитель\n"
            "Телефон получателя\n"
            "Получатель\n"
            "Банк получателя\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Комиссия (или 'Без комиссии')\n"
            "ID операции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🟣 Ozon Банк - СБП", fields_text, OZON_SBP_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=ozon_submethod_keyboard(),
    )
    return OZON_SUBMENU


async def sber_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gated = await alfa_only_gate(update, context)
    if gated is not None:
        return gated
    """Подменю Сбербанка: СБП / телефон / карта"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('sber_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    u = update.effective_user
    if "sber" not in live_banks_for(u.id if u else None, u):
        return await send_bank_wip(update, "🟢 Сбербанк")

    if text in (BTN_SBER_SBP, BTN_SBER_PHONE, BTN_SBER_CARD_OTHER):
        if text == BTN_SBER_SBP and not SBER_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Сбер СБП не загружен.",
                reply_markup=sber_submethod_keyboard(),
            )
            return SBER_SUBMENU
        if text in (BTN_SBER_PHONE,) and not (SBER_PHONE_AVAILABLE or SBER_AVAILABLE):
            await update.effective_message.reply_text(
                "⚠️ Модуль Сбер (оригинальные шаблоны) не загружен.",
                reply_markup=sber_submethod_keyboard(),
            )
            return SBER_SUBMENU
        if text == BTN_SBER_CARD_OTHER:
            if not SBER_CARD_AVAILABLE:
                await update.effective_message.reply_text(
                    "⚠️ Модуль Сбер «по карте» не загружен.",
                    reply_markup=sber_submethod_keyboard(),
                )
                return SBER_SUBMENU

        tool_id = context.user_data.get('tool_id', 'sber_check')
        if text == BTN_SBER_SBP:
            context.user_data['sber_submethod'] = 'sbp'
            fields_text = (
                "Сумма\n"
                "Отправитель\n"
                "Получатель\n"
                "Телефон получателя\n"
                "Банк получателя\n"
                "Дата (или 'сейчас')"
            )
            method_title = "🟢 Сбербанк - СБП"
            example_text = SBER_SBP_EXAMPLE
        elif text == BTN_SBER_PHONE:
            context.user_data['sber_submethod'] = 'phone'
            fields_text = (
                "Сумма\n"
                "Отправитель\n"
                "Получатель\n"
                "Телефон получателя\n"
                "Банк получателя\n"
                "Дата (или 'сейчас')"
            )
            method_title = "🟢 Сбербанк - По номеру телефона"
            example_text = SBER_PHONE_EXAMPLE
        else:
            context.user_data['sber_submethod'] = 'card_other'
            fields_text = (
                "Сумма\n"
                "Отправитель\n"
                "Получатель\n"
                "Карта получателя\n"
                "Банк получателя\n"
                "Дата (или 'сейчас' / 'авто')\n"
                "Комиссия (или 0)\n"
                "Номер операции (или 'авто')"
            )
            method_title = "🟢 Сбербанк - По карте в другой банк"
            example_text = (
                "5500\n"
                "Оюмаа Орлановна К.\n"
                "Алексей Александрович П.\n"
                "220220******8333\n"
                "Т-Банк\n"
                "сейчас\n"
                "0\n"
                "авто"
            )

        return await prompt_check_form(
            update,
            method_title, fields_text, example_text,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=sber_submethod_keyboard(),
    )
    return SBER_SUBMENU


async def alfa_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подменю Альфа-Банка: СБП / по номеру карты в другой банк"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id
    entry_kb = alfa_only_keyboard() if is_alfa_only(user_id) else back_keyboard()
    methods_kb = alfa_only_keyboard() if is_alfa_only(user_id) else alfa_submethod_keyboard()

    if is_alfa_only(user_id) and text not in ALFA_METHOD_BUTTONS:
        return await show_alfa_only_menu(update, context)

    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('alfa_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    if text in ALFA_METHOD_BUTTONS:
        context.user_data['bank'] = 'alfa'
        context.user_data['tool_id'] = 'alfa_check'

    if text == BTN_ALFA_SBP:
        if not ALFA_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа СБП не загружен.",
                reply_markup=methods_kb,
            )
            return ALFA_SUBMENU
        tool_id = context.user_data.get('tool_id', 'alfa_check')
        context.user_data['alfa_submethod'] = 'sbp'
        fields_text = (
            "Сумма\n"
            "Получатель\n"
            "Телефон\n"
            "Банк получателя (любой: Т-Банк, Сбер, Озон, ПСБ, WB, ВТБ, …)\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Счёт списания (20 цифр)\n"
            "Номер операции (или 'авто')\n"
            "Номер СБП (или 'авто')\n"
            "Сообщение"
        )
        return await prompt_check_form(
            update,
            "🔴 Альфа-Банк - СБП", fields_text, ALFA_SBP_EXAMPLE,
            reply_markup=entry_kb,
        )
        return ENTERING_DATA

    if text == BTN_ALFA_CARD:
        if not ALFA_CARD_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «по номеру карты в другой банк» не загружен.",
                reply_markup=methods_kb,
            )
            return ALFA_SUBMENU
        tool_id = context.user_data.get('tool_id', 'alfa_check')
        context.user_data['alfa_submethod'] = 'card'
        fields_text = (
            "Сумма\n"
            "Карта отправителя\n"
            "Карта получателя\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Номер операции (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🔴 Альфа-Банк - по номеру карты в другой банк", fields_text, ALFA_CARD_EXAMPLE,
            reply_markup=entry_kb,
        )
        return ENTERING_DATA

    if text in (BTN_ALFA_PHONE, BTN_ALFA_PHONE_OLD):
        if not ALFA_PHONE_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «по телефону» не загружен.",
                reply_markup=methods_kb,
            )
            return ALFA_SUBMENU
        context.user_data['alfa_submethod'] = 'phone'
        fields_text = (
            "Сумма\n"
            "Получатель\n"
            "Телефон\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Счёт списания (20 цифр)\n"
            "Номер операции (или 'авто')\n"
            "Сообщение (или 'авто')"
        )
        return await prompt_check_form(
            update,
            "🔴 Альфа-Банк - По телефону (Альфа→Альфа)",
                fields_text,
                ALFA_PHONE_EXAMPLE,
            reply_markup=entry_kb,
        )
        return ENTERING_DATA

    if text == BTN_ALFA_STATEMENT:
        if not ALFA_STATEMENT_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «выписка» не загружен.",
                reply_markup=methods_kb,
            )
            return ALFA_SUBMENU
        context.user_data['alfa_submethod'] = 'statement'
        fields_text = (
            "Номер счета (20 цифр или 'авто')\n"
            "Дата формирования выписки\n"
            "Клиент\n"
            "Адрес регистрации (или 'авто')\n"
            "Период с\n"
            "Период по\n"
            "Расходы / сумма в валюте счета\n"
            "Входящий остаток\n"
            "Исходящий остаток (или 'авто' = входящий − расходы)\n"
            "Платежный лимит (или 'авто' = исходящий)\n"
            "Текущий баланс (или 'авто' = исходящий)\n"
            "Дата проводки первой строки\n"
            "Код операции (или 'авто'; точный код - как есть)\n"
            "Описание перевода (можно несколько строк)"
        )
        return await prompt_check_form(
            update,
            "🔴 Альфа-Банк - Выписка",
            fields_text,
            ALFA_STATEMENT_EXAMPLE,
            reply_markup=entry_kb,
        )
        return ENTERING_DATA

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=methods_kb,
    )
    return ALFA_SUBMENU


PDF_GEN_TIMEOUT_SEC = 360

_COPY_HEADER_TOKENS = frozenset({"copy", "скопировать", "вставить", "paste"})


def _min_payload_lines(bank: str, context) -> int:
    """Minimum non-empty lines before we try to emit (optional date/ids default)."""
    if bank == "tbank":
        sm = context.user_data.get("tbank_submethod")
        if sm == "sbp":
            return 4
        if sm == "phone":
            return 4
        if sm == "card_tbank":
            return 4
        if sm == "card_nocomm":
            return 3
        if sm == "card_other":
            return 5
    if bank == "sber":
        sm = context.user_data.get("sber_submethod")
        if sm == "card_other":
            return 6
        return 5
    if bank == "alfa":
        sm = context.user_data.get("alfa_submethod")
        if sm == "sbp":
            return 9
        if sm == "phone":
            return 7
        if sm == "statement":
            return 14
        return 5
    if bank == "uralsib":
        return 8
    return 5


def _expect_from_payload(data: dict | None) -> dict:
    """Normalize generator payload → emit_quality_gate expect."""
    d = data if isinstance(data, dict) else {}
    exp = {
        "sender": d.get("sender") or d.get("sender_name") or "",
        "receiver": d.get("receiver") or d.get("receiver_name") or "",
        "amount": d.get("amount") or d.get("new_amount") or "",
        "phone": d.get("phone") or d.get("receiver_phone") or "",
        "bank": d.get("bank_name") or d.get("recipient_bank") or d.get("bank") or "",
        "date": d.get("date") or "",
        "time": d.get("time") or "",
        "date_time": d.get("date_time") or d.get("new_date") or "",
        "commission": d.get("commission") or "",
        "account": d.get("account") or d.get("sender_account") or "",
        "operation_num": d.get("operation_num") or d.get("receipt_num") or "",
        "receipt_num": d.get("receipt_num") or "",
        "sbp_id": d.get("sbp_id") or d.get("spb_number") or "",
        "message": d.get("message") or "",
        "sender_card": d.get("sender_card") or "",
        "receiver_card": d.get("receiver_card") or d.get("card") or d.get("dest_card") or "",
    }
    if not exp["date_time"] and exp["date"]:
        exp["date_time"] = f"{exp['date']} {exp['time']}".strip()
    return exp


def _gate_bank_hint(hint: str) -> str:
    h = (hint or "").lower()
    if "alfa_sbp" in h or "create_alfa_sbp" in h or ("альфа" in h and "сбп" in h):
        return "alfa_sbp"
    if "alfa_phone" in h or "create_alfa_phone" in h or ("альфа" in h and "телефон" in h):
        return "alfa_phone"
    if (
        "alfa_statement" in h or "create_alfa_statement" in h
        or ("альфа" in h and "выписк" in h)
    ):
        return "alfa_statement"
    if "alfa_card" in h or "create_alfa_card" in h or ("альфа" in h and "карт" in h):
        return "alfa_card"
    if "sber_sbp" in h or "create_sber_sbp" in h or ("сбер" in h and "сбп" in h):
        return "sber_sbp"
    if "sber_phone" in h or "create_sber_phone" in h or ("сбер" in h and "тел" in h):
        return "sber_phone"
    if "sber_card" in h or "create_sber_card" in h:
        return "sber_card"
    if "tbank_phone" in h or "create_tbank_phone" in h:
        return "tbank_phone"
    if "tbank_sbp" in h or "create_tbank_sbp" in h:
        return "tbank_sbp"
    if "uralsib" in h or "уралсиб" in h:
        return "uralsib_sbp"
    if "tbank_nocomm" in h or "create_tbank_nocomm" in h:
        return "tbank_nocomm"
    if "tbank_card_tbank" in h or "create_tbank_card_tbank" in h:
        return "tbank_card_tbank"
    if "create_tbank_stealth" in h or "tbank_card_sber" in h:
        return "tbank_card_sber"
    if h.startswith("tbank") or "т-банк" in h or "t-bank" in h:
        return h if h.startswith("tbank") else "tbank"
    return h or hint


async def _generate_gated_pdf(
    update: Update,
    gen_fn,
    data: dict,
    *,
    method_hint: str,
    data_as_keyword: bool,
    generator_kwargs: dict | None = None,
):
    """Generate from an unchanged payload and return only a gate-approved PDF."""
    import asyncio
    import copy
    from functools import partial

    uid = update.effective_user.id if update.effective_user else 0
    slot = reserve_generation(uid, "alfa_check", update.effective_user)
    if not slot:
        return None

    try:
        await update.message.reply_chat_action("upload_document")
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    emit_ok = None
    try:
        _tools = str(Path(__file__).resolve().parent / "tools")
        if _tools not in sys.path:
            sys.path.insert(0, _tools)
        from emit_quality_gate import emit_ok as _emit_ok
        emit_ok = _emit_ok
    except Exception as exc:
        # Never refuse emit because the advisory gate failed to import
        # (e.g. SyntaxError on older Python). Still generate and ship.
        logger.exception("PDF gate unavailable for %s: %s - ship without gate", method_hint, exc)

    if not isinstance(data, dict):
        logger.error("PDF generation rejected non-dict payload for %s", method_hint)
        refund_generation(uid, slot)
        return None
    if not method_hint.strip():
        logger.error("PDF generation rejected missing method hint")
        refund_generation(uid, slot)
        return None

    try:
        canonical = copy.deepcopy(data)
        extra_kwargs = dict(generator_kwargs or {})
    except Exception:
        refund_generation(uid, slot)
        raise

    # LAW #1: retry emit - never «не собралось» on a first None/timeout.
    candidate = None
    last_exc: BaseException | None = None
    for _emit_try in range(1, 4):
        payload = copy.deepcopy(canonical)
        call = (
            partial(gen_fn, data=payload, **extra_kwargs)
            if data_as_keyword
            else partial(gen_fn, payload, **extra_kwargs)
        )
        try:
            candidate = await asyncio.wait_for(
                loop.run_in_executor(None, call),
                timeout=PDF_GEN_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            last_exc = TimeoutError(f"{method_hint} timeout")
            logger.error(
                "PDF generation timed out for %s (try %d/3)",
                method_hint, _emit_try,
            )
            candidate = None
        except Exception as exc:
            last_exc = exc
            logger.exception(
                "PDF generation crashed for %s (try %d/3): %s",
                method_hint, _emit_try, exc,
            )
            candidate = None
        if candidate:
            break
        if _emit_try < 3:
            logger.warning(
                "PDF emit empty for %s - retry %d/3 (LAW1 always-ship)",
                method_hint, _emit_try + 1,
            )
    if not candidate:
        if last_exc is not None:
            logger.error("PDF generation failed for %s after 3 tries", method_hint)
        refund_generation(uid, slot)
        return None
    if not isinstance(candidate, (bytes, bytearray)) or not bytes(candidate).startswith(b"%PDF-"):
        logger.error("Generator returned non-PDF output for %s", method_hint)
        refund_generation(uid, slot)
        return None
    pdf_out = bytes(candidate)
    # Advisory gate only - bot ALWAYS ships a PDF once bytes exist.
    # Proton/quality failures are fixed in generators, never by refusing UX.
    if emit_ok is not None:
        try:
            ok, why = emit_ok(
                pdf_out,
                bank_hint=_gate_bank_hint(method_hint),
                expect=_expect_from_payload(canonical),
                strict_fio=True,
            )
            if not ok:
                logger.error(
                    "PDF gate soft-fail %s: %s - ship PDF anyway",
                    method_hint, why,
                )
        except Exception as exc:
            logger.exception(
                "PDF gate crashed for %s: %s - ship PDF", method_hint, exc,
            )
    return pdf_out


async def _tbank_generate(update: Update, gen_fn, data: dict, *, method_hint: str):
    return await _generate_gated_pdf(
        update,
        gen_fn,
        data,
        method_hint=method_hint,
        data_as_keyword=False,
    )


async def _run_pdf_sync(
    update: Update,
    gen_fn,
    *,
    data: dict,
    method_hint: str,
    **generator_kwargs,
):
    return await _generate_gated_pdf(
        update,
        gen_fn,
        data,
        method_hint=method_hint,
        data_as_keyword=True,
        generator_kwargs=generator_kwargs,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Handler error: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Внутренняя ошибка. Отправьте /start и попробуйте снова.",
            )
        except Exception:
            pass


async def data_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получены данные для чека"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id
    nav_kb = form_keyboard()
    
    # Если пользователь нажал кнопки меню во время ввода данных,
    # перенаправляем в соответствующие обработчики, а не парсим как чек.
    if text == BTN_FORM_EXAMPLE:
        hint = _FORM_HINTS.get(user_id) or {}
        example = str(hint.get("example") or "").strip()
        fields = str(hint.get("fields") or "").strip()
        title = str(hint.get("title") or "Пример")
        if example:
            await update.effective_message.reply_text(
                as_html(
                    f"<b>{html_lib.escape(title)}</b>\n"
                    f"{html_lib.escape(fields)}\n"
                    f"\n"
                    f"<pre>{html_lib.escape(example)}</pre>"
                ),
                reply_markup=nav_kb,
            )
        else:
            await update.effective_message.reply_text(
                "Примера нет. Выберите способ перевода ещё раз.",
                reply_markup=nav_kb,
            )
        return ENTERING_DATA
    if text in [BTN_SELECT_BANK, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_SHOP_REFRESH, BTN_SHOP_RECHECK, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in BANK_PICK_BUTTONS:
        return await checks_menu_handler(update, context)
    if text in [BTN_TBANK_SBP, BTN_TBANK_PHONE, BTN_TBANK_CARD_OTHER, BTN_TBANK_CARD_NOCOMM, BTN_TBANK_CARD_TBANK, BTN_TBANK_STATEMENT]:
        return await tbank_submenu_handler(update, context)
    if text in [BTN_OTP_SBP, BTN_OTP_CARD]:
        return await otp_submenu_handler(update, context)
    if text in [BTN_OZON_SBP]:
        return await ozon_submenu_handler(update, context)
    if text in [BTN_SBER_SBP, BTN_SBER_PHONE, BTN_SBER_CARD_OTHER]:
        return await sber_submenu_handler(update, context)
    if text in ALFA_METHOD_BUTTONS:
        return await alfa_submenu_handler(update, context)

    if text == BTN_BACK:
        if is_alfa_only(user_id):
            return await show_alfa_only_menu(update, context)
        bank = context.user_data.get('bank', 'vtb')
        if bank == 'tbank':
            context.user_data.pop('tbank_submethod', None)
            await update.effective_message.reply_text(
                "🟡 *Т-Банк*\n\n📌 Выберите способ перевода",
                reply_markup=tbank_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return TBANK_SUBMENU
        if bank == 'otp':
            context.user_data.pop('otp_submethod', None)
            await update.effective_message.reply_text(
                "🏛 *ОТП Банк*\n\n📌 Выберите способ перевода",
                reply_markup=otp_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return OTP_SUBMENU
        if bank == 'ozon':
            context.user_data.pop('ozon_submethod', None)
            await update.effective_message.reply_text(
                "🟣 *Ozon Банк*\n\n📌 Выберите способ перевода",
                reply_markup=ozon_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return OZON_SUBMENU
        if bank == 'sber':
            context.user_data.pop('sber_submethod', None)
            await update.effective_message.reply_text(
                "🟢 *Сбербанк*\n\n📌 Выберите способ перевода",
                reply_markup=sber_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return SBER_SUBMENU
        if bank == 'alfa':
            context.user_data.pop('alfa_submethod', None)
            await update.effective_message.reply_text(
                "🔴 *Альфа-Банк*\n\n📌 Выберите способ перевода",
                reply_markup=alfa_only_keyboard() if is_alfa_only(user_id) else alfa_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return ALFA_SUBMENU
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_kb(update),
            parse_mode='Markdown'
        )
        return CHECKS_MENU
    
    if is_alfa_only(user_id):
        context.user_data['bank'] = 'alfa'
        if not str(context.user_data.get('tool_id') or '').startswith('alfa'):
            context.user_data['tool_id'] = 'alfa_check'
    
    bank = context.user_data.get('bank', 'sber')
    tool_id = context.user_data.get('tool_id', 'sber_check')

    if is_alfa_only(user_id) and bank != 'alfa':
        return await show_alfa_only_menu(update, context)

    if update.callback_query:
        await update.effective_message.reply_text(
            "Отправьте данные чека текстом.",
            reply_markup=nav_kb,
        )
        return ENTERING_DATA

    if bank not in live_banks_for(user_id, update.effective_user):
        return await send_bank_wip(update, str(bank))

    if not await ensure_can_generate(update, context, tool_id):
        if can_see_shop(user_id, update.effective_user):
            return SHOP_MENU
        return MAIN_MENU

    if bank == 'tbank' and context.user_data.get('tbank_submethod') not in ('card_other', 'card_nocomm', 'card_tbank', 'sbp', 'phone'):
        await update.effective_message.reply_text(
            "🟡 Сначала выберите способ перевода Т-Банка.",
            reply_markup=tbank_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return TBANK_SUBMENU

    if bank == 'otp' and context.user_data.get('otp_submethod') not in ('sbp', 'card'):
        await update.effective_message.reply_text(
            "🏛 Сначала выберите способ перевода ОТП Банка.",
            reply_markup=otp_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return OTP_SUBMENU

    if bank == 'ozon' and context.user_data.get('ozon_submethod') not in ('sbp',):
        await update.effective_message.reply_text(
            "🟣 Сначала выберите способ перевода Ozon Банка.",
            reply_markup=ozon_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return OZON_SUBMENU

    if bank == 'sber' and context.user_data.get('sber_submethod') not in ('sbp', 'phone', 'card_other'):
        await update.effective_message.reply_text(
            "🟢 Сначала выберите способ перевода Сбербанка.",
            reply_markup=sber_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return SBER_SUBMENU

    if bank == 'alfa' and context.user_data.get('alfa_submethod') not in ('sbp', 'card', 'phone', 'statement'):
        await update.effective_message.reply_text(
            "🔴 Сначала выберите способ перевода Альфа-Банка.",
            reply_markup=alfa_only_keyboard() if is_alfa_only(user_id) else alfa_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return ALFA_SUBMENU

    if bank == 'alfa' and context.user_data.get('alfa_submethod') == 'phone':
        if not ALFA_PHONE_AVAILABLE:
            context.user_data.pop('alfa_submethod', None)
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «по телефону» не загружен.",
                reply_markup=alfa_only_keyboard() if is_alfa_only(user_id) else alfa_submethod_keyboard(),
            )
            return ALFA_SUBMENU
    
    # Проверяем возможность использования
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    # Telegram / клиенты иногда вставляют «copy» первой строкой - сдвигает все поля.
    while lines and lines[0].lower() in _COPY_HEADER_TOKENS:
        lines = lines[1:]
    while lines and not re.search(r"\d", lines[0]) and not _is_date_now_token(lines[0]):
        lines = lines[1:]

    need = _min_payload_lines(bank, context)
    if len(lines) < need:
        hint = _FORM_HINTS.get(user_id) or {}
        fields = str(hint.get("fields") or "").strip()
        extra = f"\n\nПорядок:\n{fields}" if fields else ""
        await update.effective_message.reply_text(
            f"Мало строк: есть {len(lines)}, нужно {need}.{extra}\n\n"
            f"Кнопка «📋 Пример» — образец. Не вставляйте строку copy.",
            reply_markup=nav_kb,
        )
        return ENTERING_DATA

    await update.effective_message.reply_text("⏳ Собираю PDF…")
    
    # Парсим данные
    amount_raw = lines[0]
    sender = lines[1] if len(lines) > 1 else ""
    receiver = lines[2] if len(lines) > 2 else ""
    phone = lines[3] if len(lines) > 3 else ""
    recipient_bank = lines[4] if len(lines) > 4 else "Сбербанк"
    
    # Дата
    if len(lines) >= 6:
        date_input = lines[5].strip().lower()
        if date_input in ['сейчас', 'now', '-', 'авто', 'auto']:
            date_time = now_msk().strftime("%d.%m.%Y, %H:%M")
        else:
            date_time = lines[5]
    else:
        date_time = now_msk().strftime("%d.%m.%Y, %H:%M")
    
    # Форматируем сумму
    amount_num = re.sub(r'[^\d]', '', amount_raw)
    if amount_num:
        amount = f"{int(amount_num):,}".replace(',', ' ') + ' ₽'
    else:
        amount = amount_raw + ' ₽'
    
    # Форматируем телефон (Сбер форматирует сам в _format_sber_phone)
    phone_raw = phone
    if not (bank == 'sber' and context.user_data.get('sber_submethod') in ('sbp', 'phone', 'card_other')):
        digits = re.sub(r'[^\d]', '', phone)
        if len(digits) == 11:
            phone = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    else:
        phone = phone_raw.strip()
    
    delivered = False
    try:
        pdf_bytes = None
        if bank == 'uralsib':
            if len(lines) < 8:
                await update.effective_message.reply_text(
                    "❌ *Недостаточно данных для Уралсиб СБП!*\n\n"
                    "Нужно 8 строк:\n"
                    "1. Сумма\n2. Комиссия (или 0)\n3. Дата и время (или 'сейчас')\n"
                    "4. Номер квитанции (или 'авто')\n5. Отправитель\n"
                    "6. Получатель\n7. Телефон получателя\n8. Банк получателя",
                    reply_markup=nav_kb,
                    parse_mode='Markdown',
                )
                return ENTERING_DATA
            data = {
                "amount": lines[0],
                "commission": lines[1],
                "date_time": lines[2],
                "receipt_num": lines[3],
                "sender": lines[4],
                "receiver": lines[5],
                "phone": lines[6],
                "recipient_bank": lines[7],
                "bank": lines[7],
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_uralsib_sbp_stealth, data=data, method_hint="uralsib_sbp",
            )
            if not pdf_bytes:
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Уралсиб (СБП)"
            filename = f"uralsib_sbp_{now_msk().strftime('%H%M%S')}.pdf"
            amount = lines[0]
            sender = lines[4]
            receiver = lines[5]

        elif bank == 'sber' and context.user_data.get('sber_submethod') == 'sbp':
            date_input = lines[5].strip() if len(lines) > 5 else "сейчас"
            if date_input.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                now = now_msk()
                sber_date = now.strftime("%d.%m.%Y")
                sber_time = now.strftime("%H:%M:%S")
            elif ' ' in date_input:
                parts = date_input.replace(',', ' ').split()
                sber_date = parts[0]
                sber_time = parts[1] if len(parts) > 1 else now_msk().strftime("%H:%M:%S")
            else:
                sber_date = date_input
                sber_time = now_msk().strftime("%H:%M:%S")

            data = {
                'amount': amount_raw,
                'sender_name': sender,
                'receiver_name': receiver,
                'phone': phone,
                'bank_name': recipient_bank,
                'date': sber_date,
                'time': sber_time,
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_sber_sbp_stealth, data=data, method_hint="sber_sbp",
            )
            if not pdf_bytes:
                logger.error(
                    "Sber SBP emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Сбербанк (СБП)"
            filename = f"sber_sbp_{now_msk().strftime('%H%M%S')}.pdf"

        elif bank == 'sber' and context.user_data.get('sber_submethod') == 'phone':
            date_input = lines[5].strip() if len(lines) > 5 else "сейчас"
            if date_input.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                now = now_msk()
                sber_date = now.strftime("%d.%m.%Y")
                sber_time = now.strftime("%H:%M:%S")
            elif ' ' in date_input:
                parts = date_input.replace(',', ' ').split()
                sber_date = parts[0]
                sber_time = parts[1] if len(parts) > 1 else now_msk().strftime("%H:%M:%S")
            else:
                sber_date = date_input
                sber_time = now_msk().strftime("%H:%M:%S")

            phone_data = {
                'amount': amount_raw,
                'sender_name': sender,
                'receiver_name': receiver,
                'phone': phone,
                'date': sber_date,
                'time': sber_time,
            }
            pdf_bytes = await _run_pdf_sync(
                update,
                create_sber_phone_stealth,
                data=phone_data,
                method_hint="sber_phone",
            )
            if not pdf_bytes:
                logger.error(
                    "Sber phone emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in phone_data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Сбербанк (По номеру телефона)"
            filename = f"sber_phone_{now_msk().strftime('%H%M%S')}.pdf"

        elif bank == 'sber' and context.user_data.get('sber_submethod') == 'card_other':
            if not SBER_CARD_AVAILABLE:
                await update.effective_message.reply_text(
                    "⚠️ Модуль Сбер (карта) не загружен.",
                    reply_markup=sber_submethod_keyboard(),
                )
                return SBER_SUBMENU
            date_input = lines[5].strip() if len(lines) > 5 else "сейчас"
            if date_input.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                now = now_msk()
                sber_date = now.strftime("%d.%m.%Y")
                sber_time = now.strftime("%H:%M:%S")
            elif ' ' in date_input:
                parts = date_input.replace(',', ' ').split()
                sber_date = parts[0]
                sber_time = parts[1] if len(parts) > 1 else now_msk().strftime("%H:%M:%S")
            else:
                sber_date = date_input
                sber_time = now_msk().strftime("%H:%M:%S")
            commission = lines[6].strip() if len(lines) > 6 else "0"
            bank_name_in = recipient_bank
            pdf_bytes = await _run_pdf_sync(update, create_sber_card_stealth, data={
                "amount": amount_num,
                "sender_name": sender,
                "receiver": receiver,
                "dest_card": phone_raw,
                "bank": bank_name_in,
                "date": sber_date,
                "time": sber_time,
                "commission": commission,
            }, method_hint="sber_card")
            if not pdf_bytes:
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Сбербанк (По карте в другой банк)"
            filename = f"sber_card_{now_msk().strftime('%H%M%S')}.pdf"

        elif bank == 'sber':
            await update.effective_message.reply_text(
                "❌ Выберите способ перевода Сбербанка.",
                reply_markup=sber_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return SBER_SUBMENU

        elif bank == 'ozon' and context.user_data.get('ozon_submethod') == 'sbp':
            # Ozon SBP: 8 строк - сумма, отправитель, телефон, получатель, банк, дата, комиссия, sbp_id
            oz_amount   = lines[0] if len(lines) > 0 else "10000"
            oz_sender   = lines[1] if len(lines) > 1 else "Владимир Иванович Н."
            oz_phone    = lines[2] if len(lines) > 2 else "+7 (983) 625-36-76"
            oz_receiver = lines[3] if len(lines) > 3 else "Евгений Константинович Т."
            oz_bank     = lines[4] if len(lines) > 4 else "Сбербанк"
            oz_date_raw = lines[5].strip() if len(lines) > 5 else "сейчас"
            oz_comm     = lines[6].strip() if len(lines) > 6 else "Без комиссии"
            oz_sbp_id   = lines[7].strip() if len(lines) > 7 else "авто"

            # Дата+время в формат "DD.MM.YYYY" + "HH:MM"
            if oz_date_raw.lower() in ('сейчас', 'now', '-', ''):
                now = now_msk()
                oz_date_str = now.strftime("%d.%m.%Y")
                oz_time_str = now.strftime("%H:%M")
            else:
                # допускаем форматы "DD.MM.YYYY HH:MM" или "DD.MM.YYYY, HH:MM"
                cleaned = oz_date_raw.replace(',', ' ')
                parts = cleaned.split()
                if len(parts) >= 2:
                    oz_date_str = parts[0]
                    oz_time_str = parts[1]
                else:
                    oz_date_str = cleaned
                    oz_time_str = now_msk().strftime("%H:%M")

            # Телефон в формате "+7 (XXX) XXX-XX-XX"
            oz_phone_digits = re.sub(r'[^\d]', '', oz_phone)
            if len(oz_phone_digits) == 11:
                oz_phone = f"+7 ({oz_phone_digits[1:4]}) {oz_phone_digits[4:7]}-{oz_phone_digits[7:9]}-{oz_phone_digits[9:11]}"

            # Сумма (без ₽, с пробелом-разделителем тысяч)
            oz_amount_digits = re.sub(r'[^\d]', '', oz_amount)
            if oz_amount_digits:
                oz_amount_formatted = f"{int(oz_amount_digits):,}".replace(',', ' ')
            else:
                oz_amount_formatted = oz_amount

            # Комиссия: если введена цифра - добавим " ₽"
            if oz_comm.lower() in ('0', '0.00', '0,00', 'без комиссии', 'нет', '-'):
                oz_comm_text = "Без комиссии"
            else:
                cd = re.sub(r'[^\d]', '', oz_comm)
                oz_comm_text = f"{int(cd):,} ₽".replace(',', ' ') if cd else oz_comm

            data = {
                'date':       oz_date_str,
                'time':       oz_time_str,
                'amount':     oz_amount_formatted,
                'commission': oz_comm_text,
                'receiver':   oz_receiver,
                'phone':      oz_phone,
                'bank_name':  oz_bank,
                'sender':     oz_sender,
                'sbp_id':     '' if oz_sbp_id.lower() in ('авто', 'auto', '-') else oz_sbp_id,
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_ozon_sbp_stealth, data=data, method_hint="ozon_sbp",
            )
            bank_name = "Ozon (СБП)"
            filename = f"ozon_sbp_{now_msk().strftime('%H%M%S')}.pdf"
            amount = f"{oz_amount_formatted} ₽"
            receiver = oz_receiver
            sender = oz_sender

        elif bank == 'ozon':
            # Форматируем для Озон Банка (старый карточный)
            try:
                if ',' in date_time:
                    date_part, time_part = date_time.split(',')
                else:
                    date_part = date_time
                    time_part = now_msk().strftime("%H:%M")
                
                if '.' in date_part:
                    parts = date_part.strip().split('.')
                    if len(parts) == 3:
                        day, month, year = parts
                        months_ru = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
                                    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
                        month_name = months_ru[int(month) - 1]
                        ozon_date = f"{int(day)} {month_name} {year} {time_part.strip()}"
                    else:
                        ozon_date = f"{date_time}"
                else:
                    ozon_date = f"{date_time}"
            except:
                ozon_date = f"{date_time}"
            
            ozon_amount = (
                f"{int(amount_num):,} ₽".replace(',', ' ') if amount_num else f"{amount_raw} ₽"
            )
            ozon_phone = f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}" if len(digits) == 11 else phone
            
            # Проверяем на недоступные символы
            all_ozon_text = sender + receiver + recipient_bank
            missing_ozon = ozon_check_text(all_ozon_text)
            if missing_ozon:
                logger.warning("charset soft-pass: %s", missing_ozon)
            
            data = {
                'date_time': ozon_date,
                'amount': ozon_amount,
                'sender_name': sender,
                'receiver_name': receiver,
                'receiver_phone': ozon_phone,
                'recipient_bank': recipient_bank,
            }
            
            pdf_bytes = await _run_pdf_sync(
                update,
                create_ozon_stealth,
                data=data,
                method_hint="ozon",
                auto_select=True,
            )
            bank_name = "Озон Банк"
            filename = f"ozon_{now_msk().strftime('%H%M%S')}.pdf"
        
        elif bank == 'alfa' and context.user_data.get('alfa_submethod') == 'sbp':
            if len(lines) < 9:
                await update.effective_message.reply_text(
                    "❌ *Недостаточно данных для Альфа СБП!*\n\n"
                    "Нужно 9 строк:\n"
                    "1. Сумма\n2. Получатель\n3. Телефон\n4. Банк\n"
                    "5. Дата\n6. Счёт списания\n7. Номер операции\n"
                    "8. Номер СБП\n9. Сообщение",
                    reply_markup=nav_kb,
                    parse_mode='Markdown'
                )
                return ENTERING_DATA

            alfa_receiver = lines[1]
            alfa_bank = lines[3]
            alfa_message = lines[8]

            data = {
                'amount': lines[0],
                'receiver': alfa_receiver,
                'phone': lines[2],
                'recipient_bank': alfa_bank,
                'date_time': lines[4],
                'account': lines[5],
                'operation_num': lines[6],
                'sbp_id': lines[7],
                'message': alfa_message,
            }
            reject = alfa_sbp_reject_reason(data)
            if reject:
                await update.effective_message.reply_text(
                    reject, reply_markup=nav_kb,
                )
                return ENTERING_DATA
            pdf_bytes = await _run_pdf_sync(
                update, create_alfa_sbp_stealth, data=data, method_hint="alfa_sbp",
            )
            if not pdf_bytes:
                logger.error(
                    "Alfa SBP emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (СБП)"
            now = now_msk()
            filename = f"alfa_sbp_{now.strftime('%H%M%S')}.pdf"
            amount = lines[0]
            receiver = alfa_receiver
            sender = "-"

        elif bank == 'alfa' and context.user_data.get('alfa_submethod') == 'card':
            if len(lines) < 5:
                await update.effective_message.reply_text(
                    "❌ *Недостаточно данных для Альфа «по номеру карты в другой банк»!*\n\n"
                    "Нужно 5 строк:\n"
                    "1. Сумма\n2. Карта отправителя\n3. Карта получателя\n"
                    "4. Дата (или 'сейчас')\n5. Номер операции (или 'авто')",
                    reply_markup=nav_kb,
                    parse_mode='Markdown'
                )
                return ENTERING_DATA

            data = {
                'amount': lines[0],
                'sender_card': lines[1],
                'receiver_card': lines[2],
                'date_time': lines[3],
                'operation_num': lines[4],
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_alfa_card_stealth, data=data, method_hint="alfa_card",
            )
            if not pdf_bytes:
                logger.error("Alfa CARD emit None data=%s", data)
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (карта → другой банк)"
            now = now_msk()
            filename = f"alfa_card_{now.strftime('%H%M%S')}.pdf"
            amount = lines[0]
            receiver = lines[2][-4:] if len(lines[2]) >= 4 else lines[2]
            sender = lines[1][-4:] if len(lines[1]) >= 4 else lines[1]

        elif bank == 'alfa' and context.user_data.get('alfa_submethod') == 'phone':
            if len(lines) < 7:
                await update.effective_message.reply_text(
                    "❌ *Недостаточно данных для Альфа «по телефону»!*\n\n"
                    "Нужно 7 строк:\n"
                    "1. Сумма\n2. Получатель\n3. Телефон\n"
                    "4. Дата\n5. Счёт списания\n6. Номер операции\n"
                    "7. Сообщение",
                    reply_markup=nav_kb,
                    parse_mode='Markdown'
                )
                return ENTERING_DATA

            ph_receiver = lines[1]
            ph_message = lines[6]
            data = {
                'amount': lines[0],
                'receiver': ph_receiver,
                'phone': lines[2],
                'date_time': lines[3],
                'account': lines[4],
                'operation_num': lines[5],
                'message': ph_message,
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_alfa_phone_stealth, data=data, method_hint="alfa_phone",
            )
            if not pdf_bytes:
                logger.error(
                    "Alfa PHONE emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (телефон)"
            now = now_msk()
            filename = f"alfa_phone_{now.strftime('%H%M%S')}.pdf"
            amount = lines[0]
            receiver = ph_receiver
            sender = "-"

        elif bank == 'alfa' and context.user_data.get('alfa_submethod') == 'statement':
            if len(lines) < 14:
                await update.effective_message.reply_text(
                    "❌ *Недостаточно данных для Альфа выписки!*\n\n"
                    "Нужно 14 строк:\n"
                    "1. Номер счета\n2. Дата формирования\n3. Клиент\n"
                    "4. Адрес (или 'авто')\n5. Период с\n6. Период по\n"
                    "7. Расходы\n"
                    "8. Входящий остаток\n"
                    "9. Исходящий остаток (или 'авто')\n"
                    "10. Платежный лимит (или 'авто')\n"
                    "11. Текущий баланс (или 'авто')\n"
                    "12. Дата проводки\n13. Код операции\n"
                    "14+. Описание перевода (можно несколько строк)",
                    reply_markup=nav_kb,
                    parse_mode='Markdown',
                )
                return ENTERING_DATA
            st_client = lines[2]
            data = {
                'account': lines[0],
                'date_formed': lines[1],
                'date_time': lines[1],
                'client': st_client,
                'receiver': st_client,
                'address': lines[3],
                'period_from': lines[4],
                'period_to': lines[5],
                'amount': lines[6],
                'incoming': lines[7],
                'outgoing': lines[8],
                'pay_limit': lines[9],
                'current_balance': lines[10],
                'op_date': lines[11],
                'operation_num': lines[12],
                # Описание часто переносится - склеиваем все строки после кода.
                'message': " ".join(lines[13:]),
            }
            pdf_bytes = await _run_pdf_sync(
                update, create_alfa_statement_stealth, data=data,
                method_hint="alfa_statement",
            )
            if not pdf_bytes:
                logger.error("Alfa STATEMENT emit None data=%s", data)
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (выписка)"
            now = now_msk()
            filename = f"alfa_statement_{now.strftime('%H%M%S')}.pdf"
            amount = lines[6]
            receiver = st_client
            sender = "-"

        elif bank == 'alfa':
            await update.effective_message.reply_text(
                "🔴 Сначала выберите способ перевода Альфа-Банка.",
                reply_markup=alfa_only_keyboard() if is_alfa_only(user_id) else alfa_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return ALFA_SUBMENU
        
        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'phone':
            # По номеру телефона: сумма, отправитель, телефон, получатель, дата, квитанция
            ph_amount   = lines[0] if len(lines) > 0 else "14000"
            ph_sender   = lines[1] if len(lines) > 1 else "Отправитель"
            ph_phone    = lines[2] if len(lines) > 2 else "+7 (961) 954-80-60"
            ph_receiver = lines[3] if len(lines) > 3 else "Получатель"
            ph_date_raw = lines[4].strip() if len(lines) > 4 else "сейчас"
            ph_receipt  = lines[5].strip() if len(lines) > 5 else "авто"

            if ph_date_raw.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                ph_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                ph_date = ph_date_raw

            ph_phone_digits = re.sub(r'[^\d]', '', ph_phone)
            if len(ph_phone_digits) == 11:
                ph_phone = f"+7 ({ph_phone_digits[1:4]}) {ph_phone_digits[4:7]}-{ph_phone_digits[7:9]}-{ph_phone_digits[9:11]}"

            ph_amount_formatted = ph_amount

            data = {
                'date_time':  ph_date,
                'amount':     ph_amount,
                'sender':     ph_sender,
                'phone':      ph_phone,
                'receiver':   ph_receiver,
                'receipt_num': ph_receipt,
            }
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_phone_stealth,
                data,
                method_hint="tbank_phone",
            )
            if not pdf_bytes:
                logger.error(
                    "T-Bank phone emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Т-Банк (По тел.)"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', ph_date)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"
            amount = ph_amount_formatted
            receiver = ph_receiver
            sender = ph_sender

        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'sbp':
            # СБП: сумма, отправитель, телефон, получатель, [банк], дата, [ID СБП], [квитанция]
            # Банк можно не указывать - тогда Сбербанк; дата распознаётся по dd.mm.yyyy.
            sbp_amount   = lines[0] if len(lines) > 0 else "1230"
            sbp_sender   = lines[1] if len(lines) > 1 else "Отправитель"
            sbp_phone    = lines[2] if len(lines) > 2 else "+7 (965) 585-66-55"
            sbp_receiver = lines[3] if len(lines) > 3 else "Получатель"

            rest = lines[4:]
            sbp_bank, sbp_date_raw, tail = _parse_tbank_sbp_rest(rest)

            def _tbank_sbp_auto(val: str) -> bool:
                v = val.strip().lower()
                return v in ('авто', 'auto', '-', '', 'авто или номер')

            if len(tail) >= 2:
                sbp_id_raw = tail[0].strip()
                sbp_receipt = tail[1].strip()
            elif len(tail) == 1:
                maybe = tail[0].strip()
                if (
                    not _tbank_sbp_auto(maybe)
                    and len(maybe) == 27
                    and maybe[0] in 'AB'
                    and maybe[16] == '0'
                ):
                    sbp_id_raw = maybe
                    sbp_receipt = "авто"
                else:
                    sbp_id_raw = "авто"
                    sbp_receipt = maybe
            else:
                sbp_id_raw = "авто"
                sbp_receipt = "авто"

            if _tbank_sbp_auto(sbp_id_raw):
                sbp_id = "авто"
                sbp_id_manual = False
            else:
                sbp_id = sbp_id_raw.strip().upper()
                sbp_id_manual = True
                if not decode_sbp_operation_id(sbp_id):
                    await update.effective_message.reply_text(
                        "❌ *ID операции СБП* - 27 символов, формат:\n"
                        "`B6196011155964410B101300117`\n"
                        "Или укажите `авто` для автогенерации.",
                        parse_mode='Markdown',
                        reply_markup=nav_kb,
                    )
                    return ENTERING_DATA

            if sbp_date_raw.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                # Pass through «сейчас» so prepare randomizes seconds 1–59
                # (expanding to HH:MM here made date_manual + :00 → ZERO_SECONDS).
                sbp_date = "сейчас"
            else:
                sbp_date = sbp_date_raw

            # Форматируем телефон
            sbp_phone_digits = re.sub(r'[^\d]', '', sbp_phone)
            if len(sbp_phone_digits) == 11:
                sbp_phone = f"+7 ({sbp_phone_digits[1:4]}) {sbp_phone_digits[4:7]}-{sbp_phone_digits[7:9]}-{sbp_phone_digits[9:11]}"

            sbp_amount_formatted = sbp_amount

            data = {
                'date_time':     sbp_date,
                'amount':        sbp_amount,
                'sender':        sbp_sender,
                'phone':         sbp_phone,
                'receiver':      sbp_receiver,
                'recipient_bank': sbp_bank,
                'receipt_num':   sbp_receipt,
                'sbp_id':        sbp_id,
                'sbp_id_manual': sbp_id_manual,
                'sbp_suffix':    'авто',
            }
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_sbp_stealth,
                data,
                method_hint="tbank_sbp",
            )
            if not pdf_bytes:
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Т-Банк (СБП)"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', sbp_date)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"
            amount = sbp_amount_formatted
            receiver = sbp_receiver
            sender = sbp_sender

        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'card_tbank':
            ct_sender   = lines[1].strip() if len(lines) > 1 else "Иван Иванов"
            ct_receiver = lines[2].strip() if len(lines) > 2 else "Петр П."
            ct_card     = lines[3].strip() if len(lines) > 3 else "*3269"
            ct_date_raw = lines[4].strip() if len(lines) > 4 else "сейчас"
            ct_receipt  = lines[5].strip() if len(lines) > 5 else "авто"
            if ct_date_raw.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                ct_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                ct_date = ct_date_raw
            ct_data = {
                'date_time':   ct_date,
                'amount':      amount_raw,
                'sender':      ct_sender,
                'receiver':    ct_receiver,
                'card':        ct_card,
                'receipt_num': ct_receipt,
            }
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_card_tbank_stealth,
                ct_data,
                method_hint="tbank_card_tbank",
            )
            if not pdf_bytes:
                logger.error(
                    "T-Bank card_tbank emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in ct_data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Т-Банк (Клиенту Т-Банка)"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', ct_date)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"
            sender = ct_sender
            receiver = ct_receiver

        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'card_nocomm':
            # По карте без к-и (свой шаблон без полей получатель/банк/комиссия)
            # Формат: сумма / отправитель / карта / дата / квитанция
            nc_sender   = lines[1].strip() if len(lines) > 1 else "Иван Иванов"
            nc_card     = lines[2].strip() if len(lines) > 2 else "220220******3269"
            nc_date_raw = lines[3].strip() if len(lines) > 3 else "сейчас"
            nc_receipt  = lines[4].strip() if len(lines) > 4 else "авто"
            if nc_date_raw.lower() in ('сейчас', 'now', '-', '', 'авто', 'auto'):
                nc_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                nc_date = nc_date_raw
            nc_data = {
                'date_time':   nc_date,
                'amount':      amount_raw,
                'sender':      nc_sender,
                'card':        nc_card,
                'receipt_num': nc_receipt,
            }
            from tbank_nocomm_stealth import create_tbank_nocomm_stealth
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_nocomm_stealth,
                nc_data,
                method_hint="tbank_nocomm",
            )
            if not pdf_bytes:
                logger.error(
                    "T-Bank nocomm emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in nc_data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Т-Банк (без к-и)"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', nc_date)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"
            sender = nc_sender
            receiver = nc_card

        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'statement':
            await update.effective_message.reply_text(
                "⚠️ Выписка временно отключена (нет OnlyPDF 30/30).",
                reply_markup=tbank_submethod_keyboard(),
            )
            return ENTERING_DATA
            # Выписка о движении средств - 23 строки:
            #   шапка: 1=дата, 2=ФИО, 3=адрес, 4=периодС, 5=периодПо,
            #          6=пополнения, 7=расходы
            #   операция 1 (8..15): дата_оп, время_оп, дата_спис, время_спис,
            #          сумма, описание_1, описание_2, последние4_карты
            #   операция 2 (16..23): тот же формат
            def _safe(idx, default=""):
                return lines[idx].strip() if len(lines) > idx else default

            st_date_raw = _safe(0, "сейчас")
            if st_date_raw.lower() in ("сейчас", "now", "-", ""):
                st_main_date = now_msk().strftime("%d.%m.%Y")
            else:
                m_d = re.match(r"(\d{2}\.\d{2}\.\d{4})", st_date_raw)
                st_main_date = m_d.group(1) if m_d else st_date_raw

            st_fio       = _safe(1, "Иванов Иван Иванович")
            st_addr      = _safe(2, "117303, г Москва, Севастопольский проспект, д. 28, кв. 12")
            st_period_f  = _safe(3, st_main_date)
            st_period_t  = _safe(4, st_main_date)
            st_income    = _safe(5, "0")
            st_outcome   = _safe(6, "0")

            ops = []
            for op_base in (7, 15):
                if len(lines) <= op_base:
                    break
                ops.append({
                    "date_op":  _safe(op_base + 0),
                    "time_op":  _safe(op_base + 1),
                    "date_off": _safe(op_base + 2),
                    "time_off": _safe(op_base + 3),
                    "amount":   _safe(op_base + 4),
                    "desc1":    _safe(op_base + 5),
                    "desc2":    _safe(op_base + 6),
                    "card4":    _safe(op_base + 7),
                })

            st_data = {
                "main_date":     st_main_date,
                "fio":           st_fio,
                "address":       st_addr,
                "period_from":   st_period_f,
                "period_to":     st_period_t,
                "total_income":  st_income,
                "total_outcome": st_outcome,
                "operations":    ops,
            }
            from tbank_statement_stealth import create_tbank_statement
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_statement,
                st_data,
                method_hint="tbank_statement",
            )
            bank_name = "Т-Банк (Выписка)"
            filename = f"statement_{st_main_date}.pdf"
            sender = st_fio
            receiver = st_addr[:40]
            # amount для итогового сообщения: расходы за период
            try:
                amount = f"{int(re.sub(r'[^0-9]', '', st_outcome)):,}".replace(",", " ") + " ₽"
            except Exception:
                amount = st_outcome

        elif bank == 'tbank':
            tbank_card = lines[3] if len(lines) > 3 else "220220******3269"
            tbank_bank = lines[4] if len(lines) > 4 else recipient_bank
            tbank_commission = lines[6].strip() if len(lines) > 6 else "Без комиссии"
            # Опциональное поле «Итого» (9 строк) - если задано, big amount отличается от small
            if len(lines) >= 9:
                tbank_itogo_raw = lines[7].strip()
                tbank_receipt = lines[8].strip() if len(lines) > 8 else "авто"
            else:
                tbank_itogo_raw = ""
                tbank_receipt = lines[7].strip() if len(lines) > 7 else "авто"
            data = {
                'date_time':    date_time,
                'amount':       amount_raw,
                'amount_total': tbank_itogo_raw,
                'sender':       sender,
                'receiver':     receiver,
                'recipient_bank': tbank_bank,
                'card':         tbank_card,
                'commission':   tbank_commission,
                'receipt_num':  tbank_receipt,
            }
            pdf_bytes = await _tbank_generate(
                update,
                create_tbank_stealth,
                data,
                method_hint="tbank_card_sber",
            )
            if not pdf_bytes:
                logger.error("T-Bank card_sber emit None data=%s", data)
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось - отправьте те же данные ещё раз.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA
            bank_name = "Т-Банк"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', date_time)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"

        elif bank == 'otp' and context.user_data.get('otp_submethod') == 'sbp':
            # ОТП Банк СБП: 11 строк (с маской паспорта) или 10 строк (как раньше - паспорт по умолчанию)
            otp_amount = lines[0] if len(lines) > 0 else "6500"
            otp_sender = lines[1] if len(lines) > 1 else "Андрей Андреевич А."
            if len(lines) >= 11:
                otp_passport = lines[2].strip() if lines[2].strip() else "2*** ****8"
                i = 3
            else:
                otp_passport = "2*** ****8"
                i = 2
            otp_phone    = lines[i] if len(lines) > i else "+7 949 417-04-68"
            otp_receiver = lines[i + 1] if len(lines) > i + 1 else "Надежда Игоревна П."
            otp_bank     = lines[i + 2] if len(lines) > i + 2 else "ПСБ"
            otp_bik      = lines[i + 3].strip() if len(lines) > i + 3 else "044525555"
            otp_sbp_id   = lines[i + 4].strip() if len(lines) > i + 4 else "B6127120848620170B10130011750703"
            otp_date_raw = lines[i + 5].strip() if len(lines) > i + 5 else "сейчас"
            otp_commission = lines[i + 6].strip() if len(lines) > i + 6 else "0"
            otp_receipt    = lines[i + 7].strip() if len(lines) > i + 7 else "авто"

            # Дата для Properties PDF (на чеке дата всегда = шаблон 07.05.2026)
            if otp_date_raw.lower() in ('сейчас', 'now', '-', ''):
                otp_date = now_msk().strftime("%d.%m.%Y %H:%M:%S")
            else:
                otp_date = otp_date_raw

            otp_phone_digits = re.sub(r'[^\d]', '', otp_phone)
            if len(otp_phone_digits) == 11:
                otp_phone = f"+7 {otp_phone_digits[1:4]} {otp_phone_digits[4:7]}-{otp_phone_digits[7:9]}-{otp_phone_digits[9:11]}"

            otp_amount_clean = re.sub(r'[^\d.,]', '', otp_amount)
            otp_amount_formatted = ""
            try:
                v = float(otp_amount_clean.replace(',', '.'))
                rub = int(v); kop = round((v - rub) * 100)
                otp_amount_formatted = f"{rub:,}".replace(',', ' ') + (f",{kop:02d}" if kop else ",00") + ' ₽'
            except Exception:
                otp_amount_formatted = otp_amount + ' ₽'

            # Авто-генерация receipt_num: только цифры из orig SemiBold subset
            # (0,1,2,4,5,6,8). Цифры 3/7/9 нет в шрифте и ломают валидацию.
            _SAFE_DIGITS = "01245680124568"  # вес 0,1,2 чуть выше для разнообразия
            def _auto_receipt():
                return "1-" + "".join(random.choice(_SAFE_DIGITS) for _ in range(8))

            data = {
                'amount':       otp_amount_clean or otp_amount,
                'commission':   otp_commission if otp_commission and otp_commission.lower() not in ('-', 'нет') else '0',
                'sender':       otp_sender,
                'passport':     otp_passport,
                'phone':        otp_phone,
                'receiver':     otp_receiver,
                'bank':         otp_bank,
                'bik':          otp_bik,
                'sbp_id':       otp_sbp_id,
                'date_time':    otp_date,
                'receipt_num':  otp_receipt if otp_receipt.lower() not in ('авто', 'auto', '-', '') else _auto_receipt(),
            }
            try:
                pdf_bytes = await _run_pdf_sync(
                    update,
                    create_otp_sbp_stealth,
                    data=data,
                    method_hint="otp_sbp",
                )
            except OtpStealthError as e:
                logger.exception("OTP SBP generation failed: %s", e)
                return OTP_SUBMENU
            bank_name = "ОТП Банк (СБП)"
            # Имя файла как у настоящих чеков ОТП СБП: receipt_ДД.ММ.ГГГГ.pdf
            try:
                _otp_d = otp_date[:10]  # "ДД.ММ.ГГГГ"
            except Exception:
                _otp_d = now_msk().strftime("%d.%m.%Y")
            filename = f"receipt_{_otp_d}.pdf"
            amount = otp_amount_formatted
            receiver = otp_receiver
            sender = otp_sender

        elif bank == 'otp' and context.user_data.get('otp_submethod') == 'card':
            # ОТП Банк - карта в другой банк: 7 строк
            otpc_amount    = lines[0] if len(lines) > 0 else "2670"
            otpc_sender    = lines[1] if len(lines) > 1 else "АНТОН ИГОРЕВИЧ Ш******"
            otpc_passport  = lines[2] if len(lines) > 2 else "5*** *****1"
            otpc_card      = lines[3] if len(lines) > 3 else "2204310306568331"
            otpc_date_raw  = lines[4].strip() if len(lines) > 4 else "сейчас"
            otpc_commission = lines[5].strip() if len(lines) > 5 else "150"
            otpc_receipt   = lines[6].strip() if len(lines) > 6 else "авто"

            if otpc_date_raw.lower() in ('сейчас', 'now', '-', ''):
                otpc_date = now_msk().strftime("%d.%m.%Y %H:%M:%S")
            else:
                otpc_date = otpc_date_raw

            otpc_card_digits = re.sub(r'[^\d]', '', otpc_card)
            if len(otpc_card_digits) != 16:
                await update.effective_message.reply_text(
                    "❌ Карта получателя должна содержать 16 цифр.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA

            otpc_amount_clean = re.sub(r'[^\d.,]', '', otpc_amount) or "2670"

            data = {
                'amount':       otpc_amount_clean,
                'commission':   otpc_commission if otpc_commission and otpc_commission.lower() not in ('-', 'нет', '') else '0',
                'sender':       otpc_sender,
                'passport':     otpc_passport,
                'card':         otpc_card_digits,
                'date_time':    otpc_date,
                'receipt_num':  otpc_receipt if otpc_receipt.lower() not in ('авто', 'auto', '-', '') else f"{random.randint(10000000, 99999999)}",
            }
            pdf_bytes = await _run_pdf_sync(
                update,
                create_otp_card_stealth,
                data=data,
                method_hint="otp_card",
            )
            bank_name = "ОТП Банк (карта)"
            # Имя файла как у настоящих чеков ОТП: pgc_check_NNNNNNN.pdf
            _otpc_rnum = data['receipt_num']
            filename = f"pgc_check_{_otpc_rnum}.pdf"
            try:
                v = float(otpc_amount_clean.replace(',', '.'))
                rub = int(v); kop = round((v - rub) * 100)
                amount = f"{rub:,}".replace(',', ' ') + (f",{kop:02d}" if kop else ",00") + ' ₽'
            except Exception:
                amount = otpc_amount + ' ₽'
            receiver = "Карта " + otpc_card_digits[-4:].rjust(4, '*')
            sender = otpc_sender

        else:  # VTB
            spb = f"A60141634375560Y{random.randint(100000000, 999999999)}"
            
            data = {
                'date_time': date_time,
                'amount': amount,
                'sender': sender,
                'receiver': receiver,
                'receiver_header': receiver,
                'phone': phone,
                'recipient_bank': recipient_bank,
                'spb': spb,
            }
            
            missing = get_missing_chars_for_data(data)
            if missing:
                logger.warning("charset soft-pass vtb: %s", missing)
            
            pdf_bytes = await _run_pdf_sync(
                update,
                create_vtb_stealth,
                data=data,
                method_hint="vtb",
                auto_select=True,
            )
            bank_name = "ВТБ"
            filename = f"vtb_{now_msk().strftime('%H%M%S')}.pdf"
        
        if pdf_bytes:
            import asyncio
            sent_message = None
            if is_alfa_only(user_id):
                done_caption = "✅ Чек создан!\n\nОтправьте новые данные или выберите способ ещё раз."
            else:
                done_caption = (
                    "✅ Чек создан!\n\n"
                    "Отправьте новые данные или нажмите «Назад»"
                )
            sent_ok = False
            for attempt in range(3):
                try:
                    sent_message = await update.effective_message.reply_document(
                        document=pdf_bytes,
                        filename=filename,
                        caption=done_caption,
                        reply_markup=nav_kb,
                    )
                    sent_ok = True
                    break
                except Exception as send_err:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    logger.exception("PDF send failed uid=%s: %s", user_id, send_err)
            if not sent_ok:
                refund_generation(user_id, "credit")
                await update.effective_message.reply_text(
                    "❌ Не удалось отправить чек. Квота возвращена.",
                    reply_markup=nav_kb,
                )
                return ENTERING_DATA

            delivered = True
            bump_checks_created(user_id)

            # @kronlead: текст и сам PDF. Свой чек себе не дублируем.
            if update.effective_user:
                remember_tg_user(user_id, update.effective_user)
            who = tg_user_label(update.effective_user, user_id)
            what = locals().get("bank_name") or tool_id or "чек"
            left = format_checks_left(user_id)
            report = (
                    f"🧾 Чек создан\n"
                f"Юзер: {who} ({user_id})\n"
                    f"Тип: {what}\n"
                f"Файл: {filename}\n"
                f"Осталось чеков: {left}"
            )
            if user_id != kronlead_chat_id():
                await notify_kronlead_receipt(
                    context.bot, pdf_bytes, filename, report,
                )
            if not is_admin(user_id, update.effective_user):
                await notify_admins(context.bot, report, skip=kronlead_chat_id())

            return ENTERING_DATA
        else:
            await update.effective_message.reply_text(
                "❌ Не удалось завершить создание чека.",
                reply_markup=nav_kb
            )
            return ENTERING_DATA
            
    except Exception as e:
        logger.exception("Receipt flow failed: %s", e)
        if not delivered:
            refund_generation(user_id, "credit")
        await update.effective_message.reply_text(
            "❌ Не удалось завершить создание чека.",
            reply_markup=nav_kb,
        )
        return ENTERING_DATA


async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Очистка чата"""
    user_id = update.effective_user.id
    if not has_bot_access(user_id, update.effective_user):
        await deny_without_access(update, context)
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    for i in range(message_id, max(1, message_id - 100), -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=i)
        except:
            pass
    
    if is_alfa_only(user_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="🗑 История чата очищена.\n\n" + ALFA_ONLY_MENU_TEXT,
            reply_markup=alfa_only_keyboard(),
        )
        return ALFA_SUBMENU
    await context.bot.send_message(
        chat_id=chat_id,
        text="🗑 История чата очищена.",
        reply_markup=menu_kb(update),
    )
    return MAIN_MENU


# ========== АДМИН КОМАНДЫ ==========

async def admin_give_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/give @user|ID AMOUNT - Выдать монеты"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text("📝 /give @user|ID AMOUNT")
        return

    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    try:
        amount = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("❌ Неверная сумма")
        return
    if amount <= 0:
        await update.effective_message.reply_text("❌ Сумма должна быть > 0")
        return

    add_balance(target_id, amount)
    new_balance = get_balance(target_id)

    await update.effective_message.reply_text(
        f"✅ Выдано {amount}$ → {label} (`{target_id}`)\n"
        f"Новый баланс: {new_balance}$",
        parse_mode="Markdown",
    )

    try:
        await context.bot.send_message(
            target_id,
            f"🎉 Вам зачислено {amount}$!",
        )
    except Exception:
        pass


async def admin_give_checks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/givechecks @user|ID N"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("📝 /givechecks @user|ID N")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    try:
        n = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("❌ Неверное число чеков")
        return
    if n <= 0:
        await update.effective_message.reply_text("❌ Число чеков должно быть > 0")
        return
    total = add_check_credits(target_id, n)
    await update.effective_message.reply_text(
        f"✅ Выдано {n} чек(ов) → {label} (`{target_id}`)\nВсего в пакете: {total}",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(target_id, f"🎁 Вам начислено {n} чек(ов) в пакет!")
    except Exception:
        pass


async def admin_give_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/giverent @user|ID day|week|month - тест таймера без оплаты."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("📝 /giverent @user|ID day|week|month")
        return
    who = context.args[0]
    if who.lower() in ("me", "я", "себе"):
        target_id, label = update.effective_user.id, "ты"
    else:
        target_id, label = await resolve_tg_user_arg(context.bot, who)
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(who),
            parse_mode="Markdown",
        )
        return
    key = (context.args[1] or "").strip().lower()
    offer_map = {
        "day": "rent_day",
        "день": "rent_day",
        "week": "rent_week",
        "неделя": "rent_week",
        "month": "rent_month",
        "месяц": "rent_month",
    }
    offer_id = offer_map.get(key)
    if not offer_id:
        await update.effective_message.reply_text("❌ Тариф: day | week | month")
        return
    msg = grant_shop_offer(target_id, offer_id)
    await update.effective_message.reply_text(
        f"✅ Аренда → {label} (`{target_id}`)\n{msg}",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(target_id, with_status(target_id, msg))
        sent = await context.bot.send_message(
            target_id,
            shop_catalog_html(target_id),
            reply_markup=shop_menu_keyboard(),
        )
        await _start_shop_live(context.bot, sent.chat_id, sent.message_id, target_id)
    except Exception:
        pass


async def admin_alfa_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alfaonly @user|ID - доступ только к генерации Альфа-Банка."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /alfaonly @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if is_banned(target_id):
        await update.effective_message.reply_text("❌ Сначала /unban - бот для него закрыт")
        return
    if not grant_alfa_only(target_id):
        await update.effective_message.reply_text("❌ Админу этот режим не ставится")
        return
    await update.effective_message.reply_text(
        f"Бесплатный Альфа-only снят.\n"
        f"{label} (`{target_id}`) видит все банки. Чеки — после оплаты.",
        parse_mode="Markdown",
    )


async def admin_full_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/full @user|ID - все банки, без оплаты, без админки (бета)."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /full @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if is_banned(target_id):
        await update.effective_message.reply_text("❌ Сначала /unban - бот для него закрыт")
        return
    if not grant_full_access(target_id):
        await update.effective_message.reply_text("❌ Админу этот режим не ставится")
        return
    await update.effective_message.reply_text(
        f"✅ {label} (`{target_id}`) - полный бот, чеки после оплаты.",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            target_id,
            "✅ Бот открыт: все банки. Чеки - после оплаты в магазине.\nНажмите /start",
            reply_markup=main_menu_keyboard(target_id),
        )
    except Exception:
        pass


async def admin_alfa_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alfaoff @user|ID - закрыть доступ, снова нужен PIN."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /alfaoff @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if is_admin(target_id):
        await update.effective_message.reply_text("❌ Админа так не закрыть")
        return
    revoke_bot_access(target_id)
    await update.effective_message.reply_text(
        f"🔒 Доступ закрыт: {label} (`{target_id}`)",
        parse_mode="Markdown",
    )


async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban @user|ID - бот для человека недоступен, PIN не открывает."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /ban @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if is_admin(target_id):
        await update.effective_message.reply_text("❌ Админа так не закрыть")
        return
    set_user_banned(target_id, True)
    await update.effective_message.reply_text(
        f"🚫 Бот закрыт: {label} (`{target_id}`)\n"
        f"Вернуть: /unban {context.args[0]}",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            target_id,
            LOCK_NOTICE,
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass


async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban @user|ID - снова полный доступ."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /unban @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if is_admin(target_id):
        await update.effective_message.reply_text("✅ Это админ, доступ и так есть")
        return
    set_user_banned(target_id, False)
    alfa = is_alfa_only(target_id)
    mode = "снова только Альфа-Банк" if alfa else "полное меню"
    await update.effective_message.reply_text(
        f"✅ Доступ возвращён: {label} (`{target_id}`)\n{mode}",
        parse_mode="Markdown",
    )
    try:
        if alfa:
            await context.bot.send_message(
                target_id,
                "✅ Доступ снова открыт: только Альфа-Банк.\nНажмите /start",
                reply_markup=alfa_only_keyboard(),
            )
        else:
            await context.bot.send_message(
                target_id,
                "✅ Доступ снова открыт.\nНажмите /start",
                reply_markup=main_menu_keyboard(target_id),
            )
    except Exception:
        pass


async def admin_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pin @user [123456] - именной PIN, работает только у этого человека."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "📝 /pin @user\n"
            "📝 /pin @user 123456"
        )
        return
    raw_user = context.args[0]
    custom = context.args[1] if len(context.args) > 1 else None
    target_id, label = await resolve_tg_user_arg(context.bot, raw_user)
    uname = raw_user.strip().lstrip("@")
    if uname.isdigit():
        uname = stored_username(int(uname)) or ""
    if target_id is None and not uname:
        await update.effective_message.reply_text(
            format_user_not_found(raw_user),
            parse_mode="Markdown",
        )
        return
    if target_id is not None and is_admin(target_id):
        await update.effective_message.reply_text("Админу PIN не нужен - доступ уже есть.")
        return
    try:
        pin = assign_personal_pin(target_id, uname, custom)
    except ValueError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return
    who = label if target_id else f"@{uname}"
    tid = f"`{target_id}`" if target_id else "ещё не в боте"
    await update.effective_message.reply_text(
        f"✅ Именной PIN выдан\n"
        f"Кто: {who} ({tid})\n"
        f"PIN: `{pin}`\n\n"
        f"Сработает только у этого человека. Чужой ввод - отказ.\n"
        f"После ввода откроется только Альфа-Банк.\n"
        f"Снять: /anpin {raw_user}",
        parse_mode="Markdown",
    )
    if target_id:
        try:
            await context.bot.send_message(
                target_id,
                f"🔐 Вам выдан личный PIN для входа в бота.\n\n"
                f"Код: `{pin}`\n\n"
                f"Нажмите /start и введите его. Другим этот код не подойдёт.\n"
                f"После входа будет доступен только Альфа-Банк.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            await update.effective_message.reply_text(
                "⚠️ Человеку в личку не отправилось - перешли PIN сам."
            )


async def admin_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/open @user - тихий доступ без PIN и без сообщения человеку."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /open @user")
        return
    raw_user = context.args[0]
    target_id, label = await resolve_tg_user_arg(context.bot, raw_user)
    uname = raw_user.strip().lstrip("@")
    if uname.isdigit():
        uname = stored_username(int(uname)) or ""
    if target_id is None and not uname:
        await update.effective_message.reply_text(
            format_user_not_found(raw_user),
            parse_mode="Markdown",
        )
        return
    if target_id is not None and is_admin(target_id):
        await update.effective_message.reply_text("Админу и так открыто.")
        return
    silent_open_access(target_id, uname)
    who = label if target_id else f"@{uname}"
    tid = f"`{target_id}`" if target_id else "ещё не в боте"
    await update.effective_message.reply_text(
        f"Бот и так открыт всем.\n"
        f"Чеки у {who} ({tid}) — только после оплаты в магазине.",
        parse_mode="Markdown",
    )


async def admin_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/anpin @user - забрать именной PIN и закрыть доступ."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /anpin @user")
        return
    raw_user = context.args[0]
    target_id, label = await resolve_tg_user_arg(context.bot, raw_user)
    uname = raw_user.strip().lstrip("@")
    if uname.isdigit():
        uname = stored_username(int(uname)) or ""
    if target_id is not None and is_admin(target_id):
        await update.effective_message.reply_text("Админу доступ так не снимается.")
        return
    was_open = bool(uname and uname.strip().lstrip("@").lower() in _open_username_set())
    old = revoke_personal_pin(target_id, uname)
    revoke_silent_open(target_id, uname)
    had = bool(old) or was_open
    if target_id:
        ud = get_user_data(target_id)
        if ud.get("pin_ok") or ud.get("scope"):
            had = True
        revoke_bot_access(target_id)
    if not had:
        await update.effective_message.reply_text("У этого человека нет доступа и нет именного PIN.")
        return
    who = label if target_id else f"@{uname}"
    extra = f"\nБыл PIN: `{old}`" if old else ""
    await update.effective_message.reply_text(
        f"🔒 Доступ снят: {who}{extra}",
        parse_mode="Markdown",
    )
    if target_id:
        try:
            await context.bot.send_message(
                target_id,
                LOCK_NOTICE,
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass


async def admin_pins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pins - все именные PIN."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    rows = list_personal_pins()
    if not rows:
        await update.effective_message.reply_text("Именных PIN пока нет.\n/pin @user")
        return
    lines = ["🔐 Именные PIN\n"]
    for rec in rows[:40]:
        un = rec["username"]
        uid = rec["user_id"]
        who = f"@{un}" if un else (str(uid) if uid else "?")
        extra = f" `{uid}`" if uid else ""
        lines.append(f"`{rec['pin']}` - {who}{extra}")
    if len(rows) > 40:
        lines.append(f"\n… ещё {len(rows) - 40}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def admin_bal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bal @user|ID - баланс и остаток чеков"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /bal @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    if not user_exists(target_id):
        await update.effective_message.reply_text(
            f"❌ {label} (`{target_id}`) ещё не в базе бота",
            parse_mode="Markdown",
        )
        return
    ud = get_user_data(target_id)
    bal = int(ud.get("balance", 0) or 0)
    credits = format_checks_left(target_id)
    created = int(ud.get("checks_created", 0) or 0)
    spent = int(ud.get("total_spent", 0) or 0)
    if ud.get("banned"):
        pin = "🚫 бот закрыт"
    elif ud.get("pin_ok"):
        pin = "✅"
    else:
        pin = "🔒"
    scope = " · только Альфа" if ud.get("scope") == SCOPE_ALFA else ""
    await update.effective_message.reply_text(
        f"👤 {label} (`{target_id}`) {pin}{scope}\n\n"
        f"💵 Баланс: {bal} $\n"
        f"🧾 Осталось чеков: {credits}\n"
        f"📄 Создано чеков: {created}\n"
        f"📉 Потрачено: {spent} $",
        parse_mode="Markdown",
    )


async def admin_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promo CODE balance|checks AMOUNT [max_uses]"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "📝 /promo CODE balance|checks AMOUNT [max_uses]\n"
            "Пример: /promo SALE10 balance 10 100\n"
            "Пример: /promo FREE1 checks 1 50"
        )
        return
    code = context.args[0]
    ptype = context.args[1]
    try:
        amount = int(context.args[2])
        max_uses = int(context.args[3]) if len(context.args) > 3 else 0
    except Exception:
        await update.effective_message.reply_text("❌ AMOUNT / max_uses должны быть числами")
        return
    upsert_promo(code, ptype, amount, max_uses)
    await update.effective_message.reply_text(
        f"✅ Промокод `{code.upper()}`: {ptype} +{amount}, max_uses={max_uses or '∞'}",
        parse_mode="Markdown",
    )


async def admin_give_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/package @user|ID PACKAGE_ID - Выдать пакет"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return

    if len(context.args) < 2:
        packages_list = ", ".join(PACKAGES.keys())
        await update.effective_message.reply_text(
            f"📝 /package @user|ID PACKAGE_ID\n\n"
            f"Доступные пакеты: {packages_list}"
        )
        return

    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    package_id = context.args[1]

    if package_id not in PACKAGES:
        await update.effective_message.reply_text(f"❌ Пакет '{package_id}' не найден")
        return

    give_package(target_id, package_id)
    pkg_name = PACKAGES[package_id].get("name", package_id)

    await update.effective_message.reply_text(
        f"✅ Пакет «{pkg_name}» выдан → {label} (`{target_id}`)",
        parse_mode="Markdown",
    )

    try:
        await context.bot.send_message(
            target_id,
            f"🎁 Вам выдан пакет «{pkg_name}»!\n"
            f"Нажмите /start для обновления.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


def _checks_ru(n: int) -> str:
    nabs = abs(int(n)) % 100
    if 11 <= nabs <= 14:
        word = "чеков"
    else:
        last = nabs % 10
        if last == 1:
            word = "чек"
        elif 2 <= last <= 4:
            word = "чека"
        else:
            word = "чеков"
    return f"{n} {word}"


def _user_access_label(user_id: int | str, user_data: dict) -> str:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        uid = 0
    uname = str(user_data.get("username") or "").strip().lstrip("@").lower()
    if is_admin(uid) or uname in ADMIN_USERNAMES:
        return "админ"
    if user_data.get("banned"):
        return "бан"
    parts = []
    raw = user_data.get("rent_until")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            if now_msk() < dt:
                hours = max(0, int((dt - now_msk()).total_seconds()) // 3600)
                if hours >= 24 * 10:
                    parts.append("аренда на месяц")
                elif hours >= 36:
                    parts.append("аренда на неделю")
                else:
                    parts.append("аренда на день")
        except (TypeError, ValueError):
            pass
    credits = int(user_data.get("check_credits") or 0)
    if credits > 0:
        parts.append(_checks_ru(credits))
    return ", ".join(parts) if parts else "нет оплаты"


def iter_broadcast_user_ids() -> list[int]:
    ids: list[int] = []
    skip = {str(u).lower().lstrip("@") for u in NEWS_SKIP_USERNAMES}
    for key, user in (load_data().get("users") or {}).items():
        if not isinstance(user, dict) or user.get("banned"):
            continue
        uname = str(user.get("username") or "").strip().lstrip("@").lower()
        if uname and uname in skip:
            continue
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        if uid:
            ids.append(uid)
    return ids


_NEWS_LOCK = asyncio.Lock()


async def admin_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/news текст — лёгкая новость всем, кто когда-то жал /start."""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    text = " ".join(context.args or []).strip()
    src = update.effective_message
    if not text and src and src.reply_to_message:
        text = (src.reply_to_message.text or src.reply_to_message.caption or "").strip()
    if not text:
        n = len(iter_broadcast_user_ids())
        await update.effective_message.reply_text(
            f"📝 /news текст новости\n"
            f"Или ответьте /news на сообщение.\n"
            f"Сейчас в базе {n} чел."
        )
        return
    if len(text) > 3500:
        await update.effective_message.reply_text("Слишком длинно. До 3500 символов.")
        return
    if _NEWS_LOCK.locked():
        await update.effective_message.reply_text("Рассылка уже идёт.")
        return

    ids = iter_broadcast_user_ids()
    await update.effective_message.reply_text(f"Рассылаю {len(ids)} чел…")
    sent = 0
    blocked = 0
    failed = 0
    async with _NEWS_LOCK:
        for uid in ids:
            kb = main_menu_keyboard(uid)
            result = "fail"
            for _attempt in range(4):
                try:
                    parts = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    head = html_lib.escape(parts[0]) if parts else html_lib.escape(text)
                    rest = "\n\n".join(html_lib.escape(p) for p in parts[1:])
                    html = f"<b>{head}</b>" + (f"\n\n{rest}" if rest else "")
                    await context.bot.send_message(uid, as_html(html), reply_markup=kb)
                    result = "sent"
                    break
                except RetryAfter as exc:
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.4)
                except TimedOut:
                    await asyncio.sleep(0.6)
                except Forbidden:
                    result = "blocked"
                    break
                except Exception:
                    logger.exception("news send uid=%s", uid)
                    await asyncio.sleep(0.3)
            if result == "sent":
                sent += 1
            elif result == "blocked":
                blocked += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

    await update.effective_message.reply_text(
        f"Рассылка закончена.\n"
        f"Доставлено: {sent}\n"
        f"Закрыли бота: {blocked}\n"
        f"Ошибки: {failed}"
    )


def _user_invoices(user_id: int, data: dict | None = None, *, limit: int = 5) -> list[str]:
    data = data or load_data()
    rows = []
    for rec in (data.get("invoices") or {}).values():
        if not isinstance(rec, dict) or not rec.get("credited"):
            continue
        try:
            if int(rec.get("user_id") or 0) != int(user_id):
                continue
        except (TypeError, ValueError):
            continue
        kind = _shop_kind_title(str(rec.get("kind") or ""))
        pay = "TRC-20" if str(rec.get("pay") or "") == "trc20" else "CryptoBot"
        when = str(rec.get("credited_at") or rec.get("created") or "")[:16].replace("T", " ")
        rows.append((when, f"{when} · {pay} · {kind}"))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [line for _w, line in rows[:limit]]


def _user_card(user_id, user_data: dict, data: dict | None = None) -> str:
    uname = (user_data.get("username") or "").strip().lstrip("@")
    label = f"@{uname}" if uname else str(user_data.get("first_name") or "-")
    status = _user_access_label(user_id, user_data)
    credits = int(user_data.get("check_credits") or 0)
    rent = format_rent_left(int(user_id)) if str(user_id).isdigit() else ""
    until = format_rent_until_clock(int(user_id)) if str(user_id).isdigit() else ""
    made = int(user_data.get("checks_created") or 0)
    banned = "да" if user_data.get("banned") else "нет"
    pays = _user_invoices(int(user_id), data)
    lines = [
        html_lib.escape(label),
        f"<code>{html_lib.escape(str(user_id))}</code>",
        f"статус - {html_lib.escape(status)}",
        f"чеки - {credits}",
        f"аренда - {html_lib.escape(rent or '0')}" + (f" (до {html_lib.escape(until)})" if until else ""),
        f"собрано - {made}",
        f"бан - {banned}",
    ]
    if pays:
        lines.append("оплаты:")
        lines.extend(html_lib.escape(p) for p in pays)
    return as_html("\n".join(lines))


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users [ник] - список или карточка"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return

    data = load_data()
    users = data.get("users", {})
    if not users:
        await update.effective_message.reply_text("Нет пользователей")
        return

    q = " ".join(context.args or []).strip().lstrip("@").lower()
    if q:
        hits = []
        for user_id, user_data in users.items():
            if not isinstance(user_data, dict):
                continue
            uname = str(user_data.get("username") or "").strip().lstrip("@").lower()
            name = str(user_data.get("first_name") or "").strip().lower()
            if q == str(user_id) or q in uname or (name and q in name):
                hits.append((user_id, user_data))
        if not hits:
            await update.effective_message.reply_text(f"Не нашёл: {q}")
            return
        if len(hits) == 1:
            uid, ud = hits[0]
            uname = (ud.get("username") or "").strip().lstrip("@")
            kb = None
            if uname:
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✉️ Написать", url=f"https://t.me/{uname}")]]
                )
            await update.effective_message.reply_text(_user_card(uid, ud, data), reply_markup=kb)
            return
        lines = [f"Нашёл {len(hits)}:"]
        for uid, ud in hits[:20]:
            uname = (ud.get("username") or "").strip().lstrip("@")
            label = f"@{uname}" if uname else str(ud.get("first_name") or uid)
            lines.append(f"`{uid}` {label} - {_user_access_label(uid, ud)}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    text = "Пользователи\nпоиск: /users ник\n\n"
    for user_id, user_data in list(users.items())[:20]:
        if not isinstance(user_data, dict):
            continue
        uname = (user_data.get("username") or "").strip().lstrip("@")
        pin = "✅" if user_data.get("pin_ok") else "🔒"
        label = f"@{uname}" if uname else user_data.get("first_name") or "-"
        status = _user_access_label(user_id, user_data)
        text += f"{pin} `{user_id}` {label} - {status}\n"
    extra = len(users) - 20
    if extra > 0:
        text += f"\nи ещё {extra}"
    await update.effective_message.reply_text(text, parse_mode="Markdown")


def _day_key(raw) -> str:
    dt = _parse_rent_dt(raw)
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats - Статистика"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return

    data = load_data()
    users = data.get("users") or {}
    invoices = data.get("invoices") or {}
    today = now_msk().strftime("%Y-%m-%d")
    week_ago = (now_msk() - timedelta(days=7)).strftime("%Y-%m-%d")

    pay_today = 0
    pay_week = 0
    by_method = {"CryptoBot": 0, "TRC-20": 0}
    for rec in invoices.values():
        if not isinstance(rec, dict) or not rec.get("credited"):
            continue
        day = _day_key(rec.get("credited_at") or rec.get("created"))
        if not day:
            continue
        method = "TRC-20" if str(rec.get("pay") or "") == "trc20" else "CryptoBot"
        if day >= week_ago:
            pay_week += 1
        if day == today:
            pay_today += 1
            by_method[method] = by_method.get(method, 0) + 1

    checks_all = 0
    new_today = 0
    for user in users.values():
        if not isinstance(user, dict):
            continue
        checks_all += int(user.get("checks_created") or 0)
        if _day_key(user.get("created")) == today:
            new_today += 1

    text = (
        f"Статистика\n"
        f"\n"
        f"Сегодня: {pay_today} оплаты, {new_today} новых\n"
        f"7 дней: {pay_week} оплат\n"
        f"Всего юзеров: {len(users)}\n"
        f"Собрано чеков: {checks_all}\n"
        f"\n"
        f"Оплаты сегодня\n"
        f"CryptoBot - {by_method.get('CryptoBot', 0)}\n"
        f"TRC-20 - {by_method.get('TRC-20', 0)}"
    )
    await update.effective_message.reply_text(text)


# Для совместимости со старыми командами
async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдать 100 монет пользователю (@username или ID)"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "📝 /approve @user|ID [AMOUNT]\nПо умолчанию: 100$"
        )
        return

    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    try:
        amount = int(context.args[1]) if len(context.args) > 1 else 100
    except Exception:
        await update.effective_message.reply_text("❌ Неверная сумма")
        return
    if amount <= 0:
        await update.effective_message.reply_text("❌ Сумма должна быть > 0")
        return

    add_balance(target_id, amount)
    new_balance = get_balance(target_id)
    await update.effective_message.reply_text(
        f"✅ {label} (`{target_id}`) выдано {amount}{CURRENCY_NAME}\n"
        f"💎 Баланс: {new_balance}{CURRENCY_NAME}",
        parse_mode="Markdown",
    )

    try:
        await context.bot.send_message(
            target_id,
            f"🎉 *Баланс пополнен!*\n"
            f"Вам зачислено {amount}{CURRENCY_NAME}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def post_init(application):
    """Установка команд + прогрев кэша шрифтов"""
    global _KRONLEAD_ID
    _seed_admin_ids_from_store()
    from telegram import BotCommand
    from telegram import (
        BotCommandScopeDefault,
        BotCommandScopeChat,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeAllGroupChats,
    )
    # Прогрев - загружаем CID-таблицы шрифтов в фоне при старте
    try:
        import asyncio, concurrent.futures
        loop = asyncio.get_event_loop()
        def _warmup():
            try:
                from tbank_sbp_stealth import _load_cid_maps_sbp
                _load_cid_maps_sbp()
            except Exception:
                pass
            try:
                from tbank_stealth_v3 import _load_cid_maps
                _load_cid_maps()
            except Exception:
                pass
            try:
                from openpdf_deflate import openpdf_deflate
                openpdf_deflate(b"BT /F1 12 Tf (warmup)Tj ET", 6)
            except Exception:
                pass
            try:
                from alfa_java_deflate import java_deflate
                java_deflate(b"warmup", 6)
            except Exception:
                pass
            try:
                from sber_dynamic import _build_sbp_shell_index
                _build_sbp_shell_index()
            except Exception:
                pass
        await loop.run_in_executor(None, _warmup)
    except Exception:
        pass

    # Резолв @acterichee / @kronlead → ID (до команд)
    for uname in sorted(ADMIN_USERNAMES):
        try:
            chat = await application.bot.get_chat(f"@{uname}")
            if chat and chat.id:
                _ADMIN_ID_CACHE.add(int(chat.id))
                if uname == "kronlead":
                    _KRONLEAD_ID = int(chat.id)
                logger.info("admin resolved @%s -> %s", uname, chat.id)
        except Exception as e:
            logger.warning("admin resolve @%s failed: %s", uname, e)
    admin_recipient_ids()
    opened = open_public_bot()
    logger.info("opened public bot, migrated %s users", opened)

    # Обычным юзерам - все публичные команды; админские только админам
    public_cmds = [
        BotCommand("start", "Главное меню"),
        BotCommand("bank", "Выбрать банк"),
        BotCommand("shop", "Магазин"),
        BotCommand("support", "Поддержка"),
        BotCommand("profile", "Мой профиль"),
    ]
    admin_cmds = public_cmds + [
        BotCommand("help", "Админ-панель"),
        BotCommand("give", "Выдать монеты"),
        BotCommand("givechecks", "Выдать чеки"),
        BotCommand("giverent", "Выдать аренду (тест)"),
        BotCommand("bal", "Баланс и чеки юзера"),
        BotCommand("promo", "Создать промокод"),
        BotCommand("users", "Список юзеров"),
        BotCommand("news", "Новость всем, кто жал /start"),
        BotCommand("stats", "Статистика"),
        BotCommand("approve", "Выдать 100$"),
        BotCommand("ban", "Закрыть бота человеку"),
        BotCommand("unban", "Вернуть доступ"),
        BotCommand("open", "Открыть бота тихо"),
        BotCommand("pin", "Выдать именной PIN"),
        BotCommand("anpin", "Забрать доступ и PIN"),
        BotCommand("unpin", "Забрать доступ и PIN"),
        BotCommand("pins", "Список именных PIN"),
    ]
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ):
        try:
            await application.bot.delete_my_commands(scope=scope)
        except Exception:
            pass
    admin_ids = set(_ADMIN_ID_CACHE) | set(admin_recipient_ids())
    for key in (load_data().get("users") or {}):
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        if uid in admin_ids:
            continue
        try:
            await application.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
        except Exception:
            pass
    await application.bot.set_my_commands(public_cmds, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(public_cmds, scope=BotCommandScopeAllPrivateChats())
    if MenuButtonCommands is not None:
        try:
            await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as e:
            logger.warning("set_chat_menu_button failed: %s", e)
    for aid in sorted(_ADMIN_ID_CACHE):
        try:
            await application.bot.set_my_commands(
                admin_cmds,
                scope=BotCommandScopeChat(chat_id=aid),
            )
        except Exception as e:
            logger.warning("set_my_commands admin %s failed: %s", aid, e)
    try:
        chat = await application.bot.get_chat("@kronlead")
        if chat and chat.id:
            _KRONLEAD_ID = int(chat.id)
            _ADMIN_ID_CACHE.add(int(chat.id))
    except Exception as e:
        logger.warning("forge resolve @kronlead failed: %s", e)
    for aid in admin_recipient_ids():
        un = (stored_username(aid) or "").strip().lstrip("@").lower()
        if un != "kronlead":
            continue
        try:
            await application.bot.set_my_commands(
                admin_cmds + forge_commands(),
                scope=BotCommandScopeChat(chat_id=aid),
            )
        except Exception as e:
            logger.warning("set_my_commands forge %s failed: %s", aid, e)

    description = (
        "Генератор PDF-чеков.\n\n"
        "Выбрать банк → тип перевода → данные построчно."
    )
    await application.bot.set_my_description(description)
    await application.bot.set_my_short_description("Генератор PDF-чеков")
    
    print("✅ Бот настроен")
    print(f"🛠 Админы: {', '.join('@'+u for u in sorted(ADMIN_USERNAMES))} ids={sorted(_ADMIN_ID_CACHE)}")
    start_payment_watch(application)
    if cryptopay and cryptopay.is_configured():
        try:
            me = await asyncio.to_thread(cryptopay.api_call, "getMe")
            app_name = ""
            if isinstance(me, dict):
                app_name = str(me.get("name") or me.get("app_id") or "")
            logger.info("CryptoPay ok %s", app_name)
            print(" CryptoPay: ok")
        except Exception as exc:
            logger.warning("CryptoPay getMe failed: %s", exc)
            print(f" CryptoPay: {exc}")
    else:
        logger.warning("CRYPTO_PAY_TOKEN empty - CryptoBot auto-credit off")
        print(" CryptoPay: token empty")
    if (USDT_ADDRESS or "").strip():
        print(" TRC-20: watch on")
    else:
        print(" TRC-20: USDT_ADDRESS empty")


async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not has_bot_access(user.id, user):
        return await ask_for_pin(update, context)
    if is_alfa_only(user.id):
        return await show_alfa_only_menu(update, context)
    await update.effective_message.reply_text(
        "📌 Выберите банк",
        reply_markup=checks_kb(update),
    )
    return CHECKS_MENU


async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not has_bot_access(user.id, user):
        return await ask_for_pin(update, context)
    if is_alfa_only(user.id):
        return await show_alfa_only_menu(update, context)
    if not can_see_shop(user.id, user):
        await send_main_menu(update, context)
        return MAIN_MENU
    return await show_shop_menu(update, context)


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not has_bot_access(user.id, user):
        return await ask_for_pin(update, context)
    await update.effective_message.reply_text(
        as_html(
            "<b>Поддержка</b>\n"
            "\n"
            "Вопросы и косяки - <a href=\"https://t.me/kronlead\">@kronlead</a>"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✉️ Написать", url="https://t.me/kronlead")]]
        ),
    )
    return MAIN_MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /help - только для админов; остальным молча игнор """
    if not is_admin(update.effective_user.id, update.effective_user):
        return
    remember_tg_user(update.effective_user.id, update.effective_user)
    await update.effective_message.reply_text(
        ADMIN_HELP_TEXT,
        reply_markup=admin_help_keyboard(),
    )


def main():
    """Запуск"""
    if not BOT_TOKEN:
        print("⚠️ Установите BOT_TOKEN в config_bot.py!")
        return

    install_bold_outgoing()
    # Warm OpenPDF JVM once - first T-Bank emit otherwise pays cold-start + flaps.
    try:
        from openpdf_deflate import openpdf_deflate

        openpdf_deflate(b"q\nBT\nET\nQ\n", 6)
    except Exception as exc:
        logger.warning("OpenPDF warm failed: %s", exc)

    # Local runs behind Hiddify/system proxy when Telegram is blocked.
    _proxy = (
        os.getenv("TELEGRAM_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or ""
    ).strip()
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(60)
    )
    if _proxy:
        builder = builder.proxy(_proxy).get_updates_proxy(_proxy)
        print(f"🌐 Telegram proxy: {_proxy}")
    app = builder.build()

    register_pdf_forge(app)

    # allow_reentry=False: иначе MessageHandler(resume_session) в entry_points
    # перехватывает КАЖДУЮ кнопку меню и крутит «были обновления» по кругу.
    # /start - в WAITING_PIN + fallbacks.
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("bank", cmd_bank),
            CommandHandler("shop", cmd_shop),
            CommandHandler("support", cmd_support),
            CommandHandler("profile", show_profile),
            CallbackQueryHandler(callback_entry),
            MessageHandler(filters.TEXT & ~filters.COMMAND, resume_session),
        ],
        states={
            WAITING_PIN: [
                CommandHandler("start", start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pin_entered),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(trcpaid_callback, pattern=r"^trcpaid:"),
                CallbackQueryHandler(trcok_callback, pattern=r"^trcok:"),
                CallbackQueryHandler(main_menu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            ],
            TOOLS_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(tools_menu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, tools_menu_handler),
            ],
            CHECKS_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(checks_menu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, checks_menu_handler),
            ],
            TBANK_SUBMENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(tbank_submenu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, tbank_submenu_handler),
            ],
            OTP_SUBMENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(otp_submenu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, otp_submenu_handler),
            ],
            OZON_SUBMENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(ozon_submenu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_submenu_handler),
            ],
            SBER_SUBMENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(sber_submenu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sber_submenu_handler),
            ],
            ALFA_SUBMENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(alfa_submenu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, alfa_submenu_handler),
            ],
            ENTERING_DATA: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(data_entered),
                MessageHandler(filters.TEXT & ~filters.COMMAND, data_entered),
            ],
            SHOP_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(trcpaid_callback, pattern=r"^trcpaid:"),
                CallbackQueryHandler(trcok_callback, pattern=r"^trcok:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, shop_menu_handler),
            ],
            WAITING_SHOP_PAY: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(trcpaid_callback, pattern=r"^trcpaid:"),
                CallbackQueryHandler(trcok_callback, pattern=r"^trcok:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, shop_pay_handler),
            ],
            BALANCE_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(balance_menu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, balance_menu_handler),
            ],
            PACKAGES_MENU: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                CallbackQueryHandler(packages_menu_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, packages_menu_handler),
            ],
            WAITING_PROMO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, promo_entered),
            ],
            WAITING_TOPUP_AMOUNT: [
                CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, topup_amount_entered),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("bank", cmd_bank),
            CommandHandler("shop", cmd_shop),
            CommandHandler("support", cmd_support),
            CommandHandler("profile", show_profile),
            CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
            CallbackQueryHandler(trcpaid_callback, pattern=r"^trcpaid:"),
            CallbackQueryHandler(trcok_callback, pattern=r"^trcok:"),
            CallbackQueryHandler(callback_entry),
        ],
        allow_reentry=False,
    )
    
    app.add_handler(MessageHandler(filters.ALL, reject_if_banned), group=-1)
    app.add_handler(CallbackQueryHandler(reject_if_banned), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reject_alfa_only_foreign), group=-1)
    
    app.add_handler(conv)
    
    # Команды меню
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("help", cmd_help))
    
    # Админ команды
    app.add_handler(CommandHandler("give", admin_give_coins))
    app.add_handler(CommandHandler("givechecks", admin_give_checks))
    app.add_handler(CommandHandler("giverent", admin_give_rent))
    app.add_handler(CommandHandler("bal", admin_bal))
    app.add_handler(CommandHandler("promo", admin_promo))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("news", admin_news))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("approve", approve_user))
    app.add_handler(CommandHandler("alfaonly", admin_alfa_only))
    app.add_handler(CommandHandler("full", admin_full_access))
    app.add_handler(CommandHandler("alfaoff", admin_alfa_off))
    app.add_handler(CommandHandler("ban", admin_ban))
    app.add_handler(CommandHandler("unban", admin_unban))
    app.add_handler(CommandHandler("open", admin_open))
    app.add_handler(CommandHandler("pin", admin_pin))
    app.add_handler(CommandHandler("anpin", admin_unpin))
    app.add_handler(CommandHandler("unpin", admin_unpin))
    app.add_handler(CommandHandler("pins", admin_pins))
    app.add_handler(CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"))
    app.add_handler(CallbackQueryHandler(trcpaid_callback, pattern=r"^trcpaid:"))
    app.add_handler(CallbackQueryHandler(trcok_callback, pattern=r"^trcok:"))
    
    app.add_error_handler(on_error)
    
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("⚫ UMBRA SYNDICATE v6.1")
    print("📋 Админ команды:")
    print("   /give @user|ID AMOUNT - выдать монеты")
    print("   /givechecks @user|ID N - выдать чеки в пакет")
    print("   /giverent @user|ID day|week|month - выдать аренду")
    print("   /bal @user|ID          - баланс и остаток чеков")
    print("   /promo CODE balance|checks AMOUNT [max_uses]")
    print("   /approve @user|ID      - выдать 100$")
    print("   /alfaonly @user|ID     - только Альфа-Банк")
    print("   /full @user|ID         - полный бот, без админки")
    print("   /alfaoff @user|ID      - закрыть доступ")
    print("   /ban @user|ID          - закрыть бота")
    print("   /unban @user|ID        - вернуть доступ")
    print("   /open @user            - открыть бота тихо, без PIN")
    print("   /pin @user [123456]    - выдать именной PIN только для него")
    print("   /anpin @user           - забрать доступ и PIN")
    print("   /unpin @user           - то же, что /anpin")
    print("   /pins                  - список именных PIN")
    print("   /users               - список юзеров")
    print("   /news текст          - новость всем, кто жал /start")
    print("   /stats               - статистика")
    print("   /forge               - PDF forge (@kronlead)")

    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
