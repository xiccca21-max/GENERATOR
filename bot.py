"""
Telegram бот UMBRA SYNDICATE
Версия 6.0 - Система валюты и пакетов
"""
import os
import json
import logging
import re
import random
import sys
import html as html_lib
from datetime import datetime, timedelta
from pathlib import Path
from time_msk import now_msk
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from telegram import BotCommand, Message, Bot
import urllib.request  # noqa: F401 - used via cryptopay

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
        f"<b>{html_lib.escape('📋 пример - ниже')}</b>",
    ]
    if notes:
        n_lines = notes.split("\n")
        parts.append("")
        parts.append(f"<b>{html_lib.escape(n_lines[0])}</b>")
        if len(n_lines) > 1:
            parts.append(html_lib.escape("\n".join(n_lines[1:])))
    msg = update.effective_message
    await msg.reply_text(as_html("\n".join(parts)), reply_markup=reply_markup)
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
LIVE_BANKS = frozenset({"sber", "tbank", "alfa"})  # strict50 50/50 OnlyPDF (2026-07-29)

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
    from sber_card_stealth import create_sber_card_stealth
    # OnlyPDF: donor original is flagged virtual printer - channel disabled until clean donor.
    SBER_CARD_AVAILABLE = False
except ImportError:
    SBER_CARD_AVAILABLE = False
    def create_sber_card_stealth(*_a, **_k):
        return None

# Альфа-Банк
try:
    from alfa_sbp_stealth import create_alfa_sbp_stealth, check_text as alfa_check_text
    ALFA_SBP_AVAILABLE = True
except ImportError:
    ALFA_SBP_AVAILABLE = False
    def create_alfa_sbp_stealth(*_a, **_k):
        return None
    def alfa_check_text(text): return []

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

ALFA_AVAILABLE = ALFA_SBP_AVAILABLE or ALFA_CARD_AVAILABLE or ALFA_PHONE_AVAILABLE
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
    from tbank_sbp_stealth import create_tbank_sbp_stealth, decode_sbp_operation_id
    TBANK_SBP_AVAILABLE = True
except ImportError:
    TBANK_SBP_AVAILABLE = False
    def create_tbank_sbp_stealth(*_a, **_k):
        return None
    def decode_sbp_operation_id(_): return None

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
SHOP_MENU = 14  # Баланс и проверки → пакеты / пополнение

# PIN только для новых пользователей (ещё нет записи в payments.json)
# PIN для входа
ACCESS_PIN = "2034"

# Рефералка: % от пополнения приглашённого → пригласившему
REFERRAL_PERCENT = 10

