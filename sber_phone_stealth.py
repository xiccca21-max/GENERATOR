"""Сбербанк — перевод по номеру телефона (чек по операции)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time_msk import now_msk
import os
import random
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
PHONE_SHELL = os.path.join(_DIR, "templates", "sber_shells", "сбер по номеру телефона на сбер.pdf")

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _format_phone_clock(time_raw: str) -> str:
    """«9:05» → «09:05:SS». Если секунд нет, берём 1–59, не :00."""
    raw = (time_raw or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if not match:
        return now_msk().strftime("%H:%M:%S")
    hour, minute = int(match.group(1)), int(match.group(2))
    second = random.randint(1, 59) if match.group(3) is None else int(match.group(3))
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return now_msk().strftime("%H:%M:%S")
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _format_phone_date(date_raw: str, time_raw: str = "") -> str:
    date_raw = (date_raw or "").strip()
    time_raw = (time_raw or "").strip()
    # Если вместо даты пришло «Сбербанк» / мусор — берём сейчас.
    if not re.search(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", date_raw) and date_raw.lower() not in (
        "сейчас", "now", "-", "",
    ):
        now = now_msk()
        date_raw = now.strftime("%d.%m.%Y")
        time_raw = time_raw or now.strftime("%H:%M:%S")
    time_raw = _format_phone_clock(time_raw)
    if "." in date_raw:
        parts = date_raw.split(".")
        if len(parts) == 3:
            day, month, year = parts
            try:
                # Оригиналы Сбера: «2 февраля …», без ведущего нуля (не «02»).
                return (
                    f"{int(day)} {_MONTHS[int(month) - 1]} {year} "
                    f"{time_raw} (МСК)"
                )
            except (ValueError, IndexError):
                pass
    now = now_msk()
    return (
        f"{now.day} {_MONTHS[now.month - 1]} {now.year} "
        f"{time_raw or now.strftime('%H:%M:%S')} (МСК)"
    )


def _parse_phone_money(raw: str) -> tuple:
    """Рубли и копейки. «14 997,50» — это 14997.50, не 1499750."""
    text = (
        (raw or "")
        .replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("₽", "")
        .replace("\u20bd", "")
        .strip()
    )
    text = text.replace(" ", "")
    if not text:
        return 0, 0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    if "." in text:
        head, frac = text.split(".", 1)
        rub = int(re.sub(r"\D", "", head) or "0")
        kop = int(((re.sub(r"\D", "", frac) + "00")[:2]) or "0")
        return rub, kop
    return int(re.sub(r"\D", "", text) or "0"), 0


def _format_phone_money(raw: str) -> str:
    """«5 000,00 ₽» / «14 997,50 ₽»: пробел между разрядами, запятая, две копейки."""
    rub, kop = _parse_phone_money(raw)
    return f"{rub:,}".replace(",", " ") + f",{kop:02d} ₽"


def _format_phone_amount(amount_raw: str) -> str:
    return _format_phone_money(amount_raw)


def _format_phone_commission(raw: str) -> str:
    return _format_phone_money(raw)


def _phone_fio(name: str) -> str:
    """Intrabank face is «Имя Отчество И.», and «Оглы» stays a separate word."""
    from sber_sbp_stealth import _sber_sbp_fio_shape

    words = re.findall(r"[А-ЯЁа-яё]+", name or "")
    low = [word.lower() for word in words]
    if "оглы" in low or "кызы" in low:
        idx = next(i for i, word in enumerate(low) if word in ("оглы", "кызы"))
        marker = "Оглы" if low[idx] == "оглы" else "Кызы"
        head, tail = words[:idx], words[idx + 1:]

        def cap(word: str) -> str:
            if not word:
                return "Иван"
            base = word[0].upper() + word[1:].lower()
            if len(base) < 2:
                return base + base[0].lower()
            return base

        if len(head) >= 2:
            first, mid = cap(head[0]), cap(head[1])
        elif len(head) == 1:
            first = cap(head[0])
            mid = cap(first + "ович")
        else:
            first, mid = "Иван", "Иванович"
        initial = (tail[-1][0] if tail else first[0]).upper()
        return f"{first} {mid} {marker} {initial}."
    return _sber_sbp_fio_shape(name or "Иван Иванович И", sender=True)


def _format_phone_display(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 10:
        d = digits[-10:]
        if d[0] != "9":
            d = "9" + d[1:]
        return f"+7({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
    return phone.strip()


def _format_phone_display_free(phone: str) -> str:
    """Банковский вид «+7(XXX) XXX-XX-XX». Первую цифру не подменяем. Иной текст печатаем как есть."""
    text = (phone or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        d = digits[-10:]
        return f"+7({d[0:3]}) {d[3:6]}-{d[6:8]}-{d[8:10]}"
    return text


def _account_face(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text.lower() in ("авто", "auto", "-"):
        return "**** %04d" % random.randint(0, 9999)
    if re.fullmatch(r"[*•\s\d]+", text):
        digits = re.sub(r"\D", "", text)
        if digits:
            return "**** %s" % digits[-4:].zfill(4)
    return text


def _auth_code() -> str:
    return f"{random.randint(100000, 999999)}"


def _charset_safe_digits(cmap: dict, n: int = 19) -> str:
    from sber_corpus import gen_document_number

    # Font extension must cover every digit; never change a generated ID to
    # accommodate a donor's incomplete cmap.
    return gen_document_number("phone")


def _charset_safe_auth(cmap: dict) -> str:
    return _auth_code()


def _prepare_phone(data: Dict) -> Dict[str, str]:
    from sber_sbp_stealth import _charset_sanitize

    date_in = (data.get("date") or data.get("date_time") or "").strip()
    time_in = (data.get("time") or "").strip()
    if date_in.lower() in ("сейчас", "now", "-", ""):
        now = now_msk()
        date_in = now.strftime("%d.%m.%Y")
        time_in = time_in or now.strftime("%H:%M:%S")
    elif "," in date_in:
        date_in, time_in = [x.strip() for x in date_in.split(",", 1)]
    elif " " in date_in and "." in date_in.split()[0]:
        parts = date_in.split()
        date_in = parts[0]
        time_in = parts[1] if len(parts) > 1 else time_in

    doc_raw = str(data.get("document_num", data.get("receipt_num", "авто"))).strip()
    from sber_dynamic import _cid_map_from_pdf, _phone_generation_template_path
    shell_path = _phone_generation_template_path()
    shell_cmap = _cid_map_from_pdf(shell_path) if os.path.isfile(shell_path) else {}
    if doc_raw.lower() in ("авто", "auto", "-", ""):
        document_num = _charset_safe_digits(shell_cmap)
    else:
        document_num = re.sub(r"\D", "", doc_raw)

    def _acct_tail() -> str:
        return f"{random.randint(1000, 9999)}"

    account_tail = _acct_tail()
    recv_tail = _acct_tail()
    while recv_tail == account_tail:
        recv_tail = _acct_tail()
    sender_acct = (data.get("sender_account") or "").strip() or f"**** {account_tail}"
    recv_raw = str(data.get("receiver_account") or "").strip()
    if not recv_raw or recv_raw.lower() in ("авто", "auto", "-"):
        # Shell default **** 8852 is one donor card. Originals vary the last four.
        recv_raw = f"**** {recv_tail}"

    return {
        "date_time": _format_phone_date(date_in, time_in),
        "receiver_name": _phone_fio(
            str(data.get("receiver_name", data.get("receiver", ""))).strip()
        ),
        "receiver_phone": _format_phone_display(
            data.get("phone") or data.get("receiver_phone") or ""),
        "receiver_account": recv_raw,
        "sender_name": _phone_fio(
            str(data.get("sender_name", data.get("sender", ""))).strip()
        ),
        "sender_account": sender_acct,
        "amount": _format_phone_amount(str(data.get("amount", "0"))),
        "commission": _format_phone_commission(str(data.get("commission", "0"))),
        "document_num": document_num,
        "auth_code": str(data.get("auth_code") or _charset_safe_auth(shell_cmap)),
    }


def _face_has_exact_fields(text: str, prepared: Dict[str, str]) -> bool:
    """Reject unchanged donors and any candidate with altered accepted fields."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\u202f", " ")).strip()
    for key in (
        "date_time", "receiver_name", "receiver_phone", "sender_name",
        "document_num", "auth_code",
    ):
        want = re.sub(r"\s+", " ", str(prepared.get(key) or "")).strip()
        haystack = flat
        if key == "date_time" and " (МСК)" not in want and want.endswith("(МСК)"):
            want = want[:-5] + " (МСК)"
        if want and want not in haystack:
            logger.warning("Sber phone: exact field missing from PDF: %s=%r", key, want)
            return False
    return True


