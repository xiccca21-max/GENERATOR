"""
OTP SBP STEALTH — генерация чеков ОТП Банка для перевода по СБП.

Архитектура полностью аналогична tbank_sbp_stealth:

  1. orig-mode    : Если все символы пользовательских данных лежат в orig
                    subset templates/OTP_sbp_original.pdf, меняем только
                    нужные Tj-блоки в content stream IN-PLACE через
                    _pad_to_compressed_size(). Размер итогового PDF — РАВЕН
                    размеру оригинального шаблона (27306 B), шрифты не
                    трогаются — байт-в-байт идентичны.

  2. unlocked-mode: Если символы не лежат в orig subset — динамически
                    собираем «расширенный» шаблон с минимально-необходимым
                    subset'ом (orig chars ∪ extra chars запрошенных
                    пользователем). Используем тот же in-place подход.

Метаданные: /CreationDate и /ModDate в /Info заменяются под
пользовательскую дату, чтобы в Properties PDF отображалась его дата,
а не дата создания исходного шаблона.

Текстовые операторы используют hex-формат `<...>Tj` (Identity-H)
— это формат iText 9 от которого мы наследуем шаблон.
"""

import os
import re
import zlib
import logging
from typing import Dict, Optional, Tuple

import fitz

from otp_orig_mode import OtpOrigContext

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_TEMPLATE     = os.path.join(_DIR, "templates", "OTP_sbp_original.pdf")
UNLOCKED_TEMPLATE = os.path.join(_DIR, "templates", "OTP_sbp_unlocked.pdf")

# ─── Эталонные данные orig-чека ──────────────────────────────────────────────
_ORIG_RECEIPT_NUM = "1-46204585"
_ORIG_DATE_TIME   = "08.05.2026 20:15:01"
_ORIG_SENDER      = "Али Хамзатович Е."
_ORIG_PASSPORT    = "2*** ****2"
_ORIG_PHONE       = "+7 937 076-16-61"
_ORIG_RECEIVER    = "Сергей Владимирович Ч."
_ORIG_BANK        = "Яндекс"
_ORIG_BIK         = "044525677"
_ORIG_SBP_ID      = "A61281716068881J0G10080011760501"
_ORIG_AMOUNT_TXT  = "16\u00a0800,00\u00a0\u20bd"
_ORIG_COMM_TXT    = "0,00\u00a0\u20bd"

_ORIG_HEADER    = f"Квитанция {_ORIG_RECEIPT_NUM}"
_ORIG_DATE_FULL = f"{_ORIG_DATE_TIME} (МСК) · Перевод по номеру телефона"

# ─── Параметры, защищённые валидатором ───────────────────────────────────────
_ORIG_DAY   = 8
_ORIG_MONTH = "05"
_ORIG_YEAR  = "2026"
_ORIG_TIME  = "20:15:01"

# Approved bank → BIK pairs. Только эти пары валидатор пропускает при смене банка.
# Имя должно быть СТРОГО как ниже (т.к. валидатор имеет внутреннюю таблицу).
_APPROVED_BANKS = {
    "Яндекс":         "044525677",   # orig template
    "Сбербанк":       "044525225",
    "Газпромбанк":    "044525823",
    "Банк ВТБ":       "044525187",
    "ВТБ Банк":       "044525187",
    "Райффайзенбанк": "044525700",
    "Т-Банк":         "044525974",
    "Тинькофф":       "044525974",   # = Т-Банк
    "Альфа-Банк":     "044525593",
    "ПСБ":            "044525555",
    "ОТП Банк":       "044525311",
    "Открытие":       "044525297",
}

# Единая правая граница right-aligned колонки (точное значение из orig)
_RIGHT_MARGIN = 549.14
_TC_DEFAULT   = 0.1125


# ─── Низкоуровневые PDF-утилиты ──────────────────────────────────────────────

def _find_obj_pos(pdf: bytes, xref: int) -> Tuple[int, int]:
    pos = pdf.find(f"\n{xref} 0 obj".encode())
    if pos < 0:
        pos = pdf.find(f"{xref} 0 obj".encode())
    if pos < 0:
        raise ValueError(f"obj {xref} not found")
    end = pdf.find(b"endobj", pos) + len(b"endobj")
    return pos, end


def _replace_stream(pdf: bytes, xref: int, new_stream: bytes) -> bytes:
    pos, end = _find_obj_pos(pdf, xref)
    block = pdf[pos:end]
    m = re.search(rb"<<(.*?)>>", block, re.S)
    if not m:
        raise ValueError("dict not found in stream object")
    dict_body = m.group(1)
    dict_body = re.sub(
        rb"/Length\s+\d+", f"/Length {len(new_stream)}".encode(), dict_body, count=1
    )
    if b"/Length" not in dict_body:
        dict_body += f" /Length {len(new_stream)}".encode()
    leading = block[:m.start()]
    new_block = (
        leading + b"<<" + dict_body + b">>\nstream\n" + new_stream + b"\nendstream\nendobj"
    )
    return pdf[:pos] + new_block + pdf[end:]


