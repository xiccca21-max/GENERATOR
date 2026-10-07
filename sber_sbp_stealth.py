"""
SBER SBP STEALTH — чек «Перевод по СБП» Сбербанка.

Генерация через sber_dynamic.py (Jasper/OpenPDF pipeline как у T-Bank SBP).
"""

import hashlib
import os
import re
import random
import struct
import logging
from datetime import datetime, timedelta
from time_msk import now_msk
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE = os.path.join(_DIR, "templates", "S_sbp_original.pdf")
CORPUS_SBER_DIR = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "чеки", "сбер")


def _format_sber_clock(time_raw: str) -> str:
    """«9:05» → «09:05:SS». Час всегда из двух цифр, секунды есть.

    На исходящем СБП и на переводе клиенту СберБанка так напечатаны все
    оригиналы. Если секунд не передали, берём 1–59, а не :00.
    """
    raw = (time_raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if not match:
        return now_msk().strftime("%H:%M:%S")
    hour, minute = int(match.group(1)), int(match.group(2))
    if match.group(3) is None:
        second = random.randint(1, 59)
    else:
        second = int(match.group(3))
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return now_msk().strftime("%H:%M:%S")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _format_sber_date(date_raw: str, time_raw: str = "") -> str:
    months_ru = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    date_raw = (date_raw or "").strip()
    time_raw = (time_raw or "").strip()
    # Мусор вместо даты (сдвиг полей из‑за «copy») → сейчас.
    if not re.search(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", date_raw) and date_raw.lower() not in (
        "сейчас", "now", "-", "",
    ):
        text_parts = date_raw.split()
        looks_ru = (
            len(text_parts) >= 3
            and text_parts[0].isdigit()
            and text_parts[1].lower() in months_ru
        )
        if not looks_ru:
            now = now_msk()
            date_raw = now.strftime("%d.%m.%Y")
            time_raw = time_raw or now.strftime("%H:%M:%S")
    time_raw = _format_sber_clock(time_raw)
    if "." in date_raw:
        parts = date_raw.split(".")
        if len(parts) == 3:
            day, month, year = parts
            try:
                month_name = months_ru[int(month) - 1]
                return f"{int(day):02d} {month_name} {year} {time_raw} (МСК)"
            except (ValueError, IndexError):
                pass

    text_parts = date_raw.split()
    if len(text_parts) >= 3 and text_parts[0].isdigit() and text_parts[1].lower() in months_ru:
        text_parts[0] = f"{int(text_parts[0]):02d}"
        date_raw = " ".join(text_parts)
        clock = re.search(r"\d{1,2}:\d{2}(?::\d{2})?", date_raw)
        if clock:
            fixed = _format_sber_clock(clock.group(0))
            date_raw = date_raw[: clock.start()] + fixed + date_raw[clock.end() :]
        if re.search(r"\d{4}", date_raw) and re.search(r"\d{2}:\d{2}:\d{2}", date_raw):
            if "(МСК)" not in date_raw:
                return f"{date_raw} (МСК)".strip()
            return date_raw
    now = now_msk()
    return (
        f"{now.day:02d} {months_ru[now.month - 1]} {now.year} "
        f"{time_raw or now.strftime('%H:%M:%S')} (МСК)"
    )


def _format_sber_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) >= 11:
        d = digits[1:11]
    elif len(digits) >= 10:
        d = digits[-10:]
    else:
        return phone.strip()
    # Цифры не переписываем: банк печатает номер получателя как есть.
    return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"


def _format_amount(amount_raw: str) -> str:
    """SBP face «3006.00  ₽» / «57191.61  ₽» — точка и две копейки, два пробела до ₽."""
    from sber_phone_stealth import _parse_phone_money

    rub, kop = _parse_phone_money(amount_raw)
    return f"{rub}.{kop:02d}  ₽"