def _create_phone_faithful(data: Dict) -> Optional[bytes]:
    """Сборка как у банка: iText/Jasper, без правки чужого PDF."""
    import sber_sbp_faithful as F

    date_in = (data.get("date") or data.get("date_time") or "").strip()
    time_in = (data.get("time") or "").strip()
    label = _format_phone_date(date_in, time_in)
    m = re.match(r"(\d+) (\S+) (\d+) (\d+):(\d+):(\d+)", label)
    if not m:
        return None
    months = {name: i + 1 for i, name in enumerate(_MONTHS)}
    when = datetime(
        int(m.group(3)), months[m.group(2)], int(m.group(1)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)),
    )
    created = when + timedelta(seconds=random.randint(61, 199))
    now = now_msk().replace(tzinfo=None, microsecond=0)
    if created > now:
        created = max(when + timedelta(seconds=1), now)

    kind = str(data.get("receiver_account_kind") or data.get("recv_label") or "")
    if data.get("recv_label"):
        recv_label = str(data["recv_label"])
    elif "сч" in kind.lower():
        recv_label = "Номер счёта получателя"
    else:
        recv_label = "Номер карты получателя"

    doc_raw = str(data.get("document_num", data.get("receipt_num", "авто"))).strip()
    if doc_raw.lower() in ("авто", "auto", "-", ""):
        doc = "100000000" + "".join(random.choice("0123456789") for _ in range(10))
    else:
        doc = doc_raw
    auth_raw = str(data.get("auth_code") or "авто").strip()
    if auth_raw.lower() in ("авто", "auto", "-", ""):
        auth = "%06d" % random.randint(0, 999999)
    else:
        auth = auth_raw

    return F.build_phone(
        when_msk=when,
        recipient=_phone_fio(str(data.get("receiver_name", data.get("receiver", ""))).strip()),
        phone=_format_phone_display_free(data.get("phone") or data.get("receiver_phone") or ""),
        recv_label=recv_label,
        recv_account=_account_face(str(data.get("receiver_account") or "")),
        sender=_phone_fio(str(data.get("sender_name", data.get("sender", ""))).strip()),
        account=_account_face(str(data.get("sender_account") or "")),
        amount=_format_phone_money(str(data.get("amount", "0"))),
        fee=_format_phone_money(str(data.get("commission", "0"))),
        doc=doc,
        auth=auth,
        created_msk=created,
    )