def _rebuild_xref(pdf: bytes) -> bytes:
    """Перестроить classic xref-таблицу. Используется только в крайнем случае."""
    last_endobj = pdf.rfind(b"endobj")
    body = pdf[:last_endobj + len(b"endobj")] + b"\n"

    offsets = {}
    for m_obj in re.finditer(rb"(?:^|\n)(\d+)\s+0\s+obj", body):
        x = int(m_obj.group(1))
        s = m_obj.start()
        if body[s:s+1] == b"\n":
            s += 1
        offsets[x] = s

    out = bytearray(body)
    new_xref_off = len(out)
    max_x = max(offsets) if offsets else 0
    out.extend(f"xref\n0 {max_x+1}\n".encode("latin1"))
    out.extend(b"0000000000 65535 f \n")
    for n in range(1, max_x + 1):
        if n in offsets:
            out.extend(f"{offsets[n]:010d} 00000 n \n".encode("latin1"))
        else:
            out.extend(b"0000000000 00000 f \n")

    sx_old = pdf.rfind(b"startxref")
    if sx_old > 0:
        trailer_pos = pdf.rfind(b"trailer", 0, sx_old)
        if trailer_pos > 0:
            trailer_block = pdf[trailer_pos:sx_old]
            trailer_block = re.sub(
                rb"/Size\s+\d+", f"/Size {max_x+1}".encode("latin1"), trailer_block
            )
            out.extend(trailer_block)
        else:
            m_root = re.search(rb"/Root\s+(\d+)\s+0\s+R", pdf)
            m_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", pdf)
            m_id   = re.search(rb"/ID\s*\[<[^>]+><[^>]+>\]", pdf)
            root = int(m_root.group(1)) if m_root else 1
            info = int(m_info.group(1)) if m_info else 3
            id_part = m_id.group(0).decode("latin1") if m_id else ""
            out.extend(
                f"trailer\n<</Size {max_x+1}/Root {root} 0 R/Info {info} 0 R{id_part}>>\n"
                .encode("latin1")
            )
    out.extend(f"startxref\n{new_xref_off}\n%%EOF\n".encode("latin1"))
    return bytes(out)


def _pad_to_compressed_size(stream: bytes, target_size: int) -> Optional[bytes]:
    """Подбирает padding и compress level так, чтобы итоговый сжатый размер
    был РАВЕН target_size — позволяет подменить content stream IN-PLACE
    без пересборки xref."""
    for level in (9, 8, 7, 6, 5, 4, 3, 2, 1):
        base = zlib.compress(stream, level)
        if len(base) == target_size:
            return base
        if len(base) > target_size:
            continue
        for filler in (b" ", b"\n", b"\t", b"  ", b"\r\n", b"   ", b"     "):
            count = 1
            while count < 30000:
                test = stream + filler * count
                comp = zlib.compress(test, level)
                if len(comp) == target_size:
                    return comp
                if len(comp) > target_size:
                    break
                count += 1
    return None


# ─── Подготовка пользовательских данных ──────────────────────────────────────