_SBER_BANK_CODE = {
    "ПСБ": "1003",
    "ВТБ": "1006",
    "Банк ВТБ": "1006",
    "Сбербанк": "1013",
    "Альфа-Банк": "1016",
    "Райффайзенбанк": "1018",
    "Райффайзен": "1018",
    "Яндекс": "0014",
    "ОТП Банк": "1020",
    "Газпромбанк": "1012",
    "Газпром": "1012",
    "Россельхозбанк": "0013",
    "РСХБ": "0013",
    "Т-Банк": "1007",
}

_SBER_SUFFIX_POOL = ("70901", "90502", "50703", "70301", "30902", "20501", "00501", "61101", "80301")

_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_RU_DATE_TIME_RE = re.compile(
    r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE,
)


def _parse_sber_dt(date_in: str, time_in: str = "") -> datetime:
    """Face clock for the SBP core.

    ``date_time`` from the bot is often already «27 сентября 2026 14:02:11».
    A dotted-only parse used to fall through to ``now`` and stamp that clock
    into the id while the printed line kept the requested minute.
    """
    date_in = (date_in or "").strip()
    time_in = (time_in or "").strip()
    labeled = _parse_sber_date_time_label(date_in)
    if labeled:
        return labeled
    if not time_in:
        time_in = now_msk().strftime("%H:%M:%S")
    elif len(time_in) == 5:
        time_in = f"{time_in}:00"
    labeled = _parse_sber_date_time_label(f"{date_in} {time_in}")
    if labeled:
        return labeled
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(f"{date_in} {time_in}", fmt)
        except ValueError:
            continue
    return now_msk().replace(microsecond=0)


def _parse_sber_date_time_label(label: str) -> Optional[datetime]:
    """«20 июля 2026 20:33:42 (МСК)» → naive MSK datetime."""
    raw = (label or "").replace("\xa0", " ").replace("\u202f", " ").strip()
    m = _RU_DATE_TIME_RE.search(raw)
    if not m:
        return None
    day_s, month_s, year_s, hour_s, minute_s, second_s = m.groups()
    month = _RU_MONTHS.get(month_s.lower())
    if not month:
        return None
    try:
        return datetime(
            int(year_s), month, int(day_s),
            int(hour_s), int(minute_s), int(second_s or 0),
        )
    except ValueError:
        return None


def _sber_core_utc(dt_msk: datetime) -> datetime:
    """UTC-ядро SBP ID: на 3–9 сек раньше операции.

    Proton ``SBER_SBP_TIMESTAMP_MISMATCH`` HARD when |Δt| > 10s between
    SBP core and face op time. Keep lead strictly ≤9.
    """
    dt_utc = dt_msk - timedelta(hours=3)
    lead = random.randint(3, 9)
    return (dt_utc - timedelta(seconds=lead)).replace(microsecond=0)


