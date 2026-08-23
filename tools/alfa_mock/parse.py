"""Parse the same one-message form as the T-Bank mock bot."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tools.alfa_mock.banks import resolve_bank

_MSK = ZoneInfo("Europe/Moscow")
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

EXAMPLE = """Банки с логотипом — пиши точно одно из:
Сбербанк
Озон
Т-Банк
Альфа-Банк
ВТБ
Райффайзен

Другое название тоже можно: строка будет, логотип как будто не загрузился.

ФИО: Петров Вадим Сергеевич
Имя: Вадим
Баланс: 999999.99
Траты: 12345
Доходы: 50000
Почта: vadik_huyadik@gmail.com
Телефон: +79990001122

Оп1:
-162310
Тимур В.
Переводы
Банк: Сбербанк
Тел: +79998882266
Время: 18.08.2026 22:57"""


class ParseError(ValueError):
    pass


_KV_RE = re.compile(
    r"^(ФИО|Имя|Баланс|Траты|Доходы|Почта|Телефон)\s*:\s*(.+)$",
    re.IGNORECASE,
)
_OP_SPLIT_RE = re.compile(r"^Оп\s*(\d+)\s*:\s*$", re.IGNORECASE | re.MULTILINE)
_NUM_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_DT_FULL_RE = re.compile(
    r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_DT_DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$")
_DT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DT_RU_RE = re.compile(
    r"(\d{1,2})\s+([а-яё]+)\s*,?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE,
)


def _alfa_iso(dt: datetime) -> str:
    dt = dt.astimezone(_MSK)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0300")


def _parse_op_datetime(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    now = datetime.now(_MSK)
    m = _DT_FULL_RE.search(s)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        return datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), tzinfo=_MSK)
    m = _DT_RU_RE.search(s)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return datetime(now.year, month, int(m.group(1)), int(m.group(3)), int(m.group(4)), int(m.group(5) or 0), tzinfo=_MSK)
    m = _DT_TIME_RE.fullmatch(s)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
    m = _DT_DATE_RE.fullmatch(s)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        return datetime(y, int(m.group(2)), int(m.group(1)), 12, 0, 0, tzinfo=_MSK)
    return None


def _num(raw: str, field: str) -> float:
    s = (raw or "").strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s)
    except ValueError as exc:
        raise ParseError(f"Не разобрал данные: {field}: {raw!r}") from exc


def _need(d: dict, key: str) -> str:
    val = (d.get(key) or "").strip()
    if not val:
        raise ParseError(f"Не разобрал данные: нет поля «{key}»")
    return val


def parse_phone_parts(raw: str) -> dict[str, str]:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise ParseError(f"Не разобрал данные: телефон: {raw!r}")
    return {
        "countryCode": digits[0],
        "innerCode": digits[1:4],
        "number": digits[4:],
    }


def phone_e164(parts: dict[str, str]) -> str:
    return f"+{parts['countryCode']}{parts['innerCode']}{parts['number']}"


def _split_ops(text: str) -> list[str]:
    matches = list(_OP_SPLIT_RE.finditer(text))
    if not matches:
        return []
    chunks: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunks.append(text[start:end].strip())
    return chunks


def _parse_op(block: str, index: int, cabinet: str = "alfa") -> dict[str, Any]:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ParseError(f"Не разобрал данные: Оп{index + 1} — мало строк")
    amount_s, name, category = lines[0], lines[1], lines[2]
    if not _NUM_RE.match(amount_s.replace(" ", "").replace(",", ".")):
        raise ParseError(f"Не разобрал данные: Оп{index + 1} сумма: {amount_s!r}")
    bank_raw = ""
    phone_raw = ""
    date_raw = ""
    time_raw = ""
    extra_dt = ""
    for ln in lines[3:]:
        low = ln.lower()
        if low.startswith("банк"):
            bank_raw = ln.split(":", 1)[-1].strip() if ":" in ln else ln[4:].strip()
        elif low.startswith("тел"):
            phone_raw = ln.split(":", 1)[-1].strip() if ":" in ln else ln[3:].strip()
        elif low.startswith("время"):
            time_raw = ln.split(":", 1)[-1].strip() if ":" in ln else ln[5:].strip()
        elif low.startswith("дата"):
            date_raw = ln.split(":", 1)[-1].strip() if ":" in ln else ln[4:].strip()
        elif _parse_op_datetime(ln):
            extra_dt = ln
    if not bank_raw:
        raise ParseError(f"Не разобрал данные: Оп{index + 1} — нет «Банк:»")
    if not phone_raw:
        raise ParseError(f"Не разобрал данные: Оп{index + 1} — нет «Тел:»")
    bank = resolve_bank(bank_raw, cabinet)
    parts = parse_phone_parts(phone_raw)
    dt = _parse_op_datetime(" ".join(x for x in (date_raw, time_raw) if x)) or _parse_op_datetime(time_raw or date_raw or extra_dt)
    out: dict[str, Any] = {
        "description": name,
        "brand": name,
        "amount": _num(amount_s, f"Оп{index + 1} сумма"),
        "category": category,
        "bank": bank,
        "phone": phone_e164(parts),
    }
    if dt:
        out["dateTime"] = _alfa_iso(dt)
        out["atMs"] = int(dt.timestamp() * 1000)
    return out


def parse_payload(text: str, cabinet: str = "alfa") -> dict[str, Any]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        raise ParseError("Не разобрал данные: пустое сообщение")

    op_m = _OP_SPLIT_RE.search(raw)
    header = raw if not op_m else raw[: op_m.start()]
    ops_text = raw[op_m.start() :] if op_m else ""

    folded: dict[str, str] = {}
    for ln in header.splitlines():
        m = _KV_RE.match(ln.strip())
        if m:
            folded[m.group(1).lower()] = m.group(2).strip()

    fio = _need(folded, "фио")
    parts = fio.split()
    if len(parts) < 2:
        raise ParseError("Не разобрал данные: ФИО — нужно «Фамилия Имя …»")
    last_name, given = parts[0], parts[1]
    patronymic = " ".join(parts[2:]) if len(parts) > 2 else ""
    header_name = folded.get("имя") or given

    email = _need(folded, "почта")
    if "@" not in email:
        raise ParseError(f"Не разобрал данные: почта: {email!r}")

    phone_parts = parse_phone_parts(_need(folded, "телефон"))
    ops = [_parse_op(chunk, i, cabinet) for i, chunk in enumerate(_split_ops(ops_text))]
    if len(ops) > 3:
        raise ParseError("Не разобрал данные: больше 3 операций")

    return {
        "firstName": header_name,
        "lastName": last_name,
        "patronymic": patronymic,
        "displayName": fio,
        "email": email,
        "mobilePhoneNumber": phone_parts,
        "profilePhone": phone_e164(phone_parts),
        "balance": _num(_need(folded, "баланс"), "Баланс"),
        "spending": _num(_need(folded, "траты"), "Траты"),
        "income": _num(_need(folded, "доходы"), "Доходы"),
        "operations": ops,
    }


def confirm_text(data: dict[str, Any]) -> str:
    ops = data["operations"]
    lines = [
        f"ФИО: {data['displayName']}",
        f"Имя (шапка кабинета): {data['firstName']}",
        f"Баланс: {data['balance']}",
        f"Почта (профиль): {data['email']}",
        f"Телефон (профиль): {data['profilePhone']}",
        f"Траты (виджет): {data['spending']}",
        f"Доходы (виджет): {data['income']}",
        f"Операций добавить (новые строки): {len(ops)}",
    ]
    for i, op in enumerate(ops, 1):
        bank = op["bank"]["name"]
        extra = ", без логотипа" if op["bank"].get("unknown") else ""
        when = ""
        if op.get("dateTime"):
            when = f", {op['dateTime'][8:10]}.{op['dateTime'][5:7]}.{op['dateTime'][:4]} {op['dateTime'][11:16]}"
        lines.append(f"  {i}. {op['amount']} — {op['description']} ({bank}{extra}, {op['phone']}{when})")
    n = max(len(ops), 1)
    lines.append("")
    if ops:
        lines.append(
            f"Пришли до {n} PDF (первый = самая свежая операция). /done — собрать без PDF."
        )
    else:
        lines.append("/done — собрать сейчас.")
    lines.append("/done — собрать сейчас.")
    return "\n".join(lines)