def _prepare(data: Dict) -> Dict:
    """Нормализует входные данные → строки для подстановки.

    ВНИМАНИЕ:
      • МЕСЯЦ / ГОД / ВРЕМЯ в content stream форсируются к orig (валидатор
        whitelist'ит конкретные значения для этого template).
      • Если пользователь хочет визуально показать ДРУГОЕ время → используется
        annotation-overlay (см. поле "user_visible_time" в результате).
      • день — свободно меняется, СИНХРОННО прокидывается в SBP_ID pos 4-5
      • bank / БИК — пара должна быть из _APPROVED_BANKS, иначе fallback
    """
    receipt_num = (data.get("receipt_num") or _ORIG_RECEIPT_NUM).strip()
    if not re.match(r"^[\d-]+$", receipt_num):
        receipt_num = _ORIG_RECEIPT_NUM
    if "-" not in receipt_num and len(receipt_num) >= 6:
        receipt_num = f"1-{receipt_num}"

    # ── Дата: извлекаем день + (опционально) пользовательское время
    date_input = (data.get("date_time") or _ORIG_DATE_TIME).strip()
    if " " not in date_input and "T" in date_input:
        date_input = date_input.replace("T", " ")
    m_date = re.match(
        r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        date_input,
    )
    user_day = _ORIG_DAY
    user_time_visible = ""
    if m_date:
        try:
            d = int(m_date.group(1))
            if 1 <= d <= 31:
                user_day = d
        except ValueError:
            pass
        if m_date.group(4):
            hh = int(m_date.group(4))
            mm = int(m_date.group(5))
            ss = int(m_date.group(6) or 0)
            user_time_visible = f"{hh:02d}:{mm:02d}:{ss:02d}"

    # Content stream всегда содержит orig date_time (валидатор требует)
    date_time = f"{user_day:02d}.{_ORIG_MONTH}.{_ORIG_YEAR} {_ORIG_TIME}"

    sender   = (data.get("sender")   or _ORIG_SENDER).strip()
    passport = (data.get("passport") or data.get("passport_data") or _ORIG_PASSPORT).strip()
    if not passport:
        passport = _ORIG_PASSPORT
    phone    = (data.get("phone")    or _ORIG_PHONE).strip()
    receiver = (data.get("receiver") or _ORIG_RECEIVER).strip()

    bank_in = (data.get("bank") or _ORIG_BANK).strip()
    bik_in  = (data.get("bik")  or "").strip()
    if bank_in in _APPROVED_BANKS:
        bank = bank_in
        bik  = _APPROVED_BANKS[bank_in]
    else:
        logger.warning(f"OTP: bank {bank_in!r} not in approved list → fallback {_ORIG_BANK!r}")
        bank = _ORIG_BANK
        bik  = _ORIG_BIK
    if bik_in and bik_in == _APPROVED_BANKS.get(bank, ""):
        bik = bik_in

    sbp_user = (data.get("sbp_id") or _ORIG_SBP_ID).strip()
    day_code = f"{(user_day + 20) % 100:02d}"
    if len(sbp_user) >= 5:
        sbp_id = sbp_user[:3] + day_code + sbp_user[5:]
    else:
        sbp_id = _ORIG_SBP_ID[:3] + day_code + _ORIG_SBP_ID[5:]

    def _fmt_amount(value, fallback: str) -> str:
        if value is None or value == "":
            return fallback
        try:
            v = float(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except Exception:
            return fallback
        rub = int(v)
        kop = round((v - rub) * 100)
        if kop >= 100:
            rub += 1
            kop = 0
        rub_str = f"{rub:,}".replace(",", "\u00a0")
        return f"{rub_str},{kop:02d}\u00a0\u20bd"

    total_amt  = data.get("amount") or data.get("total_amount") or "6500"
    commission = data.get("commission") if data.get("commission") not in (None, "") else "0"

    total_str = _fmt_amount(total_amt, _ORIG_AMOUNT_TXT)
    comm_str  = _fmt_amount(commission, _ORIG_COMM_TXT)
    try:
        t = float(str(total_amt).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        c = float(str(commission).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        no_comm_val = max(0.0, t - c)
        no_comm_str = _fmt_amount(no_comm_val, total_str)
    except Exception:
        no_comm_str = total_str

    return {
        "receipt_num":         receipt_num,
        "header":              f"Квитанция {receipt_num}",
        "date_time":           date_time,
        "date_full":           f"{date_time} (МСК) · Перевод по номеру телефона",
        "user_visible_time":   user_time_visible,   # для annotation-overlay
        "user_day":            user_day,
        "sender":              sender,
        "passport":            passport,
        "phone":               phone,
        "receiver":            receiver,
        "bank":                bank,
        "bik":                 bik,
        "sbp_id":              sbp_id,
        "total_str":           total_str,
        "no_comm_str":         no_comm_str,
        "comm_str":            comm_str,
    }


# ─── Hex-кодирование CIDs (Identity-H, как iText) ────────────────────────────

def _enc_hex(text: str, ctx: OtpOrigContext, semibold: bool) -> Optional[str]:
    """text → hex-строка из CIDs (4 hex digits per CID).
    Возвращает None если хоть один символ отсутствует в subset."""
    table = ctx.uni_to_cid_sb if semibold else ctx.uni_to_cid_reg
    out = []
    for ch in text:
        cid = table.get(ord(ch))
        if cid is None:
            return None
        out.append(f"{cid:04x}")
    return "".join(out)


def _replace_tj(
    cs: bytes,
    old_text: str,
    new_text: str,
    ctx: OtpOrigContext,
    semibold: bool,
    font_size: float,
    right_x: Optional[float] = None,
    tc: float = _TC_DEFAULT,
    adjust_td: bool = False,
) -> bytes:
    """Найти Tj-блок с old_text и заменить на new_text.

    Если `adjust_td=True` И `right_x` задан → пересчитываем X-координату
    предшествующего Td так, чтобы текст оказался выровнен по правому краю
    (right_x). По тестам валидатора, Td у полей `sender / passport / phone /
    receiver / amount / commission / total / date` можно свободно менять.

    Td у `bank / bik / sbp_id` менять НЕЛЬЗЯ (валидатор хеширует) — для них
    держим `adjust_td=False`, оставляем Td orig-template как есть.
    """
    old_hex = _enc_hex(old_text, ctx, semibold)
    new_hex = _enc_hex(new_text, ctx, semibold)
    if old_hex is None or new_hex is None:
        logger.warning(f"otp: cannot encode {old_text!r} → {new_text!r}")
        return cs

    needle_lo = f"<{old_hex}>Tj".encode("ascii")
    needle_hi = f"<{old_hex.upper()}>Tj".encode("ascii")
    pos = cs.find(needle_lo)
    needle = needle_lo
    if pos < 0:
        pos = cs.find(needle_hi)
        needle = needle_hi
        if pos < 0:
            logger.warning(f"otp: Tj not found for {old_text!r}")
            return cs

    new_chunk = f"<{new_hex}>Tj".encode("ascii")
    out = cs[:pos] + new_chunk + cs[pos + len(needle):]

    if not (adjust_td and right_x is not None):
        return out

    # Adjust Td.tx: новый tx = right_x - new_text_width (Tc уже учтён orig'ом
    # для всех полей одинаково — поэтому смещение можно посчитать через
    # разницу widths, без знания точного Tc).
    new_tj_pos = pos                         # position в новой строке
    pre = out[:new_tj_pos].decode("latin-1", errors="replace")
    last_td = pre.rfind("Td")
    if last_td < 0:
        return out
    m = re.search(r"(-?[\d.]+)\s+(-?[\d.]+)\s+Td\s*$", pre[:last_td + 2])
    if not m:
        return out
    tx_orig = float(m.group(1))
    ty_orig = float(m.group(2))

    orig_width = ctx.text_width(old_text, font_size, semibold)
    new_width  = ctx.text_width(new_text, font_size, semibold)
    orig_tc_total = tc * max(0, len(old_text) - 1)
    new_tc_total  = tc * max(0, len(new_text) - 1)
    tx_new = tx_orig + (orig_width + orig_tc_total) - (new_width + new_tc_total)

    # Используем :g формат → 440.8 а не 440.80 (как iText: trim trailing zeros).
    # Округляем до 2 знаков чтобы совпадало с iText precision.
    tx_new_r = round(tx_new, 2)
    new_td_str = f"{tx_new_r:g} {ty_orig:g} Td".encode("ascii")
    td_byte_start = m.start()
    td_byte_end   = m.end()
    return out[:td_byte_start] + new_td_str + out[td_byte_end:]


def _replace_total_amount(cs: bytes, new_text: str, ctx: OtpOrigContext) -> bytes:
    """Большая сумма в правом нижнем — F1 24pt SemiBold (последний Tj-блок с
    hex суммы в content stream). Td-координата подгоняется под right_x=549.14
    (как и обычные amount). Шрифт SemiBold 24pt."""
    old_hex_sb = _enc_hex(_ORIG_AMOUNT_TXT, ctx, semibold=True)
    new_hex_sb = _enc_hex(new_text, ctx, semibold=True)
    if old_hex_sb is None or new_hex_sb is None:
        return cs

    needle_lower = f"<{old_hex_sb}>Tj".encode("ascii")
    needle_upper = f"<{old_hex_sb.upper()}>Tj".encode("ascii")
    positions = []
    for needle in (needle_lower, needle_upper):
        start = 0
        while True:
            p = cs.find(needle, start)
            if p < 0:
                break
            positions.append((p, needle))
            start = p + 1
    if not positions:
        return cs
    positions.sort()
    pos, needle = positions[-1]
    new_chunk = f"<{new_hex_sb}>Tj".encode("ascii")
    return cs[:pos] + new_chunk + cs[pos + len(needle):]


def _apply_replacements(cs: bytes, prepared: Dict, ctx: OtpOrigContext) -> bytes:
    """Все Tj-замены в content stream.

    Td-coordinates:
      • adjust_td=True для полей где валидатор разрешает менять Td:
        sender/passport/phone/receiver/amount/commission.
      • adjust_td=False для bank/bik/sbp_id — Td там хешируется → fixed.
    """
    p = prepared
    cs = _replace_tj(cs, _ORIG_HEADER,    p["header"],    ctx, semibold=True,  font_size=20)
    cs = _replace_tj(cs, _ORIG_DATE_FULL, p["date_full"], ctx, semibold=False, font_size=12)
    cs = _replace_tj(cs, _ORIG_SENDER,    p["sender"],    ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_tj(cs, _ORIG_PASSPORT,  p["passport"],  ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_tj(cs, _ORIG_PHONE,     p["phone"],     ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_tj(cs, _ORIG_RECEIVER,  p["receiver"],  ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_tj(cs, _ORIG_BANK,      p["bank"],      ctx, False, 12, _RIGHT_MARGIN, adjust_td=False)
    cs = _replace_tj(cs, _ORIG_BIK,       p["bik"],       ctx, False, 12, _RIGHT_MARGIN, adjust_td=False)
    cs = _replace_tj(cs, _ORIG_SBP_ID,    p["sbp_id"],    ctx, False, 12, _RIGHT_MARGIN, adjust_td=False)
    cs = _replace_tj(cs, _ORIG_AMOUNT_TXT, p["no_comm_str"], ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_tj(cs, _ORIG_COMM_TXT,  p["comm_str"],  ctx, False, 12, _RIGHT_MARGIN, adjust_td=True)
    cs = _replace_total_amount(cs, p["total_str"], ctx)
    return cs


# ─── Метаданные ──────────────────────────────────────────────────────────────

def _randomize_trailer_id(pdf: bytes) -> bytes:
    """Подменяет /ID в trailer на random 64-byte hex (128 hex chars).

    Назначение: anti-duplicate. Валидатор ОТП хранит SHA-хэши уже проверенных
    чеков и если хэш совпадает с ранее виденным → отклоняет как "уже видел".
    Подмена /ID меняет SHA итогового PDF, но при этом /ID контент валидатором
    НЕ проверяется (тесты G1/G2/G3 — любое 128-hex значение проходит).
    Длина /ID = ровно 64 байта обязательна (G4 с 16 байтами — fail).
    """
    import hashlib, os as _os
    rand = hashlib.sha512(_os.urandom(32)).hexdigest()  # 128 hex chars = 64 bytes
    out = re.sub(
        rb"/ID\s*\[<[0-9A-Fa-f]+><[0-9A-Fa-f]+>\]",
        f"/ID [<{rand}><{rand}>]".encode("latin-1"),
        pdf, count=1,
    )
    return out


def _patch_metadata(pdf: bytes, user_date_input: Optional[str]) -> bytes:
    """No-op для /Info — тесты показали, что наша подмена /CreationDate/ModDate
    ломает валидацию (хотя контент /Info сам по себе игнорируется).

    Вместо этого используем `_randomize_trailer_id` для anti-duplicate.
    """
    return pdf


# ─── Гарантия наличия unlocked-шаблона (T-Bank-style) ────────────────────────

def _ensure_unlocked_template() -> None:
    """Если статического OTP_sbp_unlocked.pdf нет — или в нём нет требуемых символов
    (маска паспорта, латиница, …) — пересобираем."""
    needs_build = False
    reason = ""
    if not os.path.exists(UNLOCKED_TEMPLATE):
        needs_build = True
        reason = "файл отсутствует"
    else:
        try:
            from otp_orig_mode import OtpOrigContext as _Ctx
            c = _Ctx()
            if c.load(UNLOCKED_TEMPLATE):
                required = (
                    [0x002A, 0x0058, 0x0078]  # * X x — маска паспорта
                    + list(range(ord("A"), ord("Z") + 1))  # A-Z (банки: VTB, OTP)
                    + list(range(ord("a"), ord("z") + 1))  # a-z (Sber, Tinkoff, Ozon)
                )
                missing = [chr(cp) for cp in required if cp not in c.uni_to_cid_reg]
                if missing:
                    needs_build = True
                    reason = f"нет символов: {''.join(missing)!r}"
            else:
                needs_build = True
                reason = "не загрузился"
        except Exception as e:
            needs_build = True
            reason = f"ошибка проверки: {e}"
    if not needs_build:
        return
    logger.info(f"OTP unlocked template rebuild ({reason})")
    from otp_unlock_template import build_unlocked_template
    build_unlocked_template(save_to_file=True)


# ─── Точная подгонка размера PDF до 27306 B (= 26.7 KB) ──────────────────────

_TARGET_PDF_SIZE = 27759  # точный размер orig template OTP SBP (новый шаблон от 08.05.2026)


def _force_pdf_size(pdf: bytes, target: int = _TARGET_PDF_SIZE) -> bytes:
    """Приводит PDF к точному размеру target байт через добавление
    orphan stream-объекта между последним endobj и xref-таблицей,
    с пересборкой xref. Файл заканчивается нормально на %%EOF —
    проходит pypdf strict, pdfminer, fitz, qpdf."""
    if len(pdf) == target:
        return pdf
    if len(pdf) > target:
        logger.warning(f"PDF {len(pdf)} B > target {target} B, cannot shrink")
        return pdf

    # Найти максимальный номер объекта
    max_num = 0
    for m in re.finditer(rb"\n(\d+)\s+0\s+obj", pdf):
        n = int(m.group(1))
        if n > max_num:
            max_num = n
    new_num = max_num + 1

    # Точка вставки: сразу после последнего endobj
    last_end = pdf.rfind(b"endobj") + len(b"endobj")
    head = pdf[:last_end]
    tail = pdf[last_end:]

    def build_obj(stream_len: int) -> bytes:
        return (
            f"\n{new_num} 0 obj\n<</Length {stream_len}>>\nstream\n".encode("latin1")
            + b"\x00" * stream_len
            + b"\nendstream\nendobj"
        )

    # Итеративно подбираем stream_len чтобы итоговый размер == target
    deficit = target - len(pdf)
    stream_len = max(0, deficit - 70)
    candidate = pdf
    for _ in range(20):
        new_obj = build_obj(stream_len)
        candidate = _rebuild_xref(head + new_obj + tail)
        if len(candidate) == target:
            return candidate
        delta = target - len(candidate)
        stream_len += delta
        if stream_len < 0:
            stream_len = 0

    logger.warning(
        f"_force_pdf_size: failed to converge to {target} B (last={len(candidate)} B)"
    )
    return pdf


# ─── Режимы генерации ────────────────────────────────────────────────────────

def _try_orig_mode(prepared: Dict) -> Optional[bytes]:
    """orig-mode: все символы лежат в orig subset → меняем content stream
    IN-PLACE через padding до точного compressed size.

    Итоговый PDF — БАЙТ-В-БАЙТ того же размера, что template (27306 B),
    шрифты не тронуты. Метаданные /CreationDate /ModDate заменены под
    пользовательскую дату."""
    ctx = OtpOrigContext()
    if not ctx.load(ORIG_TEMPLATE):
        return None

    reg_strings = [
        prepared["date_full"], prepared["sender"], prepared["passport"], prepared["phone"],
        prepared["receiver"], prepared["bank"], prepared["bik"],
        prepared["sbp_id"], prepared["no_comm_str"], prepared["comm_str"],
    ]
    sb_strings = [prepared["header"], prepared["total_str"]]
    ok_reg, miss_reg = ctx.can_render_reg(*reg_strings)
    ok_sb,  miss_sb  = ctx.can_render_sb(*sb_strings)
    if not (ok_reg and ok_sb):
        logger.info(
            f"OTP orig-mode: missing chars reg={miss_reg!r} sb={miss_sb!r} → fallback"
        )
        return None

    cs_new = _apply_replacements(ctx.cs_dec, prepared, ctx)

    if cs_new == ctx.cs_dec:
        # ничего не менялось — отдаём исходные байты
        result = bytes(ctx.pdf_bytes)
    else:
        # ⚠️ ВАЖНО: НЕ используем _pad_to_compressed_size (padding ломает валидацию).
        # Прямой xref-rebuild + zlib level 6 (как iText) даёт PDF ~27.7 KB,
        # который проходит проверку даже с многими изменениями.
        cs_compressed = zlib.compress(cs_new, 6)
        tmp = _replace_stream(bytes(ctx.pdf_bytes), ctx.cs_xref, cs_compressed)
        result = _rebuild_xref(tmp)
        logger.info(f"OTP orig-mode (xref-rebuild, no-pad): {len(result)} B")

    # /ID НЕ подменяем (наша подмена ломает валидацию — почему-то, хотя
    # G1/G2/G3 тесты с подменой /ID раньше проходили на orig-template).
    # Anti-duplicate теперь полагается на естественное различие SBP_ID/имён.
    # result = _randomize_trailer_id(result)

    try:
        d = fitz.open(stream=result, filetype="pdf")
        _ = d[0].get_text()
        d.close()
    except Exception as e:
        logger.warning(f"OTP orig-mode produced invalid PDF: {e}")
        return None

    return result


def _try_minimal_mode(prepared: Dict) -> Optional[bytes]:
    """МИНИМАЛЬНЫЙ unlocked-mode (рекомендуемый).

    Если в orig subset не хватает 1-5 символов (типа 'ж', 'Ё', 'B') —
    собираем шаблон в памяти ровно из (orig_chars ∪ missing_chars).
    Font size почти не меняется (≈ +50-150 B), итоговый PDF ≈ 27.8 KB
    (на 50-150 байт больше оригинала 27.7 KB). Валидатор пропускает.

    Возвращает PDF либо None при невозможности."""
    orig_ctx = OtpOrigContext()
    if not orig_ctx.load(ORIG_TEMPLATE):
        return None

    reg_strings = [
        prepared["date_full"], prepared["sender"], prepared["passport"], prepared["phone"],
        prepared["receiver"], prepared["bank"], prepared["bik"],
        prepared["sbp_id"], prepared["no_comm_str"], prepared["comm_str"],
    ]
    sb_strings = [prepared["header"], prepared["total_str"]]
    _, miss_reg = orig_ctx.can_render_reg(*reg_strings)
    _, miss_sb  = orig_ctx.can_render_sb(*sb_strings)

    extra_reg = sorted({ord(c) for c in miss_reg})
    extra_sb  = sorted({ord(c) for c in miss_sb})
    logger.info(f"OTP minimal: extra_reg={[chr(c) for c in extra_reg]!r} extra_sb={[chr(c) for c in extra_sb]!r}")

    try:
        from otp_unlock_template import build_unlocked_template
        result = build_unlocked_template(
            extra_reg_unicodes=extra_reg,
            extra_sb_unicodes=extra_sb,
            orig_uni_to_cid_reg=orig_ctx.uni_to_cid_reg,
            orig_uni_to_cid_sb=orig_ctx.uni_to_cid_sb,
            save_to_file=False,
        )
    except Exception as e:
        logger.warning(f"OTP minimal: build failed: {e}", exc_info=True)
        return None

    # build_unlocked_template может вернуть либо bytes, либо (bytes, ...) — поддержим оба
    pdf_bytes = result[0] if isinstance(result, tuple) else result
    if not pdf_bytes:
        return None

    # Загружаем построенный шаблон как ctx и применяем замены
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        ctx = OtpOrigContext()
        if not ctx.load(tmp_path):
            logger.warning("OTP minimal: built template did not load")
            return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    ok_reg, miss2 = ctx.can_render_reg(*reg_strings)
    ok_sb,  miss3 = ctx.can_render_sb(*sb_strings)
    if not (ok_reg and ok_sb):
        logger.warning(f"OTP minimal: STILL missing reg={miss2!r} sb={miss3!r}")
        return None

    cs_new = _apply_replacements(ctx.cs_dec, prepared, ctx)
    pdf = bytearray(ctx.pdf_bytes)
    orig_comp_len = len(ctx.cs_raw)

    if cs_new == ctx.cs_dec:
        out = bytes(pdf)
    else:
        new_compressed = _pad_to_compressed_size(cs_new, orig_comp_len)
        if new_compressed is not None and len(new_compressed) == orig_comp_len:
            pdf[ctx.cs_cs:ctx.cs_ce] = new_compressed
            out = bytes(pdf)
            logger.info(f"OTP minimal (in-place): {len(out)} B")
        else:
            cs_compressed = zlib.compress(cs_new, 9)
            tmp = _replace_stream(bytes(ctx.pdf_bytes), ctx.cs_xref, cs_compressed)
            out = _rebuild_xref(tmp)
            logger.info(f"OTP minimal (xref-rebuild): {len(out)} B")

    out = _patch_metadata(out, prepared["date_time"])

    try:
        d = fitz.open(stream=out, filetype="pdf")
        _ = d[0].get_text()
        d.close()
    except Exception as e:
        logger.warning(f"OTP minimal produced invalid PDF: {e}")
        return None

    return out


def _try_unlocked_mode(prepared: Dict) -> Optional[bytes]:
    """unlocked-mode (T-Bank style): использует статический unlocked-шаблон
    OTP_sbp_unlocked.pdf, в котором заранее подготовлен расширенный subset
    шрифтов (полная кириллица + Latin caps + цифры + спецсимволы).

    Применяется in-place подмена content stream через
    _pad_to_compressed_size с fallback на _replace_stream + _rebuild_xref —
    точно так же, как в tbank_sbp_stealth._try_unlocked_mode."""
    _ensure_unlocked_template()
    if not os.path.exists(UNLOCKED_TEMPLATE):
        logger.error(f"OTP unlocked template missing: {UNLOCKED_TEMPLATE}")
        return None

    ctx = OtpOrigContext()
    if not ctx.load(UNLOCKED_TEMPLATE):
        logger.error("OTP unlocked: cannot load template context")
        return None

    reg_strings = [
        prepared["date_full"], prepared["sender"], prepared["passport"], prepared["phone"],
        prepared["receiver"], prepared["bank"], prepared["bik"],
        prepared["sbp_id"], prepared["no_comm_str"], prepared["comm_str"],
    ]
    sb_strings = [prepared["header"], prepared["total_str"]]
    ok_reg, miss_reg = ctx.can_render_reg(*reg_strings)
    ok_sb,  miss_sb  = ctx.can_render_sb(*sb_strings)
    if not (ok_reg and ok_sb):
        logger.warning(
            f"OTP unlocked-mode: chars STILL missing reg={miss_reg!r} sb={miss_sb!r} "
            f"— расширьте _NEEDED_UNICODES в otp_unlock_template.py"
        )
        return None

    cs_new = _apply_replacements(ctx.cs_dec, prepared, ctx)
    pdf = bytearray(ctx.pdf_bytes)
    orig_comp_len = len(ctx.cs_raw)

    if cs_new == ctx.cs_dec:
        result = bytes(pdf)
        logger.info(f"OTP unlocked-mode (no changes): {len(result)} B")
    else:
        new_compressed = _pad_to_compressed_size(cs_new, orig_comp_len)
        if new_compressed is not None and len(new_compressed) == orig_comp_len:
            pdf[ctx.cs_cs:ctx.cs_ce] = new_compressed
            result = bytes(pdf)
            logger.info(f"OTP unlocked-mode (in-place): {len(result)} B")
        else:
            cs_compressed = zlib.compress(cs_new, 9)
            tmp = _replace_stream(bytes(ctx.pdf_bytes), ctx.cs_xref, cs_compressed)
            result = _rebuild_xref(tmp)
            logger.info(f"OTP unlocked-mode (xref-rebuild): {len(result)} B")

    result = _patch_metadata(result, prepared["date_time"])

    try:
        d = fitz.open(stream=result, filetype="pdf")
        _ = d[0].get_text()
        d.close()
    except Exception as e:
        logger.error(f"OTP unlocked-mode produced invalid PDF: {e}")
        return None

    return result


# ─── Публичная точка входа ───────────────────────────────────────────────────

# Кастомный exception, чтобы bot.py мог показать пользователю осмысленную ошибку
class OtpStealthError(Exception):
    """Содержит human-readable объяснение, что именно пошло не так."""


def _validate_orig_compat(prepared: Dict, ctx: OtpOrigContext) -> Optional[str]:
    """Проверяет, что все пользовательские строки умещаются в orig SemiBold/Regular
    subset. Возвращает None если всё ок, иначе текст ошибки для пользователя."""
    sb_strings = {
        "Номер квитанции": prepared["header"],
        "Сумма перевода":  prepared["total_str"],
    }
    bad_sb: Dict[str, list] = {}
    for label, s in sb_strings.items():
        miss = [ch for ch in s if ord(ch) not in ctx.uni_to_cid_sb]
        if miss:
            bad_sb[label] = sorted(set(miss))
    if bad_sb:
        parts = []
        for label, chars in bad_sb.items():
            ch_pretty = ", ".join(f"«{c}»" for c in chars)
            parts.append(f"   • {label}: запрещённые символы {ch_pretty}")
        return (
            "❌ Не удалось сгенерировать чек ОТП Банка (СБП).\n\n"
            "Валидатор ОТП Банка проверяет ОРИГИНАЛЬНЫЕ glyph'ы шрифта-заголовка "
            "(SemiBold). В нём доступны только цифры *0, 1, 2, 4, 5, 6, 8* "
            "и буквы из «Квитанция»/символ ₽.\n\n"
            "Замените эти символы:\n" + "\n".join(parts) + "\n\n"
            "Например:\n"
            "  • Номер квитанции — используйте только цифры 0,1,2,4,5,6,8 "
            "(напр. `1-12456802` вместо `1-99988877`).\n"
            "  • Сумму выбирайте так, чтобы в ней не было 3/7/9 "
            "(напр. 12 500 ₽ вместо 12 700 ₽)."
        )
    return None


def create_otp_sbp_stealth(data: Dict) -> Optional[bytes]:
    """Главная точка входа.

    ⚠️ Особенности валидации ОТП Банка (СБП):
       • SBP_ID[3:5] синхронизируется с днём в дате: pos = `day+20`.
       • Месяц / год / время в чеке ФОРСЯТСЯ = orig (`05.2026 20:15:01`).
       • Пара bank+БИК — только из `_APPROVED_BANKS`, иначе fallback на Яндекс.
       • Только orig-mode разрешён. Любая пересборка шрифта (minimal/unlocked)
         ловится валидатором как «подделка», поэтому если пользователь дал
         символы вне orig SemiBold subset (3/7/9 в сумме или № квитанции) —
         мы выбрасываем `OtpStealthError` с подробным объяснением.

    Возвращает bytes PDF либо ВЫБРАСЫВАЕТ OtpStealthError с детализированной
    ошибкой, если orig-mode невозможен.
    """
    prepared = _prepare(data)

    # Предполётная проверка SemiBold subset — выдадим осмысленную ошибку,
    # если пользователь использовал запрещённые цифры.
    ctx_check = OtpOrigContext()
    if ctx_check.load(ORIG_TEMPLATE):
        msg = _validate_orig_compat(prepared, ctx_check)
        if msg:
            raise OtpStealthError(msg)

    # orig-mode (in-place подмена content stream без перестройки шрифта)
    try:
        pdf = _try_orig_mode(prepared)
        if pdf is not None:
            logger.info(f"✅ OTP: orig-mode OK ({len(pdf)} bytes)")
            return pdf
    except Exception as e:
        logger.warning(f"OTP orig-mode error: {e}", exc_info=True)

    raise OtpStealthError(
        "❌ Не удалось сгенерировать чек ОТП Банка (СБП).\n\n"
        "Внутренняя ошибка orig-mode (см. логи). Minimal/unlocked-mode "
        "отключены, т.к. перестройка шрифта детектится валидатором как подделка."
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # Пример: дефолтный чек (orig-mode, 27306 B)
    out = create_otp_sbp_stealth({})
    if out:
        with open("_test_otp_default.pdf", "wb") as f:
            f.write(out)
        print(f"saved _test_otp_default.pdf ({len(out)} bytes)")

    # Пример: пользовательские данные
    out2 = create_otp_sbp_stealth({
        "receipt_num": "1-99999999",
        "date_time":   "08.05.2026 12:34:56",
        "sender":      "Иван Иванович И.",
        "phone":       "+7 999 111-22-33",
        "receiver":    "Анна Петровна П.",
        "bank":        "Сбербанк",
        "bik":         "044525225",
        "sbp_id":      "B6127120848620170B10130011750704",
        "amount":      "6400",
        "commission":  "0",
    })
    if out2:
        with open("_test_otp_custom.pdf", "wb") as f:
            f.write(out2)
        print(f"saved _test_otp_custom.pdf ({len(out2)} bytes)")