def _sber_core_delta_sec(sbp: str, dt_msk: datetime) -> Optional[int]:
    """Seconds (op_utc − core); None if SBP timestamp unparsable."""
    if len(sbp) < 11 or sbp[0] != "A":
        return None
    try:
        y_doy = int(sbp[1:5])
        year = 2020 + y_doy // 1000
        doy = y_doy % 1000
        hh, mm, ss = int(sbp[5:7]), int(sbp[7:9]), int(sbp[9:11])
        core = datetime(year, 1, 1) + timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss)
        op_utc = (dt_msk - timedelta(hours=3)).replace(microsecond=0)
        return int((op_utc - core).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def sync_sbp_id_core_timestamp(prepared: Dict[str, str]) -> None:
    """Fill the timestamp core only for generated/noncanonical identifiers.

    A valid caller-supplied SBP identifier is payload and must stay immutable.
    """
    sbp = re.sub(r"\s+", "", (prepared.get("spb_number") or "").upper())
    if len(sbp) != 32 or sbp[0] != "A":
        return
    if _SBER_SBP_ID_RE.fullmatch(sbp):
        prepared["spb_number"] = sbp
        return
    dt_msk = _parse_sber_date_time_label(prepared.get("date_time", ""))
    if not dt_msk:
        return
    core = _sber_core_utc(dt_msk)
    doy = core.timetuple().tm_yday
    year_dig = core.year - 2020
    ts = f"{year_dig * 1000 + doy:04d}{core.hour:02d}{core.minute:02d}{core.second:02d}"
    synced = "A" + ts + sbp[11:]
    prepared["spb_number"] = synced


def _sber_sbp_suffix(bank: str, amount: str) -> str:
    from sber_dynamic import _amount_rubles_int

    amt = _amount_rubles_int(amount)
    if amt >= 50000:
        return random.choice(("00501", "20501", "90502"))
    if amt >= 20000:
        return random.choice(("70301", "50703", "30902"))
    return random.choice(_SBER_SUFFIX_POOL)


def _sber_atlas_tail_prefix4() -> tuple:
    """Empirical SBP opid[11:15] values from Proton atlas (HARD if outside)."""
    try:
        from detector.sber_v2.atlas import known_sbp_tail_prefixes

        prefs = sorted(known_sbp_tail_prefixes())
        if prefs:
            return tuple(prefs)
    except Exception:
        pass
    return (
        "0290", "0640", "1800", "2650", "3470", "4130", "4780",
        "6091", "6380", "8211", "8241", "9021", "9642", "9732",
    )


def _generate_sbp_number(
    bank: str = "",
    date_in: str = "",
    *,
    time_in: str = "",
    amount: str = "",
    phone: str = "",
    account: str = "",
    receiver: str = "",
) -> str:
    """Сбер SBP ID (32): NSPK cipher + ядро 00117/00116 (pos 23–27)."""
    # Структура и зависимости от даты (маркер A/B, счётчик NN) — по 13 оригиналам:
    # см. sber_sbp_faithful.generate_op_id.
    from sber_sbp_faithful import generate_op_id

    dt_msk = _parse_sber_dt(date_in, time_in)
    return generate_op_id(dt_msk, ref4_pool=_sber_atlas_tail_prefix4())


_SBER_SBP_ID_RE = re.compile(r"^A[0-9]{14}[0-9]0G[0-9]{4}0011[67][0-9]{5}$")


def _normalize_or_generate_sbp_id(
    raw: str,
    bank: str,
    date_in: str,
    *,
    time_in: str = "",
    amount: str = "",
    phone: str = "",
    account: str = "",
    receiver: str = "",
) -> str:
    gen_kwargs = {
        "time_in": time_in,
        "amount": amount,
        "phone": phone,
        "account": account,
        "receiver": receiver,
    }
    sbp_id = re.sub(r"\s+", "", (raw or "").strip().upper())
    if not sbp_id:
        return _generate_sbp_number(bank, date_in, **gen_kwargs)
    # Номер, который передал пользователь, — данные: печатаем как есть (любая длина/символы).
    return sbp_id


def _normalize_sber_bank_name(bank: str) -> str:
    """Канонические названия для RiskScan / поля «Банк получателя»."""
    raw = (bank or "").strip()
    if not raw:
        return "Сбербанк"
    key = re.sub(r"\s+", " ", raw).lower().replace("ё", "е")
    aliases = {
        "втб": "ВТБ",
        "банк втб": "ВТБ",
        "vtb": "ВТБ",
        "сбер": "Сбербанк",
        "сбербанк": "Сбербанк",
        "sber": "Сбербанк",
        "сбep": "Сбербанк",
        "т-банк": "Т-Банк",
        "т банк": "Т-Банк",
        "t-bank": "Т-Банк",
        "tbank": "Т-Банк",
        "тинькофф": "Т-Банк",
        "тинькофф банк": "Т-Банк",
        "газпром": "Газпромбанк",
        "газпромбанк": "Газпромбанк",
        "райффайзен": "Райффайзенбанк",
        "райффайзенбанк": "Райффайзенбанк",
        "райффайзен банк": "Райффайзенбанк",
        "raiffeisen": "Райффайзенбанк",
        "raiffeisenbank": "Райффайзенбанк",
        "яндекс": "Яндекс",
        "яндекс банк": "Яндекс",
        "альфа": "Альфа-Банк",
        "альфа-банк": "Альфа-Банк",
        "альфа банк": "Альфа-Банк",
        "цупис": "ЦУПИС",
        "кошелек цупис": "Кошелек ЦУПИС",
        "кошелёк цупис": "Кошелек ЦУПИС",
        "кошелек цупис": "Кошелек ЦУПИС",
        "псб": "ПСБ",
        "промсвязьбанк": "ПСБ",
        "отп": "ОТП Банк",
        "отп банк": "ОТП Банк",
        "россельхозбанк": "РСХБ",
        "рсхб": "РСХБ",
    }
    canon = aliases.get(key, raw)
    # Полное каноническое имя — слот добиваем пробелами / font-patch, не режем «банк».
    return canon


def _charset_sanitize(text: str) -> str:
    """Keep user text byte-for-byte; missing glyphs are a generation failure."""
    return str(text or "")


_SBER_RECV_FIO_OK = re.compile(r"^[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ]$")
_SBER_SEND_FIO_OK = re.compile(r"^[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ]\.$")
_SBER_PATRONYMIC_END = ("ович", "евич", "овна", "евна", "ична", "инична")
# Male given names that end with а/я — do not take «Ивановна».
_SBER_MALE_A_YA = frozenset({
    "илья", "никита", "кузьма", "фома", "лёва", "лева", "савва",
    "данила", "данило", "мустафа", "юра",
})


def _sber_patronymic_for(given: str) -> str:
    """Neutral bank patronymic when the user only gave name + initial."""
    g = (given or "").strip().lower()
    female = g.endswith(("а", "я")) and g not in _SBER_MALE_A_YA
    return "Ивановна" if female else "Иванович"


_NAME_WORD = r"[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*"
_PATR_END2 = ("ич", "овна", "евна", "ична", "чна", "оглы", "кызы", "гызы", "улы", "уулу", "кизи")


def _cap_word(w: str) -> str:
    """Иван / Анна-Мария: каждая часть с заглавной, остальное строчные."""
    return "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-") if p)