# ========== ЗАГРУЗКА КОНФИГА ==========
try:
    from config_bot import (
        BOT_TOKEN, ADMIN_IDS, CURRENCY_NAME, COINS_PER_USDT, MIN_DEPOSIT,
        TOOL_PRICES, PACKAGES, USDT_ADDRESS, PAYMENTS_FILE,
        TRIAL_ENABLED, TRIAL_USES,
        CHECK_PACKAGES, TOPUP_PRESETS_USDT, LARGE_DEPOSIT_USDT, CRYPTO_PAY_TOKEN,
        ADMIN_USERNAMES,
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
        "pack1": {"name": "1 чек", "checks": 1, "price_usdt": 2, "btn": "📦 1 чек - 2$"},
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

if CRYPTO_PAY_TOKEN and not os.getenv("CRYPTO_PAY_TOKEN"):
    os.environ["CRYPTO_PAY_TOKEN"] = CRYPTO_PAY_TOKEN

# Кэш ID админов (по username / getChat)
_ADMIN_ID_CACHE: set[int] = set(int(x) for x in (ADMIN_IDS or []) if str(x).isdigit() or isinstance(x, int))
ADMIN_USERNAMES = {str(u).lower().lstrip("@") for u in (ADMIN_USERNAMES or {"acterichee", "kronlead"})}

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
79991234567
Озон Банк
сейчас
40817810123456789012
авто
авто
Перевод"""

ALFA_CARD_EXAMPLE = """4875
2200151234567890
2202201234568275
сейчас
авто"""

ALFA_PHONE_EXAMPLE = """12500
Волков Д. В.
79131234511
сейчас
40817810123456780922
авто
авто"""

ALFA_EXAMPLE = ALFA_SBP_EXAMPLE

# ========== ТЕКСТЫ КНОПОК ==========
BTN_SELECT_BANK = "🏦 Выбрать банк"
BTN_GUARANTEE = "🛡 Гарантия"
BTN_PROFILE = "💼 Мой профиль"
BTN_BALANCE_CHECKS = "💵 Баланс и проверки"
BTN_PACKAGES = "📦 Приобрести пакеты"
BTN_BALANCE = "💰 Пополнить"
BTN_SUPPORT = "💀 Поддержка"
BTN_CLEAR = "🗑 Очистить следы"
BTN_HELP = "📖 Инфо"
BTN_COPY_REF = "📋 Скопировать ссылку"
BTN_PROMO = "🎟 Промокод"
BTN_TOPUP_CUSTOM = "💵 Ввести сумму USDT"
BTN_CRYPTOBOT = "🤖 Cryptobot"
BTN_USDT_TRC = "💵 USDT (trc-20)"
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

BTN_SBER = "🟢 Сбер"
BTN_VTB = "🔵 ВТБ"
BTN_OZON = "🟣 ОЗОН"
BTN_ALFA = "🔴 Альфа"
BTN_TBANK = "🟡 Т-Банк"
BTN_OTP = "🏛 ОТП Банк"

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
BTN_ALFA_CARD = "💳 Карта на карту"
BTN_ALFA_PHONE = "📱 По телефону (Альфа→Альфа)"

BTN_BACK = "◀️ Назад"
BTN_HOME = "🏠 На главную"

MAIN_MENU_TEXT = "📌 Выберите желаемую функцию"

GUARANTEE_TEXT = (
    "🛡 *Гарантия*\n\n"
    "Если чек не проходит в любом валидаторе "
    "(pdfchecker, SafeCheck и т.п.) - сумма за этот чек "
    "возвращается на баланс.\n\n"
    "Условие возврата:\n"
    "• пришлите доказательство (скрин, PDF ответ, валидатора)\n\n"
    "Поддержка: @kronlead"
)

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


def has_bot_access(user_id: int) -> bool:
    """Доступ только после верного PIN (флаг pin_ok)."""
    data = load_data()
    user = data.get("users", {}).get(str(user_id))
    if not user:
        return False
    return bool(user.get("pin_ok"))


def set_pending_referrer(user_id: int, referrer_id: int) -> None:
    """Запомнить реферера на диске (не теряется при рестарте бота)."""
    if not referrer_id or user_id == referrer_id:
        return
    data = load_data()
    pending = data.setdefault("pending_refs", {})
    pending[str(user_id)] = int(referrer_id)
    save_data(data)


def pop_pending_referrer(user_id: int) -> int | None:
    data = load_data()
    pending = data.get("pending_refs") or {}
    key = str(user_id)
    if key not in pending:
        return None
    raw = pending.pop(key)
    data["pending_refs"] = pending
    save_data(data)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def peek_pending_referrer(user_id: int) -> int | None:
    data = load_data()
    raw = (data.get("pending_refs") or {}).get(str(user_id))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def try_bind_referrer(user_id: int, referrer_id: int) -> bool:
    """Привязать реферера один раз (не себе)."""
    if not referrer_id or int(user_id) == int(referrer_id):
        logger.info("ref_bind skip self_or_empty uid=%s ref=%s", user_id, referrer_id)
        return False
    data = load_data()
    users = data.setdefault("users", {})
    key = str(user_id)
    ref_key = str(referrer_id)
    if ref_key not in users:
        logger.info("ref_bind skip no_referrer uid=%s ref=%s", user_id, referrer_id)
        return False
    if key not in users:
        logger.info("ref_bind skip no_user uid=%s ref=%s", user_id, referrer_id)
        return False
    existing = users[key].get("referrer_id")
    if existing:
        logger.info("ref_bind skip already uid=%s has=%s", user_id, existing)
        return False
    users[key]["referrer_id"] = int(referrer_id)
    save_data(data)
    logger.info("ref_bind OK uid=%s ref=%s", user_id, referrer_id)
    return True


def apply_pending_referrer(user_id: int) -> int | None:
    """Достать pending с диска и привязать. Вернуть id реферера при успехе."""
    ref = pop_pending_referrer(user_id)
    if not ref:
        return None
    if try_bind_referrer(user_id, ref):
        return ref
    # не удалось - вернём pending обратно, чтобы не потерять
    set_pending_referrer(user_id, ref)
    return None


def parse_referrer_arg(args) -> int | None:
    """Из /start ref_123 или /start 123."""
    if not args:
        return None
    raw = (args[0] or "").strip()
    if raw.lower().startswith("ref_"):
        raw = raw[4:]
    try:
        rid = int(raw)
        return rid if rid > 0 else None
    except (TypeError, ValueError):
        return None


def count_referrals(user_id: int) -> int:
    data = load_data()
    uid = int(user_id)
    total = 0
    for u in data.get("users", {}).values():
        try:
            if int(u.get("referrer_id") or 0) == uid:
                total += 1
        except (TypeError, ValueError):
            continue
    return total


def remember_tg_user(user_id: int, tg_user) -> None:
    """Сохранить @username / имя для отображения в рефералке."""
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
            "referral_earned": 0,
            "created": now_msk().isoformat(),
            "pin_ok": False,
        }
    uname = (getattr(tg_user, "username", None) or "").strip().lstrip("@")
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
    """Буквы+цифры в lower — Naval_pay_manager == Navalpaymanager."""
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
                    "referral_earned": 0,
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


async def format_inviter_label(bot, referrer_id: int) -> str:
    """@username пригласившего, иначе его ID."""
    rid = int(referrer_id)
    uname = stored_username(rid)
    if uname:
        return f"@{uname}"
    try:
        chat = await bot.get_chat(rid)
        if getattr(chat, "username", None):
            uname = str(chat.username).strip().lstrip("@")
            if uname:
                data = load_data()
                users = data.setdefault("users", {})
                if str(rid) in users:
                    users[str(rid)]["username"] = uname
                    save_data(data)
                return f"@{uname}"
    except Exception:
        pass
    return str(rid)


def mark_pin_ok(user_id: int) -> int | None:
    """После верного PIN - открыть доступ. Вернуть referrer_id если только что привязали."""
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
            "referral_earned": 0,
            "created": now_msk().isoformat(),
        }
    users[key]["pin_ok"] = True
    save_data(data)
    # Привязка реферера с диска (после создания записи пользователя)
    return apply_pending_referrer(user_id)


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
            "referral_earned": 0,
            "created": now_msk().isoformat(),
            # Не выдаём доступ автоматически - только после PIN / явного mark_pin_ok
            "pin_ok": False,
        }
        save_data(data)
    return data["users"][user_str]


def update_user_data(user_id: int, user_data: dict):
    """Обновление данных пользователя"""
    data = load_data()
    data["users"][str(user_id)] = user_data
    save_data(data)


def is_admin(user_id: int, tg_user=None) -> bool:
    """Админ только @acterichee / @kronlead (по ID-кэшу или username)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _ADMIN_ID_CACHE:
        return True
    candidates = set()
    if tg_user is not None:
        un = (getattr(tg_user, "username", None) or "").strip().lstrip("@").lower()
        if un:
            candidates.add(un)
    stored = stored_username(uid)
    if stored:
        candidates.add(stored.lower().lstrip("@"))
    if candidates & ADMIN_USERNAMES:
        _ADMIN_ID_CACHE.add(uid)
        return True
    return False


def note_admin_user(user_id: int, tg_user=None) -> None:
    """Если username из whitelist - запомнить ID."""
    is_admin(user_id, tg_user)


def admin_recipient_ids() -> list[int]:
    ids = set(_ADMIN_ID_CACHE)
    # Из payments.json - у кого сохранён admin username
    data = load_data()
    for uid, u in (data.get("users") or {}).items():
        un = (u.get("username") or "").strip().lstrip("@").lower()
        if un in ADMIN_USERNAMES:
            try:
                ids.add(int(uid))
                _ADMIN_ID_CACHE.add(int(uid))
            except (TypeError, ValueError):
                pass
    return sorted(ids)


def get_balance(user_id: int) -> int:
    """Реальный баланс (для отображения). Админы всё равно безлимитны при списании."""
    return int(get_user_data(user_id).get("balance", 0) or 0)


def add_balance(user_id: int, amount: int):
    """Добавление монет (без рефералки - для админ-выдач)."""
    user_data = get_user_data(user_id)
    user_data["balance"] = user_data.get("balance", 0) + amount
    update_user_data(user_id, user_data)


def credit_deposit(user_id: int, amount: int) -> int:
    """
    Пополнение баланса (CryptoBot и т.п.).
    Зачисляет amount пользователю и REFERRAL_PERCENT% его рефереру.
    Возвращает размер бонуса рефереру (0 если нет).
    """
    if amount <= 0:
        return 0
    user_data = get_user_data(user_id)
    user_data["balance"] = user_data.get("balance", 0) + amount
    user_data["total_deposited"] = user_data.get("total_deposited", 0) + amount
    update_user_data(user_id, user_data)
    return _pay_referral_bonus(user_id, amount)


def _pay_referral_bonus(user_id: int, base_amount: int) -> int:
    """Начислить рефереру % от base_amount (без изменения баланса покупателя)."""
    if base_amount <= 0:
        return 0
    user_data = get_user_data(user_id)
    referrer_id = user_data.get("referrer_id")
    if not referrer_id:
        return 0
    bonus = max(0, int(base_amount * REFERRAL_PERCENT / 100))
    if bonus <= 0:
        return 0
    ref_data = get_user_data(int(referrer_id))
    ref_data["balance"] = ref_data.get("balance", 0) + bonus
    ref_data["referral_earned"] = ref_data.get("referral_earned", 0) + bonus
    update_user_data(int(referrer_id), ref_data)
    return bonus


def get_check_credits(user_id: int) -> int:
    return int(get_user_data(user_id).get("check_credits", 0) or 0)


def status_bar(user_id: int) -> str:
    """Короткий статус: баланс + чеки в пакете."""
    return (
        f"Баланс: {get_balance(user_id)} $ · "
        f"Осталось чеков: {get_check_credits(user_id)}"
    )


def with_status(user_id: int, text: str) -> str:
    """Текст + статус-бар внизу."""
    t = (text or "").rstrip()
    bar = status_bar(user_id)
    if not t:
        return bar
    return f"{t}\n\n{bar}"


def add_check_credits(user_id: int, n: int) -> int:
    if n <= 0:
        return get_check_credits(user_id)
    ud = get_user_data(user_id)
    ud["check_credits"] = int(ud.get("check_credits", 0) or 0) + int(n)
    update_user_data(user_id, ud)
    return int(ud["check_credits"])


def spend_check_credit(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    ud = get_user_data(user_id)
    credits = int(ud.get("check_credits", 0) or 0)
    if credits <= 0:
        return False
    ud["check_credits"] = credits - 1
    update_user_data(user_id, ud)
    return True


def save_invoice_record(invoice_id: int | str, record: dict) -> None:
    data = load_data()
    inv = data.setdefault("invoices", {})
    inv[str(invoice_id)] = record
    save_data(data)


def get_invoice_record(invoice_id: int | str) -> dict | None:
    data = load_data()
    return (data.get("invoices") or {}).get(str(invoice_id))


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
    """Применить промокод. Возвращает (ok, message)."""
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


def list_referrals(user_id: int) -> list[tuple[str, dict]]:
    """Список (uid, user_dict) кого привёл user_id."""
    data = load_data()
    uid = int(user_id)
    out = []
    for k, u in (data.get("users") or {}).items():
        try:
            if int(u.get("referrer_id") or 0) == uid:
                out.append((k, u))
        except (TypeError, ValueError):
            continue
    return out


async def notify_admins(bot, text: str) -> None:
    for admin_id in admin_recipient_ids():
        try:
            await bot.send_message(int(admin_id), text)
        except Exception:
            pass


def spend_balance(user_id: int, amount: int) -> bool:
    """Списание монет"""
    if amount <= 0:
        return False
    if is_admin(user_id):
        return True  # Админы не платят

    user_data = get_user_data(user_id)
    if user_data.get("balance", 0) >= amount:
        user_data["balance"] -= amount
        user_data["total_spent"] = user_data.get("total_spent", 0) + amount
        update_user_data(user_id, user_data)
        return True
    return False


def has_unlimited(user_id: int, tool_id: str) -> bool:
    """Проверка безлимита на инструмент"""
    if is_admin(user_id):
        return True  # Админы имеют безлимит на всё
    
    user_data = get_user_data(user_id)
    packages = user_data.get("packages", [])
    
    for pkg in packages:
        # Проверяем срок действия
        if pkg.get("expires"):
            expires = datetime.fromisoformat(pkg["expires"])
            if now_msk() > expires:
                continue  # Пакет истёк
        
        # Проверяем инструменты
        unlimited = pkg.get("unlimited_tools", [])
        if "all" in unlimited or tool_id in unlimited:
            return True
    
    return False


def get_tool_price(tool_id: str) -> int:
    """Цена инструмента"""
    return TOOL_PRICES.get(tool_id, 2)


def can_use_tool(user_id: int, tool_id: str) -> tuple:
    """
    Проверка возможности использования инструмента
    Возвращает: (can_use: bool, reason: str, cost: int)
    """
    if is_admin(user_id):
        return True, "admin", 0

    if has_unlimited(user_id, tool_id):
        return True, "unlimited", 0

    if get_check_credits(user_id) > 0:
        return True, "credit", 0

    if TRIAL_ENABLED:
        user_data = get_user_data(user_id)
        trial_uses = user_data.get("trial_uses", {})
        max_trial = TRIAL_USES.get(tool_id, 0)
        used = trial_uses.get(tool_id, 0)
        if used < max_trial:
            return True, "trial", 0

    price = get_tool_price(tool_id)
    balance = get_balance(user_id)
    if balance >= price:
        return True, "paid", price

    return False, "no_balance", price


def use_tool(user_id: int, tool_id: str) -> bool:
    """Списать оплату за инструмент. False = не списывалось / не хватило."""
    _can, reason, cost = can_use_tool(user_id, tool_id)
    if not _can:
        return False

    if reason == "admin" or reason == "unlimited":
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

    if reason == "paid":
        return spend_balance(user_id, cost)

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
        for pkg in user_data.get("packages", []):
            try:
                if pkg.get("expires") and now_msk() > datetime.fromisoformat(pkg["expires"]):
                    continue
            except (TypeError, ValueError):
                continue
            unlimited = pkg.get("unlimited_tools", [])
            if "all" in unlimited or tool_id in unlimited:
                reason = "unlimited"
                break

        if not reason and int(user_data.get("check_credits", 0) or 0) > 0:
            reason = "credit"
        if not reason and TRIAL_ENABLED:
            trial_uses = user_data.get("trial_uses", {})
            if int(trial_uses.get(tool_id, 0) or 0) < int(TRIAL_USES.get(tool_id, 0) or 0):
                reason = "trial"
        if not reason:
            cost = get_tool_price(tool_id)
            if int(user_data.get("balance", 0) or 0) < cost:
                return False
            reason = "paid"

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


async def ensure_can_generate(update: Update, context: ContextTypes.DEFAULT_TYPE, tool_id: str) -> bool:
    """Перед генерацией PDF - хватает ли баланса/пакета."""
    user_id = update.effective_user.id
    ok, reason, price = can_use_tool(user_id, tool_id)
    if ok:
        return True
    await update.effective_message.reply_text(
        with_status(
            user_id,
            f"❌ Недостаточно средств.\n"
            f"Чек стоит {price} $.\n"
            f"Открой «{BTN_BALANCE_CHECKS}».",
        ),
        reply_markup=menu_kb(update),
    )
    return False


def give_package(user_id: int, package_id: str) -> bool:
    """Выдача пакета пользователю"""
    if package_id not in PACKAGES:
        return False
    
    pkg_config = PACKAGES[package_id]
    user_data = get_user_data(user_id)
    
    # Создаём запись о пакете
    package = {
        "id": package_id,
        "name": pkg_config.get("name", package_id),
        "unlimited_tools": pkg_config.get("unlimited_tools", []),
        "given_at": now_msk().isoformat(),
    }
    
    # Срок действия
    duration = pkg_config.get("duration_days", 0)
    if duration > 0:
        package["expires"] = (now_msk() + timedelta(days=duration)).isoformat()
    
    # Добавляем пакет
    if "packages" not in user_data:
        user_data["packages"] = []
    user_data["packages"].append(package)
    
    # Бонусные монеты
    bonus = pkg_config.get("bonus_coins", 0)
    if bonus > 0:
        user_data["balance"] = user_data.get("balance", 0) + bonus
    
    update_user_data(user_id, user_data)
    return True


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
    "/refs @user - рефералы\n"
    "/users - список юзеров\n"
    "/stats - статистика\n"
    "/approve @user - +100$\n\n"
    "Доступ только у @acterichee и @kronlead."
)


def main_menu_keyboard(user_id: int | None = None, tg_user=None):
    """2 столбца: слева банк/гарантия, справа баланс/профиль."""
    rows = [
        [BTN_SELECT_BANK, BTN_BALANCE_CHECKS],
        [BTN_GUARANTEE, BTN_PROFILE],
    ]
    if user_id is not None and is_admin(user_id, tg_user):
        rows.append([BTN_ADMIN_HELP])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def menu_kb(update: Update):
    u = update.effective_user
    return main_menu_keyboard(u.id if u else None, u)


def shop_menu_keyboard():
    """Пакеты чеков и пополнение баланса."""
    return ReplyKeyboardMarkup(
        [
            [BTN_PACKAGES, BTN_BALANCE],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def admin_help_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ADMIN_USERS, BTN_ADMIN_STATS],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def tools_keyboard():
    """legacy - сразу выбор банка"""
    return checks_keyboard()


def checks_keyboard():
    """Меню выбора банка"""
    return ReplyKeyboardMarkup(
        [
            [BTN_SBER, BTN_TBANK],
            [BTN_ALFA],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


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
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def alfa_submethod_keyboard():
    return ReplyKeyboardMarkup(
        [
            [BTN_ALFA_SBP, BTN_ALFA_CARD],
            [BTN_ALFA_PHONE],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


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


def pressed_text(update: Update) -> str:
    """Текст нажатой кнопки / сообщения."""
    raw = ""
    if update.callback_query and update.callback_query.data:
        raw = update.callback_query.data
    elif update.message and update.message.text:
        raw = update.message.text
    return normalize_btn_text(raw)


async def ack_press(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = MAIN_MENU_TEXT):
    uid = update.effective_user.id if update.effective_user else None
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
    """Просьба ввести PIN у нового пользователя."""
    user_id = update.effective_user.id
    if update.effective_user:
        remember_tg_user(user_id, update.effective_user)
    pending = peek_pending_referrer(user_id) or context.user_data.get("pending_referrer")
    extra = ""
    if pending:
        label = await format_inviter_label(context.bot, int(pending))
        extra = f"\n\n🤝 Вас пригласил: {label}"
    await update.effective_message.reply_text(
        "🔐 Для доступа введите PIN-код:" + extra,
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_PIN


async def pin_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверка PIN. Пока неверный - доступ закрыт."""
    text = normalize_btn_text((update.message.text or "").strip())
    # Пока ждём PIN, /start ref_… тоже должен обновить реферера
    if text.startswith("/start"):
        return await start(update, context)
    if text == ACCESS_PIN:
        user_id = update.effective_user.id
        if update.effective_user:
            remember_tg_user(user_id, update.effective_user)
        ctx_ref = context.user_data.get("pending_referrer")
        if ctx_ref:
            set_pending_referrer(user_id, int(ctx_ref))
        bound_ref = mark_pin_ok(user_id)
        context.user_data.pop("pending_referrer", None)
        if bound_ref:
            await _notify_referrer(
                context,
                bound_ref,
                invitee_id=user_id,
                invitee_user=update.effective_user,
            )
            who = tg_user_label(update.effective_user, user_id)
            await notify_admins(
                context.bot,
                f"🆕 Новый пользователь\n"
                f"{who} (`{user_id}`)\n"
                f"Реферер: {await format_inviter_label(context.bot, int(bound_ref))}",
            )
            label = await format_inviter_label(context.bot, int(bound_ref))
            await update.effective_message.reply_text(
                f"✅ Доступ открыт.\n🤝 Вас пригласил: {label}\n\n" + MAIN_MENU_TEXT,
                reply_markup=menu_kb(update),
            )
        else:
            who = tg_user_label(update.effective_user, user_id)
            await notify_admins(
                context.bot,
                f"🆕 Новый пользователь\n{who} (`{user_id}`)",
            )
            await update.effective_message.reply_text(
                "✅ Доступ открыт.\n\n" + MAIN_MENU_TEXT,
                reply_markup=menu_kb(update),
            )
        return MAIN_MENU

    await update.effective_message.reply_text(
        "❌ Неверный PIN-код. Доступ закрыт.\n\n"
        "Введите PIN-код:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_PIN


async def deny_without_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Блок команд вне диалога, пока новый пользователь не прошёл PIN."""
    if has_bot_access(update.effective_user.id):
        return
    await update.effective_message.reply_text(
        "🔒 Доступ закрыт. Нажмите /start и введите PIN-код."
    )


async def callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inline-кнопка без активной сессии (после рестарта и т.п.)."""
    await ack_press(update)
    user_id = update.effective_user.id
    if not has_bot_access(user_id):
        await update.effective_message.reply_text(
            "🔒 Доступ закрыт. Нажмите /start и введите PIN-код."
        )
        return ConversationHandler.END

    text = pressed_text(update)
    if text in (
        BTN_SELECT_BANK, BTN_PROFILE, BTN_GUARANTEE,
        BTN_SUPPORT, BTN_HELP, BTN_CLEAR, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES,
    ):
        return await main_menu_handler(update, context)
    if text in (BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP):
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
    if text in (BTN_ALFA_SBP, BTN_ALFA_CARD, BTN_ALFA_PHONE):
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


async def _notify_referrer(
    context: ContextTypes.DEFAULT_TYPE,
    referrer_id: int,
    invitee_id: int | None = None,
    invitee_user=None,
) -> None:
    who = tg_user_label(invitee_user, invitee_id)
    try:
        await context.bot.send_message(
            int(referrer_id),
            f"🎉 По вашей реферальной ссылке зарегистрировался {who}!",
        )
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало. Важно: payload ref_ID из deep-link."""
    # PTB кладёт payload в context.args; на всякий случай парсим и из текста
    pending_ref = parse_referrer_arg(context.args)
    if not pending_ref and update.message and update.message.text:
        parts = (update.message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            pending_ref = parse_referrer_arg([parts[1].strip()])

    # Не затираем pending_referrer, если новый /start без payload
    prev_pending = context.user_data.get("pending_referrer")
    context.user_data.clear()
    user_id = update.effective_user.id
    if update.effective_user:
        remember_tg_user(user_id, update.effective_user)

    if pending_ref:
        set_pending_referrer(user_id, pending_ref)
        context.user_data["pending_referrer"] = pending_ref
        logger.info("ref_start uid=%s pending_ref=%s args=%s", user_id, pending_ref, context.args)
    elif prev_pending:
        context.user_data["pending_referrer"] = prev_pending
        set_pending_referrer(user_id, int(prev_pending))
        logger.info("ref_start keep_prev uid=%s pending_ref=%s", user_id, prev_pending)
    else:
        logger.info("ref_start uid=%s no_payload args=%s text=%r",
                    user_id, context.args, (update.message.text if update.message else None))

    if not has_bot_access(user_id):
        return await ask_for_pin(update, context)

    # Уже в боте: реферера после PIN больше не привязываем (анти-абуз)
    await send_main_menu(update, context)
    return MAIN_MENU


async def resume_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """После перезапуска бота сессия пустая - любое сообщение возвращает в меню."""
    context.user_data.clear()
    user_id = update.effective_user.id
    if not has_bot_access(user_id):
        return await ask_for_pin(update, context)
    logger.info("resume_session uid=%s text=%r", user_id, update.message.text[:80] if update.message else "")
    await update.effective_message.reply_text(
        "🔄 В боте были небольшие обновления.\n\n"
        "Нажмите /start, чтобы продолжить работу.\n\n" + MAIN_MENU_TEXT,
        reply_markup=menu_kb(update),
    )
    return MAIN_MENU


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню"""
    await ack_press(update)
    text = pressed_text(update)

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
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
        )
        return CHECKS_MENU

    elif text == BTN_PROFILE:
        return await show_profile(update, context)

    elif text == BTN_COPY_REF:
        return await send_ref_link_only(update, context)

    elif text == BTN_BALANCE_CHECKS:
        return await show_shop_menu(update, context)

    elif text == BTN_PACKAGES:
        return await show_packages_menu(update, context)

    elif text == BTN_BALANCE:
        return await show_balance_menu(update, context)

    elif text == BTN_GUARANTEE:
        await send_with_back(
            update, context, GUARANTEE_TEXT, parse_mode="Markdown"
        )
        return MAIN_MENU
    
    elif text == BTN_SUPPORT:
        await send_with_back(
            update,
            context,
            "💀 *Связь с синдикатом*\n\n"
            "По всем вопросам:\n"
            "➤ @kronlead",
            parse_mode="Markdown",
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
    if not has_bot_access(user_id):
        await deny_without_access(update, context)
        return ConversationHandler.END
    user = update.effective_user
    user_data = get_user_data(user_id)

    if update.effective_user:
        remember_tg_user(user_id, update.effective_user)

    balance = f"{get_balance(user_id)} $"
    checks = int(user_data.get("checks_created", 0) or 0)
    spent = int(user_data.get("total_spent", 0) or 0)
    refs = count_referrals(user_id)
    earned = int(user_data.get("referral_earned", 0) or 0)
    credits = get_check_credits(user_id)
    bot_username = (context.bot.username or "").strip() or "pdf_forge_v1_bot"
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    context.user_data["ref_link"] = ref_link

    text = (
        f"💼 Мой профиль\n\n"
        f"ID: {user_id}\n"
        f"Имя: {user.full_name}\n\n"
        f"Баланс: {balance}\n"
        f"Потрачено: {spent} $\n"
        f"Создано чеков: {checks}\n"
        f"Осталось чеков: {credits}\n\n"
        f"🤝 Рефералка {REFERRAL_PERCENT}%\n"
        f"С пополнений друга - {REFERRAL_PERCENT}%.\n"
        f"Приглашено: {refs}\n"
        f"Заработано с рефералов: {earned} $\n\n"
        f"🔗 Твоя ссылка - нажми на сообщение ниже, чтобы скопировать:"
    )

    await update.effective_message.reply_text(text, reply_markup=profile_keyboard())
    await update.effective_message.reply_text(
        as_html(f"<code>{html_lib.escape(ref_link)}</code>")
    )
    return MAIN_MENU


async def send_ref_link_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отдельное сообщение - только ссылка (тап = копировать)."""
    user_id = update.effective_user.id
    link = context.user_data.get("ref_link")
    if not link:
        bot_username = (context.bot.username or "").strip() or "pdf_forge_v1_bot"
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    await update.effective_message.reply_text(
        as_html(f"<code>{html_lib.escape(link)}</code>")
    )
    return MAIN_MENU


async def show_shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Единое меню: пакеты + пополнение."""
    if not has_bot_access(update.effective_user.id):
        await deny_without_access(update, context)
        return ConversationHandler.END
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        with_status(uid, "📌 Выберите желаемую функцию"),
        reply_markup=shop_menu_keyboard(),
    )
    return SHOP_MENU


async def shop_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_press(update)
    text = pressed_text(update)
    if text == BTN_BACK or text == BTN_HOME:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text == BTN_PACKAGES:
        return await show_packages_menu(update, context)
    if text == BTN_BALANCE:
        return await show_balance_menu(update, context)
    if text == BTN_BALANCE_CHECKS:
        return await show_shop_menu(update, context)
    if text in (BTN_SELECT_BANK, BTN_PROFILE, BTN_GUARANTEE, BTN_ADMIN_HELP):
        return await main_menu_handler(update, context)
    await show_shop_menu(update, context)
    return SHOP_MENU


async def show_packages_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Покупка пакетов чеков."""
    if not has_bot_access(update.effective_user.id):
        await deny_without_access(update, context)
        return ConversationHandler.END
    uid = update.effective_user.id
    text = (
        f"📦 Приобрести пакеты\n\n"
        f"1 чек = {get_tool_price('sber_check')} $\n\n"
        f"📌 Выберите пакет"
    )
    await update.effective_message.reply_text(
        with_status(uid, text),
        reply_markup=packages_keyboard(),
    )
    return PACKAGES_MENU


async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор способа пополнения (Cryptobot / USDT TRC-20)."""
    if not has_bot_access(update.effective_user.id):
        await deny_without_access(update, context)
        return ConversationHandler.END
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        with_status(uid, "📌 Выберите способ пополнения из указанных"),
        reply_markup=balance_keyboard(),
    )
    return BALANCE_MENU


async def ask_topup_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """После Cryptobot - ввод суммы."""
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        with_status(uid, "📄 Введите сумму пополнения в USDT"),
        reply_markup=back_keyboard(),
    )
    return WAITING_TOPUP_AMOUNT


def _shop_keyboard_for_kind(kind: str):
    if kind in CHECK_PACKAGES:
        return packages_keyboard()
    return balance_keyboard()


def _shop_state_for_kind(kind: str) -> int:
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
    shop_kb = _shop_keyboard_for_kind(kind)
    shop_state = _shop_state_for_kind(kind)
    if not (cryptopay and cryptopay.is_configured()):
        await update.effective_message.reply_text(
            "⚠️ CryptoBot недоступен. Проверьте CRYPTO_PAY_TOKEN.",
            reply_markup=shop_kb,
        )
        return shop_state
    payload = json.dumps(
        {"uid": user_id, "kind": kind, "usdt": float(amount_usdt)},
        ensure_ascii=False,
    )
    import asyncio
    try:
        inv = await asyncio.to_thread(
            lambda: cryptopay.create_invoice(
                amount_usdt=amount_usdt,
                description=description,
                payload=payload,
            )
        )
    except Exception as e:
        logger.exception("create_invoice failed")
        await update.effective_message.reply_text(
            f"❌ Не удалось создать счёт: {e}",
            reply_markup=shop_kb,
        )
        return shop_state

    invoice_id = inv.get("invoice_id")
    pay_url = cryptopay.invoice_pay_url(inv)
    save_invoice_record(
        invoice_id,
        {
            "user_id": user_id,
            "kind": kind,
            "amount_usdt": float(amount_usdt),
            "credited": False,
            "created": now_msk().isoformat(),
        },
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Оплатить", url=pay_url)],
            [InlineKeyboardButton("✅ Я оплатил", callback_data=f"paycheck:{invoice_id}")],
        ]
    )
    await update.effective_message.reply_text(
        "⚡️ Заявка успешно создана. Нажмите на кнопку ниже, чтобы оплатить.",
        reply_markup=kb,
    )
    await update.effective_message.reply_text(
        with_status(user_id, "📌 После оплаты нажмите «Я оплатил»."),
        reply_markup=back_keyboard(),
    )
    return shop_state


async def fulfill_paid_invoice(bot, invoice_id: str, invoice: dict | None = None) -> tuple[bool, str]:
    """Зачислить оплату по invoice_id. (ok, message)."""
    rec = get_invoice_record(invoice_id)
    if not rec:
        return False, "Счёт не найден."
    if rec.get("credited"):
        return False, "Уже зачислено ранее."
    import asyncio
    if invoice is None:
        try:
            invoice = await asyncio.to_thread(cryptopay.get_invoice, invoice_id)
        except Exception as e:
            return False, f"Ошибка CryptoBot: {e}"
    if not invoice:
        return False, "Счёт не найден в CryptoBot."
    status = (invoice.get("status") or "").lower()
    if status != "paid":
        return False, "Статус: Оплата ещё не прошла ❌"

    user_id = int(rec["user_id"])
    kind = rec.get("kind") or "balance"
    usdt = float(rec.get("amount_usdt") or 0)
    try:
        paid_amt = float(invoice.get("amount") or 0)
    except (TypeError, ValueError):
        paid_amt = 0.0
    if paid_amt > 0:
        # Не зачислять больше, чем реально оплатили в CryptoBot
        usdt = min(usdt, paid_amt)
    if usdt <= 0:
        return False, "Некорректная сумма счёта."

    if not mark_invoice_credited(invoice_id):
        return False, "Уже зачислено ранее."

    coins = max(1, int(round(usdt * COINS_PER_USDT)))

    if kind in CHECK_PACKAGES:
        checks = int(CHECK_PACKAGES[kind]["checks"])
        add_check_credits(user_id, checks)
        bonus = _pay_referral_bonus(user_id, coins)
        msg = f"✅ Оплата получена: +{checks} чек(ов) в пакет."
        if bonus:
            msg += f"\nРефереру начислено {bonus} $."
        detail = f"pack {kind} +{checks} checks"
    else:
        bonus = credit_deposit(user_id, coins)
        msg = f"✅ Оплата получена: +{coins} $ на баланс."
        if bonus:
            msg += f"\nРефереру начислено {bonus} $."
        detail = f"balance +{coins}"

    who = tg_user_label(user_id=user_id)
    title = "💸 Крупное пополнение" if usdt >= float(LARGE_DEPOSIT_USDT) else "💸 Пополнение"
    await notify_admins(
        bot,
        f"{title}\n"
        f"Юзер: {who} (`{user_id}`)\n"
        f"Сумма: {usdt} USDT\n"
        f"{detail}",
    )
    return True, msg


async def packages_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        cfg = CHECK_PACKAGES[pack_id]
        user_id = update.effective_user.id
        price = int(cfg["price_usdt"])
        n = int(cfg["checks"])
        bal = get_balance(user_id)
        if bal < price and not is_admin(user_id, update.effective_user):
            await update.effective_message.reply_text(
                f"❌ Недостаточно средств.\n"
                f"Нужно {price} $, на балансе {bal} $.\n"
                f"Пополни баланс.",
                reply_markup=packages_need_topup_keyboard(),
            )
            return PACKAGES_MENU
        if not spend_balance(user_id, price):
            await update.effective_message.reply_text(
                "❌ Недостаточно средств. Пополни баланс.",
                reply_markup=packages_need_topup_keyboard(),
            )
            return PACKAGES_MENU
        add_check_credits(user_id, n)
        left = get_check_credits(user_id)
        await update.effective_message.reply_text(
            with_status(user_id, f"✅ +{n} чеков · осталось {left}"),
            reply_markup=packages_keyboard(),
        )
        return PACKAGES_MENU

    await show_packages_menu(update, context)
    return PACKAGES_MENU


async def balance_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if text == BTN_CRYPTOBOT:
        return await ask_topup_amount(update, context)
    if text == BTN_USDT_TRC:
        await update.effective_message.reply_text(
            "⚠️ USDT (trc-20) пока недоступен.\n📌 Выберите способ пополнения из указанных",
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
    if text == BTN_CRYPTOBOT:
        return await ask_topup_amount(update, context)
    if text == BTN_USDT_TRC:
        await update.effective_message.reply_text(
            "⚠️ USDT (trc-20) пока недоступен.",
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
    if owner and clicker and owner != clicker and not is_admin(clicker, update.effective_user):
        await update.effective_message.reply_text("❌ Это чужой счёт.")
        return MAIN_MENU
    ok, msg = await fulfill_paid_invoice(context.bot, invoice_id)
    if ok:
        uid = update.effective_user.id
        await update.effective_message.reply_text(
            with_status(uid, msg),
            reply_markup=home_keyboard(),
        )
        return MAIN_MENU
    await update.effective_message.reply_text(msg, reply_markup=back_keyboard())
    kind = rec.get("kind") or "balance"
    if kind in CHECK_PACKAGES:
        return PACKAGES_MENU
    return BALANCE_MENU


async def show_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await show_packages_menu(update, context)


async def tools_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """legacy: сразу в выбор банка"""
    await ack_press(update)
    text = pressed_text(update)
    if text == BTN_BACK:
        await send_main_menu(update, context)
        return MAIN_MENU
    if text in (
        BTN_CHECKS, BTN_SELECT_BANK, BTN_SPOOF, BTN_TELEGRAM,
        BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI,
    ):
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
        )
        return CHECKS_MENU
    return await checks_menu_handler(update, context)


async def checks_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню чеков"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id
    
    if text == BTN_BACK:
        await send_main_menu(update, context)
        return MAIN_MENU
    
    bank_map = {
        BTN_VTB: ('vtb', 'vtb_check', '🔵 ВТБ', VTB_EXAMPLE),
        BTN_SBER: ('sber', 'sber_check', '🟢 Сбербанк', SBER_EXAMPLE),
        BTN_OZON: ('ozon', 'ozon_check', '🟣 ОЗОН', None),
        BTN_ALFA: ('alfa', 'alfa_check', '🔴 Альфа-Банк', ALFA_EXAMPLE),
        BTN_TBANK: ('tbank', 'tbank_check', '🟡 Т-Банк', TBANK_EXAMPLE),
        BTN_OTP: ('otp', 'otp_check', '🏛 ОТП Банк', OTP_SBP_EXAMPLE),
    }
    
    if text in bank_map:
        bank_id, tool_id, bank_name, example = bank_map[text]

        if bank_id not in LIVE_BANKS:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Канал временно отключён (нет зелёной галки OnlyPDF).",
                reply_markup=checks_keyboard(),
                parse_mode="Markdown",
            )
            return CHECKS_MENU
        
        # Проверяем доступность
        # ОЗОН
        if bank_id == 'ozon' and not OZON_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Озон Банка не загружен.",
                reply_markup=checks_keyboard(),
                parse_mode='Markdown'
            )
            return CHECKS_MENU
        
        # Сбербанк
        if bank_id == 'sber' and not (SBER_SBP_AVAILABLE or SBER_PHONE_AVAILABLE or SBER_AVAILABLE):
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Сбербанка не загружен.",
                reply_markup=checks_keyboard(),
                parse_mode='Markdown'
            )
            return CHECKS_MENU
        
        # Альфа-Банк
        if bank_id == 'alfa' and not ALFA_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Альфа-Банка не загружен.",
                reply_markup=checks_keyboard(),
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        # Т-Банк
        if bank_id == 'tbank' and not TBANK_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль Т-Банка не загружен.",
                reply_markup=checks_keyboard(),
                parse_mode='Markdown'
            )
            return CHECKS_MENU

        # ОТП Банк
        if bank_id == 'otp' and not OTP_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                f"{bank_name}\n\n"
                f"⚠️ Модуль ОТП Банка не загружен.",
                reply_markup=checks_keyboard(),
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
        else:
            fields_text = (
                f"Сумма\n"
                f"Отправитель\n"
                f"Получатель\n"
                f"Телефон\n"
                f"Банк получателя\n"
                f"Дата (или 'сейчас')"
            )
        
        await send_data_entry_prompt(
            update,
            bank_name, fields_text, example or VTB_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA
    
    return CHECKS_MENU


async def tbank_submenu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подменю Т-Банка: СБП / карта в другой банк / карта в Т-Банк"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('tbank_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

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
        await send_data_entry_prompt(
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
        await send_data_entry_prompt(
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
        await send_data_entry_prompt(
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
        await send_data_entry_prompt(
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
        await send_data_entry_prompt(
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
    """Подменю ОТП Банка: СБП"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('otp_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

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
        await send_data_entry_prompt(
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
        await send_data_entry_prompt(
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
    """Подменю Ozon Банка: СБП"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('ozon_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

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
        await send_data_entry_prompt(
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
    """Подменю Сбербанка: СБП / телефон / карта"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('sber_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

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
            await update.effective_message.reply_text(
                "⚠️ Сбер «по карте в другой банк» отключён: нет OnlyPDF-чистого донора "
                "(оригинал ловится как virtual printer). Доступны только СБП и телефон.",
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

        await send_data_entry_prompt(
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
    """Подменю Альфа-Банка: СБП / карта на карту"""
    await ack_press(update)
    text = pressed_text(update)
    user_id = update.effective_user.id

    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)

    if text == BTN_BACK:
        context.user_data.pop('alfa_submethod', None)
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown',
        )
        return CHECKS_MENU

    if text == BTN_ALFA_SBP:
        if not ALFA_SBP_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа СБП не загружен.",
                reply_markup=alfa_submethod_keyboard(),
            )
            return ALFA_SUBMENU
        tool_id = context.user_data.get('tool_id', 'alfa_check')
        context.user_data['alfa_submethod'] = 'sbp'
        fields_text = (
            "Сумма\n"
            "Получатель\n"
            "Телефон\n"
            "Банк получателя\n"
            "Дата (или 'сейчас' / 'авто')\n"
            "Счёт списания (20 цифр)\n"
            "Номер операции (или 'авто')\n"
            "Номер СБП (или 'авто')\n"
            "Сообщение"
        )
        await send_data_entry_prompt(
            update,
            "🔴 Альфа-Банк - СБП", fields_text, ALFA_SBP_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_ALFA_CARD:
        if not ALFA_CARD_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «карта на карту» не загружен.",
                reply_markup=alfa_submethod_keyboard(),
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
        await send_data_entry_prompt(
            update,
            "🔴 Альфа-Банк - Карта на карту", fields_text, ALFA_CARD_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    if text == BTN_ALFA_PHONE:
        if not ALFA_PHONE_AVAILABLE:
            await update.effective_message.reply_text(
                "⚠️ Модуль Альфа «по телефону» не загружен.",
                reply_markup=alfa_submethod_keyboard(),
            )
            return ALFA_SUBMENU
        tool_id = context.user_data.get('tool_id', 'alfa_check')
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
        await send_data_entry_prompt(
            update,
            "🔴 Альфа-Банк - По телефону (Альфа→Альфа)",
                fields_text,
                ALFA_PHONE_EXAMPLE,
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA

    await update.effective_message.reply_text(
        "📌 Выберите способ перевода",
        reply_markup=alfa_submethod_keyboard(),
    )
    return ALFA_SUBMENU


PDF_GEN_TIMEOUT_SEC = 45


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
        logger.exception("PDF gate unavailable for %s: %s — ship without gate", method_hint, exc)

    if not isinstance(data, dict):
        logger.error("PDF generation rejected non-dict payload for %s", method_hint)
        return None
    if not method_hint.strip():
        logger.error("PDF generation rejected missing method hint")
        return None

    canonical = copy.deepcopy(data)
    extra_kwargs = dict(generator_kwargs or {})

    def _soften_payload(src: dict, *, aggressive: bool) -> dict:
        """Lookalike only for non-identity fields — never remap FIO/bank letters."""
        out = copy.deepcopy(src)
        # Product law: user FIO/bank/message stay exact (any Cyrillic+digits).
        # Soft-cover retries must not rewrite face text.
        return out

    for attempt in range(3):
        if attempt == 0:
            payload = copy.deepcopy(canonical)
        elif attempt == 1:
            payload = _soften_payload(canonical, aggressive=False)
            logger.warning("%s: soft-cover retry", method_hint)
        else:
            payload = _soften_payload(canonical, aggressive=True)
            logger.warning("%s: aggressive soft-cover retry", method_hint)
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
            logger.error("PDF generation timed out for %s", method_hint)
            if attempt >= 2:
                return None
            continue
        except Exception as exc:
            logger.exception(
                "PDF generation crashed for %s attempt=%d: %s",
                method_hint, attempt + 1, exc,
            )
            candidate = None
        if not candidate:
            await asyncio.sleep(0.05)
            continue
        if not isinstance(candidate, (bytes, bytearray)) or not bytes(candidate).startswith(b"%PDF-"):
            logger.error("Generator returned non-PDF output for %s", method_hint)
            await asyncio.sleep(0.05)
            continue
        pdf_out = bytes(candidate)
        # Advisory gate only — bot ALWAYS ships a PDF once bytes exist.
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
                        "PDF gate soft-fail %s: %s — ship PDF anyway",
                        method_hint, why,
                    )
            except Exception as exc:
                logger.exception(
                    "PDF gate crashed for %s: %s — ship PDF", method_hint, exc,
                )
        return pdf_out
    return None


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
    
    # Если пользователь нажал кнопки меню во время ввода данных,
    # перенаправляем в соответствующие обработчики, а не парсим как чек.
    if text in [BTN_SELECT_BANK, BTN_GUARANTEE, BTN_PROFILE, BTN_BALANCE_CHECKS, BTN_BALANCE, BTN_PACKAGES, BTN_COPY_REF, BTN_HOME, BTN_ADMIN_HELP, BTN_ADMIN_USERS, BTN_ADMIN_STATS, BTN_SUPPORT, BTN_CLEAR, BTN_HELP]:
        return await main_menu_handler(update, context)
    if text in [BTN_CHECKS, BTN_SPOOF, BTN_TELEGRAM, BTN_VOICE, BTN_CHAT_DRAW, BTN_DOC_DRAW, BTN_AI]:
        return await tools_menu_handler(update, context)
    if text in [BTN_SBER, BTN_VTB, BTN_OZON, BTN_ALFA, BTN_TBANK, BTN_OTP]:
        return await checks_menu_handler(update, context)
    if text in [BTN_TBANK_SBP, BTN_TBANK_PHONE, BTN_TBANK_CARD_OTHER, BTN_TBANK_CARD_NOCOMM, BTN_TBANK_CARD_TBANK, BTN_TBANK_STATEMENT]:
        return await tbank_submenu_handler(update, context)
    if text in [BTN_OTP_SBP, BTN_OTP_CARD]:
        return await otp_submenu_handler(update, context)
    if text in [BTN_OZON_SBP]:
        return await ozon_submenu_handler(update, context)
    if text in [BTN_SBER_SBP, BTN_SBER_PHONE, BTN_SBER_CARD_OTHER]:
        return await sber_submenu_handler(update, context)
    if text in [BTN_ALFA_SBP, BTN_ALFA_CARD, BTN_ALFA_PHONE]:
        return await alfa_submenu_handler(update, context)

    if text == BTN_BACK:
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
                reply_markup=alfa_submethod_keyboard(),
                parse_mode='Markdown',
            )
            return ALFA_SUBMENU
        await update.effective_message.reply_text(
            "📌 Выберите банк",
            reply_markup=checks_keyboard(),
            parse_mode='Markdown'
        )
        return CHECKS_MENU
    
    bank = context.user_data.get('bank', 'sber')
    tool_id = context.user_data.get('tool_id', 'sber_check')

    if not await ensure_can_generate(update, context, tool_id):
        return MAIN_MENU

    await update.effective_message.reply_text("⏳ Собираю PDF…")

    if bank not in LIVE_BANKS:
        await update.effective_message.reply_text(
            "⚠️ Канал временно отключён (нет OnlyPDF 30/30).",
            reply_markup=checks_keyboard(),
        )
        return CHECKS_MENU

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

    if bank == 'alfa' and context.user_data.get('alfa_submethod') not in ('sbp', 'card', 'phone'):
        await update.effective_message.reply_text(
            "🔴 Сначала выберите способ перевода Альфа-Банка.",
            reply_markup=alfa_submethod_keyboard(),
            parse_mode='Markdown',
        )
        return ALFA_SUBMENU
    
    # Проверяем возможность использования
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    # Telegram / клиенты иногда вставляют «copy» первой строкой - сдвигает все поля.
    while lines and (
        not re.search(r"\d", lines[0])
        or lines[0].lower() in ("copy", "скопировать", "вставить", "paste")
    ):
        lines = lines[1:]
    
    if len(lines) < 5:
        await update.effective_message.reply_text(
            "❌ *Недостаточно данных!*\n\n"
            "Нужно минимум 5 строк.\n"
            "Не вставляйте строку `copy` - только данные чека.",
            reply_markup=back_keyboard(),
            parse_mode='Markdown'
        )
        return ENTERING_DATA
    
    # Парсим данные
    amount_raw = lines[0]
    sender = lines[1]
    receiver = lines[2]
    phone = lines[3]
    recipient_bank = lines[4]
    
    # Дата
    if len(lines) >= 6:
        date_input = lines[5].strip().lower()
        if date_input in ['сейчас', 'now', '-']:
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
    if not (bank == 'sber' and context.user_data.get('sber_submethod') in ('sbp', 'phone')):
        digits = re.sub(r'[^\d]', '', phone)
        if len(digits) == 11:
            phone = f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    else:
        phone = phone_raw.strip()
    
    try:
        if bank == 'sber' and context.user_data.get('sber_submethod') == 'sbp':
            date_input = lines[5].strip() if len(lines) > 5 else "сейчас"
            if date_input.lower() in ('сейчас', 'now', '-', ''):
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
                'amount': amount_num,
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
                )
                return ENTERING_DATA
            bank_name = "Сбербанк (СБП)"
            filename = f"sber_sbp_{now_msk().strftime('%H%M%S')}.pdf"

        elif bank == 'sber' and context.user_data.get('sber_submethod') == 'phone':
            date_input = lines[5].strip() if len(lines) > 5 else "сейчас"
            if date_input.lower() in ('сейчас', 'now', '-', ''):
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
                'amount': amount_num,
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
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
            if date_input.lower() in ('сейчас', 'now', '-', ''):
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
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
                    reply_markup=back_keyboard(),
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
            pdf_bytes = await _run_pdf_sync(
                update, create_alfa_sbp_stealth, data=data, method_hint="alfa_sbp",
            )
            if not pdf_bytes:
                logger.error(
                    "Alfa SBP emit None data=%s",
                    {k: (str(v)[:40] if v else v) for k, v in data.items()},
                )
                await update.effective_message.reply_text(
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
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
                    "❌ *Недостаточно данных для Альфа «карта на карту»!*\n\n"
                    "Нужно 5 строк:\n"
                    "1. Сумма\n2. Карта отправителя\n3. Карта получателя\n"
                    "4. Дата (или 'сейчас')\n5. Номер операции (или 'авто')",
                    reply_markup=back_keyboard(),
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (карта)"
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
                    reply_markup=back_keyboard(),
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
                )
                return ENTERING_DATA
            bank_name = "Альфа-Банк (телефон)"
            now = now_msk()
            filename = f"alfa_phone_{now.strftime('%H%M%S')}.pdf"
            amount = lines[0]
            receiver = ph_receiver
            sender = "-"

        elif bank == 'alfa':
            await update.effective_message.reply_text(
                "🔴 Сначала выберите способ перевода Альфа-Банка.",
                reply_markup=alfa_submethod_keyboard(),
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

            if ph_date_raw.lower() in ('сейчас', 'now', '-', ''):
                ph_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                ph_date = ph_date_raw

            ph_phone_digits = re.sub(r'[^\d]', '', ph_phone)
            if len(ph_phone_digits) == 11:
                ph_phone = f"+7 ({ph_phone_digits[1:4]}) {ph_phone_digits[4:7]}-{ph_phone_digits[7:9]}-{ph_phone_digits[9:11]}"

            ph_amount_digits = re.sub(r'[^\d]', '', ph_amount)
            ph_amount_formatted = f"{int(ph_amount_digits):,}".replace(',', ' ') + ' ₽' if ph_amount_digits else ph_amount

            data = {
                'date_time':  ph_date,
                'amount':     ph_amount_digits or ph_amount,
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
            bank_name = "Т-Банк (По тел.)"
            _receipt_date = re.match(r'(\d{2}\.\d{2}\.\d{4})', ph_date)
            filename = f"receipt_{_receipt_date.group(1) if _receipt_date else now_msk().strftime('%d.%m.%Y')}.pdf"
            amount = ph_amount_formatted
            receiver = ph_receiver
            sender = ph_sender

        elif bank == 'tbank' and context.user_data.get('tbank_submethod') == 'sbp':
            # СБП: сумма, отправитель, телефон, получатель, [банк], дата, [ID СБП], [квитанция]
            # Банк можно не указывать — тогда Сбербанк; дата распознаётся по dd.mm.yyyy.
            sbp_amount   = lines[0] if len(lines) > 0 else "1230"
            sbp_sender   = lines[1] if len(lines) > 1 else "Отправитель"
            sbp_phone    = lines[2] if len(lines) > 2 else "+7 (965) 585-66-55"
            sbp_receiver = lines[3] if len(lines) > 3 else "Получатель"

            def _looks_like_date(s: str) -> bool:
                return bool(re.match(
                    r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}",
                    (s or "").strip(),
                ))

            rest = lines[4:]
            if rest and _looks_like_date(rest[0]):
                sbp_bank = "Сбербанк"
                sbp_date_raw = rest[0].strip()
                tail = rest[1:]
            else:
                sbp_bank = rest[0] if rest else "Сбербанк"
                sbp_date_raw = rest[1].strip() if len(rest) > 1 else "сейчас"
                tail = rest[2:]

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
                        reply_markup=back_keyboard(),
                    )
                    return ENTERING_DATA

            if sbp_date_raw.lower() in ('сейчас', 'now', '-', ''):
                # Pass through «сейчас» so prepare randomizes seconds 1–59
                # (expanding to HH:MM here made date_manual + :00 → ZERO_SECONDS).
                sbp_date = "сейчас"
            else:
                sbp_date = sbp_date_raw

            # Форматируем телефон
            sbp_phone_digits = re.sub(r'[^\d]', '', sbp_phone)
            if len(sbp_phone_digits) == 11:
                sbp_phone = f"+7 ({sbp_phone_digits[1:4]}) {sbp_phone_digits[4:7]}-{sbp_phone_digits[7:9]}-{sbp_phone_digits[9:11]}"

            sbp_amount_digits = re.sub(r'[^\d]', '', sbp_amount)
            sbp_amount_formatted = f"{int(sbp_amount_digits):,}".replace(',', ' ') + ' ₽' if sbp_amount_digits else sbp_amount

            data = {
                'date_time':     sbp_date,
                'amount':        sbp_amount_digits or sbp_amount,
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
                    "❌ Сейчас не собралось — отправьте те же данные ещё раз.",
                    reply_markup=back_keyboard(),
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
            if ct_date_raw.lower() in ('сейчас', 'now', '-', ''):
                ct_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                ct_date = ct_date_raw
            ct_data = {
                'date_time':   ct_date,
                'amount':      amount_num,
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
            if nc_date_raw.lower() in ('сейчас', 'now', '-', ''):
                nc_date = now_msk().strftime("%d.%m.%Y, %H:%M")
            else:
                nc_date = nc_date_raw
            nc_data = {
                'date_time':   nc_date,
                'amount':      amount_num,
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
                'amount':       amount_num,
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
                    reply_markup=back_keyboard(),
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
            for attempt in range(3):
                try:
                    sent_message = await update.effective_message.reply_document(
                        document=pdf_bytes,
                        filename=filename,
                        caption=(
                            "✅ Чек создан!\n\n"
                            "❌ В случае непроходимости в валидаторе - гарантия 100% "
                            "(подробнее в разделе «Гарантия»)\n\n"
                            "Отправьте новые данные или нажмите «Назад»"
                        ),
                        reply_markup=back_keyboard(),
                    )
                    break
                except Exception as send_err:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    raise send_err

            try:
                committed = commit_generated_check(user_id, tool_id)
            except Exception as exc:
                logger.exception("Post-send accounting failed uid=%s: %s", user_id, exc)
                committed = False
            if not committed:
                logger.error("Post-send accounting rejected uid=%s tool=%s", user_id, tool_id)
                if sent_message is not None:
                    try:
                        await sent_message.delete()
                    except Exception as exc:
                        logger.exception(
                            "Could not retract uncharged PDF uid=%s: %s", user_id, exc,
                        )
                await update.effective_message.reply_text(
                    "❌ Не удалось завершить создание чека.",
                    reply_markup=menu_kb(update),
                )
                return MAIN_MENU

            # Админам: любой успешный чек от не-админа
            if not is_admin(user_id, update.effective_user):
                if update.effective_user:
                    remember_tg_user(user_id, update.effective_user)
                who = tg_user_label(update.effective_user, user_id)
                what = locals().get("bank_name") or tool_id or "чек"
                left = get_check_credits(user_id)
                await notify_admins(
                    context.bot,
                    f"🧾 Чек создан\n"
                    f"Юзер: {who} (`{user_id}`)\n"
                    f"Тип: {what}\n"
                    f"Осталось чеков: {left}",
                )

            return ENTERING_DATA
        else:
            await update.effective_message.reply_text(
                "❌ Не удалось завершить создание чека.",
                reply_markup=back_keyboard()
            )
            return ENTERING_DATA
            
    except Exception as e:
        logger.exception("Receipt flow failed: %s", e)
        await update.effective_message.reply_text(
            "❌ Не удалось завершить создание чека.",
            reply_markup=back_keyboard(),
        )
        return ENTERING_DATA


async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Очистка чата"""
    user_id = update.effective_user.id
    if not has_bot_access(user_id):
        await deny_without_access(update, context)
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    for i in range(message_id, max(1, message_id - 100), -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=i)
        except:
            pass
    
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
    total = add_check_credits(target_id, n)
    await update.effective_message.reply_text(
        f"✅ Выдано {n} чек(ов) → {label} (`{target_id}`)\nВсего в пакете: {total}",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(target_id, f"🎁 Вам начислено {n} чек(ов) в пакет!")
    except Exception:
        pass


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
    credits = int(ud.get("check_credits", 0) or 0)
    created = int(ud.get("checks_created", 0) or 0)
    spent = int(ud.get("total_spent", 0) or 0)
    pin = "✅" if ud.get("pin_ok") else "🔒"
    await update.effective_message.reply_text(
        f"👤 {label} (`{target_id}`) {pin}\n\n"
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


async def admin_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/refs @user|ID - сколько привёл и кто"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    if not context.args:
        await update.effective_message.reply_text("📝 /refs @user|ID")
        return
    target_id, label = await resolve_tg_user_arg(context.bot, context.args[0])
    if target_id is None:
        await update.effective_message.reply_text(
            format_user_not_found(context.args[0]),
            parse_mode="Markdown",
        )
        return
    refs = list_referrals(target_id)
    lines = [f"🤝 Рефералы {label} (`{target_id}`): {len(refs)}"]
    for uid, u in refs[:50]:
        uname = (u.get("username") or "").strip()
        ref_label = f"@{uname}" if uname else uid
        pin = "✅" if u.get("pin_ok") else "🔒"
        lines.append(f"• {ref_label} (`{uid}`) {pin}")
    if len(refs) > 50:
        lines.append(f"… и ещё {len(refs) - 50}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


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


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users - Список пользователей"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    
    data = load_data()
    users = data.get("users", {})
    
    if not users:
        await update.effective_message.reply_text("📋 Нет пользователей")
        return
    
    text = "📋 *Пользователи:*\n\n"
    
    for user_id, user_data in list(users.items())[:20]:  # Первые 20
        balance = user_data.get("balance", 0)
        uname = (user_data.get("username") or "").strip().lstrip("@")
        pin = "✅" if user_data.get("pin_ok") else "🔒"
        label = f"@{uname}" if uname else user_data.get("first_name") or "—"
        text += f"{pin} `{user_id}` {label} — {balance}{CURRENCY_NAME}\n"
    
    await update.effective_message.reply_text(text, parse_mode='Markdown')


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats - Статистика"""
    if not is_admin(update.effective_user.id, update.effective_user):
        await update.effective_message.reply_text("❌ Нет доступа")
        return
    
    data = load_data()
    users = data.get("users", {})
    
    total_users = len(users)
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_spent = sum(u.get("total_spent", 0) for u in users.values())
    
    text = (
        f"📊 *Статистика*\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💎 Общий баланс: {total_balance}{CURRENCY_NAME}\n"
        f"📈 Всего потрачено: {total_spent}{CURRENCY_NAME}"
    )
    
    await update.effective_message.reply_text(text, parse_mode='Markdown')


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
    from telegram import BotCommand
    from telegram import BotCommandScopeDefault, BotCommandScopeChat
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
                logger.info("admin resolved @%s -> %s", uname, chat.id)
        except Exception as e:
            logger.warning("admin resolve @%s failed: %s", uname, e)
    admin_recipient_ids()

    # Обычным юзерам - все публичные команды; админские только админам
    public_cmds = [
        BotCommand("start", "Главное меню"),
        BotCommand("profile", "Мой профиль"),
    ]
    admin_cmds = public_cmds + [
        BotCommand("help", "Админ-панель"),
        BotCommand("give", "Выдать монеты"),
        BotCommand("givechecks", "Выдать чеки"),
        BotCommand("bal", "Баланс и чеки юзера"),
        BotCommand("promo", "Создать промокод"),
        BotCommand("refs", "Рефералы юзера"),
        BotCommand("users", "Список юзеров"),
        BotCommand("stats", "Статистика"),
        BotCommand("approve", "Выдать 100$"),
    ]
    await application.bot.set_my_commands(public_cmds, scope=BotCommandScopeDefault())
    for aid in sorted(_ADMIN_ID_CACHE):
        try:
            await application.bot.set_my_commands(
                admin_cmds,
                scope=BotCommandScopeChat(chat_id=aid),
            )
        except Exception as e:
            logger.warning("set_my_commands admin %s failed: %s", aid, e)
    
    description = (
        "Генератор PDF-чеков.\n\n"
        "Выбрать банк → тип перевода → данные построчно."
    )
    await application.bot.set_my_description(description)
    await application.bot.set_my_short_description("Генератор PDF-чеков")
    
    print("✅ Бот настроен")
    print(f"🛠 Админы: {', '.join('@'+u for u in sorted(ADMIN_USERNAMES))} ids={sorted(_ADMIN_ID_CACHE)}")


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
    # Warm OpenPDF JVM once — first T-Bank emit otherwise pays cold-start + flaps.
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
    
    # allow_reentry=False: иначе MessageHandler(resume_session) в entry_points
    # перехватывает КАЖДУЮ кнопку меню и крутит «были обновления» по кругу.
    # /start (в т.ч. ref_) - в WAITING_PIN + fallbacks.
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, shop_menu_handler),
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
            CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"),
            CallbackQueryHandler(callback_entry),
        ],
        allow_reentry=False,
    )
    
    app.add_handler(conv)
    
    # Команды меню
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("help", cmd_help))
    
    # Админ команды
    app.add_handler(CommandHandler("give", admin_give_coins))
    app.add_handler(CommandHandler("givechecks", admin_give_checks))
    app.add_handler(CommandHandler("bal", admin_bal))
    app.add_handler(CommandHandler("promo", admin_promo))
    app.add_handler(CommandHandler("refs", admin_refs))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("approve", approve_user))
    app.add_handler(CallbackQueryHandler(paycheck_callback, pattern=r"^paycheck:"))
    
    app.add_error_handler(on_error)
    
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("⚫ UMBRA SYNDICATE v6.1")
    print("📋 Админ команды:")
    print("   /give @user|ID AMOUNT - выдать монеты")
    print("   /givechecks @user|ID N - выдать чеки в пакет")
    print("   /bal @user|ID          - баланс и остаток чеков")
    print("   /promo CODE balance|checks AMOUNT [max_uses]")
    print("   /refs @user|ID         - рефералы пользователя")
    print("   /approve @user|ID      - выдать 100$")
    print("   /users               - список юзеров")
    print("   /stats               - статистика")
    
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