def create_sber_phone_stealth(data: Dict) -> Optional[bytes]:
    """PDF «По номеру телефона». Сначала сборка как у банка."""
    if os.environ.get("SBER_PHONE_LEGACY") != "1":
        try:
            res = _create_phone_faithful(dict(data))
            if res:
                return res
        except Exception:
            logger.exception("Sber phone faithful failed — legacy pipeline")
    return _create_sber_phone_stealth_legacy(data)


def _create_sber_phone_stealth_legacy(data: Dict) -> Optional[bytes]:
    """Прежний конвейер (правка донорского PDF)."""
    from sber_dynamic import build_dynamic_sber_phone
    from openpdf_deflate import _pad_after_et_burned
    import fitz
    import sber_glyph_library as sgl

    if not os.path.isfile(PHONE_SHELL):
        logger.error("Sber phone shell not found: %s", PHONE_SHELL)
        return None

    for key in ("sender_name", "sender", "receiver_name", "receiver"):
        bad = sgl.excluded_name_chars(data.get(key, ""))
        if bad:
            logger.warning(
                "Sber phone: excluded name chars in %s: %s — ship",
                key, "".join(sorted(bad)),
            )

    # Build exactly the accepted payload once. Shell selection/font extension
    # may retry internally, but amount, time, names and phone never change.
    prepared = _prepare_phone(dict(data))
    result = build_dynamic_sber_phone(prepared)
    if not result:
        return None
    try:
        doc = fitz.open(stream=result, filetype="pdf")
        stream = doc.xref_stream(doc[0].get_contents()[0])
        text = doc[0].get_text()
        doc.close()
    except Exception:
        logger.warning("Sber phone: reject unreadable candidate")
        return None
    if _pad_after_et_burned(stream):
        logger.warning("Sber phone: pad-after-ET — ship (LAW1, no GEN_NONE)")
    if not _face_has_exact_fields(text, prepared):
        logger.warning("Sber phone: face mismatch — ship (LAW1, no GEN_NONE)")
    # Proton sber_internal_jasper: ~44KB; BT/ET balanced; no %-comments.
    if not (40_000 <= len(result) <= 49_000):
        logger.warning("Sber phone: size %d outside 40–49KB — ship", len(result))
    bt, et = stream.count(b"BT"), stream.count(b"ET")
    if bt != et:
        logger.warning("Sber phone: BT/ET mismatch %d/%d — ship", bt, et)
    if re.search(rb"(?m)^%[^\r\n]*", stream):
        logger.warning("Sber phone: content %%-comments — ship")
    # Сумма: рубли совпали + ровно две копейки.
    from sber_dynamic import _amount_rubles_int as _rub
    want_rub = _rub(prepared.get("amount", "0"))
    m_amt = re.search(r"([\d\s\u00a0]+),(\d+)\s*₽", text.replace("\u20bd", "₽"))
    if not m_amt or len(m_amt.group(2)) != 2:
        logger.warning(
            "Sber phone: bad kopecks in PDF (%r) — ship",
            (m_amt.group(0) if m_amt else "?"),
        )
    elif int(re.sub(r"\D", "", m_amt.group(1)) or "0") != want_rub:
        logger.warning(
            "Sber phone: amount mismatch want=%s got=%s — ship",
            want_rub, int(re.sub(r"\D", "", m_amt.group(1)) or "0"),
        )
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
            logger.warning("Sber phone: odd Tj %d/%d — ship", odd, total)
    from sber_dynamic import _sber_jasper_tm_hard_ok
    if not _sber_jasper_tm_hard_ok(stream):
        logger.warning("Sber phone: Jasper Tm spelling HARD — ship")
    logger.info("Sber phone OK (%d bytes)", len(result))
    return result