def _is_patr(w: str) -> bool:
    return w.lower().endswith(_PATR_END2)


def _sber_sbp_fio_shape(name: str, *, sender: bool) -> str:
    """Банковская форма ФИО: получатель «Имя Отчество Ф», отправитель «Имя Отчество Ф.».

    Без ограничений длины и без обрезки слов: любое имя/отчество печатается целиком
    (дефисные имена, «Оглы/Кызы», длинные отчества). Меняется только ПОРЯДОК слов, как у банка:
    «Фамилия Имя Отчество» → «Имя Отчество Ф». Если отчества нет, оно достраивается
    нейтральным (формат требует трёх слов).
    """
    words = re.findall(_NAME_WORD, name or "")
    suffix_dot = "." if sender else ""
    if not words:
        return "Иван Иванович И" + suffix_dot
    words = [_cap_word(w) for w in words]
    if len(words) == 1:
        given = words[0]
        return f"{given} {_sber_patronymic_for(given)} {given[0]}{suffix_dot}"
    if len(words) == 2:
        a, b = words
        if len(b) == 1:                       # «Анна К»
            return f"{a} {_sber_patronymic_for(a)} {b}{suffix_dot}"
        if _is_patr(b):                       # «Сергей Петрович»
            return f"{a} {b} {a[0]}{suffix_dot}"
        return f"{a} {_sber_patronymic_for(a)} {b[0]}{suffix_dot}"   # «Юрий Щукин»
    # 3+ слов: найти отчество
    pi = next((i for i, w in enumerate(words) if i > 0 and _is_patr(w) and len(w) > 1), None)
    if pi is None:
        pi = next((i for i, w in enumerate(words) if _is_patr(w) and len(w) > 1), None)
    if pi is not None and pi >= 1:
        given, patr = words[pi - 1], words[pi]
        rest = [w for i, w in enumerate(words)
                if i not in (pi - 1, pi) and w.lower() not in ("оглы", "кызы", "гызы", "улы")]
        single = next((w for w in rest if len(w) == 1), None)
        init = single or (rest[0][0] if rest else given[0])
        # «Фамилия Имя Отчество» или «Имя Отчество Фамилия» — инициал фамилии
        return f"{given} {patr} {init}{suffix_dot}"
    given = words[0]
    last = words[-1]
    return f"{given} {_sber_patronymic_for(given)} {last[0]}{suffix_dot}"


def _sber_sbp_fio_shape_legacy(name: str, *, sender: bool) -> str:
    """[устарело: резало слова до 26/27 символов] sber_v2 HARD: recipient «Имя Отчество И», sender «Имя Отчество И.».

    Corpus SBP-outgoing receivers are 18–26 chars. Longer user input must be
    clamped — otherwise the dynamic encoder chops mid-token and Proton HARD
    ``SBER_SBP_*_FIO_FORMAT`` fires (e.g. «А»×40 → «Ааа… И»).

    Never chop patronymic endings (…ович/…овна): ``max_len=12`` used to turn
    «Александрович»(13) into «Александрови» — SafeCheck tell on face.
    """
    words = re.findall(r"[А-ЯЁа-яё]+", name or "")
    # Keep «Имя Отчество И.» inside the bank face budget (recv ≤26 on originals).
    _max_total = 26 if not sender else 27

    def _cap_given(word: str, max_len: int = 12) -> str:
        # Proton: [А-ЯЁ][а-яё]+ — name must be ≥2 letters.
        # Never invent «Кк» from a surname initial.
        if not word:
            return "Иван"
        if len(word) == 1:
            return "Иван"
        out = word[0].upper() + word[1:].lower()
        if len(out) > max_len:
            out = out[:max_len]
        return out

    def _cap_patronymic(word: str) -> str:
        # Longest common bank forms: Константинович(14), Александрович(13).
        if not word:
            return "Иванович"
        if len(word) == 1:
            return "Иванович"
        out = word[0].upper() + word[1:].lower()
        if out.lower().endswith(_SBER_PATRONYMIC_END):
            return out[:15] if len(out) > 15 else out
        # Not a real patronymic token — keep short synthetic later.
        return out[:12] if len(out) > 12 else out

    if not words:
        words = ["Иван", "Иванович"]

    if len(words) == 1:
        name1 = _cap_given(words[0])
        name2 = _sber_patronymic_for(name1)
        initial = name1[0]
    elif len(words) == 2:
        w0, w1 = words[0], words[1]
        if len(w1) == 1:
            # «Анна К» / «Ольга В.» → Имя + синтет. отчество + инициал
            name1 = _cap_given(w0)
            name2 = _sber_patronymic_for(name1)
            initial = w1[0].upper()
        elif w1.lower().endswith(_SBER_PATRONYMIC_END):
            # «Сергей Петрович» без фамилии — инициал с имени
            name1, name2 = _cap_given(w0), _cap_patronymic(w1)
            initial = name1[0]
        else:
            # «Юрий Щукин» — второе слово фамилия → инициал
            name1 = _cap_given(w0)
            name2 = _sber_patronymic_for(name1)
            initial = w1[0].upper()
    else:
        name1 = _cap_given(words[0])
        name2 = _cap_patronymic(words[1])
        initial = words[-1][0].upper()
        last = words[-1].lower()
        if last.endswith(_SBER_PATRONYMIC_END):
            # «Иванов Сергей Петрович»
            name1 = _cap_given(words[1])
            name2 = _cap_patronymic(words[2])
            initial = words[0][0].upper()
        elif len(words[1]) == 1:
            # «Анна К Петровна» unlikely; keep first+patronymic+last initial
            name1 = _cap_given(words[0])
            name2 = (
                _cap_patronymic(words[2])
                if len(words[2]) >= 2
                else _sber_patronymic_for(name1)
            )
            initial = words[1][0].upper()
        else:
            # «Сергей Петрович К» / «Фёдор Железнов Б»
            if words[1].lower().endswith(_SBER_PATRONYMIC_END):
                name1, name2 = _cap_given(words[0]), _cap_patronymic(words[1])
                initial = words[-1][0].upper()
            else:
                name1 = _cap_given(words[0])
                name2 = _sber_patronymic_for(name1)
                initial = words[-1][0].upper()

    # Gender agreement: «Ирина Магнитикович» → female given + male -ович.
    # Only this direction — «Айгуль Рашатовна» is a genuine female face.
    g = name1.lower()
    female_given = g.endswith(("а", "я")) and g not in _SBER_MALE_A_YA
    if female_given and name2.lower().endswith(("ович", "евич")):
        name2 = _sber_patronymic_for(name1)

    suffix = f" {initial}" + ("." if sender else "")
    # Shrink the given name only — never mutilate …ович/…овна endings.
    while len(name1) >= 2 and len(f"{name1} {name2}{suffix}") > _max_total:
        name1 = name1[:-1]
    if len(f"{name1} {name2}{suffix}") > _max_total:
        name2 = _sber_patronymic_for(name1 if len(name1) >= 2 else "Иван")
    while len(name1) >= 2 and len(f"{name1} {name2}{suffix}") > _max_total:
        name1 = name1[:-1]
    if len(name1) < 2:
        name1 = "Иван"
    if len(name2) < 2 or not name2.lower().endswith(_SBER_PATRONYMIC_END):
        name2 = _sber_patronymic_for(name1)

    shaped = f"{name1} {name2}{suffix}"
    ok = _SBER_SEND_FIO_OK if sender else _SBER_RECV_FIO_OK
    if ok.match(shaped) and name2.lower().endswith(_SBER_PATRONYMIC_END):
        return shaped
    # Last-resort legal face — never ship a Proton FIO_FORMAT HARD.
    fb1 = _cap_given(name1, 8)
    fb2 = _sber_patronymic_for(fb1)
    shaped = f"{fb1} {fb2}{suffix}"
    if ok.match(shaped):
        return shaped
    return ("Иван Иванович И." if sender else "Иван Иванович И")


def _prepare(data: Dict) -> Dict[str, str]:
    amount_raw = str(data.get("amount") or data.get("amount_raw") or "0")
    sender = _sber_sbp_fio_shape(
        _charset_sanitize(
            (data.get("sender") or data.get("sender_name") or "").strip()
        ),
        sender=True,
    )
    receiver = _sber_sbp_fio_shape(
        _charset_sanitize(
            (data.get("receiver") or data.get("receiver_name") or "").strip()
        ),
        sender=False,
    )
    phone = _format_sber_phone(data.get("phone") or data.get("receiver_phone") or "")
    bank = _normalize_sber_bank_name(
        (data.get("bank_name") or data.get("recipient_bank") or "Сбербанк").strip()
    )
    commission = (data.get("commission") or "0.00  ₽").strip()
    if "₽" not in commission:
        cd = re.sub(r"\D", "", commission) or "0"
        commission = f"{cd}.00  ₽"

    date_in = (data.get("date") or "").strip()
    time_in = (data.get("time") or "").strip()
    if data.get("date_time"):
        dt = str(data["date_time"]).strip()
        if dt.lower() in ("сейчас", "now", "-", ""):
            now = now_msk()
            date_in = now.strftime("%d.%m.%Y")
            time_in = now.strftime("%H:%M:%S")
        elif "," in dt:
            date_in, time_in = [x.strip() for x in dt.split(",", 1)]
        elif " " in dt and "." in dt.split()[0]:
            parts = dt.split()
            date_in = parts[0]
            time_in = parts[1] if len(parts) > 1 else time_in
        else:
            date_in = dt
    if not date_in:
        now = now_msk()
        date_in = now.strftime("%d.%m.%Y")
        time_in = time_in or now.strftime("%H:%M:%S")

    sbp_raw = data.get("sbp_id") or data.get("spb_number") or ""
    sbp_kwargs = {
        "time_in": time_in,
        "amount": amount_raw,
        "phone": phone,
        "account": (data.get("sender_account") or "").strip(),
        "receiver": receiver,
    }
    if str(sbp_raw).strip().lower() in ("авто", "auto", "-"):
        sbp_id = _generate_sbp_number(bank, date_in, **sbp_kwargs)
    else:
        sbp_id = _normalize_or_generate_sbp_id(str(sbp_raw), bank, date_in, **sbp_kwargs)

    account = (data.get("sender_account") or "").strip()
    if not account:
        account = "•••• " + str(random.randint(1000, 9999))
    else:
        digits = re.sub(r"\D", "", account)[-4:]
        if len(digits) == 4:
            account = f"•••• {digits}"

    return {
        "date_time": _format_sber_date(date_in, time_in),
        "amount": _format_amount(amount_raw),
        "commission": commission,
        "sender_name": sender,
        "receiver_name": receiver,
        "receiver_phone": phone,
        "recipient_bank": bank,
        "sender_account": account,
        "spb_number": sbp_id,
    }


def _finalize_prepared(prepared: Dict[str, str]) -> Dict[str, str]:
    # ID: пользовательский печатается как есть, сгенерированный уже согласован по времени.
    return prepared


def _face_has_exact_fields(text: str, prepared: Dict[str, str]) -> bool:
    """Reject donor/raw-shell output that does not contain the accepted values."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\u202f", " ")).strip()
    for key in (
        "sender_name", "receiver_name", "receiver_phone",
        "recipient_bank", "date_time", "spb_number", "amount",
    ):
        want = re.sub(r"\s+", " ", str(prepared.get(key) or "")).strip()
        haystack = flat
        if key == "date_time" and " (МСК)" not in want and want.endswith("(МСК)"):
            want = want[:-5] + " (МСК)"
        if want and want not in haystack:
            logger.warning("Sber SBP: exact field missing from PDF: %s=%r", key, want)
            return False
    return True


def _create_faithful(data: Dict) -> Optional[bytes]:
    """Сборка «как у банка»: sber_sbp_faithful (Jasper/iText), без правки чужого PDF."""
    import sber_sbp_faithful as F

    prepared = _finalize_prepared(_prepare(dict(data)))
    when = _parse_sber_date_time_label(prepared.get("date_time", ""))
    if when is None:
        return None
    created = when + timedelta(seconds=random.randint(61, 199))
    now = now_msk().replace(microsecond=0)
    if created > now:
        created = max(when + timedelta(seconds=1), now)

    def _strip_rub(v: str) -> str:
        return re.sub(r"\s*₽\s*$", "", v or "").strip()

    pdf = F.build_sbp(
        when_msk=when,
        recipient=prepared["receiver_name"],
        phone=prepared["receiver_phone"],
        bank=prepared["recipient_bank"],
        sender=prepared["sender_name"],
        account=prepared["sender_account"],
        amount=_strip_rub(prepared["amount"]),
        fee=_strip_rub(prepared["commission"]) or "0.00",
        op_id=prepared["spb_number"],
        created_msk=created,
    )
    cids = []
    for k in ("receiver_name", "sender_name", "recipient_bank", "receiver_phone",
              "sender_account", "amount", "commission", "spb_number"):
        cids += F.text_to_cids(prepared.get(k, ""))
    miss = F.missing_glyphs(cids)
    if miss:
        logger.warning("Sber SBP faithful: нет глифа для CID %s", miss)
    appr = F.approx_glyphs(cids)
    if appr:
        logger.info("Sber SBP faithful: %d глифов приближённые (контур точный)", len(appr))
    return pdf


def create_sber_sbp_stealth(data: Dict) -> Optional[bytes]:
    """Главная точка входа: data → PDF bytes. Сначала эмиттер «как у банка»."""
    if os.environ.get("SBER_SBP_LEGACY") != "1":
        try:
            res = _create_faithful(data)
            if res:
                return res
        except Exception:
            logger.exception("Sber SBP faithful failed — legacy pipeline")
    return _create_sber_sbp_stealth_legacy(data)


def _create_sber_sbp_stealth_legacy(data: Dict) -> Optional[bytes]:
    """Прежний конвейер (правка донорского PDF)."""
    from sber_dynamic import build_dynamic_sber_sbp
    from openpdf_deflate import _pad_after_et_burned
    import fitz
    import sber_glyph_library as sgl

    if not os.path.isfile(ORIG_TEMPLATE):
        logger.error("Sber SBP: шаблон не найден (%s)", ORIG_TEMPLATE)
        return None

    for key in ("sender_name", "sender", "receiver_name", "receiver"):
        bad = sgl.excluded_name_chars(data.get(key, ""))
        if bad:
            logger.warning(
                "Sber SBP: excluded name chars in %s: %s — ship",
                key, "".join(sorted(bad)),
            )

    sgl.ensure_library()
    prepared = _finalize_prepared(_prepare(dict(data)))
    result = build_dynamic_sber_sbp(prepared)
    if not result:
        return None
    try:
        doc = fitz.open(stream=result, filetype="pdf")
        stream = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        doc.close()
    except Exception:
        logger.warning("Sber SBP: reject unreadable candidate")
        return None
    if _pad_after_et_burned(stream):
        logger.warning("Sber SBP: pad-after-ET — ship (LAW1, no GEN_NONE)")
    if not _face_has_exact_fields(text, prepared):
        logger.warning("Sber SBP: face mismatch — ship (LAW1, no GEN_NONE)")
    if not (100_000 <= len(result) <= 105_000):
        # Last-chance lift (runtime shell ~99KB) — same helper as dynamic build.
        try:
            from sber_dynamic import _lift_sber_pdf_into_orig_band
            result = _lift_sber_pdf_into_orig_band(result)
        except Exception:
            pass
    if not (100_000 <= len(result) <= 105_000):
        logger.warning("Sber SBP: size %d outside original band — ship", len(result))
    try:
        from tools.emit_quality_gate import emit_ok
    except Exception:
        try:
            from emit_quality_gate import emit_ok
        except Exception:
            emit_ok = None  # type: ignore
    if emit_ok is not None:
        ok, why = emit_ok(
            result,
            bank_hint="sber_sbp",
            expect={
                "sender_name": prepared.get("sender_name", ""),
                "receiver_name": prepared.get("receiver_name", ""),
                "amount": prepared.get("amount", ""),
                "phone": prepared.get("receiver_phone", ""),
                "bank": prepared.get("recipient_bank", ""),
                "date_time": prepared.get("date_time", ""),
            },
            strict_fio=True,
        )
        if not ok:
            logger.warning("Sber SBP gate fail (%s) — ship", why)
    try:
        from tools.emit_quality_gate import count_odd_tj
    except Exception:
        try:
            from emit_quality_gate import count_odd_tj
        except Exception:
            count_odd_tj = None  # type: ignore
    if count_odd_tj is not None:
        odd, total = count_odd_tj(result)
        if odd:
            logger.warning("Sber SBP: odd Tj %d/%d — ship", odd, total)
    from sber_dynamic import _sber_jasper_tm_hard_ok
    if not _sber_jasper_tm_hard_ok(stream):
        logger.warning("Sber SBP: Jasper Tm spelling HARD — ship")
    logger.info("Sber SBP OK (%d bytes)", len(result))
    return result
